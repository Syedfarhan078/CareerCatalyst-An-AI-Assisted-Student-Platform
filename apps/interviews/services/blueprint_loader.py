"""
Blueprint Registry Service & Controlled Task Bundle Selector.

Synchronizes versioned JSON blueprints into RoleAssessmentBlueprint records,
resolves seeded roadmap Topic remediation references safely across both dev/prod
and isolated test databases, and assigns calibrated task variants fairly.
"""

import random
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction

from apps.interviews.blueprints import (
    BlueprintValidationError,
    compute_spec_checksum,
    list_blueprint_files,
    load_blueprint_file,
)
from apps.interviews.models import AssessmentSession, RoleAssessmentBlueprint
from apps.roadmaps.models import CareerPath, Topic


# Sprint 1 Pilot Lock: only Software Engineer may be activated in the database.
PILOT_ACTIVE_ROLE_SLUGS = {'software-engineer'}


class BlueprintRegistryService:
    """Manages loading, validation, DB synchronization, and bundle selection."""

    @classmethod
    def get_active_spec_for_role(cls, role_slug: str) -> Dict[str, Any]:
        """Returns the validated JSON spec matching the active blueprint version for role_slug."""
        for path in list_blueprint_files():
            spec = load_blueprint_file(path)
            if spec.get('role_slug') == role_slug and spec.get('is_active'):
                return spec
        raise BlueprintValidationError(f"No active blueprint specification found for role '{role_slug}'.")

    @classmethod
    def resolve_remediation_topics(
        cls,
        career_path: CareerPath,
        topic_refs: List[Dict[str, Any]],
    ) -> Tuple[List[int], List[str]]:
        """
        Resolves portable (week_number, title) topic refs into live DB Topic IDs.
        Returns (resolved_topic_ids, unresolved_warnings).
        """
        resolved_ids: List[int] = []
        warnings: List[str] = []

        topics_qs = Topic.objects.filter(
            milestone__career_path=career_path
        ).select_related('milestone')

        if not topics_qs.exists():
            # Fresh unit-test DB without seeded roadmaps: skip DB topic lookup gracefully
            return resolved_ids, warnings

        by_key = {
            (t.milestone.week_number, t.title.strip().lower()): t.id
            for t in topics_qs
        }
        by_title = {t.title.strip().lower(): t.id for t in topics_qs}

        for ref in topic_refs:
            wk = int(ref.get('week_number', 0))
            title = str(ref.get('title', '')).strip().lower()
            tid = by_key.get((wk, title)) or by_title.get(title)
            if tid:
                resolved_ids.append(tid)
            else:
                warnings.append(f"Unresolved topic ref for {career_path.slug}: W{wk} '{ref.get('title')}'")

        return resolved_ids, warnings

    @classmethod
    def sync_all(
        cls,
        dry_run: bool = False,
        validate_only: bool = False,
        strict_topics: bool = False,
    ) -> Dict[str, Any]:
        """
        Validates all versioned blueprint specs and optionally syncs them into DB.
        Enforces pilot boundary: non-pilot roles cannot be marked active.
        """
        files = list_blueprint_files()
        if not files:
            raise BlueprintValidationError("No blueprint JSON files found.")

        results: Dict[str, Any] = {
            'validated_count': 0,
            'synced_count': 0,
            'warnings': [],
            'blueprints': [],
        }

        specs: List[Dict[str, Any]] = []
        for path in files:
            spec = load_blueprint_file(path)
            role_slug = spec['role_slug']
            if spec.get('is_active') and role_slug not in PILOT_ACTIVE_ROLE_SLUGS:
                raise BlueprintValidationError(
                    f"Pilot policy violation: role '{role_slug}' cannot be active before pilot approval."
                )
            specs.append(spec)
            results['validated_count'] += 1

        if validate_only:
            for spec in specs:
                cp = CareerPath.objects.filter(slug=spec['role_slug']).first()
                if not cp:
                    raise BlueprintValidationError(
                        f"CareerPath '{spec['role_slug']}' does not exist in database."
                    )
                for comp in spec['competency_definitions']:
                    _, warnings = cls.resolve_remediation_topics(cp, comp['remediation_topic_refs'])
                    results['warnings'].extend(warnings)
                results['blueprints'].append({
                    'role_slug': spec['role_slug'],
                    'version': spec['blueprint_version'],
                    'bundles': len(spec['task_bundles']),
                    'checksum': compute_spec_checksum(spec),
                })

            if strict_topics and results['warnings']:
                raise BlueprintValidationError(
                    f"Strict topic validation failed: {results['warnings']}"
                )
            return results

        with transaction.atomic():
            for spec in specs:
                cp = CareerPath.objects.filter(slug=spec['role_slug']).first()
                if not cp:
                    raise BlueprintValidationError(
                        f"CareerPath '{spec['role_slug']}' does not exist in database."
                    )

                enriched_competencies = []
                for comp in spec['competency_definitions']:
                    topic_ids, warnings = cls.resolve_remediation_topics(cp, comp['remediation_topic_refs'])
                    results['warnings'].extend(warnings)
                    comp_copy = dict(comp)
                    comp_copy['resolved_topic_ids'] = topic_ids
                    enriched_competencies.append(comp_copy)

                if strict_topics and results['warnings']:
                    raise BlueprintValidationError(
                        f"Strict topic validation failed: {results['warnings']}"
                    )

                checksum = compute_spec_checksum(spec)
                results['blueprints'].append({
                    'role_slug': spec['role_slug'],
                    'version': spec['blueprint_version'],
                    'bundles': len(spec['task_bundles']),
                    'checksum': checksum,
                })

                if dry_run:
                    continue

                if spec['is_active']:
                    RoleAssessmentBlueprint.objects.filter(
                        career_path=cp,
                        is_active=True,
                    ).exclude(version=spec['blueprint_version']).update(is_active=False)

                RoleAssessmentBlueprint.objects.update_or_create(
                    career_path=cp,
                    version=spec['blueprint_version'],
                    defaults={
                        'task_bank_version': spec['task_bank_version'],
                        'archetype': spec['archetype'],
                        'calibration_status': spec['calibration_status'],
                        'is_active': bool(spec['is_active']),
                        'is_pilot': bool(spec['is_pilot']),
                        'passing_score': Decimal(str(spec['passing_score'])),
                        'borderline_low': Decimal(str(spec['borderline_low'])),
                        'borderline_high': Decimal(str(spec['borderline_high'])),
                        'stage_weights': spec['stage_weights'],
                        'stage_durations': spec['stage_durations'],
                        'stopping_rules': spec['stopping_rules'],
                        'competency_definitions': enriched_competencies,
                        'calibration_metadata': spec.get('calibration_metadata', {}),
                        'spec_checksum': checksum,
                    }
                )
                results['synced_count'] += 1

        return results

    @classmethod
    def select_task_bundle(cls, user, career_path: CareerPath, spec: Dict[str, Any]) -> Dict[str, Any]:
        """
        Controlled randomization: selects among equivalent calibrated bundles while
        excluding the candidate's last 2 seen task_bundle_ids for this role.
        """
        bundles = list(spec.get('task_bundles', []))
        if not bundles:
            raise BlueprintValidationError(f"No task bundles configured for '{career_path.slug}'.")

        recent_ids = list(
            AssessmentSession.objects.filter(user=user, career_path=career_path)
            .order_by('-created_at')
            .values_list('task_bundle_id', flat=True)[:2]
        )
        eligible = [b for b in bundles if b['bundle_id'] not in recent_ids]
        pool = eligible if eligible else bundles
        return random.choice(pool)

    @classmethod
    def get_bundle_by_id(cls, spec: Dict[str, Any], bundle_id: str) -> Dict[str, Any]:
        """Fetches exact bundle dict by ID from a validated spec."""
        for bundle in spec.get('task_bundles', []):
            if bundle.get('bundle_id') == bundle_id:
                return bundle
        raise BlueprintValidationError(f"Task bundle '{bundle_id}' not found in spec '{spec.get('blueprint_version')}'.")

    # Standard neutral taxonomy shown to candidates (never exposes hidden planted defect categories/lines)
    CANDIDATE_ISSUE_CATEGORIES = [
        ('LOGIC_ERROR', 'Logic Error / Incorrect Assumption'),
        ('PERFORMANCE_COMPLEXITY', 'Performance / Algorithmic Complexity'),
        ('INPUT_VALIDATION', 'Input Validation / Edge Case'),
        ('SECURITY_VULNERABILITY', 'Security / Auth Vulnerability'),
        ('CONCURRENCY_RACE', 'Concurrency / Race Condition'),
        ('ERROR_HANDLING', 'Error Handling / Failure Visibility'),
        ('DATA_INTEGRITY', 'Data Integrity / Precision'),
        ('RESOURCE_SCALABILITY', 'Resource / Scalability Risk'),
    ]

    CANDIDATE_SEVERITY_LEVELS = [
        ('CRITICAL', 'Critical (Data loss / Security / Outage)'),
        ('HIGH', 'High (Major reliability / SLA risk)'),
        ('MEDIUM', 'Medium (Edge case / Maintainability)'),
        ('LOW', 'Low (Minor hygiene)'),
    ]

    @classmethod
    def build_stage_snapshot(cls, bundle: Dict[str, Any], stage_number: int) -> Dict[str, Any]:
        """
        Builds immutable task snapshot stored in AssessmentStageResponse.
        Keeps hidden grading keys cleanly isolated inside 'grading_meta' so
        views never serialize answers or planted defect lines before submission.
        """
        if stage_number == 1:
            s1 = bundle['stage_1']
            return {
                'task_id': s1['task_id'],
                'bundle_id': bundle['bundle_id'],
                'title': s1.get('title', bundle.get('title', 'Applied Code Diagnosis')),
                'prompt': s1['prompt'],
                'scenario': s1.get('scenario', ''),
                'requirements': list(s1.get('requirements', [])),
                'expected_behavior': s1.get('expected_behavior', ''),
                'constraints': list(s1.get('constraints', [])),
                'competency_targets': list(s1.get('competency_targets', [])),
                'language': s1.get('language', 'python'),
                'artifact_code': s1['artifact_code'],
                'grading_meta': {
                    'planted_defects': s1['planted_defects'],
                    'testing_expectations': s1.get('testing_expectations', []),
                    'ast_invariants': s1['ast_invariants'],
                },
            }
        if stage_number == 2:
            s2 = bundle['stage_2']
            return {
                'task_id': s2['task_id'],
                'bundle_id': bundle['bundle_id'],
                'title': s2.get('title', bundle.get('title', 'Scenario & Constraint Pivot')),
                'competency_targets': list(s2.get('competency_targets', [])),
                'part_a': s2['part_a'],
                'part_b_twist': s2['part_b_twist'],
                'grading_meta': s2.get('grading_meta', {}),
            }
        if stage_number == 3:
            s3 = bundle['stage_3']
            return {
                'task_id': s3.get('task_id', f"{bundle['bundle_id']}-s3"),
                'bundle_id': bundle['bundle_id'],
                'title': s3.get('title', bundle.get('title', 'Adaptive Ownership Defense')),
                'scenario_context': s3.get('scenario_context', ''),
                'system_constraints': list(s3.get('system_constraints', [])),
                'competency_targets': list(s3.get('competency_targets', [])),
                'initial_question': s3.get('initial_question', ''),
                'initial_probe_type': s3.get('initial_probe_type', 'FAILURE_HANDLING'),
                'initial_target_competency': s3.get('initial_target_competency', 'se_ownership'),
                'focus_competencies': s3['focus_competencies'],
                'twist_pool': s3['twist_pool'],
                'grading_meta': s3.get('grading_meta', {}),
            }
        raise ValueError(f"Unsupported stage_number: {stage_number}")

    @classmethod
    def get_candidate_stage_context(cls, stage_response) -> Dict[str, Any]:
        """
        Returns a strictly candidate-safe view of task_snapshot with all hidden
        grading rubrics, planted defects, and invariants stripped out.
        For Stage 2, also hides part_b_twist until Phase A has been submitted/locked.
        """
        snap = dict(stage_response.task_snapshot or {})
        snap.pop('grading_meta', None)
        snap.pop('defect_categories', None)

        if getattr(stage_response, 'stage_number', 1) == 2:
            part_a_locked = getattr(stage_response, 'part_a_submitted_at', None) is not None
            snap['phase'] = 'PHASE_B' if part_a_locked else 'PHASE_A'
            snap['part_a_locked'] = part_a_locked
            if not part_a_locked:
                # Never reveal the constraint pivot before candidate submits Phase A
                snap.pop('part_b_twist', None)
            return snap

        if getattr(stage_response, 'stage_number', 1) == 3:
            # Strip server-only probe pools and rubric anchors before UI rendering
            snap.pop('twist_pool', None)
            snap.pop('initial_probe_type', None)
            snap.pop('initial_target_competency', None)
            return snap

        code = snap.get('artifact_code', '')
        lines = code.splitlines()
        snap['artifact_lines'] = [
            {'number': idx + 1, 'text': line}
            for idx, line in enumerate(lines)
        ]
        snap['issue_categories'] = [
            {'code': code_val, 'label': label_val}
            for code_val, label_val in cls.CANDIDATE_ISSUE_CATEGORIES
        ]
        snap['severity_levels'] = [
            {'code': code_val, 'label': label_val}
            for code_val, label_val in cls.CANDIDATE_SEVERITY_LEVELS
        ]
        return snap
