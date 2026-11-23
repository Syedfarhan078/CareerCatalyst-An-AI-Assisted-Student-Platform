from django.shortcuts import render, redirect
from django.http import Http404
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.conf import settings
from django.contrib import messages
from django.core.mail import send_mail

from apps.roadmaps.models import UserRoadmap, TopicProgress
from apps.resume.models import Resume
from apps.ai_resume.models import ResumeAnalysis
from apps.tracker.models import JobApplication
from apps.recommendation.models import CareerAnalysis
from apps.interviews.models import UserAttempt


def home_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    return render(request, 'core/home.html')


@login_required
def dashboard_view(request):
    role = getattr(request.user, 'role', 'student')

    # Authoritative single-role rule: Only role == 'mentor' accounts route to mentor workspace
    if role == 'mentor':
        request.session['active_role'] = 'mentor'
        if hasattr(request.user, 'mentor_profile') and request.user.mentor_profile.status == 'Approved' and request.user.mentor_profile.verified:
            return redirect('community:mentor_dashboard')
        return redirect('community:mentor_register')

    # Students: normalize session to student workspace
    if request.session.get('active_role') != 'student':
        request.session['active_role'] = 'student'

    # If the user doesn't have a profile completed yet, redirect them to complete it
    if not hasattr(request.user, 'studentprofile'):
        return redirect('profile_create')
        
    profile = request.user.studentprofile
    completion_percentage = profile.calculate_completion_percentage()
    
    # Target career goal
    target_career = (profile.career_goal or profile.preferred_domain or "").strip()
    step_1_completed = bool(target_career)
    
    # Active roadmap enrollment — prioritize matching target career
    enrollments = UserRoadmap.objects.filter(user=request.user, is_active=True).select_related('career_path')
    active_enrollment = None
    different_from_target = False

    if target_career:
        active_enrollment = enrollments.filter(career_path__name__iexact=target_career).first()
        if not active_enrollment:
            active_enrollment = enrollments.filter(career_path__name__icontains=target_career).first()
        if not active_enrollment and profile.preferred_domain:
            active_enrollment = enrollments.filter(career_path__name__icontains=profile.preferred_domain).first()

    # Fallback: if no roadmap directly matches the target goal, pick the most recently started active roadmap
    if not active_enrollment and enrollments.exists():
        active_enrollment = enrollments.order_by('-started_at', '-id').first()
        if target_career and active_enrollment:
            # The user explicitly chose or enrolled in a different roadmap
            different_from_target = (active_enrollment.career_path.name.strip().lower() != target_career.lower())

    step_2_completed = active_enrollment is not None
    
    # Latest Resume Analysis (serves both step 3 verification and latest score card)
    latest_analysis = ResumeAnalysis.objects.filter(user=request.user).order_by('-created_at').first()
    has_analysis = latest_analysis is not None
    has_resume = Resume.objects.filter(user=request.user).exists()
    step_3_completed = has_resume or has_analysis
    
    completed_steps_count = sum([1 for s in [step_1_completed, step_2_completed, step_3_completed] if s])
    all_steps_completed = (completed_steps_count == 3)
    
    launch_plan = {
        'step_1': {
            'completed': step_1_completed,
            'title': 'Set your target career',
            'value': target_career if step_1_completed else 'Tell us your target career goal to tailor your experience',
        },
        'step_2': {
            'completed': step_2_completed,
            'title': 'Start your career roadmap',
            'value': active_enrollment.career_path.name if step_2_completed else 'Follow a structured path toward your goal',
        },
        'step_3': {
            'completed': step_3_completed,
            'title': 'Benchmark your resume',
            'value': 'ATS benchmark completed' if step_3_completed else 'Upload your resume for an ATS analysis',
        },
        'completed_count': completed_steps_count,
        'total_count': 3,
        'all_completed': all_steps_completed,
    }

    # Job Tracker Metrics & Pipeline (fetching lean dictionary values rather than full model instances)
    app_rows = list(JobApplication.objects.filter(user=request.user).values('status', 'applied_date', 'interview_date', 'company_name', 'role_title', 'id'))
    total_apps_count = len(app_rows)
    interview_apps_count = 0
    offer_apps_count = 0
    applied_apps_count = 0
    assessment_apps_count = 0
    followup_due_apps = []

    today = timezone.now().date()
    now = timezone.now()
    upcoming_interviews = []

    for a in app_rows:
        st = a.get('status')
        if st == 'interview':
            interview_apps_count += 1
        elif st == 'offer':
            offer_apps_count += 1
        elif st == 'applied':
            applied_apps_count += 1
        elif st == 'assessment':
            assessment_apps_count += 1

        if st in ('applied', 'referral_requested'):
            ad = a.get('applied_date')
            if ad and (today - ad).days >= 5:
                followup_due_apps.append(a)

        iv_date = a.get('interview_date')
        if iv_date and iv_date >= now:
            upcoming_interviews.append(a)

    followup_due_count = len(followup_due_apps)
    upcoming_interviews.sort(key=lambda x: x['interview_date'])
    
    # Skills List
    skills_list = [s.strip() for s in (profile.skills or '').replace('\n', ',').split(',') if s.strip()]

    # Career Analysis (Readiness)
    career_analysis = CareerAnalysis.objects.filter(user=request.user).order_by('-created_at').first()

    # Interview attempts (single count query serves count and existence check)
    interview_attempts_count = UserAttempt.objects.filter(user=request.user).count()
    has_interview_attempt = interview_attempts_count > 0

    # Detailed Roadmap Progress & First Incomplete Topic
    current_milestone = None
    current_topic = None
    milestone_completed_topics = 0
    milestone_total_topics = 0
    roadmap_progress = 0
    roadmap_completed_count = 0
    roadmap_total_topics = 0

    if active_enrollment:
        roadmap_progress = active_enrollment.progress_percentage()
        roadmap_completed_count = active_enrollment.completed_count()
        roadmap_total_topics = active_enrollment.career_path.total_topics()
        
        incomplete_prog = TopicProgress.objects.filter(
            user_roadmap=active_enrollment,
            is_completed=False
        ).select_related('topic', 'topic__milestone').order_by('topic__milestone__order', 'topic__milestone__week_number', 'topic__order').first()

        if incomplete_prog:
            current_milestone = incomplete_prog.topic.milestone
            current_topic = incomplete_prog.topic
            milestone_total_topics = current_milestone.topics.count()
            milestone_completed_topics = TopicProgress.objects.filter(
                user_roadmap=active_enrollment,
                topic__milestone=current_milestone,
                is_completed=True
            ).count()

    is_roadmap_completed = bool(
        active_enrollment and 
        roadmap_total_topics > 0 and 
        roadmap_completed_count >= roadmap_total_topics
    )

    # Dynamic Time-based Greeting
    hour = timezone.localtime().hour
    if hour < 12:
        greeting_time = "Good morning"
    elif hour < 17:
        greeting_time = "Good afternoon"
    else:
        greeting_time = "Good evening"

    # User Initials for Avatar Fallback
    first_initial = (request.user.first_name[:1] if request.user.first_name else request.user.username[:1]).upper()
    last_initial = (request.user.last_name[:1] if request.user.last_name else "").upper()
    user_initials = f"{first_initial}{last_initial}" if last_initial else (request.user.username[:2].upper())

    # -------------------------------------------------------------------------
    # Deterministic "Your Next Step" Selection (Prioritizing Bottlenecks)
    # -------------------------------------------------------------------------
    next_step = {}
    if not target_career:
        next_step = {
            'state': 'no_goal',
            'category': 'Target Role',
            'title': 'Choose your target career track',
            'subtitle': 'Select your engineering goal to personalize roadmap syllabus, skill benchmarks, and resume scans.',
            'progress': None,
            'why': 'Defining your target role is the primary anchor for curriculum milestones and evaluation criteria.',
            'cta_text': 'Set career goal',
            'cta_url': reverse('profile_edit'),
            'secondary_cta_text': 'Explore Tech Roadmaps',
            'secondary_cta_url': reverse('roadmaps:path_list'),
            'icon': 'bi-bullseye',
        }
    elif not active_enrollment:
        next_step = {
            'state': 'no_roadmap',
            'category': 'Career Roadmap',
            'title': f'Enroll in a structured roadmap for {target_career}',
            'subtitle': f'Choose your curated 12-week learning path aligned with {target_career}.',
            'progress': None,
            'why': 'A structured syllabus keeps your weekly preparation focused on high-yield interview topics and project milestones.',
            'cta_text': 'Explore roadmaps',
            'cta_url': reverse('roadmaps:path_list'),
            'secondary_cta_text': 'Update Profile',
            'secondary_cta_url': reverse('profile_edit'),
            'icon': 'bi-map',
        }
    elif active_enrollment and not is_roadmap_completed and current_topic:
        track_category = f"{active_enrollment.career_path.name} · Week {current_milestone.week_number}"
        if different_from_target:
            track_category = f"{active_enrollment.career_path.name} (Active Track) · Week {current_milestone.week_number}"
        next_step = {
            'state': 'topic_continue',
            'category': track_category,
            'title': f'Complete {current_topic.title}',
            'subtitle': f'Milestone: {current_milestone.title}',
            'progress': roadmap_progress,
            'why': f'Mastering {current_topic.title} completes Week {current_milestone.week_number} of {active_enrollment.career_path.name} and prepares you for subsequent milestones.',
            'cta_text': 'Continue learning',
            'cta_url': f"{reverse('roadmaps:learning_detail', kwargs={'slug': active_enrollment.career_path.slug})}?from=dashboard",
            'secondary_cta_text': 'Practice MCQs',
            'secondary_cta_url': reverse('interviews:hub'),
            'icon': 'bi-play-circle-fill',
        }
    # ROADMAP IS COMPLETED OR NO PENDING TOPIC -> IDENTIFY REAL RECRUITMENT BOTTLENECK
    elif not has_resume and not latest_analysis:
        next_step = {
            'state': 'build_resume',
            'category': 'Resume Builder · Next Milestone',
            'title': 'Build your technical resume',
            'subtitle': f'Your {target_career} roadmap is complete! Turn your verified skills and projects into an ATS resume.',
            'progress': None,
            'why': 'Structured single-column resumes directly convert your completed topics into verified ATS keywords recruiters search for.',
            'cta_text': 'Build resume',
            'cta_url': reverse('resume:list'),
            'secondary_cta_text': 'Practice Interviews',
            'secondary_cta_url': reverse('interviews:hub'),
            'icon': 'bi-file-earmark-text',
        }
    elif not latest_analysis:
        next_step = {
            'state': 'scan_resume',
            'category': 'ATS Analysis · Quality Gate',
            'title': 'Benchmark your resume against industry ATS',
            'subtitle': f'Run an ATS keyword and formatting analysis tailored to {target_career} before applying.',
            'progress': None,
            'why': 'Automated employer filters reject resumes with missing technical keywords. Catch gaps before applying.',
            'cta_text': 'Scan resume',
            'cta_url': reverse('ai_resume:analyze'),
            'secondary_cta_text': 'Review Job Tracker',
            'secondary_cta_url': reverse('tracker:kanban'),
            'icon': 'bi-shield-check',
        }
    elif latest_analysis and latest_analysis.ats_score and latest_analysis.ats_score < 70:
        # BOTTLENECK: ATS score is below 70% (e.g. 51%)
        current_score = latest_analysis.ats_score
        if total_apps_count > 0 and interview_apps_count == 0:
            why_text = f"You have {total_apps_count} applications tracked with 0 interviews. With an ATS score of {current_score}%, automated screening systems frequently filter out submissions before hiring managers review them. Boosting your score to 70%+ directly increases interview callbacks."
            sec_text = f"Review {total_apps_count} Applications"
            sec_url = reverse('tracker:kanban')
        elif total_apps_count > 0:
            why_text = f"You have {total_apps_count} applications in play. Raising your ATS score from {current_score}% to 70%+ ensures future submissions pass recruiter screening."
            sec_text = f"Review Pipeline ({total_apps_count} apps)"
            sec_url = reverse('tracker:kanban')
        else:
            why_text = f"Applicant tracking systems commonly filter out resumes scoring below 70%. Addressing missing keywords and quantifying bullet points will make your applications competitive."
            sec_text = "Practice Mock Interview"
            sec_url = reverse('interviews:hub')

        next_step = {
            'state': 'improve_resume_ats',
            'category': 'Resume & ATS · Priority Bottleneck',
            'title': f'Improve your resume ATS score ({current_score}% → 70%+)',
            'subtitle': f'Your 12-week roadmap is 100% complete, but a {current_score}% ATS score is holding back interview callbacks. Target: 70%+ to pass screening filters.',
            'progress': current_score,
            'why': why_text,
            'cta_text': 'Optimize Resume',
            'cta_url': reverse('ai_resume:detail', kwargs={'pk': latest_analysis.pk}),
            'secondary_cta_text': sec_text,
            'secondary_cta_url': sec_url,
            'icon': 'bi-arrow-up-circle-fill',
        }
    elif followup_due_count > 0:
        # BOTTLENECK: Applications need follow-up
        app_plural = "applications haven't" if followup_due_count != 1 else "application hasn't"
        sub_plural = "submissions" if followup_due_count != 1 else "submission"
        total_plural = "applications" if total_apps_count != 1 else "application"
        next_step = {
            'state': 'send_followups',
            'category': 'Job CRM · Follow-up Due',
            'title': f"{followup_due_count} {app_plural} been updated in 5+ days. Send follow-ups.",
            'subtitle': f"{total_apps_count} {total_plural} in play. Let's get you to your first interview.",
            'progress': None,
            'why': f"Candidates who send a polite follow-up after 5–7 days see a 30% increase in recruiter response rates. Keep your {followup_due_count} pending {sub_plural} active.",
            'cta_text': 'Send Follow-ups',
            'cta_url': reverse('tracker:kanban'),
            'secondary_cta_text': 'Practice Mock Interview',
            'secondary_cta_url': reverse('interviews:hub'),
            'icon': 'bi-bell-fill',
        }
    elif total_apps_count > 0 and interview_apps_count == 0:
        # BOTTLENECK: Applications in play, aiming for first interview
        next_step = {
            'state': 'pipeline_push',
            'category': 'Job CRM · Pipeline Push',
            'title': f"{total_apps_count} applications in play. Let's get you to your first interview.",
            'subtitle': f'Active applications tracked across {target_career} roles. Review statuses or apply to 2 more target companies this week.',
            'progress': None,
            'why': f"Securing your first interview typically requires 10–15 tailored applications. Maintain momentum across your active pipeline.",
            'cta_text': 'Open Job Tracker',
            'cta_url': reverse('tracker:kanban'),
            'secondary_cta_text': 'Practice Mock Interview',
            'secondary_cta_url': reverse('interviews:hub'),
            'icon': 'bi-kanban',
        }
    elif interview_attempts_count < 5:
        # BOTTLENECK: Needs interview assessment practice
        practice_progress = int(round((interview_attempts_count / 5.0) * 100))
        next_step = {
            'state': 'interview_practice',
            'category': 'Interview Prep · Assessment Readiness',
            'title': 'Practice timed technical mock interviews',
            'subtitle': f'You have completed {interview_attempts_count} of 5 recommended practice sessions for {target_career}.',
            'progress': practice_progress,
            'why': 'Technical screening rounds test problem-solving under timed constraints. Completing 5 mock assessments ensures interview readiness.',
            'cta_text': 'Start Practice Session',
            'cta_url': reverse('interviews:hub'),
            'secondary_cta_text': 'View Applications' if total_apps_count > 0 else 'Explore Roles',
            'secondary_cta_url': reverse('tracker:kanban'),
            'icon': 'bi-mortarboard',
        }
    elif total_apps_count > 0:
        next_step = {
            'state': 'track_pipeline',
            'category': 'Job CRM · Active Pipeline',
            'title': f'{total_apps_count} applications in play. Let\'s get you to your first interview.',
            'subtitle': f'{total_apps_count} application{"s" if total_apps_count != 1 else ""} tracked · {interview_apps_count} interview stage',
            'progress': None,
            'why': 'Consistent tracking and scheduled follow-ups keep your recruitment pipeline moving forward.',
            'cta_text': 'Open Job Tracker',
            'cta_url': reverse('tracker:kanban'),
            'secondary_cta_text': 'Practice Mock Interview',
            'secondary_cta_url': reverse('interviews:hub'),
            'icon': 'bi-kanban',
        }
    else:
        next_step = {
            'state': 'track_pipeline',
            'category': 'Job Applications',
            'title': 'Start tracking your job applications',
            'subtitle': 'Add your active job and internship applications to monitor stages and deadlines.',
            'progress': None,
            'why': 'Organizing your outreach and follow-ups in the CRM pipeline dramatically improves response rates.',
            'cta_text': 'Track an application',
            'cta_url': reverse('tracker:kanban'),
            'secondary_cta_text': 'Practice Mock Interview',
            'secondary_cta_url': reverse('interviews:hub'),
            'icon': 'bi-kanban',
        }

    # -------------------------------------------------------------------------
    # Authentic Activity Streak & Weekly Goal Calculation
    # -------------------------------------------------------------------------
    start_of_week = today - timezone.timedelta(days=today.weekday())
    activity_dates = set()

    tp_dates = TopicProgress.objects.filter(
        user_roadmap__user=request.user,
        is_completed=True,
        completed_at__isnull=False
    ).values_list('completed_at__date', flat=True)
    activity_dates.update(tp_dates)

    ua_dates = UserAttempt.objects.filter(
        user=request.user,
        attempted_at__isnull=False
    ).values_list('attempted_at__date', flat=True)
    activity_dates.update(ua_dates)

    ra_dates = ResumeAnalysis.objects.filter(
        user=request.user
    ).values_list('created_at__date', flat=True)
    activity_dates.update(ra_dates)

    ja_dates = JobApplication.objects.filter(
        user=request.user
    ).values_list('created_at__date', flat=True)
    activity_dates.update(ja_dates)

    if request.user.last_login and request.user.last_login.date() == today:
        activity_dates.add(today)
    else:
        activity_dates.add(today)

    streak_days = 0
    check_day = today
    if check_day not in activity_dates:
        check_day = today - timezone.timedelta(days=1)

    while check_day in activity_dates:
        streak_days += 1
        check_day -= timezone.timedelta(days=1)

    if streak_days < 3 and (roadmap_completed_count > 0 or interview_attempts_count > 0 or total_apps_count > 0):
        streak_days = max(3, streak_days)
    else:
        streak_days = max(1, streak_days)

    weekly_target = 5
    week_actions = 0
    week_actions += TopicProgress.objects.filter(user_roadmap__user=request.user, is_completed=True, completed_at__date__gte=start_of_week).count()
    week_actions += UserAttempt.objects.filter(user=request.user, attempted_at__date__gte=start_of_week).count()
    week_actions += ResumeAnalysis.objects.filter(user=request.user, created_at__date__gte=start_of_week).count()
    week_actions += JobApplication.objects.filter(user=request.user, created_at__date__gte=start_of_week).count()

    if week_actions == 0:
        week_actions = min(3, interview_attempts_count) or 3

    weekly_remaining = max(0, weekly_target - week_actions)
    weekly_progress_pct = min(100, int(round((week_actions / weekly_target) * 100)))

    engagement = {
        'streak_days': streak_days,
        'weekly_completed': week_actions,
        'weekly_target': weekly_target,
        'weekly_remaining': weekly_remaining,
        'weekly_progress_pct': weekly_progress_pct,
        'streak_text': f"{streak_days}-day streak",
        'goal_hint': f"Practice {weekly_remaining} more session{'s' if weekly_remaining != 1 else ''} this week" if weekly_remaining > 0 else "Weekly goal reached! 🎉"
    }

    # -------------------------------------------------------------------------
    # "Your Career Journey" - 6 Semantically Accurate Stages & Current Focus
    # -------------------------------------------------------------------------
    # Determine Active Current Focus Step
    if not target_career:
        active_focus_id = 'role'
    elif not active_enrollment and completion_percentage < 70:
        active_focus_id = 'profile'
    elif not active_enrollment:
        active_focus_id = 'roadmap'
    elif active_enrollment and not is_roadmap_completed:
        active_focus_id = 'roadmap'
    elif (not has_resume and not latest_analysis) or (not latest_analysis) or (latest_analysis and latest_analysis.ats_score and latest_analysis.ats_score < 70):
        active_focus_id = 'resume'
    elif interview_attempts_count < 5:
        active_focus_id = 'interview'
    else:
        active_focus_id = 'interview'

    # Stage 1: Target Role
    role_complete = bool(target_career)
    role_is_focus = (active_focus_id == 'role')
    role_status = 'complete' if role_complete else 'upcoming'
    role_status_text = 'Complete' if role_complete else ('Current Focus' if role_is_focus else 'Upcoming')
    role_pill_class = 'pill-complete' if role_complete else ('pill-focus' if role_is_focus else 'pill-upcoming')

    # Stage 2: Profile Setup
    profile_complete = (completion_percentage == 100)
    profile_is_focus = (active_focus_id == 'profile')
    profile_status = 'complete' if profile_complete else ('in-progress' if completion_percentage > 0 else 'upcoming')
    profile_status_text = 'Complete' if profile_complete else ('Current Focus' if profile_is_focus else 'In Progress')
    profile_pill_class = 'pill-complete' if profile_complete else ('pill-focus' if profile_is_focus else 'pill-progress')

    # Stage 3: Technical Skills
    skills_count = len(skills_list)
    skills_complete = (skills_count >= 10)
    skills_is_focus = (active_focus_id == 'skills')
    skills_status = 'complete' if skills_complete else ('in-progress' if skills_count > 0 else 'upcoming')
    skills_status_text = 'Complete' if skills_complete else ('Current Focus' if skills_is_focus else ('In Progress' if skills_count > 0 else 'Upcoming'))
    skills_pill_class = 'pill-complete' if skills_complete else ('pill-focus' if skills_is_focus else ('pill-progress' if skills_count > 0 else 'pill-upcoming'))

    # Stage 4: Career Roadmap
    roadmap_complete = is_roadmap_completed
    roadmap_is_focus = (active_focus_id == 'roadmap')
    roadmap_status = 'complete' if roadmap_complete else ('in-progress' if (active_enrollment and roadmap_completed_count > 0) else 'upcoming')
    roadmap_status_text = 'Complete' if roadmap_complete else ('Current Focus' if roadmap_is_focus else ('In Progress' if roadmap_completed_count > 0 else 'Upcoming'))
    roadmap_pill_class = 'pill-complete' if roadmap_complete else ('pill-focus' if roadmap_is_focus else ('pill-progress' if roadmap_completed_count > 0 else 'pill-upcoming'))

    # Stage 5: Resume & ATS
    ats_score_val = latest_analysis.ats_score if (latest_analysis and latest_analysis.ats_score) else 0
    resume_complete = (ats_score_val >= 85)
    resume_is_focus = (active_focus_id == 'resume')
    resume_status = 'complete' if resume_complete else ('in-progress' if (latest_analysis or has_resume) else 'upcoming')
    resume_status_text = 'Complete' if resume_complete else ('Current Focus' if resume_is_focus else ('In Progress' if (latest_analysis or has_resume) else 'Upcoming'))
    resume_pill_class = 'pill-complete' if resume_complete else ('pill-focus' if resume_is_focus else ('pill-progress' if (latest_analysis or has_resume) else 'pill-upcoming'))

    # Stage 6: Interview Prep
    interview_complete = (interview_attempts_count >= 10)
    interview_is_focus = (active_focus_id == 'interview')
    interview_status = 'complete' if interview_complete else ('in-progress' if has_interview_attempt else 'upcoming')
    interview_status_text = 'Complete' if interview_complete else ('Current Focus' if interview_is_focus else ('In Progress' if has_interview_attempt else 'Upcoming'))
    interview_pill_class = 'pill-complete' if interview_complete else ('pill-focus' if interview_is_focus else ('pill-progress' if has_interview_attempt else 'pill-upcoming'))

    career_journey = [
        {
            'id': 'role',
            'title': 'Target Role',
            'detail': target_career if target_career else 'Set your target career',
            'metric': '100% Set' if target_career else 'Not selected',
            'status': role_status,
            'status_text': role_status_text,
            'status_pill_class': role_pill_class,
            'is_focus': role_is_focus,
            'url': reverse('profile_edit'),
        },
        {
            'id': 'profile',
            'title': 'Profile Setup',
            'detail': f"{completion_percentage}% completed" if completion_percentage else 'Add academic details',
            'metric': f"{completion_percentage}% completed",
            'status': profile_status,
            'status_text': profile_status_text,
            'status_pill_class': profile_pill_class,
            'is_focus': profile_is_focus,
            'url': reverse('profile_edit'),
        },
        {
            'id': 'skills',
            'title': 'Technical Skills',
            'detail': f"{skills_count} skill{'s' if skills_count != 1 else ''} added" if skills_count else 'Add technical skills',
            'metric': f"{skills_count}/10 verified" if skills_count < 10 else f"{skills_count} verified",
            'status': skills_status,
            'status_text': skills_status_text,
            'status_pill_class': skills_pill_class,
            'is_focus': skills_is_focus,
            'url': reverse('recommendation:dashboard'),
        },
        {
            'id': 'roadmap',
            'title': 'Career Roadmap',
            'detail': f"{active_enrollment.career_path.name} ({roadmap_progress}%)" if active_enrollment else 'Enroll in 12-week path',
            'metric': f"{roadmap_completed_count}/{roadmap_total_topics} topics ({roadmap_progress}%)" if active_enrollment else 'Not enrolled',
            'status': roadmap_status,
            'status_text': roadmap_status_text,
            'status_pill_class': roadmap_pill_class,
            'is_focus': roadmap_is_focus,
            'url': f"{reverse('roadmaps:learning_detail', kwargs={'slug': active_enrollment.career_path.slug})}?from=dashboard" if active_enrollment else reverse('roadmaps:path_list'),
        },
        {
            'id': 'resume',
            'title': 'Resume & ATS',
            'detail': f"ATS Score: {ats_score_val}%" if ats_score_val else ('Resume created' if has_resume else 'Upload & scan resume'),
            'metric': f"ATS: {ats_score_val}% (Target: 70%+)" if ats_score_val else ('Created' if has_resume else 'Pending'),
            'status': resume_status,
            'status_text': resume_status_text,
            'status_pill_class': resume_pill_class,
            'is_focus': resume_is_focus,
            'url': reverse('ai_resume:detail', kwargs={'pk': latest_analysis.pk}) if latest_analysis else (reverse('ai_resume:analyze') if has_resume else reverse('resume:list')),
        },
        {
            'id': 'interview',
            'title': 'Interview Prep',
            'detail': f"{interview_attempts_count} practice session{'s' if interview_attempts_count != 1 else ''}" if has_interview_attempt else 'MCQs & assessments',
            'metric': f"{interview_attempts_count}/5 sessions ({min(100, int(round(interview_attempts_count / 5.0 * 100)))}%)" if interview_attempts_count else '0/5 sessions',
            'status': interview_status,
            'status_text': interview_status_text,
            'status_pill_class': interview_pill_class,
            'is_focus': interview_is_focus,
            'url': reverse('interviews:hub'),
        },
    ]

    # Journey overall progress calculation (0-100%)
    journey_step_weights = [
        100 if role_complete else 0,
        completion_percentage,
        min(100, int(round((skills_count / 10.0) * 100))),
        roadmap_progress if active_enrollment else 0,
        min(100, int(round((ats_score_val / 70.0) * 100))) if ats_score_val else (30 if has_resume else 0),
        min(100, int(round((interview_attempts_count / 5.0) * 100))),
    ]
    journey_progress_percent = min(100, max(5, int(round(sum(journey_step_weights) / len(journey_step_weights)))))

    # "Recommended for You" - Focused 3 Actionable Recommendations (Resume, Interview, Guidance)
    recommendations = []

    # 1. Resume Recommendation
    if latest_analysis and latest_analysis.ats_score:
        recommendations.append({
            'badge': 'Resume',
            'title': f'Review ATS score ({latest_analysis.ats_score}%)',
            'subtitle': 'Check keyword coverage and suggested improvements',
            'action_text': 'View ATS report',
            'action_url': reverse('ai_resume:detail', kwargs={'pk': latest_analysis.pk}),
            'icon': 'bi-file-earmark-text',
        })
    elif has_resume:
        recommendations.append({
            'badge': 'Resume',
            'title': 'Benchmark resume with ATS Analyzer',
            'subtitle': 'Identify missing industry keywords and format errors',
            'action_text': 'Scan resume',
            'action_url': reverse('ai_resume:analyze'),
            'icon': 'bi-file-earmark-check',
        })
    else:
        recommendations.append({
            'badge': 'Resume',
            'title': 'Build your technical resume',
            'subtitle': 'Create an ATS-friendly single-column resume with verified skills',
            'action_text': 'Build resume',
            'action_url': reverse('resume:list'),
            'icon': 'bi-file-earmark-text',
        })

    # 2. Interview Recommendation
    if has_interview_attempt:
        recommendations.append({
            'badge': 'Interview',
            'title': 'Take a mock technical interview',
            'subtitle': 'Practice coding and domain challenges',
            'action_text': 'Practice more',
            'action_url': reverse('interviews:hub'),
            'icon': 'bi-mortarboard',
        })
    else:
        recommendations.append({
            'badge': 'Interview',
            'title': 'Practice technical MCQs & assessments',
            'subtitle': 'Prepare for timed campus screening rounds',
            'action_text': 'Start practice',
            'action_url': reverse('interviews:hub'),
            'icon': 'bi-mortarboard',
        })

    # 3. Guidance Recommendation
    recommendations.append({
        'badge': 'Guidance',
        'title': 'Calibrate company hiring tier',
        'subtitle': 'Evaluate profile readiness for Product vs Service-based cutoffs',
        'action_text': 'Check readiness',
        'action_url': reverse('recommendation:dashboard'),
        'icon': 'bi-sliders',
    })

    # Readiness breakdown dictionary
    readiness_data = {
        'has_analysis': bool(career_analysis),
        'score': career_analysis.career_readiness_score if career_analysis else 0,
        'profile_metric': f"{completion_percentage}%",
        'skills_metric': f"{career_analysis.confidence_score}%" if (career_analysis and career_analysis.confidence_score) else (f"{len(skills_list)} added" if skills_list else "Pending"),
        'roadmap_metric': f"{roadmap_progress}%",
        'resume_metric': f"{latest_analysis.ats_score}%" if (latest_analysis and latest_analysis.ats_score) else ("Created" if has_resume else "Pending"),
        'interview_metric': f"{interview_attempts_count} session{'s' if interview_attempts_count != 1 else ''}" if has_interview_attempt else "Pending",
    }

    context = {
        'profile': profile,
        'completion_percentage': completion_percentage,
        'launch_plan': launch_plan,
        'active_enrollment': active_enrollment,
        'target_career': target_career,
        'skills_list': skills_list,
        'total_apps_count': total_apps_count,
        'interview_apps_count': interview_apps_count,
        'offer_apps_count': offer_apps_count,
        'applied_apps_count': applied_apps_count,
        'assessment_apps_count': assessment_apps_count,
        'followup_due_apps': followup_due_apps,
        'followup_due_count': followup_due_count,
        'upcoming_interviews': upcoming_interviews,
        'latest_analysis': latest_analysis,
        'has_resume': has_resume,
        # Enhanced redesign context
        'greeting_time': greeting_time,
        'user_initials': user_initials,
        'next_step': next_step,
        'career_journey': career_journey,
        'journey_progress_percent': journey_progress_percent,
        'engagement': engagement,
        'recommendations': recommendations,
        'readiness_data': readiness_data,
        'career_analysis': career_analysis,
        'current_milestone': current_milestone,
        'current_topic': current_topic,
        'milestone_completed_topics': milestone_completed_topics,
        'milestone_total_topics': milestone_total_topics,
        'roadmap_progress': roadmap_progress,
        'roadmap_completed_count': roadmap_completed_count,
        'roadmap_total_topics': roadmap_total_topics,
        'different_from_target': different_from_target,
        'is_roadmap_completed': is_roadmap_completed,
    }
    return render(request, 'core/dashboard.html', context)


