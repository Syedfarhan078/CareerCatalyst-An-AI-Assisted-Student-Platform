import xml.etree.ElementTree as ET
from django.test import TestCase, Client, override_settings
from django.contrib.auth import get_user_model
from django.urls import reverse
from apps.profiles.models import StudentProfile
from apps.ai_resume.models import ResumeAnalysis
from apps.roadmaps.models import CareerPath, Milestone, Topic, UserRoadmap, TopicProgress

User = get_user_model()


class DashboardRefinementTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='testuser@example.com',
            password='password123'
        )
        self.profile = StudentProfile.objects.create(
            user=self.user,
            career_goal='Software Engineer',
            skills='Python, Django, PostgreSQL, Docker',
            college='Test University'
        )
        self.client = Client(HTTP_HOST='127.0.0.1')
        self.client.force_login(self.user)

    def test_dashboard_renders_successfully(self):
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)

    def test_dashboard_job_application_metrics_optimization(self):
        from apps.tracker.models import JobApplication
        from django.utils import timezone
        from datetime import timedelta

        # Create a few job applications across statuses
        JobApplication.objects.create(user=self.user, role_title='Backend Dev', company_name='Acme', status='interview', interview_date=timezone.now() + timedelta(days=2))
        JobApplication.objects.create(user=self.user, role_title='Fullstack Dev', company_name='Beta Corp', status='offer')
        JobApplication.objects.create(user=self.user, role_title='Frontend Dev', company_name='Gamma LLC', status='applied', applied_date=timezone.now().date() - timedelta(days=7))

        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_apps_count'], 3)
        self.assertEqual(response.context['interview_apps_count'], 1)
        self.assertEqual(response.context['offer_apps_count'], 1)
        self.assertEqual(response.context['followup_due_count'], 1)
        self.assertEqual(len(response.context['upcoming_interviews']), 1)

    def test_ats_report_recommendation_links_to_analysis_detail(self):
        analysis = ResumeAnalysis.objects.create(
            user=self.user,
            target_role='Software Engineer',
            ats_score=71,
            overall_score=71,
            skill_coverage_score=80,
            keyword_coverage_score=70,
            achievement_score=65,
            completeness_score=70
        )
        
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        
        recommendations = response.context['recommendations']
        # Exactly 3 recommendations: Resume, Interview, Guidance
        self.assertEqual(len(recommendations), 3)
        self.assertEqual([r['badge'] for r in recommendations], ['Resume', 'Interview', 'Guidance'])

        resume_rec = recommendations[0]
        expected_url = reverse('ai_resume:detail', kwargs={'pk': analysis.pk})
        self.assertEqual(resume_rec['action_url'], expected_url)
        self.assertEqual(resume_rec['action_text'], 'View ATS report')
        
        detail_response = self.client.get(resume_rec['action_url'])
        self.assertEqual(detail_response.status_code, 200)

    def test_career_journey_statuses_are_truthful(self):
        # User has 4 skills, profile not 100%, ATS 71%
        analysis = ResumeAnalysis.objects.create(
            user=self.user,
            target_role='Software Engineer',
            ats_score=71,
            overall_score=71,
            skill_coverage_score=80,
            keyword_coverage_score=70,
            achievement_score=65,
            completeness_score=70
        )
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        
        stages = {s['id']: s for s in response.context['career_journey']}
        
        # Target role is complete because user has set it
        self.assertEqual(stages['role']['status'], 'complete')
        
        # Profile is not 100% -> MUST NOT be marked complete
        self.assertEqual(stages['profile']['status'], 'in-progress')
        
        # Skills exist (4 skills) -> MUST NOT be falsely marked complete (must be in-progress)
        self.assertEqual(stages['skills']['status'], 'in-progress')
        
        # Resume ATS is 71% (<85%) -> MUST NOT be marked complete (in-progress)
        self.assertEqual(stages['resume']['status'], 'in-progress')

        # Verify all stage links resolve with 200 or 302
        for stage in stages.values():
            res = self.client.get(stage['url'])
            self.assertIn(res.status_code, [200, 302], f"Stage '{stage['id']}' URL {stage['url']} returned {res.status_code}")

    def test_roadmap_completion_logic_and_cta(self):
        # Create a career path with 2 topics
        path = CareerPath.objects.create(name='Software Engineer', slug='software-engineer')
        milestone = Milestone.objects.create(career_path=path, title='Core Basics', week_number=1, order=1)
        topic1 = Topic.objects.create(milestone=milestone, title='Variables', order=1)
        topic2 = Topic.objects.create(milestone=milestone, title='Functions', order=2)
        
        user_roadmap = UserRoadmap.objects.create(user=self.user, career_path=path, is_active=True)
        prog1 = TopicProgress.objects.create(user_roadmap=user_roadmap, topic=topic1, is_completed=True)
        prog2 = TopicProgress.objects.create(user_roadmap=user_roadmap, topic=topic2, is_completed=False)
        
        # Case 1: In progress (1 of 2 completed)
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['is_roadmap_completed'])
        self.assertContains(response, 'Continue roadmap')
        self.assertNotContains(response, 'Review roadmap')

        # Case 2: 100% Complete (both topics completed)
        prog2.is_completed = True
        prog2.save()
        
        response_completed = self.client.get(reverse('dashboard'))
        self.assertEqual(response_completed.status_code, 200)
        self.assertTrue(response_completed.context['is_roadmap_completed'])
        self.assertContains(response_completed, 'Review roadmap')
        self.assertContains(response_completed, 'Roadmap completed')
        self.assertContains(response_completed, "You've completed all 2 topics in this track.")

    def test_next_step_prioritizes_low_ats_bottleneck_when_roadmap_completed(self):
        from apps.tracker.models import JobApplication

        # 1. Complete roadmap
        path = CareerPath.objects.create(name='Software Engineer', slug='swe-track')
        milestone = Milestone.objects.create(career_path=path, title='Milestone 1', week_number=1, order=1)
        t1 = Topic.objects.create(milestone=milestone, title='Topic 1', order=1)
        ur = UserRoadmap.objects.create(user=self.user, career_path=path, is_active=True)
        TopicProgress.objects.create(user_roadmap=ur, topic=t1, is_completed=True)

        # 2. 7 Applications, 0 interviews
        for i in range(7):
            JobApplication.objects.create(user=self.user, company_name=f'Company {i}', role_title='SWE', status='applied')

        # 3. Resume analysis with 51% score
        ResumeAnalysis.objects.create(
            user=self.user,
            target_role='Software Engineer',
            ats_score=51,
            overall_score=51
        )

        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)

        next_step = response.context['next_step']
        self.assertEqual(next_step['state'], 'improve_resume_ats')
        self.assertIn('51%', next_step['title'])
        self.assertIn('70%+', next_step['title'])
        self.assertEqual(next_step['secondary_cta_text'], 'Review 7 Applications')
        self.assertIn('7 applications', next_step['why'])
        self.assertIn('51%', next_step['why'])

        # Journey Step 5 (Resume & ATS) is highlighted as the active focus
        stages = {s['id']: s for s in response.context['career_journey']}
        self.assertTrue(stages['resume']['is_focus'])
        self.assertEqual(stages['resume']['status_pill_class'], 'pill-focus')

        # Engagement metrics exist
        engagement = response.context['engagement']
        self.assertGreaterEqual(engagement['streak_days'], 1)
        self.assertEqual(engagement['weekly_target'], 5)
        self.assertGreaterEqual(response.context['journey_progress_percent'], 50)

    def test_next_step_followups_due_when_ats_healthy(self):
        from apps.tracker.models import JobApplication
        from datetime import timedelta
        from django.utils import timezone

        # Completed roadmap & 75% ATS
        path = CareerPath.objects.create(name='Software Engineer', slug='swe-followups')
        milestone = Milestone.objects.create(career_path=path, title='Milestone 1', week_number=1, order=1)
        t1 = Topic.objects.create(milestone=milestone, title='Topic 1', order=1)
        ur = UserRoadmap.objects.create(user=self.user, career_path=path, is_active=True)
        TopicProgress.objects.create(user_roadmap=ur, topic=t1, is_completed=True)

        ResumeAnalysis.objects.create(
            user=self.user,
            target_role='Software Engineer',
            ats_score=75,
            overall_score=75
        )

        # 3 stale applications (applied 6 days ago)
        for i in range(3):
            JobApplication.objects.create(
                user=self.user,
                company_name=f'Corp {i}',
                role_title='SWE',
                status='applied',
                applied_date=timezone.now().date() - timedelta(days=6)
            )

        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)

        next_step = response.context['next_step']
        self.assertEqual(next_step['state'], 'send_followups')
        self.assertIn("haven't been updated in 5+ days", next_step['title'])
        self.assertIn("in play. Let's get you to your first interview.", next_step['subtitle'])

    def test_no_fake_percentages_in_next_step(self):
        # When user has completed roadmap or has no active topic progress
        response = self.client.get(reverse('dashboard'))
        next_step = response.context['next_step']
        # Progress must either be a real percentage (active topic) or None
        if next_step['state'] != 'topic_continue':
            self.assertIsNone(next_step['progress'], f"Fake progress found in state: {next_step['state']}")


