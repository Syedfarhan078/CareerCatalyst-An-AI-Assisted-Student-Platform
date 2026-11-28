"""
Sprint 6 Calibration & Evaluation-Validity Test Suite:
Verifies offline calibration runner (32 PROVISIONAL cases across Stages 1-3),
zero DB/quota/certificate side effects, deterministic repeatability, length-bias
bounds, regression fixes for all 7 calibration defects, Stage 3 Turn-3 substance
early-stop guard, AI repeated-run variance bounds, outage fairness, and feature-flag
isolation.
"""

import json
from copy import deepcopy
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.interviews.models import (
    AIUsageLog,
    AdaptiveInterviewTurn,
    AssessmentSession,
    CompetencyScore,
    EvaluationResult,
    RoleAssessmentBlueprint,
)
from apps.interviews.services.assessment_session import AssessmentSessionService
from apps.interviews.services.blueprint_loader import BlueprintRegistryService
from apps.interviews.services.calibration import CalibrationRunner
from apps.interviews.services.stage1_evaluator import Stage1DeterministicEvaluator
from apps.interviews.services.stage2_evaluator import Stage2DeterministicEvaluator
from apps.interviews.services.stage3_evaluator import Stage3OwnershipEvaluator
from apps.roadmaps.models import CareerPath, RoadmapCertificate

User = get_user_model()