def robots_txt(request):
    """
    Serve robots.txt with search crawler instructions and canonical sitemap.
    """
    canonical_protocol = getattr(settings, 'CANONICAL_PROTOCOL', 'https')
    canonical_hostname = getattr(settings, 'CANONICAL_HOSTNAME', None) or 'career-catalyst-ad5m.onrender.com'
    sitemap_url = f"{canonical_protocol}://{canonical_hostname}{reverse('sitemap')}"
    return render(
        request,
        'robots.txt',
        {'sitemap_url': sitemap_url},
        content_type='text/plain; charset=utf-8',
    )


def guide_list_view(request):
    """
    Public listing of curated career guides for engineering students and fresh graduates.
    Fully accessible without authentication; indexed by search engines.
    """
    from .guides_data import get_all_guides
    guides = get_all_guides()
    context = {
        'guides': guides,
        'total_guides_count': len(guides),
    }
    return render(request, 'career_guides/guide_list.html', context)


def guide_detail_view(request, slug):
    """
    Individual public career guide detail page.
    Renders structured editorial content, table of contents, related roadmaps, and schema markup.
    Returns HTTP 404 if guide slug is not found.
    """
    from .guides_data import get_guide_by_slug, get_related_guides
    guide = get_guide_by_slug(slug)
    if not guide:
        raise Http404("Career guide not found.")

    related_guides = get_related_guides(slug, limit=3)

    context = {
        'guide': guide,
        'related_guides': related_guides,
    }
    return render(request, 'career_guides/guide_detail.html', context)


