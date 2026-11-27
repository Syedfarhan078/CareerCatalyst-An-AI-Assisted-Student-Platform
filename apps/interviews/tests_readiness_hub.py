"""
Sprint 2 Test Suite:
Tests Feature Flag toggling (OFF vs ON), all 15 CareerPath states (Software Engineer
pilot active vs 14 Coming Soon), strict 403 blocking on inactive roles, explicit
policy consent & +25% extended-time persistence, DB-backed Practice History &
8/24h AI Quota display, and full legacy route & dashboard compatibility.
"""

from decimal import Decimal
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.interviews.models import (
    AIUsageLog,
    AssessmentSession,
    Question,
    QuestionCategory,
    ReadinessUserPreference,
    RoleAssessmentBlueprint,
)
from apps.profiles.models import StudentProfile
from apps.roadmaps.models import CareerPath

User = get_user_model()


class Sprint2ReadinessHubTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='sprint2_student',
            email='sprint2_student@example.com',
            password='Password123!',
            first_name='Aarav',
            last_name='Sharma',
        )
        StudentProfile.objects.create(
            user=self.user,
            career_goal='Software Engineer',
            skills='Python, SQL, Django',
        )
        self.other_user = User.objects.create_user(
            username='sprint2_other',
            email='sprint2_other@example.com',
            password='Password123!',
        )

        call_command('seed_roadmaps')
        call_command('sync_assessment_blueprints')

        self.se_path = CareerPath.objects.get(slug='software-engineer')
        self.backend_path = CareerPath.objects.get(slug='backend-developer')
        self.se_blueprint = RoleAssessmentBlueprint.objects.get(career_path=self.se_path, is_active=True)

        # Minimal legacy QuestionCategory for legacy route checks
        self.legacy_cat = QuestionCategory.objects.create(
            name='Technical MCQ',
            slug='technical',
            description='Legacy MCQ',
        )
        Question.objects.create(
            category=self.legacy_cat,
            title='HTTP 200 status',
            content='Meaning of 200?',
            question_type='MCQ',
            options=['OK', 'Bad', 'Wait', 'Skip'],
            correct_option='A',
        )

        self.client.force_login(self.user)

    def test_feature_flag_off_preserves_legacy_hub_and_hides_readiness_routes(self):
        self.assertFalse(settings.ENABLE_READINESS_ASSESSMENT)

        hub_resp = self.client.get(reverse('interviews:hub'), HTTP_HOST='127.0.0.1')
        self.assertEqual(hub_resp.status_code, 200)
        self.assertTemplateUsed(hub_resp, 'interviews/hub.html')

        for route in [
            reverse('interviews:readiness_roles'),
            reverse('interviews:readiness_role_overview', args=['software-engineer']),
            reverse('interviews:readiness_policy', args=['software-engineer']),
            reverse('interviews:readiness_history'),
        ]:
            resp = self.client.get(route, HTTP_HOST='127.0.0.1')
            self.assertEqual(resp.status_code, 404)

    @override_settings(ENABLE_READINESS_ASSESSMENT=True)
    def test_feature_flag_on_displays_new_hub_with_real_empty_metrics(self):
        resp = self.client.get(reverse('interviews:hub'), HTTP_HOST='127.0.0.1')
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'interviews/readiness_hub.html')

        content = resp.content.decode('utf-8')
        self.assertIn('Software Engineer Practice Track', content)
        self.assertIn('Unavailable in Pilot Shell', content)
        self.assertIn('8 <span class="readiness-stat-unit">/ 8 left</span>', content)
        self.assertIn('No readiness practice sessions yet', content)
        self.assertEqual(resp.context['practice_total'], 0)
        self.assertIsNone(resp.context['practice_avg_score'])

    @override_settings(ENABLE_READINESS_ASSESSMENT=True)
    def test_unauthenticated_redirects_on_readiness_routes(self):
        self.client.logout()
        for route in [
            reverse('interviews:readiness_roles'),
            reverse('interviews:readiness_role_overview', args=['software-engineer']),
            reverse('interviews:readiness_policy', args=['software-engineer']),
            reverse('interviews:readiness_history'),
        ]:
            resp = self.client.get(route, HTTP_HOST='127.0.0.1')
            self.assertEqual(resp.status_code, 302)
            self.assertIn(reverse('login'), resp.url)

    @override_settings(ENABLE_READINESS_ASSESSMENT=True)
    def test_career_selector_displays_all_15_roles_with_single_active_pilot(self):
        resp = self.client.get(reverse('interviews:readiness_roles'), HTTP_HOST='127.0.0.1')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['total_role_count'], 15)
        self.assertEqual(resp.context['active_pilot_count'], 1)

        role_cards = resp.context['role_cards']
        self.assertEqual(len(role_cards), 15)
        active_slugs = [c['career_path'].slug for c in role_cards if c['is_active_pilot']]
        self.assertEqual(active_slugs, ['software-engineer'])

        content = resp.content.decode('utf-8')
        self.assertIn('Active Pilot', content)
        self.assertEqual(content.count('Coming Soon'), 14 * 2)  # Pill + disabled button per inactive role

        # Archetype filtering works cleanly
        ml_resp = self.client.get(f"{reverse('interviews:readiness_roles')}?archetype=DATA_ML", HTTP_HOST='127.0.0.1')
        self.assertEqual(ml_resp.status_code, 200)
        self.assertEqual(len(ml_resp.context['role_cards']), 4)

    @override_settings(ENABLE_READINESS_ASSESSMENT=True)
    def test_inactive_roles_blocked_via_url_and_post_manipulation(self):
        for inactive_slug in ['backend-developer', 'data-scientist', 'product-manager', 'ml-engineer']:
            get_overview = self.client.get(
                reverse('interviews:readiness_role_overview', args=[inactive_slug]),
                HTTP_HOST='127.0.0.1',
            )
            self.assertEqual(get_overview.status_code, 403)
            self.assertTemplateUsed(get_overview, 'interviews/readiness_role_locked.html')

            get_policy = self.client.get(
                reverse('interviews:readiness_policy', args=[inactive_slug]),
                HTTP_HOST='127.0.0.1',
            )
            self.assertEqual(get_policy.status_code, 403)

            post_policy = self.client.post(
                reverse('interviews:readiness_policy', args=[inactive_slug]),
                data={'accept_policy': 'on', 'extended_time': 'on', 'scope': 'FULL'},
                HTTP_HOST='127.0.0.1',
            )
            self.assertEqual(post_policy.status_code, 403)

        self.assertFalse(ReadinessUserPreference.objects.filter(user=self.user).exists())

        unknown_resp = self.client.get(
            reverse('interviews:readiness_role_overview', args=['nonexistent-role']),
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(unknown_resp.status_code, 404)

    @override_settings(ENABLE_READINESS_ASSESSMENT=True)
    def test_software_engineer_overview_shows_3_formats_competencies_and_locked_cert(self):
        resp = self.client.get(
            reverse('interviews:readiness_role_overview', args=['software-engineer']),
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'interviews/readiness_overview.html')
        content = resp.content.decode('utf-8')
        self.assertIn('Code &amp; AI-Generated Artifact Review', content)
        self.assertIn('Engineering Scenario &amp; Constraint Pivot', content)
        self.assertIn('Adaptive Technical Defense', content)
        self.assertIn('Locked', content)
        self.assertIn('Critical', content)

    @override_settings(ENABLE_READINESS_ASSESSMENT=True)
    def test_explicit_consent_required_and_accessibility_preference_persists(self):
        policy_url = reverse('interviews:readiness_policy', args=['software-engineer'])

        # Missing consent -> 400 and does NOT save consent or extended_time
        bad_post = self.client.post(
            policy_url,
            data={'extended_time': 'on', 'scope': 'STAGE_1'},
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(bad_post.status_code, 400)
        pref = ReadinessUserPreference.objects.get(user=self.user)
        self.assertIsNone(pref.last_consented_at)
        self.assertFalse(pref.extended_time_enabled)

        # Invalid scope rejected cleanly
        bad_scope = self.client.post(
            policy_url,
            data={'accept_policy': 'on', 'scope': 'ADMIN_BYPASS'},
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(bad_scope.status_code, 400)

        # Explicit consent + extended_time persists cleanly
        good_post = self.client.post(
            policy_url,
            data={'accept_policy': 'on', 'extended_time': 'on', 'scope': 'STAGE_1'},
            HTTP_HOST='127.0.0.1',
        )
        self.assertEqual(good_post.status_code, 302)
        pref.refresh_from_db()
        self.assertTrue(pref.extended_time_enabled)
        self.assertEqual(pref.last_consent_version, self.se_blueprint.version)
        self.assertIsNotNone(pref.last_consented_at)

        # Subsequent GET keeps extendedTimeCheck pre-checked while acceptPolicyCheck stays unchecked (explicit)
        follow = self.client.get(good_post.url, HTTP_HOST='127.0.0.1')
        self.assertEqual(follow.status_code, 200)
        body = follow.content.decode('utf-8')
        self.assertIn('Preferences and policy consent saved', body)
        self.assertIn('+25% Extended Time Active', body)
        self.assertIn('id="extendedTimeCheck" name="extended_time" checked', body)
        self.assertNotIn('id="acceptPolicyCheck" name="accept_policy" required checked', body)

    @override_settings(ENABLE_READINESS_ASSESSMENT=True)
    def test_practice_history_and_quota_accuracy_with_user_isolation(self):
        # Record real Practice and Certification sessions + other user's session
        AssessmentSession.objects.create(
            user=self.user,
            career_path=self.se_path,
            blueprint=self.se_blueprint,
            blueprint_version=self.se_blueprint.version,
            task_bank_version=self.se_blueprint.task_bank_version,
            task_bundle_id='se-2026-1-bundle-a',
            mode='PRACTICE',
            practice_scope='STAGE_1',
            status='EVALUATED',
            overall_score=Decimal('78.50'),
            passed=True,
        )
        AssessmentSession.objects.create(
            user=self.user,
            career_path=self.se_path,
            blueprint=self.se_blueprint,
            blueprint_version=self.se_blueprint.version,
            task_bank_version=self.se_blueprint.task_bank_version,
            task_bundle_id='se-2026-1-bundle-b',
            mode='CERTIFICATION',
            practice_scope='FULL',
            status='EXPIRED',
        )
        AssessmentSession.objects.create(
            user=self.other_user,
            career_path=self.se_path,
            blueprint=self.se_blueprint,
            blueprint_version=self.se_blueprint.version,
            task_bank_version=self.se_blueprint.task_bank_version,
            task_bundle_id='se-2026-1-bundle-c',
            mode='PRACTICE',
            practice_scope='FULL',
            status='EVALUATED',
            overall_score=Decimal('99.00'),
        )

        # Log 8 AI practice calls to test quota accuracy & exhausted message
        for _ in range(8):
            AIUsageLog.objects.create(
                user=self.user,
                operation='PRACTICE_EVAL',
                provider='groq',
                model_name='llama-3.1-8b-instant',
                success=True,
            )

        hist_resp = self.client.get(reverse('interviews:readiness_history'), HTTP_HOST='127.0.0.1')
        self.assertEqual(hist_resp.status_code, 200)
        self.assertEqual(len(hist_resp.context['practice_sessions']), 1)
        self.assertEqual(len(hist_resp.context['certification_sessions']), 1)
        self.assertFalse(hist_resp.context['quota']['allowed'])
        self.assertEqual(hist_resp.context['quota']['remaining'], 0)

        content = hist_resp.content.decode('utf-8')
        self.assertIn('78.50%', content)
        self.assertNotIn('99.00%', content)
        self.assertIn('Daily AI limit reached (8/8)', content)
        self.assertIn('Deterministic Stage 1 structural diagnosis drills remain accessible', content)

    @override_settings(ENABLE_READINESS_ASSESSMENT=True)
    def test_legacy_routes_and_dashboard_remain_functional_when_flag_on(self):
        for url in [
            reverse('dashboard'),
            reverse('interviews:reports'),
            reverse('interviews:coding_list'),
            reverse('interviews:behavioral_list'),
            reverse('interviews:mock_list'),
        ]:
            resp = self.client.get(url, HTTP_HOST='127.0.0.1')
            self.assertEqual(resp.status_code, 200, f"Route {url} failed with flag ON")
