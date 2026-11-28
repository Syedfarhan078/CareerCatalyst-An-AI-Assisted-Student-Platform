"""
Bounded Adaptive Follow-Up Engine for Stage 3 (Engineering Ownership Defense).

Enforces server-authoritative turn caps (max 4 candidate turns, 1 initial question
+ up to 3 adaptive follow-ups), context compression across turns, prompt-injection
isolation, strict structured-output schema validation, duplicate-submission /
duplicate-generation prevention, and zero-penalty PAUSED_OUTAGE recovery.
"""

from datetime import timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.interviews.models import (
    AIUsageLog,
    AdaptiveInterviewTurn,
    AssessmentSession,
    AssessmentStageResponse,
)
from apps.interviews.services.ai_gateway import (
    AIGatewayOutageError,
    AISchemaValidationError,
    AssessmentAIService,
)
from apps.interviews.services.assessment_session import (
    AssessmentSessionService,
    InvalidStateTransitionError,
    StageLockedError,
)


VALID_PROBE_TYPES = {
    'INITIAL_OWNERSHIP',
    'CLARIFICATION',
    'TECHNICAL_DEPTH',
    'TRADEOFFS',
    'FAILURE_HANDLING',
    'SECURITY_RELIABILITY',
    'TESTING',
    'CONTRADICTION',
    'WEAK_REASONING',
    'ADVANCED_EXTENSION',
}

VALID_DIFFICULTY_ADJUSTMENTS = {'MAINTAIN', 'STEP_UP', 'CLARIFY_DOWN'}

FORBIDDEN_QUESTION_SNIPPETS = (
    'tell me about yourself',
    'why do you like coding',
    'candidate_untrusted_input',
    'security boundary',
)


class QuotaExhaustedError(PermissionDenied):
    """Raised when a user's 24-hour AI practice quota is exhausted."""