class SEOCrawlerEndpointTests(TestCase):
    def setUp(self):
        self.client = Client(HTTP_HOST='127.0.0.1')

    def test_robots_txt_status_and_content_type(self):
        response = self.client.get('/robots.txt')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response['Content-Type'].startswith('text/plain'))

    def test_robots_txt_rules_allow_and_disallow(self):
        response = self.client.get('/robots.txt')
        content = response.content.decode('utf-8')

        # Verify user-agent and home page allowance
        self.assertIn('User-agent: *', content)
        self.assertIn('Allow: /$', content)

        # Verify disallow rules for authenticated/private areas
        private_prefixes = [
            '/admin-portal/',
            '/accounts/',
            '/dashboard/',
            '/profile/',
            '/resume/',
            '/ai-resume/',
            '/roadmaps/',
            '/interviews/',
            '/community/',
            '/recommendation/',
            '/tracker/',
            '/verify/',
            '/media/',
        ]
        for prefix in private_prefixes:
            self.assertIn(f'Disallow: {prefix}', content, f"robots.txt missing Disallow for {prefix}")

        # Verify sitemap directive presence
        self.assertIn('Sitemap: https://career-catalyst-ad5m.onrender.com/sitemap.xml', content)

    def test_robots_txt_canonical_host_configuration(self):
        with override_settings(CANONICAL_HOSTNAME='staging.careercatalyst.com', CANONICAL_PROTOCOL='https'):
            response = self.client.get('/robots.txt')
            content = response.content.decode('utf-8')
            self.assertIn('Sitemap: https://staging.careercatalyst.com/sitemap.xml', content)

    def test_sitemap_xml_status_and_content_type(self):
        response = self.client.get('/sitemap.xml')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response['Content-Type'].startswith('application/xml'))

    def test_sitemap_xml_validity_and_canonical_urls(self):
        response = self.client.get('/sitemap.xml')
        self.assertEqual(response.status_code, 200)

        # Parse XML to verify well-formed syntax
        root = ET.fromstring(response.content)
        self.assertEqual(root.tag, '{http://www.sitemaps.org/schemas/sitemap/0.9}urlset')

        # Extract all <loc> values
        urls = [loc.text for loc in root.findall('{http://www.sitemaps.org/schemas/sitemap/0.9}url/{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]

        # The homepage should be present as canonical
        self.assertIn('https://career-catalyst-ad5m.onrender.com/', urls)

    def test_sitemap_xml_excludes_private_and_user_specific_pages(self):
        response = self.client.get('/sitemap.xml')
        content = response.content.decode('utf-8')

        private_prefixes = [
            '/dashboard/',
            '/admin-portal/',
            '/accounts/',
            '/profile/',
            '/resume/',
            '/ai-resume/',
            '/roadmaps/my/',
            '/interviews/',
            '/community/',
            '/recommendation/',
            '/tracker/',
            '/verify/',
            '/media/',
        ]
        for prefix in private_prefixes:
            self.assertNotIn(prefix, content, f"Sitemap XML must not contain private prefix: {prefix}")

    def test_sitemap_xml_canonical_host_configuration(self):
        with override_settings(CANONICAL_HOSTNAME='custom.domain.example', CANONICAL_PROTOCOL='https'):
            response = self.client.get('/sitemap.xml')
            root = ET.fromstring(response.content)
            urls = [loc.text for loc in root.findall('{http://www.sitemaps.org/schemas/sitemap/0.9}url/{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]
            self.assertIn('https://custom.domain.example/', urls)

    def test_private_areas_excluded_by_robots_txt_redirect_anonymous_users(self):
        # Confirm that areas excluded in robots.txt require authentication
        private_urls = [
            '/dashboard/',
            '/profile/',
            '/resume/',
            '/roadmaps/my/',
            '/interviews/',
            '/community/',
            '/recommendation/',
            '/tracker/',
        ]
        for url in private_urls:
            res = self.client.get(url)
            self.assertEqual(res.status_code, 302, f"Expected 302 redirect for unauthenticated access to {url}")
            self.assertIn('/accounts/login/', res.url)


class ComplianceAndTrustPagesTests(TestCase):
    def setUp(self):
        self.client = Client(HTTP_HOST='127.0.0.1')

    def test_about_us_page(self):
        response = self.client.get(reverse('about_us'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        # Check team members and college
        self.assertIn("Syed Farhan Ahmed", content)
        self.assertIn("Shaik Sandhani", content)
        self.assertIn("Mahesh Ankalagi", content)
        self.assertIn("Abdul Kalam", content)
        self.assertIn("Atria Institute of Technology", content)

    def test_privacy_policy_page(self):
        response = self.client.get(reverse('privacy_policy'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn("Digital Personal Data Protection", content)
        self.assertIn("DPDP Act, 2023", content)
        self.assertIn("Grievance Redressal Officer", content)
        self.assertIn("Render", content)
        self.assertIn("Cloudinary", content)

    def test_terms_of_service_page(self):
        response = self.client.get(reverse('terms_of_service'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn("Terms of Service", content)
        self.assertIn("Job &amp; Placement Disclaimer", content)
        self.assertIn("ATS Resume Diagnostic Disclaimer", content)
        self.assertIn("Intellectual Property", content)

    def test_cookie_policy_page(self):
        response = self.client.get(reverse('cookie_policy'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn("Cookie Policy", content)
        self.assertIn("Essential Cookies", content)
        self.assertIn("Zero Third-Party Advertising", content)

    def test_data_deletion_page(self):
        response = self.client.get(reverse('data_deletion'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn("Data Deletion", content)
        self.assertIn(reverse('delete_account'), content)
        self.assertIn("What Data Is Permanently Purged?", content)

    def test_community_guidelines_page(self):
        response = self.client.get(reverse('community_guidelines'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn("Community Guidelines", content)
        self.assertIn("Discussion &amp; Code Review Standards", content)
        self.assertIn("Academic Integrity", content)
        self.assertIn("community@careercatalyst.in", content)

    def test_contact_page_get(self):
        response = self.client.get(reverse('contact'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn("devfarhan7890@gmail.com", content)
        self.assertIn("supportatcareercatalyst@gmail.com", content)
        self.assertIn("Send Us a Message", content)
        self.assertIn("24 to 48 business hours", content)

    def test_contact_page_post_success(self):
        response = self.client.post(reverse('contact'), {
            'name': 'Priya Patel',
            'email': 'priya@example.com',
            'category': 'Roadmaps & Syllabus',
            'message': 'Can you add more system design resources to the backend roadmap?',
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn("Thank you, Priya Patel!", content)

    def test_contact_page_post_validation_failure(self):
        response = self.client.post(reverse('contact'), {
            'name': '',
            'email': 'invalid',
            'category': 'General Question',
            'message': '',
        })
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn("Please fill in all required fields", content)

    def test_sitemap_includes_all_compliance_pages(self):
        response = self.client.get('/sitemap.xml')
        self.assertEqual(response.status_code, 200)
        root = ET.fromstring(response.content)
        urls = [loc.text for loc in root.findall('{http://www.sitemaps.org/schemas/sitemap/0.9}url/{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]

        expected_pages = [
            'https://career-catalyst-ad5m.onrender.com/about/',
            'https://career-catalyst-ad5m.onrender.com/privacy/',
            'https://career-catalyst-ad5m.onrender.com/terms/',
            'https://career-catalyst-ad5m.onrender.com/contact/',
            'https://career-catalyst-ad5m.onrender.com/cookies/',
            'https://career-catalyst-ad5m.onrender.com/data-deletion/',
            'https://career-catalyst-ad5m.onrender.com/community-guidelines/',
        ]
        for page in expected_pages:
            self.assertIn(page, urls, f"Missing public page from sitemap: {page}")

