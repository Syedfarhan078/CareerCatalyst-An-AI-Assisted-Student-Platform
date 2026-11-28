"""
Sprint 3 Test Suite — Stage 1 Applied Diagnosis & Safe AST Workbench:
Covers feature-flag gating, role isolation, cross-user IDOR, explicit consent,
draft autosave, hidden-rubric protection, duplicate/expired/abandoned lifecycle,
order-independent deterministic evaluation, critical-defect floors, false positives,
AST parser safety bounds (no code execution), zero AI quota burn, and history isolation.
"""

import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.interviews.models import (
    AIUsageLog,
    AssessmentSession,
    AssessmentStageResponse,
    CompetencyScore,
    EvaluationResult,
    ReadinessUserPreference,
    RoleAssessmentBlueprint,
)
from apps.interviews.services.blueprint_loader import BlueprintRegistryService
from apps.interviews.services.stage1_ast import Stage1ASTAnalyzer
from apps.interviews.services.stage1_evaluator import Stage1DeterministicEvaluator
from apps.profiles.models import StudentProfile
from apps.roadmaps.models import CareerPath, RoadmapCertificate

User = get_user_model()


class Stage1ASTSafetyTests(TestCase):
    def test_ast_handles_valid_malformed_empty_large_and_pathological_without_execution(self):
        # Empty input
        empty = Stage1ASTAnalyzer.analyze("")
        self.assertEqual(empty['status'], 'EMPTY')
        self.assertFalse(empty['code_executed'])

        # Malformed Python syntax
        syntax_err = Stage1ASTAnalyzer.analyze("def broken(:\n    pass")
        self.assertEqual(syntax_err['status'], 'SYNTAX_ERROR')
        self.assertFalse(syntax_err['syntax_valid'])
        self.assertFalse(syntax_err['code_executed'])

        # Oversized input rejected cleanly
        huge = Stage1ASTAnalyzer.analyze("x = 1\n" * 3000)
        self.assertEqual(huge['status'], 'INPUT_TOO_LARGE')
        self.assertFalse(huge['code_executed'])

        # Deep pathological AST nesting (> MAX_AST_DEPTH = 40) does not raise RecursionError
        deep = "[" * 60 + "1" + "]" * 60
        patho = Stage1ASTAnalyzer.analyze(deep)
        self.assertEqual(patho['status'], 'PATHOLOGICAL_AST')
        self.assertFalse(patho['code_executed'])

        # Infinite loop or OS import is NEVER executed (no timeout, no subprocess, no exec)
        dangerous = "import os\ndef exploit():\n    while True:\n        os.system('echo PWNED')\n"
        safe_scan = Stage1ASTAnalyzer.analyze(dangerous, invariants=[{'rule': 'no_bare_except_pass', 'description': 'check'}])
        self.assertEqual(safe_scan['status'], 'VALID_AST')
        self.assertFalse(safe_scan['code_executed'])

    def test_ast_invariants_detect_bare_except_and_fail_open(self):
        bad_patch = "def f(x):\n    try:\n        return x\n    except Exception:\n        pass\n"
        res_bad = Stage1ASTAnalyzer.analyze(bad_patch, invariants=[{'rule': 'no_bare_except_pass'}])
        self.assertEqual(res_bad['passed_rules'], 0)

        fail_open = "def auth(h):\n    try:\n        return False\n    except Exception:\n        return True\n"
        res_open = Stage1ASTAnalyzer.analyze(fail_open, invariants=[{'rule': 'no_fail_open_return_true'}])
        self.assertEqual(res_open['passed_rules'], 0)


