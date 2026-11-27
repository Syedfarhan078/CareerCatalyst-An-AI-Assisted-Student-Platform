"""
Server-Authoritative State Machine & Session Management for Assessments.

Enforces eligibility, DB-backed daily practice quotas and 24-hour certification
cooldowns, concurrency safety via select_for_update() + partial unique constraints,
conservative Stage 3 stopping rules, and zero-penalty outage recovery.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.interviews.models import (
    AIUsageLog,
    AdaptiveInterviewTurn,
    AssessmentSession,
    AssessmentStageResponse,
    RoleAssessmentBlueprint,
)
from apps.interviews.services.blueprint_loader import (
    BlueprintRegistryService,
    PILOT_ACTIVE_ROLE_SLUGS,
)
from apps.roadmaps.models import CareerPath, RoadmapCertificate, Topic, UserRoadmap


class EligibilityError(PermissionDenied):
    """Raised when a user does not meet certification assessment prerequisites."""


class CooldownActiveError(PermissionDenied):
    """Raised when a candidate is still inside a 24-hour retake cooldown."""

    def __init__(self, message: str, cooldown_until=None):
        super().__init__(message)
        self.cooldown_until = cooldown_until


class InvalidStateTransitionError(ValidationError):
    """Raised on illegal session state or stage transitions."""


class StageLockedError(PermissionDenied):
    """Raised when attempting to modify or resubmit a locked stage."""


class ConcurrencyConflictError(ValidationError):
    """Raised when concurrent certification start/submit requests collide."""


class AssessmentSessionService:
    """Encapsulates all assessment session transitions and policy enforcement."""

    VALID_STAGE_TRANSITIONS = {
        'CREATED': {'STAGE_1', 'STAGE_2', 'STAGE_3', 'CANCELLED'},
        'STAGE_1': {'STAGE_2', 'SUBMITTED', 'PAUSED_OUTAGE', 'EXPIRED', 'CANCELLED'},
        'STAGE_2': {'STAGE_3', 'SUBMITTED', 'PAUSED_OUTAGE', 'EXPIRED', 'CANCELLED'},
        'STAGE_3': {'SUBMITTED', 'PAUSED_OUTAGE', 'EXPIRED', 'CANCELLED'},
        'PAUSED_OUTAGE': {'STAGE_1', 'STAGE_2', 'STAGE_3', 'CANCELLED'},
        'SUBMITTED': {'EVALUATING', 'PENDING_EVALUATION'},
        'EVALUATING': {'EVALUATED', 'PENDING_EVALUATION'},
        'PENDING_EVALUATION': {'EVALUATING', 'EVALUATED'},
        'EVALUATED': set(),
        'EXPIRED': set(),
        'CANCELLED': set(),
    }

    @classmethod
    def get_active_blueprint(cls, career_path: CareerPath) -> RoleAssessmentBlueprint:
        if career_path.slug not in PILOT_ACTIVE_ROLE_SLUGS:
            raise EligibilityError(
                f"Career Readiness Assessment is not yet active for '{career_path.name}'."
            )
        bp = RoleAssessmentBlueprint.objects.filter(
            career_path=career_path,
            is_active=True,
        ).first()
        if not bp:
            raise EligibilityError(
                f"No active assessment blueprint configured for '{career_path.name}'."
            )
        return bp

    @classmethod
    def check_practice_daily_quota(cls, user) -> Dict[str, Any]:
        """
        DB-backed per-user daily Practice AI allowance (safe across multi-worker Gunicorn).
        """
        limit = int(getattr(settings, 'READINESS_PRACTICE_DAILY_AI_LIMIT', 8))
        cutoff = timezone.now() - timedelta(hours=24)
        used = AIUsageLog.objects.filter(
            user=user,
            operation='PRACTICE_EVAL',
            success=True,
            created_at__gte=cutoff,
        ).count()
        return {
            'limit': limit,
            'used': used,
            'remaining': max(0, limit - used),
            'allowed': used < limit,
        }

    @classmethod
    def check_certification_eligibility(
        cls,
        user,
        career_path: CareerPath,
        now=None,
    ) -> Dict[str, Any]:
        """
        Server-side check for Certification eligibility:
        1. Role blueprint active in pilot
        2. 100% roadmap topic completion for active UserRoadmap
        3. No active 24h failure cooldown
        4. No existing CAREER_READINESS certificate for this role
        """
        now = now or timezone.now()
        blueprint = cls.get_active_blueprint(career_path)

        enrollment = UserRoadmap.objects.filter(
            user=user,
            career_path=career_path,
        ).first()
        if not enrollment:
            raise EligibilityError(
                f"You must enroll in and complete 100% of the {career_path.name} roadmap first."
            )

        total_topics = Topic.objects.filter(milestone__career_path=career_path).count()
        completed_topics = enrollment.topic_progress.filter(is_completed=True).count()
        if total_topics == 0 or completed_topics < total_topics:
            raise EligibilityError(
                f"Roadmap completion required: {completed_topics}/{total_topics} topics completed."
            )

        existing_readiness_cert = RoadmapCertificate.objects.filter(
            user=user,
            career_path=career_path,
            is_valid=True,
            credential_tier='CAREER_READINESS',
        ).first()
        if existing_readiness_cert:
            raise EligibilityError(
                f"You already hold a verified Career Readiness Certificate ({existing_readiness_cert.certificate_id})."
            )

        active_cooldown_session = AssessmentSession.objects.filter(
            user=user,
            career_path=career_path,
            mode='CERTIFICATION',
            status='EVALUATED',
            passed=False,
            cooldown_until__gt=now,
        ).order_by('-cooldown_until').first()
        if active_cooldown_session:
            raise CooldownActiveError(
                "Certification retake cooldown is still active. Use Practice Mode to sharpen weak areas.",
                cooldown_until=active_cooldown_session.cooldown_until,
            )

        active_session = AssessmentSession.objects.filter(
            user=user,
            career_path=career_path,
            mode='CERTIFICATION',
            status__in=AssessmentSession.ACTIVE_CERT_STATUSES,
        ).first()

        return {
            'eligible': True,
            'blueprint': blueprint,
            'enrollment': enrollment,
            'total_topics': total_topics,
            'completed_topics': completed_topics,
            'active_session': active_session,
        }

    @classmethod
    def _required_stages_for_scope(cls, mode: str, practice_scope: str) -> List[int]:
        if mode == 'CERTIFICATION' or practice_scope == 'FULL':
            return [1, 2, 3]
        mapping = {
            'STAGE_1': [1],
            'STAGE_2': [2],
            'STAGE_3': [3],
        }
        if practice_scope not in mapping:
            raise ValidationError(f"Invalid practice_scope '{practice_scope}'.")
        return mapping[practice_scope]

    @classmethod
    def create_session(
        cls,
        user,
        career_path: CareerPath,
        mode: str = 'PRACTICE',
        practice_scope: str = 'FULL',
        extended_time: bool = False,
    ) -> AssessmentSession:
        """
        Atomically initializes a Practice or Certification AssessmentSession with
        frozen blueprint/task snapshots. Enforces single active Certification per role.
        """
        if mode == 'CERTIFICATION' and practice_scope != 'FULL':
            raise ValidationError("Certification assessments require all three mandatory stages.")

        blueprint = cls.get_active_blueprint(career_path)
        spec = BlueprintRegistryService.get_active_spec_for_role(career_path.slug)

        with transaction.atomic():
            if mode == 'CERTIFICATION':
                elig = cls.check_certification_eligibility(user, career_path)
                if elig['active_session']:
                    return elig['active_session']

            bundle = BlueprintRegistryService.select_task_bundle(user, career_path, spec)
            required_stages = cls._required_stages_for_scope(mode, practice_scope)
            multiplier = (
                Decimal(str(spec['timing_policy'].get('accessibility_multiplier', 1.25)))
                if extended_time
                else Decimal('1.00')
            )

            try:
                session = AssessmentSession.objects.create(
                    user=user,
                    career_path=career_path,
                    blueprint=blueprint,
                    blueprint_version=blueprint.version,
                    task_bank_version=blueprint.task_bank_version,
                    task_bundle_id=bundle['bundle_id'],
                    mode=mode,
                    practice_scope=practice_scope,
                    status='CREATED',
                    current_stage=required_stages[0],
                    extended_time_enabled=bool(extended_time),
                    time_multiplier=multiplier,
                    rubric_snapshot=blueprint.competency_definitions,
                )
            except IntegrityError as exc:
                raise ConcurrencyConflictError(
                    "An active certification assessment session already exists for this career role."
                ) from exc

            for stage_num in required_stages:
                snapshot = BlueprintRegistryService.build_stage_snapshot(bundle, stage_num)
                AssessmentStageResponse.objects.create(
                    session=session,
                    stage_number=stage_num,
                    task_id=snapshot['task_id'],
                    task_snapshot=snapshot,
                )

            return session

    @classmethod
    def _stage_duration_seconds(cls, session: AssessmentSession, stage_num: int) -> int:
        if session.mode == 'PRACTICE' and session.practice_scope != 'FULL':
            spec = BlueprintRegistryService.get_active_spec_for_role(session.career_path.slug)
            base_sec = int(spec['practice_durations'].get(session.practice_scope, 600))
        else:
            durations = session.blueprint.stage_durations or {'1': 900, '2': 900, '3': 720}
            base_sec = int(durations.get(str(stage_num), 900))
        return int(round(base_sec * float(session.time_multiplier)))

    @classmethod
    def accept_policy_and_start(cls, user, session_uuid) -> AssessmentSession:
        """Records explicit policy consent and activates the initial assessment stage timer."""
        now = timezone.now()
        with transaction.atomic():
            session = AssessmentSession.objects.select_for_update().filter(
                session_uuid=session_uuid
            ).first()
            if not session:
                raise ValidationError("Assessment session not found.")
            if session.user_id != user.id:
                raise PermissionDenied("You do not have permission to access this session.")
            if session.status != 'CREATED':
                raise InvalidStateTransitionError(
                    f"Cannot accept policy from status '{session.status}'."
                )

            first_stage = session.current_stage
            target_status = f"STAGE_{first_stage}"
            duration_sec = cls._stage_duration_seconds(session, first_stage)

            session.policy_accepted_at = now
            session.started_at = now
            session.stage_started_at = now
            session.stage_expires_at = now + timedelta(seconds=duration_sec)
            session.last_heartbeat_at = now
            session.status = target_status
            session.save(update_fields=[
                'policy_accepted_at', 'started_at', 'stage_started_at',
                'stage_expires_at', 'last_heartbeat_at', 'status', 'updated_at',
            ])
            return session

    @classmethod
    def _assert_active_stage(cls, user, session: AssessmentSession, stage_number: int) -> None:
        if session.user_id != user.id:
            raise PermissionDenied("You do not have permission to access this session.")
        expected_status = f"STAGE_{stage_number}"
        if session.status != expected_status or session.current_stage != stage_number:
            raise InvalidStateTransitionError(
                f"Session is in state '{session.status}' (stage {session.current_stage}), expected '{expected_status}'."
            )

    @classmethod
    def check_and_apply_reconnect_grace(cls, session: AssessmentSession, now=None) -> AssessmentSession:
        """
        Grants a single bounded reconnect grace per stage if a network gap occurred,
        never permitting unbounded timer resets.
        """
        now = now or timezone.now()
        stage_num = session.current_stage
        used = list(session.reconnect_grace_used_stages or [])
        if stage_num in used or not session.stage_expires_at:
            return session

        if session.last_heartbeat_at and (now - session.last_heartbeat_at).total_seconds() >= 45:
            if now <= session.stage_expires_at + timedelta(seconds=120):
                spec = BlueprintRegistryService.get_active_spec_for_role(session.career_path.slug)
                grace_sec = int(spec['timing_policy'].get('reconnect_grace_seconds', 120))
                session.stage_expires_at = session.stage_expires_at + timedelta(seconds=grace_sec)
                used.append(stage_num)
                session.reconnect_grace_used_stages = used
                session.last_heartbeat_at = now
                session.save(update_fields=['stage_expires_at', 'reconnect_grace_used_stages', 'last_heartbeat_at', 'updated_at'])
        return session

    @classmethod
    def save_stage_draft(
        cls,
        user,
        session_uuid,
        stage_number: int,
        payload: Dict[str, Any],
    ) -> AssessmentStageResponse:
        now = timezone.now()
        with transaction.atomic():
            session = AssessmentSession.objects.select_for_update().filter(
                session_uuid=session_uuid
            ).first()
            if not session:
                raise ValidationError("Assessment session not found.")
            cls._assert_active_stage(user, session, stage_number)

            stage_resp = AssessmentStageResponse.objects.select_for_update().filter(
                session=session,
                stage_number=stage_number,
            ).first()
            if not stage_resp or stage_resp.is_locked:
                raise StageLockedError("This assessment stage is locked and cannot be modified.")

            if session.stage_expires_at and now > session.stage_expires_at + timedelta(seconds=30):
                raise InvalidStateTransitionError("Stage timer has expired.")

            merged = dict(stage_resp.response_payload or {})
            merged.update(payload or {})
            stage_resp.response_payload = merged
            stage_resp.draft_saved_at = now
            stage_resp.save(update_fields=['response_payload', 'draft_saved_at'])

            session.last_heartbeat_at = now
            session.save(update_fields=['last_heartbeat_at', 'updated_at'])
            return stage_resp

    @classmethod
    def submit_stage_2_part_a(
        cls,
        user,
        session_uuid,
        part_a_payload: Dict[str, Any],
    ) -> AssessmentStageResponse:
        """Locks Stage 2 Part A before revealing Part B requirement shift."""
        now = timezone.now()
        with transaction.atomic():
            session = AssessmentSession.objects.select_for_update().filter(
                session_uuid=session_uuid
            ).first()
            if not session:
                raise ValidationError("Assessment session not found.")
            cls._assert_active_stage(user, session, 2)

            stage_resp = AssessmentStageResponse.objects.select_for_update().filter(
                session=session,
                stage_number=2,
            ).first()
            if not stage_resp or stage_resp.is_locked:
                raise StageLockedError("Stage 2 is locked.")
            if stage_resp.part_a_submitted_at is not None:
                raise StageLockedError("Stage 2 Part A is already locked and cannot be modified.")

            merged = dict(stage_resp.response_payload or {})
            merged['part_a'] = part_a_payload
            stage_resp.response_payload = merged
            stage_resp.part_a_submitted_at = now
            stage_resp.save(update_fields=['response_payload', 'part_a_submitted_at'])
            return stage_resp

    @classmethod
    def lock_and_advance_stage(
        cls,
        user,
        session_uuid,
        stage_number: int,
        final_payload: Dict[str, Any],
        deterministic_result: Optional[Dict[str, Any]] = None,
    ) -> AssessmentSession:
        """
        One-way locks the current stage response and transitions cleanly to the next
        stage or SUBMITTED status. Never allows skipping stages or modifying locked stages.
        """
        now = timezone.now()
        with transaction.atomic():
            session = AssessmentSession.objects.select_for_update().filter(
                session_uuid=session_uuid
            ).first()
            if not session:
                raise ValidationError("Assessment session not found.")
            cls._assert_active_stage(user, session, stage_number)

            stage_resp = AssessmentStageResponse.objects.select_for_update().filter(
                session=session,
                stage_number=stage_number,
            ).first()
            if not stage_resp:
                raise ValidationError(f"Stage {stage_number} response record missing.")
            if stage_resp.is_locked:
                raise StageLockedError(f"Stage {stage_number} is already locked.")

            merged = dict(stage_resp.response_payload or {})
            merged.update(final_payload or {})
            stage_resp.response_payload = merged
            if deterministic_result is not None:
                stage_resp.deterministic_result = deterministic_result
            stage_resp.submitted_at = now
            stage_resp.is_locked = True
            stage_resp.save(update_fields=[
                'response_payload', 'deterministic_result', 'submitted_at', 'is_locked'
            ])

            is_single_drill = session.mode == 'PRACTICE' and session.practice_scope != 'FULL'
            if is_single_drill or stage_number >= 3:
                session.status = 'SUBMITTED'
                session.submitted_at = now
                session.stage_expires_at = None
                session.save(update_fields=['status', 'submitted_at', 'stage_expires_at', 'updated_at'])
            else:
                next_stage = stage_number + 1
                next_duration = cls._stage_duration_seconds(session, next_stage)
                session.current_stage = next_stage
                session.status = f"STAGE_{next_stage}"
                session.stage_started_at = now
                session.stage_expires_at = now + timedelta(seconds=next_duration)
                session.last_heartbeat_at = now
                session.save(update_fields=[
                    'current_stage', 'status', 'stage_started_at',
                    'stage_expires_at', 'last_heartbeat_at', 'updated_at',
                ])
            return session

    @classmethod
    def evaluate_stage3_stopping(
        cls,
        session: AssessmentSession,
        answered_turns: Optional[int] = None,
        covered_competencies: Optional[List[str]] = None,
        ai_confidence: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """
        Conservative, configurable Stage 3 stopping decision (Refinement #7).
        Never stops solely on AI confidence; enforces min_turns and Certification coverage.
        """
        rules = session.blueprint.stopping_rules or {}
        min_turns = int(rules.get('min_turns', 3))
        max_turns = int(rules.get('max_turns', 4))

        if answered_turns is None:
            answered_turns = AdaptiveInterviewTurn.objects.filter(
                session=session,
                replied_at__isnull=False,
            ).count()

        if answered_turns >= max_turns:
            return True, 'MAX_TURNS_REACHED'

        if answered_turns < min_turns:
            return False, 'BELOW_MIN_TURNS'

        if session.mode == 'CERTIFICATION' and not rules.get('allow_early_stop_certification', False):
            return False, 'CERTIFICATION_REQUIRES_FULL_TURNS'

        if session.mode == 'PRACTICE' and not rules.get('allow_early_stop_practice', True):
            return False, 'PRACTICE_EARLY_STOP_DISABLED'

        # Early stop requires deterministic competency coverage AND AI confidence threshold
        if rules.get('require_all_target_competencies_covered', True):
            stage3_resp = session.stage_responses.filter(stage_number=3).first()
            target_set = set(
                (stage3_resp.task_snapshot or {}).get('focus_competencies', [])
                if stage3_resp else []
            )
            covered_set = set(covered_competencies or [])
            if not target_set.issubset(covered_set):
                return False, 'TARGET_COMPETENCIES_INCOMPLETE'

        min_conf = float(rules.get('min_ai_confidence', 0.85))
        if float(ai_confidence or 0.0) < min_conf:
            return False, 'CONFIDENCE_BELOW_THRESHOLD'

        return True, 'CONSERVATIVE_EARLY_STOP_MET'

    @classmethod
    def pause_for_outage(cls, user, session_uuid) -> AssessmentSession:
        """Pauses active stage due to AI outage without failure penalty or cooldown."""
        now = timezone.now()
        with transaction.atomic():
            session = AssessmentSession.objects.select_for_update().filter(
                session_uuid=session_uuid
            ).first()
            if not session:
                raise ValidationError("Assessment session not found.")
            if session.user_id != user.id:
                raise PermissionDenied("You do not have permission to pause this session.")
            if session.status == 'PAUSED_OUTAGE':
                return session
            if session.status not in {'STAGE_1', 'STAGE_2', 'STAGE_3'}:
                raise InvalidStateTransitionError(f"Cannot pause from status '{session.status}'.")

            session.paused_from_status = session.status
            session.paused_at = now
            session.status = 'PAUSED_OUTAGE'
            session.save(update_fields=['paused_from_status', 'paused_at', 'status', 'updated_at'])
            return session

    @classmethod
    def resume_from_outage(cls, user, session_uuid) -> AssessmentSession:
        """Restores paused stage and shifts stage_expires_at by paused elapsed seconds."""
        now = timezone.now()
        with transaction.atomic():
            session = AssessmentSession.objects.select_for_update().filter(
                session_uuid=session_uuid
            ).first()
            if not session:
                raise ValidationError("Assessment session not found.")
            if session.user_id != user.id:
                raise PermissionDenied("You do not have permission to resume this session.")
            if session.status != 'PAUSED_OUTAGE' or not session.paused_from_status:
                raise InvalidStateTransitionError("Session is not paused for outage recovery.")

            paused_secs = 0
            if session.paused_at:
                paused_secs = max(0, int((now - session.paused_at).total_seconds()))

            if session.stage_expires_at:
                session.stage_expires_at = session.stage_expires_at + timedelta(seconds=paused_secs)
            session.accumulated_pause_seconds += paused_secs
            session.status = session.paused_from_status
            session.paused_from_status = ''
            session.paused_at = None
            session.last_heartbeat_at = now
            session.save(update_fields=[
                'stage_expires_at', 'accumulated_pause_seconds', 'status',
                'paused_from_status', 'paused_at', 'last_heartbeat_at', 'updated_at',
            ])
            return session

    @classmethod
    def mark_expired(cls, session_uuid) -> AssessmentSession:
        """
        Transitions timed-out active session cleanly to EXPIRED.
        Does NOT issue certificate and does NOT set failure cooldown.
        """
        now = timezone.now()
        with transaction.atomic():
            session = AssessmentSession.objects.select_for_update().filter(
                session_uuid=session_uuid
            ).first()
            if not session:
                raise ValidationError("Assessment session not found.")
            if session.status not in {'STAGE_1', 'STAGE_2', 'STAGE_3'}:
                raise InvalidStateTransitionError(f"Cannot expire session from '{session.status}'.")
            session.status = 'EXPIRED'
            session.cooldown_until = None
            session.submitted_at = session.submitted_at or now
            session.save(update_fields=['status', 'cooldown_until', 'submitted_at', 'updated_at'])
            return session

    @classmethod
    def mark_pending_evaluation(cls, session_uuid) -> AssessmentSession:
        """
        Parks Certification session safely when AI grading fails during final evaluation.
        Explicitly clears passed/cooldown_until so neither accidental cert nor false cooldown triggers.
        """
        with transaction.atomic():
            session = AssessmentSession.objects.select_for_update().filter(
                session_uuid=session_uuid
            ).first()
            if not session:
                raise ValidationError("Assessment session not found.")
            if session.status not in {'SUBMITTED', 'EVALUATING', 'PENDING_EVALUATION'}:
                raise InvalidStateTransitionError(
                    f"Cannot park for evaluation from '{session.status}'."
                )
            session.status = 'PENDING_EVALUATION'
            session.passed = None
            session.cooldown_until = None
            session.save(update_fields=['status', 'passed', 'cooldown_until', 'updated_at'])
            return session

    @classmethod
    def finalize_evaluation_outcome(
        cls,
        session_uuid,
        overall_score: Decimal,
        passed: bool,
    ) -> AssessmentSession:
        """
        Commits final evaluated verdict atomically. Only failed EVALUATED Certification
        sessions receive the 24-hour retake cooldown.
        """
        now = timezone.now()
        cooldown_hours = int(getattr(settings, 'READINESS_CERT_COOLDOWN_HOURS', 24))
        with transaction.atomic():
            session = AssessmentSession.objects.select_for_update().filter(
                session_uuid=session_uuid
            ).first()
            if not session:
                raise ValidationError("Assessment session not found.")
            if session.status not in {'SUBMITTED', 'EVALUATING', 'PENDING_EVALUATION'}:
                raise InvalidStateTransitionError(
                    f"Cannot finalize evaluation from '{session.status}'."
                )

            session.status = 'EVALUATED'
            session.evaluated_at = now
            session.overall_score = overall_score
            session.passed = bool(passed)
            if session.mode == 'CERTIFICATION' and not passed:
                session.cooldown_until = now + timedelta(hours=cooldown_hours)
            else:
                session.cooldown_until = None

            session.save(update_fields=[
                'status', 'evaluated_at', 'overall_score',
                'passed', 'cooldown_until', 'updated_at',
            ])
            return session