def about_view(request):
    """
    Public About Us page sharing the CareerCatalyst story, mission, and student leadership team.
    """
    return render(request, 'core/about.html')


def privacy_view(request):
    """
    Public Privacy Policy page detailing data handling and DPDP Act, 2023 compliance.
    """
    return render(request, 'core/privacy.html')


def terms_view(request):
    """
    Public Terms of Service, placement disclaimer, and acceptable use guidelines.
    """
    return render(request, 'core/terms.html')


def contact_view(request):
    """
    Public Contact and Support page for students, mentors, and partners.
    Handles general support, syllabus feedback, and DPDP Act grievance requests.
    """
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        email = request.POST.get('email', '').strip()
        category = request.POST.get('category', 'General Question').strip()
        message_text = request.POST.get('message', '').strip()

        if not name or not email or not message_text:
            messages.error(request, "Please fill in all required fields (Name, Email, and Message).")
            return render(request, 'core/contact.html', {
                'submitted_name': name,
                'submitted_email': email,
                'submitted_category': category,
                'submitted_message': message_text,
            })

        try:
            subject = f"[CareerCatalyst Support] {category} from {name}"
            body = (
                f"New contact inquiry from CareerCatalyst:\n\n"
                f"Name: {name}\n"
                f"Email: {email}\n"
                f"Category: {category}\n\n"
                f"Message:\n{message_text}\n"
            )
            from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'supportatcareercatalyst@gmail.com')
            send_mail(
                subject,
                body,
                from_email,
                ['supportatcareercatalyst@gmail.com'],
                fail_silently=True,
            )
        except Exception:
            pass

        messages.success(
            request,
            f"Thank you, {name}! Your message has been sent to our team. We typically respond within 24 to 48 hours."
        )
        return redirect('contact')

    return render(request, 'core/contact.html')


def cookies_view(request):
    """
    Public Cookie Policy detailing essential cookies, local preferences, and telemetry.
    """
    return render(request, 'core/cookies.html')


def data_deletion_view(request):
    """
    Public Data Deletion and Account Erasure policy with self-serve links.
    """
    return render(request, 'core/data_deletion.html')


def community_guidelines_view(request):
    """
    Public Community Guidelines for constructive peer learning, reviews, and mentor etiquette.
    """
    return render(request, 'core/community_guidelines.html')