class Stage1DeterministicEvaluationUnitTests(TestCase):
    def setUp(self):
        call_command('seed_roadmaps')
        call_command('sync_assessment_blueprints')
        self.se_path = CareerPath.objects.get(slug='software-engineer')
        self.bp = RoleAssessmentBlueprint.objects.get(career_path=self.se_path, is_active=True)
        self.spec = BlueprintRegistryService.get_active_spec_for_role('software-engineer')
        self.bundle_a = BlueprintRegistryService.get_bundle_by_id(self.spec, 'se-2026-1-bundle-a')
        self.snapshot_a = BlueprintRegistryService.build_stage_snapshot(self.bundle_a, 1)

    def test_strong_order_independent_diagnosis_and_repeatability(self):
        # Submit findings in reverse/scrambled order with alternative valid phrasing
        payload = {
            'findings': [
                {
                    'title': 'Check-then-act stale dictionary causes race condition oversell',
                    'category': 'CONCURRENCY_RACE',
                    'severity': 'CRITICAL',
                    'line_ref': '8-10',
                    'impact': 'Concurrent workers read stale stock and double-sell / oversell the same SKU.',
                    'fix_suggestion': 'Perform an atomic conditional DB update inside a transaction or select_for_update lock.',
                },
                {
                    'title': 'Swallowing Exception with pass hides failures silently',
                    'category': 'ERROR_HANDLING',
                    'severity': 'CRITICAL',
                    'line_ref': '11-12',
                    'impact': 'DB errors cause silent data loss and invisible retries.',
                    'fix_suggestion': 'Log the failure with structured error context and re-raise cleanly.',
                },
                {
                    'title': 'Quadratic nested loop scans processed orders and catalog linearly',
                    'category': 'PERFORMANCE_COMPLEXITY',
                    'severity': 'HIGH',
                    'line_ref': '4-7',
                    'impact': 'High CPU latency and timeouts under 5,000-order throughput.',
                    'fix_suggestion': 'Build a seen_ids set and SKU dict lookup for O(1) access.',
                },
                {
                    'title': 'Missing input validation on None or negative quantity',
                    'category': 'INPUT_VALIDATION',
                    'severity': 'HIGH',
                    'line_ref': '1-8',
                    'impact': 'Negative qty can corrupt stock or missing keys crash with KeyError.',
                    'fix_suggestion': 'Guard if not orders or qty <= 0 and raise ValueError.',
                },
            ],
            'proposed_fix_code': (
                "def process_order_batch(orders, catalog_items, repo):\n"
                "    if not orders:\n"
                "        return []\n"
                "    seen = set()\n"
                "    processed = []\n"
                "    for order in orders:\n"
                "        oid = order.get('order_id')\n"
                "        qty = order.get('qty', 0)\n"
                "        if not oid or qty <= 0:\n"
                "            raise ValueError('Invalid order')\n"
                "        if oid in seen:\n"
                "            continue\n"
                "        seen.add(oid)\n"
                "        repo.decrement_stock(order['sku'], qty)\n"
                "        processed.append(order)\n"
                "    return processed\n"
            ),
            'test_plan': (
                "1. Test duplicate order_id batch deduplication.\n"
                "2. Test zero/negative qty and None/missing key raises ValueError.\n"
                "3. Mock repo exception to assert error propagation instead of silent pass.\n"
                "4. Test concurrent repeated same-SKU stock decrement prevents overselling."
            ),
            'overall_summary': 'The AI helper looks tidy on happy paths but fails under concurrent load, bad inputs, and DB errors.',
        }

        eval_1 = Stage1DeterministicEvaluator.evaluate_payload(
            self.snapshot_a, payload, self.bp.competency_definitions, self.se_path.id
        )
        eval_2 = Stage1DeterministicEvaluator.evaluate_payload(
            self.snapshot_a, payload, self.bp.competency_definitions, self.se_path.id
        )

        # Deterministic repeatability
        self.assertEqual(eval_1['weighted_total'], eval_2['weighted_total'])
        self.assertGreaterEqual(eval_1['weighted_total'], 80.0)
        self.assertTrue(eval_1['critical_gates_passed'])
        self.assertEqual(len(eval_1['matched_defects']), 4)
        self.assertEqual(len(eval_1['unsupported_findings']), 0)

    def test_missing_critical_issue_caps_diagnosis_below_critical_floor(self):
        # Candidate identifies the two non-critical defects perfectly, but misses BOTH critical defects
        payload = {
            'findings': [
                {
                    'title': 'Quadratic nested loop complexity',
                    'category': 'PERFORMANCE_COMPLEXITY',
                    'severity': 'HIGH',
                    'line_ref': '4-7',
                    'impact': 'Slow latency at scale.',
                    'fix_suggestion': 'Use a set and dict lookup.',
                },
                {
                    'title': 'No validation for negative qty or None',
                    'category': 'INPUT_VALIDATION',
                    'severity': 'HIGH',
                    'line_ref': '1-8',
                    'impact': 'KeyError crash or corrupt qty.',
                    'fix_suggestion': 'Raise ValueError if qty <= 0.',
                },
            ],
            'test_plan': 'Test negative qty, duplicate order_id, exception propagation, concurrent stock.',
        }
        res = Stage1DeterministicEvaluator.evaluate_payload(
            self.snapshot_a, payload, self.bp.competency_definitions, self.se_path.id
        )
        self.assertFalse(res['critical_gates_passed'])
        self.assertFalse(res['practice_benchmark_met'])
        self.assertEqual(len(res['missed_critical_defects']), 2)
        diag_comp = next(c for c in res['competency_breakdown'] if c['code'] == 'se_diagnosis')
        self.assertLess(diag_comp['score'], 60.0)
        self.assertFalse(diag_comp['gate_passed'])

    def test_false_positive_and_artifact_echo_penalized(self):
        payload = {
            'findings': [
                {
                    'title': 'SQL injection in for order in orders:',
                    'category': 'SECURITY_VULNERABILITY',
                    'severity': 'CRITICAL',
                    'line_ref': '3',
                    'impact': 'Attacker drops table via microservices kafka blockchain.',
                    'fix_suggestion': 'Add parameterized ORM.',
                },
                {
                    'title': "if any(p['order_id'] == order['order_id'] for p in processed):",
                    'category': 'LOGIC_ERROR',
                    'severity': 'HIGH',
                    'line_ref': '4',
                    'impact': "if any(p['order_id'] == order['order_id'] for p in processed):",
                    'fix_suggestion': "continue",
                },
            ],
            'test_plan': 'test good output',
        }
        res = Stage1DeterministicEvaluator.evaluate_payload(
            self.snapshot_a, payload, self.bp.competency_definitions, self.se_path.id
        )
        self.assertLess(res['weighted_total'], 30.0)
        self.assertEqual(len(res['matched_defects']), 0)
        self.assertEqual(len(res['unsupported_findings']), 2)