class Stage3OwnershipEngine:
    """Orchestrates bounded adaptive follow-ups for Stage 3 Ownership Defense."""

    HARD_MAX_TURNS = 4
    MIN_REPLY_CHARS = 12
    MAX_REPLY_CHARS = 3000

    @classmethod
    def get_turn_bounds(cls, session: AssessmentSession) -> Tuple[int, int]:
        rules = session.blueprint.stopping_rules or {}
        min_turns = max(2, min(cls.HARD_MAX_TURNS, int(rules.get('min_turns', 3))))
        max_turns = max(min_turns, min(cls.HARD_MAX_TURNS, int(rules.get('max_turns', 4))))
        return min_turns, max_turns

    @classmethod
    def ensure_initial_turn(cls, session: AssessmentSession) -> AdaptiveInterviewTurn:
        """
        Idempotently initializes Turn 1 from the frozen Stage 3 snapshot.
        Uses authored domain-specific engineering prompts (0 AI calls, 0 quota burned).
        """
        with transaction.atomic():
            locked_session = AssessmentSession.objects.select_for_update().get(pk=session.pk)
            existing = AdaptiveInterviewTurn.objects.filter(
                session=locked_session,
                turn_number=1,
            ).first()
            if existing:
                return existing

            stage_resp = AssessmentStageResponse.objects.filter(
                session=locked_session,
                stage_number=3,
            ).first()
            snap = dict((stage_resp.task_snapshot if stage_resp else {}) or {})

            initial_q = str(snap.get('initial_question') or '').strip()
            if not initial_q:
                twists = snap.get('twist_pool') or []
                initial_q = (
                    str(twists[0])
                    if twists
                    else "Walk through the most critical design choice in this system and explain how it handles unexpected failures or retries."
                )

            target_comp = str(snap.get('initial_target_competency') or 'se_ownership')
            focus_comps = list(snap.get('focus_competencies') or ['se_ownership'])
            if target_comp not in focus_comps:
                target_comp = focus_comps[0]

            probe_type = str(snap.get('initial_probe_type') or 'FAILURE_HANDLING')
            if probe_type not in VALID_PROBE_TYPES:
                probe_type = 'INITIAL_OWNERSHIP'

            brief = AssessmentAIService.build_stage3_context_brief(locked_session, max_chars=1400)

            return AdaptiveInterviewTurn.objects.create(
                session=locked_session,
                turn_number=1,
                target_competencies=[target_comp],
                interviewer_prompt=initial_q,
                twist_injected=False,
                context_summary_used=brief,
                turn_analysis={
                    'probe_type': probe_type,
                    'difficulty_adjustment': 'MAINTAIN',
                    'source': 'BLUEPRINT_INITIAL',
                    'reason_for_probing': 'Initial engineering decision & failure-mode ownership baseline.',
                },
                provider_name='blueprint',
                provider_model='deterministic-init',
                tokens_used=0,
            )

    @classmethod
    def compress_turn_history(
        cls,
        turns: List[AdaptiveInterviewTurn],
        max_chars: int = 1350,
    ) -> str:
        """
        Compresses prior turns into a strict bounded digest so conversation prompts
        never grow unboundedly across multi-turn follow-ups.
        """
        lines: List[str] = []
        for t in turns:
            if t.replied_at is None:
                continue
            probe = (t.turn_analysis or {}).get('probe_type', 'PROBE')
            q_short = (t.interviewer_prompt or '').strip().replace('\n', ' ')[:150]
            a_short = (t.candidate_reply or '').strip().replace('\n', ' ')[:210]
            lines.append(f"T{t.turn_number}[{probe}] Q:{q_short} | A:{a_short}")
        digest = "\n".join(lines)
        if len(digest) > max_chars:
            return digest[-max_chars:]
        return digest

    @classmethod
    def validate_followup_output(
        cls,
        raw: Any,
        *,
        allowed_competencies: Set[str],
        min_turns: int,
        answered_turn_number: int,
    ) -> Dict[str, Any]:
        """
        Strictly validates and bounds structured AI follow-up output before
        storing or displaying. Rejects malformed fields or spoofed competencies.
        """
        if not isinstance(raw, dict):
            raise AISchemaValidationError("AI follow-up payload must be a JSON object.")

        question = str(raw.get('followup_question', '') or '').strip()
        if len(question) < 15 or len(question) > 650:
            raise AISchemaValidationError("followup_question length outside [15, 650] bounds.")
        q_lower = question.lower()
        if any(bad in q_lower for bad in FORBIDDEN_QUESTION_SNIPPETS):
            raise AISchemaValidationError("followup_question violated generic-trivia or boundary rules.")

        target_comp = str(raw.get('target_competency', '') or '').strip()
        if target_comp not in allowed_competencies:
            raise AISchemaValidationError(f"Unsupported target_competency '{target_comp}'.")

        probe_type = str(raw.get('probe_type', '') or '').strip().upper()
        if probe_type not in VALID_PROBE_TYPES:
            raise AISchemaValidationError(f"Invalid probe_type '{probe_type}'.")

        difficulty = str(raw.get('difficulty_adjustment', 'MAINTAIN') or 'MAINTAIN').strip().upper()
        if difficulty not in VALID_DIFFICULTY_ADJUSTMENTS:
            raise AISchemaValidationError(f"Invalid difficulty_adjustment '{difficulty}'.")

        reason = str(raw.get('reason_for_probing', '') or '').strip()[:450]
        evidence = str(raw.get('evidence_from_response', '') or '').strip()[:350]
        direction = str(raw.get('expected_reasoning_direction', '') or '').strip()[:450]
        if not reason or not evidence or not direction:
            raise AISchemaValidationError(
                "AI follow-up missing required reason_for_probing, evidence_from_response, or expected_reasoning_direction."
            )

        raw_covered = raw.get('covered_competencies', [])
        if not isinstance(raw_covered, list):
            raw_covered = [target_comp]
        covered = [c for c in raw_covered if isinstance(c, str) and c in allowed_competencies]
        if target_comp not in covered:
            covered.append(target_comp)

        try:
            conf = float(raw.get('ai_confidence', 0.7))
        except (TypeError, ValueError) as exc:
            raise AISchemaValidationError("Invalid ai_confidence value.") from exc
        conf = max(0.0, min(1.0, conf))

        recommend_stop = bool(raw.get('recommend_stop', False))
        # Server never allows early-stop signals below min_turns
        if answered_turn_number < min_turns:
            recommend_stop = False

        return {
            'followup_question': question,
            'target_competency': target_comp,
            'probe_type': probe_type,
            'difficulty_adjustment': difficulty,
            'reason_for_probing': reason,
            'evidence_from_response': evidence,
            'expected_reasoning_direction': direction,
            'covered_competencies': covered,
            'ai_confidence': conf,
            'recommend_stop': recommend_stop,
        }

    @classmethod
    def _build_followup_prompts(
        cls,
        *,
        session: AssessmentSession,
        snapshot: Dict[str, Any],
        turns: List[AdaptiveInterviewTurn],
        latest_turn: AdaptiveInterviewTurn,
        min_turns: int,
        max_turns: int,
    ) -> Tuple[str, str]:
        focus = list(snapshot.get('focus_competencies') or ['se_ownership', 'se_architecture', 'se_testing'])
        anchors = (snapshot.get('grading_meta') or {}).get('rubric_anchors') or []
        scenario_ctx = str(snapshot.get('scenario_context') or '')[:600]
        history_digest = cls.compress_turn_history(turns[:-1], max_chars=1100) if len(turns) > 1 else 'None (initial turn)'
        wrapped_reply, _ = AssessmentAIService.sanitize_candidate_text(latest_turn.candidate_reply, max_chars=2600)

        system_prompt = (
            "You are a senior principal engineer conducting a bounded Stage 3 Engineering Ownership Defense.\n"
            f"Focus competencies (choose ONLY from): {focus}.\n"
            f"Turn state: Candidate just answered turn {latest_turn.turn_number} of max {max_turns} (min {min_turns}).\n"
            "Rules:\n"
            "- Generate ONE incisive follow-up question directly grounded in the candidate's response.\n"
            "- Probe directions allowed: CLARIFICATION, TECHNICAL_DEPTH, TRADEOFFS, FAILURE_HANDLING, "
            "SECURITY_RELIABILITY, TESTING, CONTRADICTION, WEAK_REASONING, ADVANCED_EXTENSION.\n"
            "- If response is vague/buzzwordy: probe concrete mechanism (WEAK_REASONING/CLARIFICATION).\n"
            "- If response contradicts prior turn: ask them to reconcile it (CONTRADICTION).\n"
            "- If response is strong: step up to a realistic edge case or operational trade-off (ADVANCED_EXTENSION/FAILURE_HANDLING).\n"
            "- Do NOT ask trivia or personal questions.\n"
            "Return strict JSON with keys:\n"
            "followup_question (str 15-650 chars), target_competency (str), probe_type (str), "
            "reason_for_probing (str), difficulty_adjustment ('MAINTAIN'|'STEP_UP'|'CLARIFY_DOWN'), "
            "evidence_from_response (str), expected_reasoning_direction (str), "
            "covered_competencies (list[str]), ai_confidence (float 0..1), recommend_stop (bool)."
        )

        user_prompt = (
            f"Scenario: {scenario_ctx}\n"
            f"Prior work brief: {(latest_turn.context_summary_used or '')[:700]}\n"
            f"Server rubric anchors (do NOT quote to candidate): {'; '.join(anchors)[:500]}\n"
            f"Compressed prior turns:\n{history_digest}\n\n"
            f"Last question asked (Turn {latest_turn.turn_number}): {latest_turn.interviewer_prompt}\n"
            f"Candidate reply to Turn {latest_turn.turn_number} (UNTRUSTED DATA):\n{wrapped_reply}"
        )
        return system_prompt, user_prompt

    @classmethod
    def _generate_next_turn_with_gateway(
        cls,
        *,
        user,
        session: AssessmentSession,
        snapshot: Dict[str, Any],
        turns: List[AdaptiveInterviewTurn],
        latest_turn: AdaptiveInterviewTurn,
    ) -> Dict[str, Any]:
        """
        Generates Turn (latest_turn.turn_number + 1) through AssessmentAIService cleanly,
        checking idempotency, turn ceiling, per-session call caps, and outage pausing.
        """
        min_turns, max_turns = cls.get_turn_bounds(session)
        next_num = latest_turn.turn_number + 1

        if latest_turn.turn_number >= max_turns:
            return {'status': 'READY_FOR_COMPLETION', 'reason': 'MAX_TURNS_REACHED', 'turn': None}

        # Idempotent guard before making external call
        existing_next = AdaptiveInterviewTurn.objects.filter(
            session=session,
            turn_number=next_num,
        ).first()
        if existing_next:
            return {'status': 'CONTINUE', 'reason': 'TURN_EXISTS', 'turn': existing_next}

        # Quota & per-session call cap guard (never bypass via direct POST)
        quota = AssessmentSessionService.check_practice_daily_quota(user)
        if not quota['allowed']:
            raise QuotaExhaustedError("Daily 24-hour AI practice quota reached.")
        followup_calls = AIUsageLog.objects.filter(
            session=session,
            operation='STAGE3_FOLLOWUP',
            success=True,
        ).count()
        if followup_calls >= (max_turns - 1):
            return {'status': 'READY_FOR_COMPLETION', 'reason': 'CALL_CAP_REACHED', 'turn': None}

        allowed_comps = set(snapshot.get('focus_competencies') or ['se_diagnosis', 'se_architecture', 'se_testing', 'se_ownership'])
        system_prompt, user_prompt = cls._build_followup_prompts(
            session=session,
            snapshot=snapshot,
            turns=turns,
            latest_turn=latest_turn,
            min_turns=min_turns,
            max_turns=max_turns,
        )

        def _validator(raw_dict):
            return cls.validate_followup_output(
                raw_dict,
                allowed_competencies=allowed_comps,
                min_turns=min_turns,
                answered_turn_number=latest_turn.turn_number,
            )

        try:
            ai_res = AssessmentAIService.call_structured_json(
                operation='STAGE3_FOLLOWUP',
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema_validator=_validator,
                user=user,
                session=session,
                tier='fast',
                max_tokens=650,
                temperature=0.2,
            )
        except AIGatewayOutageError:
            # Save candidate progress safely and pause session without penalty/cooldown
            AssessmentSessionService.pause_for_outage(user, session.session_uuid)
            return {'status': 'PAUSED_OUTAGE', 'reason': 'AI_PROVIDER_OUTAGE', 'turn': None}

        validated = ai_res['data']

        # Accumulate unique covered competencies across turns safely
        covered_acc: Set[str] = set()
        for t in turns:
            covered_acc.update(t.target_competencies or [])
        covered_acc.update(validated['covered_competencies'])

        with transaction.atomic():
            locked_session = AssessmentSession.objects.select_for_update().get(pk=session.pk)
            if locked_session.status != 'STAGE_3':
                raise InvalidStateTransitionError(f"Session moved to '{locked_session.status}' concurrently.")

            # Double-check duplicate generation inside lock
            dup_next = AdaptiveInterviewTurn.objects.filter(
                session=locked_session,
                turn_number=next_num,
            ).first()
            if dup_next:
                return {'status': 'CONTINUE', 'reason': 'TURN_EXISTS', 'turn': dup_next}

            should_stop, stop_reason = AssessmentSessionService.evaluate_stage3_stopping(
                locked_session,
                answered_turns=latest_turn.turn_number,
                covered_competencies=list(covered_acc),
                ai_confidence=validated['ai_confidence'],
            )
            # Only early-stop at >= min_turns if BOTH AI recommended stop AND conservative policy allows it
            if should_stop and validated['recommend_stop']:
                return {'status': 'READY_FOR_COMPLETION', 'reason': stop_reason, 'turn': None}

            twist_flag = validated['probe_type'] in {
                'FAILURE_HANDLING', 'CONTRADICTION', 'ADVANCED_EXTENSION', 'SECURITY_RELIABILITY'
            }
            new_turn = AdaptiveInterviewTurn.objects.create(
                session=locked_session,
                turn_number=next_num,
                target_competencies=[validated['target_competency']],
                interviewer_prompt=validated['followup_question'],
                twist_injected=twist_flag,
                context_summary_used=cls.compress_turn_history(turns, max_chars=900),
                turn_analysis={
                    'probe_type': validated['probe_type'],
                    'difficulty_adjustment': validated['difficulty_adjustment'],
                    'reason_for_probing': validated['reason_for_probing'],
                    'evidence_from_response': validated['evidence_from_response'],
                    'expected_reasoning_direction': validated['expected_reasoning_direction'],
                    'covered_competencies': validated['covered_competencies'],
                    'ai_confidence': validated['ai_confidence'],
                    'source': 'AI_ADAPTIVE',
                },
                provider_name=ai_res['provider'],
                provider_model=ai_res['model'],
                tokens_used=int((ai_res.get('usage') or {}).get('total_tokens', 0)),
            )
            return {'status': 'CONTINUE', 'reason': 'FOLLOWUP_CREATED', 'turn': new_turn}

    @classmethod
    def submit_turn_reply(
        cls,
        *,
        user,
        session_uuid,
        turn_number: int,
        reply_text: str,
    ) -> Dict[str, Any]:
        """
        Atomically validates and locks candidate's reply for the current open turn,
        then either enters READY_FOR_COMPLETION or generates next adaptive follow-up.
        """
        now = timezone.now()
        cleaned_raw = str(reply_text or '').replace('\x00', ' ').strip()[:cls.MAX_REPLY_CHARS]
        if len(cleaned_raw) < cls.MIN_REPLY_CHARS:
            raise ValidationError("Please provide a substantive engineering explanation (at least 12 characters).")

        _, flags = AssessmentAIService.sanitize_candidate_text(cleaned_raw, max_chars=cls.MAX_REPLY_CHARS)

        with transaction.atomic():
            session = AssessmentSession.objects.select_for_update().filter(
                session_uuid=session_uuid
            ).first()
            if not session:
                raise ValidationError("Assessment session not found.")
            if session.user_id != user.id:
                raise PermissionDenied("You do not have permission to access this session.")
            if session.status != 'STAGE_3':
                raise InvalidStateTransitionError(f"Cannot submit turn while session is '{session.status}'.")
            if session.stage_expires_at and now > session.stage_expires_at + timedelta(seconds=30):
                raise InvalidStateTransitionError("Stage timer has expired.")

            stage_resp = AssessmentStageResponse.objects.select_for_update().filter(
                session=session,
                stage_number=3,
            ).first()
            if not stage_resp or stage_resp.is_locked:
                raise StageLockedError("Stage 3 is already locked.")

            turns = list(
                AdaptiveInterviewTurn.objects.select_for_update()
                .filter(session=session)
                .order_by('turn_number')
            )
            if not turns:
                raise ValidationError("Initial question missing.")

            open_turns = [t for t in turns if t.replied_at is None]
            if not open_turns:
                raise StageLockedError("All generated questions have already been answered.")
            active_turn = open_turns[0]
            if active_turn.turn_number != int(turn_number):
                raise InvalidStateTransitionError(
                    f"Turn mismatch: expected turn {active_turn.turn_number}, received {turn_number}."
                )

            analysis = dict(active_turn.turn_analysis or {})
            if flags:
                analysis['injection_flags'] = flags
            active_turn.candidate_reply = cleaned_raw
            active_turn.replied_at = now
            active_turn.turn_analysis = analysis
            active_turn.save(update_fields=['candidate_reply', 'replied_at', 'turn_analysis'])

            # Clear draft text in Stage 3 response_payload for the submitted turn
            payload = dict(stage_resp.response_payload or {})
            drafts = dict(payload.get('turn_drafts') or {})
            drafts[str(active_turn.turn_number)] = cleaned_raw
            payload['turn_drafts'] = drafts
            payload['answered_turns'] = active_turn.turn_number
            stage_resp.response_payload = payload
            stage_resp.draft_saved_at = now
            stage_resp.save(update_fields=['response_payload', 'draft_saved_at'])

            session.last_heartbeat_at = now
            session.save(update_fields=['last_heartbeat_at', 'updated_at'])

            snapshot = dict(stage_resp.task_snapshot or {})

        # Refresh ordered turns cleanly after committing reply
        all_turns = list(AdaptiveInterviewTurn.objects.filter(session=session).order_by('turn_number'))
        return cls._generate_next_turn_with_gateway(
            user=user,
            session=session,
            snapshot=snapshot,
            turns=all_turns,
            latest_turn=active_turn,
        )

    @classmethod
    def resume_from_outage_and_recover(cls, *, user, session_uuid) -> Dict[str, Any]:
        """
        Safely resumes a PAUSED_OUTAGE Stage 3 session, preserving all answered turns
        and generating only the pending follow-up if needed. Never duplicates turns.
        """
        session = AssessmentSessionService.resume_from_outage(user, session_uuid)
        stage_resp = AssessmentStageResponse.objects.get(session=session, stage_number=3)
        turns = list(AdaptiveInterviewTurn.objects.filter(session=session).order_by('turn_number'))
        if not turns:
            t1 = cls.ensure_initial_turn(session)
            return {'status': 'CONTINUE', 'reason': 'INITIALIZED', 'turn': t1}

        open_turns = [t for t in turns if t.replied_at is None]
        if open_turns:
            # Candidate hadn't answered the current question yet -> 0 AI calls!
            return {'status': 'CONTINUE', 'reason': 'EXISTING_OPEN_TURN', 'turn': open_turns[0]}

        latest = turns[-1]
        _, max_turns = cls.get_turn_bounds(session)
        if latest.turn_number >= max_turns:
            return {'status': 'READY_FOR_COMPLETION', 'reason': 'MAX_TURNS_REACHED', 'turn': None}

        return cls._generate_next_turn_with_gateway(
            user=user,
            session=session,
            snapshot=stage_resp.task_snapshot or {},
            turns=turns,
            latest_turn=latest,
        )
