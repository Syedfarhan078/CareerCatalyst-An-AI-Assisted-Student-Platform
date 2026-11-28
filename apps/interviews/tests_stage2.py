"""
Sprint 4 Test Suite — Stage 2 Engineering Scenario & Constraint Pivot Workbench:
Covers blueprint validation, feature-flag gating, role isolation, cross-user IDOR,
explicit consent, pre-Phase-A pivot/grading-data leakage protection, Phase A immutability,
Phase B draft autosave, duplicate/invalid/expired/abandoned transitions, deterministic
evaluation (strong, valid alternative, concise, pivot-ignored, wrong-direction trap,
contradictions, unsupported assumptions, testing critical gate, echo/spam), persistence,
zero AI quota burn, and Practice History integration.
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
from apps.interviews.services.assessment_session import AssessmentSessionService
from apps.interviews.services.blueprint_loader import BlueprintRegistryService
from apps.interviews.services.stage2_evaluator import Stage2DeterministicEvaluator
from apps.profiles.models import StudentProfile
from apps.roadmaps.models import CareerPath, RoadmapCertificate

User = get_user_model()


class Stage2DeterministicEvaluatorTests(TestCase):
    def setUp(self):
        call_command('seed_roadmaps')
        call_command('sync_assessment_blueprints')
        self.se_path = CareerPath.objects.get(slug='software-engineer')
        self.bp = RoleAssessmentBlueprint.objects.get(career_path=self.se_path, is_active=True)
        self.spec = BlueprintRegistryService.get_active_spec_for_role('software-engineer')
        self.bundle_a = BlueprintRegistryService.get_bundle_by_id(self.spec, 'se-2026-1-bundle-a')
        self.bundle_b = BlueprintRegistryService.get_bundle_by_id(self.spec, 'se-2026-1-bundle-b')
        self.bundle_c = BlueprintRegistryService.get_bundle_by_id(self.spec, 'se-2026-1-bundle-c')
        self.snap_a = BlueprintRegistryService.build_stage_snapshot(self.bundle_a, 2)
        self.snap_b = BlueprintRegistryService.build_stage_snapshot(self.bundle_b, 2)
        self.snap_c = BlueprintRegistryService.build_stage_snapshot(self.bundle_c, 2)

    def test_strong_adaptation_and_valid_concise_alternative_both_score_high_deterministically(self):
        # Alternative 1: PostgreSQL conditional UPDATE + async pending reservation + event_id state machine
        payload_sql = {
            'part_a': {
                'initial_approach': (
                    'Use conditional UPDATE WHERE stock >= qty inside PostgreSQL transaction with '
                    'unique request_id idempotency key and external payment timeout handling.'
                ),
                'data_and_concurrency': (
                    'Keep transaction short around stock reservation while invoking payment capture asynchronously outside lock.'
                ),
                'initial_tradeoffs': 'Higher checkout latency under synchronous capture for simpler ACID order state.',
            },
            'part_b': {
                'updated_approach': (
                    'Switch to two-phase pending stock reservation releasing DB connections immediately before async payment webhook, '
                    'updating hero SKU counts via atomic conditional UPDATE WHERE stock >= qty and deduplicating 4x webhooks via processed_events unique constraint.'
                ),
                'what_changes': (
                    'Remove payment HTTP I/O from database transactions to protect connection pool and keep p95 <= 100ms; '
                    'enforce monotonic state machine ignoring stale out-of-order webhooks.'
                ),
                'what_stays_unchanged': (
                    'PostgreSQL remains source of truth guaranteeing zero overselling invariant and unique order_id schema rules.'
                ),
                'tradeoffs': (
                    'Accept eventual reconciliation lag and added state-machine complexity in exchange for <=100ms p95 under 500 req/sec spikes.'
                ),
                'risks_and_failures': (
                    'Abandoned reservations cleaned via 10m TTL reaper; handle DB connection pool backpressure with 429; '
                    'alert on p95 lock wait latency and retain canary feature-flag rollback.'
                ),
                'validation_and_testing': (
                    'Run 500 req/s hot-SKU concurrent load test verifying zero negative stock; replay 4x duplicate out-of-order webhooks; '
                    'inject payment timeout and TTL expiry reconciliation tests; assert bounded DB pool usage.'
                ),
            },
        }

        eval_1 = Stage2DeterministicEvaluator.evaluate_payload(
            self.snap_a, payload_sql, self.bp.competency_definitions, self.se_path.id
        )
        eval_2 = Stage2DeterministicEvaluator.evaluate_payload(
            self.snap_a, payload_sql, self.bp.competency_definitions, self.se_path.id
        )
        self.assertEqual(eval_1['weighted_total'], eval_2['weighted_total'])
        self.assertGreaterEqual(eval_1['weighted_total'], 85.0)
        self.assertTrue(eval_1['critical_gates_passed'])
        self.assertTrue(eval_1['practice_benchmark_met'])
        self.assertFalse(eval_1['pivot_ignored'])

        # Alternative 2: Concise Redis atomic DECR + TTL reservation + worker DB sync
        payload_redis_concise = {
            'part_a': {
                'initial_approach': 'SELECT FOR UPDATE on catalog row with idempotency unique key inside pending transaction.',
                'data_and_concurrency': 'Short transaction releases lock before payment gateway timeout.',
                'initial_tradeoffs': 'Lower concurrency ceiling traded for straightforward PostgreSQL transaction control.',
            },
            'part_b': {
                'updated_approach': (
                    'Use Redis atomic DECR Lua script reservation with TTL and async worker DB reconciliation; '
                    'process webhooks idempotently with event_id unique key and out-of-order monotonic status transitions.'
                ),
                'what_changes': 'Offload 500 req/s hot SKU reservation to Redis atomic counters within 100ms p95 instead of PostgreSQL row locks.',
                'what_stays_unchanged': 'Zero overselling invariant and PostgreSQL authoritative order ledger remain unchanged.',
                'tradeoffs': 'Trades strong cache-DB synchronization for latency and throughput, accepting Redis-DB drift risk and TTL reconciliation complexity.',
                'risks_and_failures': 'Orphaned TTL cleanup reaper, Redis drift reconciliation job, 503 backpressure, p95 latency alerts and canary rollback.',
                'validation_and_testing': '500 req/s concurrent spike test on hot SKUs; duplicate 4x out-of-order webhook replay; TTL expiry fault injection; DB pool p95 regression.',
            },
        }
        eval_alt = Stage2DeterministicEvaluator.evaluate_payload(
            self.snap_a, payload_redis_concise, self.bp.competency_definitions, self.se_path.id
        )
        self.assertGreaterEqual(eval_alt['weighted_total'], 82.0)
        self.assertTrue(eval_alt['critical_gates_passed'])

    def test_ignoring_pivot_penalized_heavily(self):
        baseline_text = (
            'Run SELECT FOR UPDATE in PostgreSQL inside one synchronous transaction and call payment API before commit '
            'with unique order idempotency constraint.'
        )
        payload = {
            'part_a': {
                'initial_approach': baseline_text,
                'data_and_concurrency': 'PostgreSQL row lock during checkout request handles 50 req/sec cleanly.',
                'initial_tradeoffs': 'Simple synchronous code versus lock wait during slow gateway latency.',
            },
            'part_b': {
                'updated_approach': baseline_text,
                'what_changes': 'Nothing changes needed; keep Phase A unchanged since row lock works well.',
                'what_stays_unchanged': 'Everything remains synchronous in PostgreSQL.',
                'tradeoffs': 'No downsides expected from keeping synchronous flow.',
                'risks_and_failures': 'Minimal risks.',
                'validation_and_testing': 'Run basic happy-path checkout check.',
            },
        }
        res = Stage2DeterministicEvaluator.evaluate_payload(
            self.snap_a, payload, self.bp.competency_definitions, self.se_path.id
        )
        self.assertTrue(res['pivot_ignored'])
        self.assertLess(res['weighted_total'], 45.0)
        self.assertFalse(res['critical_gates_passed'])
        self.assertTrue(len(res['weak_tradeoff_reasoning']) >= 1)

    def test_technical_trap_contradiction_assumption_and_testing_floor(self):
        # Bundle C: Fail-open auth trap + worker LocMemCache assumption + contradiction + weak tests
        payload = {
            'part_a': {
                'initial_approach': 'Store token revocations and 100 req/min rate counters in Redis with constant-time compare_digest.',
                'data_and_concurrency': 'Atomic Redis INCR + TTL per window and PostgreSQL revocation source.',
                'initial_tradeoffs': 'Sub-15ms Redis lookup latency vs DB load.',
            },
            'part_b': {
                'updated_approach': (
                    'During Redis outage, store revocations only in Python dict and fail open for authentication '
                    'so traffic avoids 2.5s SSO latency.'
                ),
                'what_changes': 'Keep zero unauthorized access strictly fail closed while allowing skip signature validation during spikes.',
                'what_stays_unchanged': 'Bearer headers stay.',
                'tradeoffs': 'No trade-offs introduced by adding microservices blockchain service mesh fixes everything.',
                'risks_and_failures': 'Assume sso will never timeout and in-memory cache is shared across gunicorn.',
                'validation_and_testing': 'Check valid token works in unit test.',
            },
        }
        res = Stage2DeterministicEvaluator.evaluate_payload(
            self.snap_c, payload, self.bp.competency_definitions, self.se_path.id
        )
        self.assertFalse(res['critical_gates_passed'])
        self.assertFalse(res['practice_benchmark_met'])
        self.assertLess(res['weighted_total'], 40.0)
        self.assertTrue(any('fail-open' in m.lower() or 'fail open' in m.lower() or 'zero-unauthorized' in m.lower() for m in res['weak_tradeoff_reasoning']))
        self.assertTrue(len(res['unsupported_assumptions']) >= 1)
        self.assertTrue(len(res['risks_not_considered']) >= 1)
        test_comp = next(c for c in res['competency_breakdown'] if c['code'] == 'se_testing')
        self.assertFalse(test_comp['gate_passed'])
        self.assertLess(test_comp['score'], 60.0)

    def test_empty_and_prompt_echo_rejected(self):
        echo_text = self.bundle_b['stage_2']['part_b_twist']['pivot_summary']
        payload = {
            'part_a': {'initial_approach': '', 'data_and_concurrency': '', 'initial_tradeoffs': ''},
            'part_b': {
                'updated_approach': echo_text,
                'what_changes': 'a a a a a a a a',
                'what_stays_unchanged': '',
                'tradeoffs': echo_text,
                'risks_and_failures': '',
                'validation_and_testing': 'test good',
            },
        }
        res = Stage2DeterministicEvaluator.evaluate_payload(
            self.snap_b, payload, self.bp.competency_definitions, self.se_path.id
        )
        self.assertLess(res['weighted_total'], 20.0)
        self.assertFalse(res['practice_benchmark_met'])


class Stage2WorkbenchLifecycleAndSecurityTests(TestCase):
    def setUp(self):
        self.user_a = User.objects.create_user(
            username='s4_student_a',
            email='s4_a@example.com',
            password='Password123!',
        )
        self.user_b = User.objects.create_user(
            username='s4_student_b',
            email='s4_b@example.com',
            password='Password123!',
        )
        StudentProfile.objects.create(user=self.user_a, career_goal='Software Engineer')
        call_command('seed_roadmaps')
        call_command('sync_assessment_blueprints')

        self.se_path = CareerPath.objects.get(slug='software-engineer')
        self.bp = RoleAssessmentBlueprint.objects.get(career_path=self.se_path, is_active=True)

        ReadinessUserPreference.objects.create(
            user=self.user_a,
            extended_time_enabled=True,
            last_consent_version=self.bp.version,
            last_consented_at=timezone.now(),
        )
        self.client.force_login(self.user_a)

    def test_feature_flag_off_blocks_all_stage2_routes_and_keeps_legacy_hub(self):
        self.assertFalse(settings.ENABLE_READINESS_ASSESSMENT)
        hub_resp = self.client.get(reverse('interviews:hub'), HTTP_HOST='127.0.0.1')
        self.assertEqual(hub_resp.status_code, 200)
        self.assertTemplateUsed(hub_resp, 'interviews/hub.html')

        dummy_uuid = '00000000-0000-0000-0000-000000000002'
        self.assertEqual(
            self.client.post(reverse('interviews:stage2_practice_start', args=['software-engineer']), HTTP_HOST='127.0.0.1').status_code,
            404,
        )
        self.assertEqual(
            self.client.get(reverse('interviews:stage2_workbench', args=[dummy_uuid]), HTTP_HOST='127.0.0.1').status_code,
            404,
        )
        self.assertEqual(
            self.client.get(reverse('interviews:stage2_result', args=[dummy_uuid]), HTTP_HOST='127.0.0.1').status_code,
            404,
        )

    @override_settings(ENABLE_READINESS_ASSESSMENT=True)
    def test_inactive_roles_and_missing_consent_blocked_or_redirected(self):
        for slug in ['backend-developer', 'ml-engineer', 'ui-ux-designer']:
            resp = self.client.post(
                reverse('interviews:stage2_practice_start', args=[slug]),
                HTTP_HOST='127.0.0.1',
            )
            self.assertEqual(resp.status_code, 403)

        self.client.force_login(self.user_b)
        no_consent_resp = self.client.post(
            reverse('interviews:stage2_practice_start', args=['software-engineer']),
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(no_consent_resp.status_code, 302)
        self.assertIn(f"{reverse('interviews:readiness_policy', args=['software-engineer'])}?scope=STAGE_2", no_consent_resp.url)
        self.assertEqual(AssessmentSession.objects.filter(user=self.user_b).count(), 0)

    @override_settings(ENABLE_READINESS_ASSESSMENT=True)
    def test_full_two_phase_stage2_lifecycle_isolation_and_persistence(self):
        # 1. Start Stage 2 Practice -> creates active session with 1.25x timer
        start_resp = self.client.post(
            reverse('interviews:stage2_practice_start', args=['software-engineer']),
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(start_resp.status_code, 302)
        session = AssessmentSession.objects.get(user=self.user_a, mode='PRACTICE', practice_scope='STAGE_2')
        self.assertEqual(session.status, 'STAGE_2')
        self.assertEqual(session.time_multiplier, Decimal('1.25'))

        wb_url = reverse('interviews:stage2_workbench', args=[session.session_uuid])
        draft_url = reverse('interviews:stage2_save_draft', args=[session.session_uuid])
        res_url = reverse('interviews:stage2_result', args=[session.session_uuid])

        # 2. Phase A render must hide grading_meta AND hide Phase B constraint pivot!
        phase_a_resp = self.client.get(wb_url, HTTP_HOST='127.0.0.1')
        self.assertEqual(phase_a_resp.status_code, 200)
        html_a = phase_a_resp.content.decode('utf-8')
        self.assertNotIn('grading_meta', html_a)
        self.assertNotIn('adaptation_concepts', html_a)
        self.assertNotIn('technical_traps', html_a)
        self.assertNotIn('Constraint Pivot Revealed', html_a)
        self.assertIn('Submit Initial Approach (Phase A)', html_a)

        # Cannot skip Phase A straight to final submission
        skip_resp = self.client.post(
            wb_url,
            data={'action': 'submit_final', 'updated_approach': 'Trying to skip Phase A cleanly.'},
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(skip_resp.status_code, 400)
        sr = AssessmentStageResponse.objects.get(session=session, stage_number=2)
        self.assertIsNone(sr.part_a_submitted_at)

        # 3. Cross-user IDOR strictly blocked across all Stage 2 endpoints
        self.client.force_login(self.user_b)
        self.assertEqual(self.client.get(wb_url, HTTP_HOST='127.0.0.1').status_code, 403)
        self.assertEqual(
            self.client.post(draft_url, data=json.dumps({'part_a': {}}), content_type='application/json', HTTP_HOST='127.0.0.1').status_code,
            403,
        )
        self.assertEqual(
            self.client.post(reverse('interviews:stage2_abandon', args=[session.session_uuid]), HTTP_HOST='127.0.0.1').status_code,
            403,
        )
        self.assertEqual(self.client.get(res_url, HTTP_HOST='127.0.0.1').status_code, 403)

        # 4. Back to owner: save Phase A draft, then lock Phase A -> reveals Phase B Pivot
        self.client.force_login(self.user_a)
        draft_a_resp = self.client.post(
            draft_url,
            data=json.dumps({
                'part_a': {
                    'initial_approach': 'Baseline synchronous API design with PostgreSQL transaction.',
                    'data_and_concurrency': 'Unique idempotency key and atomic DB update.',
                }
            }),
            content_type='application/json',
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(draft_a_resp.status_code, 200)
        self.assertEqual(draft_a_resp.json()['phase'], 'PHASE_A')

        original_initial_approach = 'Authoritative baseline initial design with transaction boundaries and unique idempotency constraint.'
        lock_resp = self.client.post(
            wb_url,
            data={
                'action': 'lock_part_a',
                'initial_approach': original_initial_approach,
                'data_and_concurrency': 'Atomic PostgreSQL updates and Redis TTL cache with short timeouts.',
                'initial_tradeoffs': 'Simpler request path traded against connection saturation risk at higher RPS.',
            },
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(lock_resp.status_code, 302)
        self.assertEqual(lock_resp.url, wb_url)

        sr.refresh_from_db()
        self.assertIsNotNone(sr.part_a_submitted_at)

        # 5. Now GET shows Constraint Pivot Revealed + Read-Only Phase A, still NO hidden grading_meta
        phase_b_resp = self.client.get(wb_url, HTTP_HOST='127.0.0.1')
        self.assertEqual(phase_b_resp.status_code, 200)
        html_b = phase_b_resp.content.decode('utf-8')
        self.assertIn('Constraint Pivot Revealed', html_b)
        self.assertIn('Read-Only (Phase A)', html_b)
        self.assertIn(original_initial_approach, html_b)
        self.assertNotIn('grading_meta', html_b)
        self.assertNotIn('assumption_red_flags', html_b)

        # Attempting draft overwrite of part_a once locked must leave part_a immutable!
        tamper_draft = self.client.post(
            draft_url,
            data=json.dumps({
                'part_a': {'initial_approach': 'HACKED OVERWRITE'},
                'part_b': {'updated_approach': 'Valid Phase B draft update preserved.'},
            }),
            content_type='application/json',
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(tamper_draft.status_code, 200)
        sr.refresh_from_db()
        self.assertEqual(sr.response_payload['part_a']['initial_approach'], original_initial_approach)
        self.assertEqual(sr.response_payload['part_b']['updated_approach'], 'Valid Phase B draft update preserved.')

        # Submitting tampered scores / trying to re-lock Part A is rejected / ignored
        relock_attempt = self.client.post(
            wb_url,
            data={'action': 'lock_part_a', 'initial_approach': 'OVERWRITE'},
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(relock_attempt.status_code, 400)

        # 6. Final Phase B submission with tampered client score=100 ignored -> deterministic eval & 0 AI quota
        with patch('apps.interviews.services.ai_gateway.requests.post') as mock_ai:
            final_resp = self.client.post(
                wb_url,
                data={
                    'action': 'submit_final',
                    'updated_approach': (
                        'Adopt fast-lane queue isolation / pending reservation or circuit breaker fallback '
                        'with atomic conditional updates, idempotent keys, and streaming cursor bounds.'
                    ),
                    'what_changes': 'Move blocking work off synchronous threads and isolate failures cleanly.',
                    'what_stays_unchanged': 'Authoritative PostgreSQL source of truth and security fail-closed invariants remain unchanged.',
                    'tradeoffs': 'Accept eventual lag and queue complexity to preserve latency under load.',
                    'risks_and_failures': 'Monitor p95, alert on dead-letter queue lag, and keep canary rollback ready.',
                    'validation_and_testing': 'Verify with concurrent spike load test, fault-injection timeout simulation, and replay tests.',
                    'overall_score': '100',
                    'passed': 'true',
                },
                HTTP_HOST='127.0.0.1',
            )
            mock_ai.assert_not_called()

        self.assertEqual(final_resp.status_code, 302)
        self.assertEqual(final_resp.url, res_url)

        session.refresh_from_db()
        sr.refresh_from_db()
        self.assertEqual(session.status, 'EVALUATED')
        self.assertTrue(sr.is_locked)
        self.assertIsNone(session.cooldown_until)
        self.assertEqual(AIUsageLog.objects.filter(user=self.user_a).count(), 0)
        self.assertEqual(RoadmapCertificate.objects.filter(user=self.user_a).count(), 0)

        eval_res = EvaluationResult.objects.get(session=session)
        self.assertEqual(eval_res.scoring_mode, 'DETERMINISTIC_PRACTICE')
        self.assertIn('2', eval_res.stage_scores)
        self.assertEqual(CompetencyScore.objects.filter(evaluation=eval_res).count(), 3)

        # Duplicate submission redirects idempotently to result; draft returns 409
        self.assertEqual(self.client.post(wb_url, data={'action': 'submit_final', 'updated_approach': 'Again'}, HTTP_HOST='127.0.0.1').url, res_url)
        self.assertEqual(
            self.client.post(draft_url, data=json.dumps({'part_b': {}}), content_type='application/json', HTTP_HOST='127.0.0.1').status_code,
            409,
        )

        # Result & Practice History pages integrate cleanly
        result_page = self.client.get(res_url, HTTP_HOST='127.0.0.1')
        self.assertEqual(result_page.status_code, 200)
        self.assertTemplateUsed(result_page, 'interviews/stage2_result.html')
        res_html = result_page.content.decode('utf-8')
        self.assertIn('What You Handled Well', res_html)
        self.assertIn('Missed Considerations', res_html)
        self.assertIn('Weak Trade-off Reasoning', res_html)
        self.assertIn('Unsupported Assumptions', res_html)
        self.assertIn('Risks Not Considered', res_html)
        self.assertIn('Recommended Testing', res_html)

        hist_page = self.client.get(reverse('interviews:readiness_history'), HTTP_HOST='127.0.0.1')
        self.assertEqual(hist_page.status_code, 200)
        self.assertEqual(len(hist_page.context['practice_sessions']), 1)
        self.assertEqual(hist_page.context['quota']['remaining'], 8)
        self.assertIn(res_url, hist_page.content.decode('utf-8'))

    @override_settings(ENABLE_READINESS_ASSESSMENT=True)
    def test_expired_and_abandoned_stage2_sessions_handled_safely(self):
        self.client.post(reverse('interviews:stage2_practice_start', args=['software-engineer']), HTTP_HOST='127.0.0.1')
        s1 = AssessmentSession.objects.get(user=self.user_a, status='STAGE_2')
        s1.stage_expires_at = timezone.now() - timedelta(minutes=2)
        s1.save(update_fields=['stage_expires_at'])

        exp_resp = self.client.post(
            reverse('interviews:stage2_workbench', args=[s1.session_uuid]),
            data={'action': 'lock_part_a', 'initial_approach': 'Too late submission', 'data_and_concurrency': 'Late'},
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(exp_resp.status_code, 400)
        self.assertTemplateUsed(exp_resp, 'interviews/stage2_closed.html')
        s1.refresh_from_db()
        self.assertEqual(s1.status, 'EXPIRED')
        self.assertIsNone(s1.cooldown_until)

        self.client.post(reverse('interviews:stage2_practice_start', args=['software-engineer']), HTTP_HOST='127.0.0.1')
        s2 = AssessmentSession.objects.get(user=self.user_a, status='STAGE_2')
        ab_resp = self.client.post(
            reverse('interviews:stage2_abandon', args=[s2.session_uuid]),
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(ab_resp.status_code, 302)
        s2.refresh_from_db()
        self.assertEqual(s2.status, 'CANCELLED')
        self.assertIsNone(s2.cooldown_until)