class Stage1WorkbenchLifecycleAndSecurityTests(TestCase):
    def setUp(self):
        self.user_a = User.objects.create_user(
            username='s3_student_a',
            email='s3_a@example.com',
            password='Password123!',
        )
        self.user_b = User.objects.create_user(
            username='s3_student_b',
            email='s3_b@example.com',
            password='Password123!',
        )
        StudentProfile.objects.create(user=self.user_a, career_goal='Software Engineer')
        call_command('seed_roadmaps')
        call_command('sync_assessment_blueprints')

        self.se_path = CareerPath.objects.get(slug='software-engineer')
        self.bp = RoleAssessmentBlueprint.objects.get(career_path=self.se_path, is_active=True)

        # Grant explicit consent for user_a with +25% extended time
        ReadinessUserPreference.objects.create(
            user=self.user_a,
            extended_time_enabled=True,
            last_consent_version=self.bp.version,
            last_consented_at=timezone.now(),
        )
        self.client.force_login(self.user_a)

    def test_feature_flag_off_blocks_all_stage1_routes_and_keeps_legacy_hub(self):
        self.assertFalse(settings.ENABLE_READINESS_ASSESSMENT)
        hub_resp = self.client.get(reverse('interviews:hub'), HTTP_HOST='127.0.0.1')
        self.assertEqual(hub_resp.status_code, 200)
        self.assertTemplateUsed(hub_resp, 'interviews/hub.html')

        dummy_uuid = '00000000-0000-0000-0000-000000000001'
        self.assertEqual(
            self.client.post(reverse('interviews:stage1_practice_start', args=['software-engineer']), HTTP_HOST='127.0.0.1').status_code,
            404,
        )
        self.assertEqual(
            self.client.get(reverse('interviews:stage1_workbench', args=[dummy_uuid]), HTTP_HOST='127.0.0.1').status_code,
            404,
        )
        self.assertEqual(
            self.client.get(reverse('interviews:stage1_result', args=[dummy_uuid]), HTTP_HOST='127.0.0.1').status_code,
            404,
        )

    @override_settings(ENABLE_READINESS_ASSESSMENT=True)
    def test_inactive_roles_and_missing_consent_blocked_or_redirected(self):
        # Other 14 roles strictly return 403
        for slug in ['backend-developer', 'data-scientist', 'product-manager']:
            resp = self.client.post(
                reverse('interviews:stage1_practice_start', args=[slug]),
                HTTP_HOST='127.0.0.1',
            )
            self.assertEqual(resp.status_code, 403)

        # User B without explicit consent cannot skip straight into Stage 1
        self.client.force_login(self.user_b)
        no_consent_resp = self.client.post(
            reverse('interviews:stage1_practice_start', args=['software-engineer']),
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(no_consent_resp.status_code, 302)
        self.assertIn(reverse('interviews:readiness_policy', args=['software-engineer']), no_consent_resp.url)
        self.assertEqual(AssessmentSession.objects.filter(user=self.user_b).count(), 0)

    @override_settings(ENABLE_READINESS_ASSESSMENT=True)
    def test_full_stage1_lifecycle_autosave_submission_security_and_zero_ai_quota(self):
        # 1. Start drill -> creates 1 active session with 1.25x timer (600 * 1.25 = 750s)
        start_resp = self.client.post(
            reverse('interviews:stage1_practice_start', args=['software-engineer']),
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(start_resp.status_code, 302)
        session = AssessmentSession.objects.get(user=self.user_a, mode='PRACTICE', practice_scope='STAGE_1')
        self.assertEqual(session.status, 'STAGE_1')
        self.assertTrue(session.extended_time_enabled)
        self.assertEqual(session.time_multiplier, Decimal('1.25'))

        # Repeating start reuses the active open session instead of spawning duplicates
        dup_start = self.client.post(
            reverse('interviews:stage1_practice_start', args=['software-engineer']),
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(dup_start.status_code, 302)
        self.assertEqual(AssessmentSession.objects.filter(user=self.user_a).count(), 1)

        # 2. Workbench renders WITHOUT leaking hidden defect labels or grading_meta
        wb_url = reverse('interviews:stage1_workbench', args=[session.session_uuid])
        wb_resp = self.client.get(wb_url, HTTP_HOST='127.0.0.1')
        self.assertEqual(wb_resp.status_code, 200)
        self.assertTemplateUsed(wb_resp, 'interviews/stage1_workbench.html')
        html = wb_resp.content.decode('utf-8')
        self.assertNotIn('grading_meta', html)
        self.assertNotIn('planted_defects', html)
        self.assertNotIn('root_cause_concepts', html)
        self.assertIn('+25% Extended Time', html)

        # 3. Cross-user IDOR strictly blocked across all Stage 1 endpoints
        self.client.force_login(self.user_b)
        self.assertEqual(self.client.get(wb_url, HTTP_HOST='127.0.0.1').status_code, 403)
        self.assertEqual(
            self.client.post(
                reverse('interviews:stage1_save_draft', args=[session.session_uuid]),
                data=json.dumps({'findings': []}),
                content_type='application/json',
                HTTP_HOST='127.0.0.1',
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(reverse('interviews:stage1_abandon', args=[session.session_uuid]), HTTP_HOST='127.0.0.1').status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse('interviews:stage1_result', args=[session.session_uuid]), HTTP_HOST='127.0.0.1').status_code,
            403,
        )

        # 4. Back to owner: save draft persists cleanly
        self.client.force_login(self.user_a)
        draft_resp = self.client.post(
            reverse('interviews:stage1_save_draft', args=[session.session_uuid]),
            data=json.dumps({
                'findings': [{'title': 'Draft race bug', 'category': 'CONCURRENCY_RACE', 'severity': 'CRITICAL', 'line_ref': '8-10', 'impact': 'Oversell', 'fix_suggestion': 'Use atomic update'}],
                'test_plan': 'Check overselling',
            }),
            content_type='application/json',
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(draft_resp.status_code, 200)
        self.assertTrue(draft_resp.json()['ok'])

        # Empty final submission rejected cleanly (400)
        empty_submit = self.client.post(
            wb_url,
            data={'findings_json': '[]', 'test_plan': ''},
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(empty_submit.status_code, 400)

        # 5. Final submission with tampered score/passed params ignored; evaluated deterministically with 0 AI calls
        sr = AssessmentStageResponse.objects.get(session=session, stage_number=1)
        # Construct findings aligned to whichever bundle was assigned cleanly via snapshot
        planted = sr.task_snapshot['grading_meta']['planted_defects']
        submitted_findings = [
            {
                'title': d['label'],
                'category': d['category'],
                'severity': d['severity'],
                'line_ref': d['line_range'],
                'impact': " ".join(d.get('impact_concepts', [])[:2]),
                'fix_suggestion': " ".join(d.get('fix_concepts', [])[:2]),
            }
            for d in planted[:2]  # Only submit 2 defects intentionally -> partial score (ignores tampered score=100)
        ]

        with patch('apps.interviews.services.ai_gateway.requests.post') as mock_ai:
            final_resp = self.client.post(
                wb_url,
                data={
                    'findings_json': json.dumps(submitted_findings),
                    'test_plan': 'Test missing/None inputs and failure exception handling.',
                    'score': '100',
                    'passed': 'true',
                },
                HTTP_HOST='127.0.0.1',
            )
            mock_ai.assert_not_called()

        self.assertEqual(final_resp.status_code, 302)
        result_url = reverse('interviews:stage1_result', args=[session.session_uuid])
        self.assertEqual(final_resp.url, result_url)

        session.refresh_from_db()
        sr.refresh_from_db()
        self.assertEqual(session.status, 'EVALUATED')
        self.assertTrue(sr.is_locked)
        self.assertLess(float(session.overall_score), 95.0)  # Tampered score=100 ignored
        self.assertEqual(AIUsageLog.objects.filter(user=self.user_a).count(), 0)
        self.assertEqual(RoadmapCertificate.objects.filter(user=self.user_a).count(), 0)
        self.assertEqual(CompetencyScore.objects.filter(evaluation__session=session).count(), 3)

        # 6. Draft & duplicate submit blocked after evaluation
        post_lock_draft = self.client.post(
            reverse('interviews:stage1_save_draft', args=[session.session_uuid]),
            data=json.dumps({'findings': []}),
            content_type='application/json',
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(post_lock_draft.status_code, 409)

        dup_submit = self.client.post(
            wb_url,
            data={'findings_json': json.dumps(submitted_findings)},
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(dup_submit.status_code, 302)
        self.assertEqual(dup_submit.url, result_url)

        # 7. Result page shows breakdown + remediation + History shows completed session separately from cert
        res_page = self.client.get(result_url, HTTP_HOST='127.0.0.1')
        self.assertEqual(res_page.status_code, 200)
        self.assertTemplateUsed(res_page, 'interviews/stage1_result.html')
        self.assertIn('Zero Code Execution', res_page.content.decode('utf-8'))

        hist_page = self.client.get(reverse('interviews:readiness_history'), HTTP_HOST='127.0.0.1')
        self.assertEqual(hist_page.status_code, 200)
        self.assertEqual(len(hist_page.context['practice_sessions']), 1)
        self.assertEqual(len(hist_page.context['certification_sessions']), 0)
        self.assertEqual(hist_page.context['quota']['remaining'], 8)

    @override_settings(ENABLE_READINESS_ASSESSMENT=True)
    def test_expired_and_abandoned_stage1_sessions_handled_safely(self):
        # Start and expire
        self.client.post(reverse('interviews:stage1_practice_start', args=['software-engineer']), HTTP_HOST='127.0.0.1')
        s1 = AssessmentSession.objects.get(user=self.user_a, status='STAGE_1')
        s1.stage_expires_at = timezone.now() - timedelta(minutes=2)
        s1.save(update_fields=['stage_expires_at'])

        wb_resp = self.client.post(
            reverse('interviews:stage1_workbench', args=[s1.session_uuid]),
            data={'findings_json': json.dumps([{'title': 'Late submit', 'impact': 'too late', 'fix_suggestion': 'x'}])},
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(wb_resp.status_code, 400)
        self.assertTemplateUsed(wb_resp, 'interviews/stage1_closed.html')
        s1.refresh_from_db()
        self.assertEqual(s1.status, 'EXPIRED')
        self.assertIsNone(s1.cooldown_until)

        # Start new session and abandon cleanly
        self.client.post(reverse('interviews:stage1_practice_start', args=['software-engineer']), HTTP_HOST='127.0.0.1')
        s2 = AssessmentSession.objects.get(user=self.user_a, status='STAGE_1')
        ab_resp = self.client.post(
            reverse('interviews:stage1_abandon', args=[s2.session_uuid]),
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(ab_resp.status_code, 302)
        s2.refresh_from_db()
        self.assertEqual(s2.status, 'CANCELLED')
        self.assertIsNone(s2.cooldown_until)