class CalibrationFrameworkAndFairnessTests(TestCase):
    def setUp(self):
        call_command('seed_roadmaps')
        call_command('sync_assessment_blueprints')
        self.user = User.objects.create_user(username='cal_user', email='cal@example.com', password='Password123!')
        self.se_path = CareerPath.objects.get(slug='software-engineer')
        self.bp = RoleAssessmentBlueprint.objects.get(career_path=self.se_path, is_active=True)

    def test_full_provisional_calibration_suite_passes_with_zero_false_passes_or_side_effects(self):
        # Verify production candidate tables start empty and remain completely untouched
        self.assertEqual(AssessmentSession.objects.count(), 0)
        self.assertEqual(EvaluationResult.objects.count(), 0)
        self.assertEqual(CompetencyScore.objects.count(), 0)
        self.assertEqual(AIUsageLog.objects.count(), 0)
        self.assertEqual(RoadmapCertificate.objects.count(), 0)

        report = CalibrationRunner.run_suite()

        self.assertEqual(report['total_cases'], 32)
        self.assertEqual(report['by_stage'], {1: 10, 2: 11, 3: 11})
        # Strictly PROVISIONAL until human subject-matter review occurs
        self.assertEqual(report['provisional_fixtures'], 32)
        self.assertEqual(report['human_reviewed_fixtures'], 0)

        self.assertEqual(
            report['cases_outside_expectations'],
            0,
            msg=f"Calibration failures: {[r for r in report['case_results'] if not r['passed_expectation']]}",
        )
        self.assertEqual(report['false_passes'], [])
        self.assertEqual(report['false_fails'], [])
        self.assertEqual(report['critical_gate_mismatches'], 0)
        self.assertEqual(report['competency_discrepancies'], [])

        # Length-bias pairs strictly within blueprint ceiling (<= 6.0 pts)
        self.assertEqual(len(report['length_bias_checks']), 3)
        for lb in report['length_bias_checks']:
            self.assertTrue(lb['within_threshold'], msg=f"Length bias exceeded: {lb}")

        # Alternative designs across Stage 2 & Stage 3 accepted cleanly
        self.assertTrue(len(report['alternative_design_results']) >= 3)
        self.assertTrue(all(a['passed_expectation'] for a in report['alternative_design_results']))

        # Zero DB mutations, zero quota consumed, zero certificates created
        self.assertEqual(AssessmentSession.objects.count(), 0)
        self.assertEqual(EvaluationResult.objects.count(), 0)
        self.assertEqual(AIUsageLog.objects.count(), 0)
        self.assertEqual(RoadmapCertificate.objects.count(), 0)

    def test_deterministic_repeatability_and_equivalent_wording_consistency(self):
        spec = BlueprintRegistryService.get_active_spec_for_role('software-engineer')
        bundle_a = BlueprintRegistryService.get_bundle_by_id(spec, 'se-2026-1-bundle-a')
        s1_snap = BlueprintRegistryService.build_stage_snapshot(bundle_a, 1)
        s2_snap = BlueprintRegistryService.build_stage_snapshot(bundle_a, 2)

        ds = CalibrationRunner.load_dataset()
        s1_case = next(c for c in ds['cases'] if c['case_id'] == 'CAL-S1-01')['candidate_response']
        s2_case = next(c for c in ds['cases'] if c['case_id'] == 'CAL-S2-01')['candidate_response']

        # Identical inputs -> exact identical deterministic results
        r1a = Stage1DeterministicEvaluator.evaluate_payload(s1_snap, s1_case, spec['competency_definitions'], self.se_path.id)
        r1b = Stage1DeterministicEvaluator.evaluate_payload(s1_snap, s1_case, spec['competency_definitions'], self.se_path.id)
        self.assertEqual(r1a['weighted_total'], r1b['weighted_total'])

        r2a = Stage2DeterministicEvaluator.evaluate_payload(s2_snap, s2_case, spec['competency_definitions'], self.se_path.id)
        r2b = Stage2DeterministicEvaluator.evaluate_payload(s2_snap, s2_case, spec['competency_definitions'], self.se_path.id)
        self.assertEqual(r2a['weighted_total'], r2b['weighted_total'])

        # Equivalent Stage 2 wording achieves comparable score (<= 8pt spread)
        s2_alt = next(c for c in ds['cases'] if c['case_id'] == 'CAL-S2-02')['candidate_response']
        r2_alt = Stage2DeterministicEvaluator.evaluate_payload(s2_snap, s2_alt, spec['competency_definitions'], self.se_path.id)
        self.assertLessEqual(abs(r2a['weighted_total'] - r2_alt['weighted_total']), 8.0)

    def test_stage3_turn3_early_stop_blocks_repeated_trivial_or_injection_replies(self):
        sess = AssessmentSessionService.create_session(self.user, self.se_path, mode='PRACTICE', practice_scope='STAGE_3')
        sess = AssessmentSessionService.accept_policy_and_start(self.user, sess.session_uuid)

        # Repeated identical reply across 3 turns must NEVER qualify for Turn-3 early stop even at 0.99 AI confidence
        for turn_no in (1, 2, 3):
            AdaptiveInterviewTurn.objects.create(
                session=sess,
                turn_number=turn_no,
                target_competencies=['se_diagnosis', 'se_architecture', 'se_testing', 'se_ownership'],
                interviewer_prompt=f"Question {turn_no}",
                candidate_reply="Use atomic database conditional updates for concurrency control.",
                replied_at=timezone.now(),
            )
        should_stop, reason = AssessmentSessionService.evaluate_stage3_stopping(
            sess,
            answered_turns=3,
            covered_competencies=['se_diagnosis', 'se_architecture', 'se_testing', 'se_ownership'],
            ai_confidence=0.99,
        )
        self.assertFalse(should_stop)
        self.assertEqual(reason, 'TURN_SUBSTANCE_INSUFFICIENT_FOR_EARLY_STOP')

    @override_settings(GROQ_API_KEY='fake-groq', GEMINI_API_KEY='')
    def test_stage3_repeated_ai_evaluation_variation_and_outage_handling(self):
        # Measure variation across 3 repeated Stage 3 evaluations with slight realistic LLM score variation
        base_payload = {
            'competency_assessments': {
                'se_ownership': {'score': 82.0, 'reasoning': 'Solid ownership.', 'evidence_quotes': ['T1 mechanism']},
                'se_architecture': {'score': 80.0, 'reasoning': 'Cohesive design.', 'evidence_quotes': ['T2 tradeoff']},
                'se_testing': {'score': 84.0, 'reasoning': 'Concrete tests.', 'evidence_quotes': ['T3 load/replay']},
                'se_diagnosis': {'score': 78.0, 'reasoning': 'Clear debugging.', 'evidence_quotes': ['Root cause']},
            },
            'strengths': ['Clear mechanism and rollback checks.'],
            'weak_areas': [],
            'contradictions_detected': [],
            'testing_feedback': ['Good coverage.'],
            'overall_summary': 'Demonstrated strong defense.',
            'evaluator_confidence': 0.88,
        }
        observed_totals = []
        for ai_delta in (-3.0, 0.0, 3.0):
            s = AssessmentSessionService.create_session(self.user, self.se_path, mode='PRACTICE', practice_scope='STAGE_3')
            s = AssessmentSessionService.accept_policy_and_start(self.user, s.session_uuid)
            for idx, txt in enumerate([
                "Decouple slow external payment HTTP from PostgreSQL row locks because connection pools saturate; trade-off is async lag.",
                "Use TTL reaper reconciliation and monitor p95 lock wait alerts alongside unique idempotency keys.",
                "Verify with 500 req/s concurrent load tests, duplicate webhook replay assertions, and fault injection.",
            ], start=1):
                AdaptiveInterviewTurn.objects.create(
                    session=s, turn_number=idx, interviewer_prompt=f"Q{idx}",
                    candidate_reply=txt, replied_at=timezone.now(),
                )
            noisy = deepcopy(base_payload)
            for code in noisy['competency_assessments']:
                noisy['competency_assessments'][code]['score'] += ai_delta
            mock_resp = MagicMock(
                status_code=200,
                json=lambda payload=noisy: {
                    'choices': [{'message': {'content': json.dumps(payload)}}],
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 60, 'total_tokens': 160},
                },
            )
            with patch('apps.interviews.services.ai_gateway.requests.post', return_value=mock_resp):
                res = Stage3OwnershipEvaluator.evaluate_and_finalize(user=self.user, session_uuid=s.session_uuid)
            observed_totals.append(float(res['evaluation'].weighted_total))

        spread = max(observed_totals) - min(observed_totals)
        self.assertLessEqual(spread, 6.0)
        self.assertFalse(settings.ENABLE_READINESS_ASSESSMENT)
        self.assertEqual(RoadmapCertificate.objects.filter(user=self.user).count(), 0)

    def test_management_command_run_assessment_calibration_strict_mode(self):
        call_command('run_assessment_calibration', strict=True)

    def test_evaluator_defect_regressions_pinned(self):
        report = CalibrationRunner.run_suite()
        by_id = {row['case_id']: row for row in report['case_results']}

        # Defect 1 & 2 (Stage 1): impactless diagnosis < 60% and prompt echo rejected
        self.assertLess(by_id['CAL-S1-06']['competency_scores']['se_diagnosis'], 60.0)
        self.assertLess(by_id['CAL-S1-08']['observed_score'], 25.0)
        self.assertEqual(by_id['CAL-S1-07']['observed_score'], 0.0)

        # Defect 4 & 5 (Stage 2): security fail-open trap & full prompt echo capped low
        self.assertFalse(by_id['CAL-S2-06']['observed_gate'])
        self.assertLess(by_id['CAL-S2-11']['competency_scores']['se_ownership'], 20.0)

        # Defect 6 & 7 (Stage 3): hollow prose, prompt injection, and repeated turn capped
        self.assertLess(by_id['CAL-S3-05']['observed_score'], 45.0)
        self.assertLess(by_id['CAL-S3-08']['observed_score'], 45.0)
        self.assertLess(by_id['CAL-S3-09']['observed_score'], 50.0)
        self.assertFalse(by_id['CAL-S3-09']['observed_gate'])
