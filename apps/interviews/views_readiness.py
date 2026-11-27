"""
Sprint 2 Views for the AI-Era Career Readiness Assessment Hub Shell.

All routes are strictly gated behind settings.ENABLE_READINESS_ASSESSMENT (default False)
and scoped solely to the Software Engineer pilot role. Does not activate Stage 1-3
workbenches, scoring execution, or Certification Mode.
"""

from functools import wraps
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Avg
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.generic import TemplateView

from apps.roadmaps.models import CareerPath, RoadmapCertificate, Topic, UserRoadmap
from .models import (
    AssessmentSession,
    ReadinessUserPreference,
    RoleAssessmentBlueprint,
    UserAttempt,
)
from .services.assessment_session import AssessmentSessionService
from .services.blueprint_loader import PILOT_ACTIVE_ROLE_SLUGS


# Authoritative display metadata for all 15 database CareerPath roles
ROLE_ARCHETYPE_META = {
    'software-engineer': {
        'archetype': 'ENGINEERING',
        'archetype_label': 'Engineering & Code Review',
        'focus': 'AI-code defect review, architecture pivots, testing rigor, and adaptive technical defense.',
    },
    'backend-developer': {
        'archetype': 'ENGINEERING',
        'archetype_label': 'Engineering & Code Review',
        'focus': 'API security, concurrency/ORM integrity, async queues, and database performance.',
    },
    'frontend-developer': {
        'archetype': 'ENGINEERING',
        'archetype_label': 'Engineering & Code Review',
        'focus': 'React/DOM async bugs, WCAG accessibility, state management, and Core Web Vitals.',
    },
    'full-stack-developer': {
        'archetype': 'ENGINEERING',
        'archetype_label': 'Engineering & Code Review',
        'focus': 'End-to-end API contracts, CORS/auth boundaries, and zero-downtime migrations.',
    },
    'mobile-app-developer': {
        'archetype': 'ENGINEERING',
        'archetype_label': 'Engineering & Code Review',
        'focus': 'Flutter lifecycle state, offline sync, frame budgets, and platform security.',
    },
    'blockchain-developer': {
        'archetype': 'ENGINEERING',
        'archetype_label': 'Engineering & Code Review',
        'focus': 'Smart contract security audits, reentrancy/EVM invariants, and gas optimization.',
    },
    'data-scientist': {
        'archetype': 'DATA_ML',
        'archetype_label': 'Data, ML & AI Lab',
        'focus': 'Statistical rigor, data leakage detection, SQL analysis, and A/B test interpretation.',
    },
    'ml-engineer': {
        'archetype': 'DATA_ML',
        'archetype_label': 'Data, ML & AI Lab',
        'focus': 'Production model serving, MLOps reproducibility, quantization, and drift monitoring.',
    },
    'data-engineer': {
        'archetype': 'DATA_ML',
        'archetype_label': 'Data, ML & AI Lab',
        'focus': 'Idempotent ETL pipelines, PySpark skew optimization, and warehouse modeling.',
    },
    'ai-research-scientist': {
        'archetype': 'DATA_ML',
        'archetype_label': 'Data, ML & AI Lab',
        'focus': 'Attention/optimization theory, experimental rigor, and LLM hallucination detection.',
    },
    'cloud-architect': {
        'archetype': 'CLOUD_SEC',
        'archetype_label': 'Cloud & Security War Room',
        'focus': 'Multi-AZ VPC topology, IAM least privilege, IaC review, and FinOps trade-offs.',
    },
    'devops-engineer': {
        'archetype': 'CLOUD_SEC',
        'archetype_label': 'Cloud & Security War Room',
        'focus': 'Kubernetes probe debugging, CI/CD supply-chain gates, and SRE outage triage.',
    },
    'cybersecurity-analyst': {
        'archetype': 'CLOUD_SEC',
        'archetype_label': 'Cloud & Security War Room',
        'focus': 'SIEM/packet forensics, OWASP exploit verification, and incident containment.',
    },
    'product-manager': {
        'archetype': 'PRODUCT_DESIGN',
        'archetype_label': 'Product & Design Studio',
        'focus': 'Funnel root-cause analysis, RICE prioritization, PRDs, and AI product guardrails.',
    },
    'ui-ux-designer': {
        'archetype': 'PRODUCT_DESIGN',
        'archetype_label': 'Product & Design Studio',
        'focus': 'WCAG 2.1 audits, Nielsen heuristic critique, and responsive interaction states.',
    },
}

VALID_PRACTICE_SCOPES = {'STAGE_1', 'STAGE_2', 'STAGE_3', 'FULL'}


def readiness_feature_required(view_func):
    """Returns 404 when ENABLE_READINESS_ASSESSMENT is False."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not getattr(settings, 'ENABLE_READINESS_ASSESSMENT', False):
            raise Http404("AI-Era Career Readiness Assessment is not enabled.")
        return view_func(request, *args, **kwargs)
    return _wrapped


def _get_sidebar_context(user):
    profile = getattr(user, 'studentprofile', None)
    first_initial = (user.first_name[:1] if user.first_name else user.username[:1]).upper()
    last_initial = (user.last_name[:1] if user.last_name else "").upper()
    user_initials = f"{first_initial}{last_initial}" if last_initial else (user.username[:2].upper())
    target_career = profile.career_goal.strip() if (profile and profile.career_goal) else ""
    return {
        'profile': profile,
        'user_initials': user_initials,
        'target_career': target_career,
    }


def _is_role_active_pilot(career_path: CareerPath):
    if career_path.slug not in PILOT_ACTIVE_ROLE_SLUGS:
        return None
    return RoleAssessmentBlueprint.objects.filter(
        career_path=career_path,
        is_active=True,
    ).first()


def _get_roadmap_progress_summary(user, career_path: CareerPath):
    if not career_path:
        return {'enrolled': False, 'completed': 0, 'total': 0, 'pct': 0}
    total_topics = Topic.objects.filter(milestone__career_path=career_path).count()
    enrollment = UserRoadmap.objects.filter(user=user, career_path=career_path).first()
    if not enrollment:
        return {'enrolled': False, 'completed': 0, 'total': total_topics, 'pct': 0}
    completed_topics = enrollment.topic_progress.filter(is_completed=True).count()
    pct = int(round((completed_topics / total_topics) * 100)) if total_topics > 0 else 0
    return {
        'enrolled': True,
        'completed': completed_topics,
        'total': total_topics,
        'pct': pct,
    }


def build_readiness_hub_context(user):
    """Builds strictly real DB-backed context for the Sprint 2 Readiness Hub."""
    context = _get_sidebar_context(user)

    pilot_path = CareerPath.objects.filter(slug='software-engineer', is_active=True).first()
    pilot_blueprint = _is_role_active_pilot(pilot_path) if pilot_path else None
    roadmap_summary = _get_roadmap_progress_summary(user, pilot_path)

    quota = AssessmentSessionService.check_practice_daily_quota(user)

    practice_qs = AssessmentSession.objects.filter(user=user, mode='PRACTICE').select_related('career_path')
    practice_total = practice_qs.count()
    evaluated_practice = practice_qs.filter(status='EVALUATED', overall_score__isnull=False)
    practice_evaluated_count = evaluated_practice.count()
    practice_avg_score = evaluated_practice.aggregate(Avg('overall_score'))['overall_score__avg']
    recent_practice = list(practice_qs.order_by('-created_at')[:5])

    cert_qs = AssessmentSession.objects.filter(user=user, mode='CERTIFICATION').select_related('career_path')
    cert_total = cert_qs.count()
    pilot_cert = (
        RoadmapCertificate.objects.filter(user=user, career_path=pilot_path, is_valid=True).first()
        if pilot_path else None
    )

    pref = ReadinessUserPreference.objects.filter(user=user).first()
    legacy_attempts_count = UserAttempt.objects.filter(user=user).count()
    total_catalog_roles = CareerPath.objects.filter(is_active=True).count()

    context.update({
        'pilot_path': pilot_path,
        'pilot_blueprint': pilot_blueprint,
        'pilot_available': bool(pilot_path and pilot_blueprint),
        'roadmap_summary': roadmap_summary,
        'quota': quota,
        'practice_total': practice_total,
        'practice_evaluated_count': practice_evaluated_count,
        'practice_avg_score': practice_avg_score,
        'recent_practice': recent_practice,
        'cert_total': cert_total,
        'pilot_cert': pilot_cert,
        'readiness_pref': pref,
        'legacy_attempts_count': legacy_attempts_count,
        'total_catalog_roles': total_catalog_roles,
    })
    return context


@method_decorator([login_required, readiness_feature_required], name='dispatch')
class CareerSelectorView(TemplateView):
    """Displays all 15 CareerPath roles with Software Engineer active and 14 Coming Soon."""
    template_name = 'interviews/readiness_roles.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(_get_sidebar_context(self.request.user))

        selected_archetype = self.request.GET.get('archetype', 'ALL').strip().upper()
        if selected_archetype not in {'ALL', 'ENGINEERING', 'DATA_ML', 'CLOUD_SEC', 'PRODUCT_DESIGN'}:
            selected_archetype = 'ALL'

        active_bp_map = {
            bp.career_path_id: bp
            for bp in RoleAssessmentBlueprint.objects.filter(is_active=True).select_related('career_path')
        }

        all_paths = CareerPath.objects.filter(is_active=True).order_by('name')
        role_cards = []
        active_count = 0
        for cp in all_paths:
            meta = ROLE_ARCHETYPE_META.get(cp.slug, {
                'archetype': 'ENGINEERING',
                'archetype_label': 'Technical Role',
                'focus': cp.description[:120],
            })
            if selected_archetype != 'ALL' and meta['archetype'] != selected_archetype:
                continue

            is_pilot = cp.slug in PILOT_ACTIVE_ROLE_SLUGS and cp.id in active_bp_map
            if is_pilot:
                active_count += 1
            role_cards.append({
                'career_path': cp,
                'archetype': meta['archetype'],
                'archetype_label': meta['archetype_label'],
                'focus': meta['focus'],
                'is_active_pilot': is_pilot,
                'blueprint': active_bp_map.get(cp.id),
            })

        # Ensure Software Engineer pilot surfaces first when visible
        role_cards.sort(key=lambda item: (0 if item['is_active_pilot'] else 1, item['career_path'].name))

        context.update({
            'role_cards': role_cards,
            'selected_archetype': selected_archetype,
            'active_pilot_count': active_count,
            'total_role_count': all_paths.count(),
        })
        return context


@method_decorator([login_required, readiness_feature_required], name='dispatch')
class RolePracticeOverviewView(TemplateView):
    """
    Software Engineer Practice Overview. Blocks inactive roles strictly with HTTP 403
    to prevent URL manipulation into unconfigured career roles.
    """
    template_name = 'interviews/readiness_overview.html'

    def get(self, request, slug, *args, **kwargs):
        cp = get_object_or_404(CareerPath, slug=slug, is_active=True)
        bp = _is_role_active_pilot(cp)
        ctx = _get_sidebar_context(request.user)
        ctx['career_path'] = cp

        if not bp:
            ctx['role_meta'] = ROLE_ARCHETYPE_META.get(cp.slug, {})
            return render(request, 'interviews/readiness_role_locked.html', ctx, status=403)

        quota = AssessmentSessionService.check_practice_daily_quota(request.user)
        roadmap_summary = _get_roadmap_progress_summary(request.user, cp)
        pref = ReadinessUserPreference.objects.filter(user=request.user).first()

        practice_formats = [
            {
                'scope': 'STAGE_1',
                'stage_label': 'Stage 1 Drill',
                'title': 'Code & AI-Generated Artifact Review',
                'duration_min': 10,
                'evaluates': 'Locating subtle complexity, boundary, concurrency, and fail-open defects in AI-drafted code without unsafe host execution.',
                'requires_ai': False,
            },
            {
                'scope': 'STAGE_2',
                'stage_label': 'Stage 2 Drill',
                'title': 'Engineering Scenario & Constraint Pivot',
                'duration_min': 10,
                'evaluates': 'Designing pragmatic API/database solutions and adapting decisively when scale, SLA, or failure constraints shift mid-task.',
                'requires_ai': True,
            },
            {
                'scope': 'STAGE_3',
                'stage_label': 'Stage 3 Drill',
                'title': 'Adaptive Technical Defense',
                'duration_min': 7,
                'evaluates': 'Defending trade-offs, spotting gaps when challenged by the interviewer, and taking clear engineering ownership.',
                'requires_ai': True,
            },
        ]

        ctx.update({
            'blueprint': bp,
            'competencies': bp.competency_definitions or [],
            'practice_formats': practice_formats,
            'quota': quota,
            'roadmap_summary': roadmap_summary,
            'readiness_pref': pref,
            'cert_locked_reason': 'Certification Mode activates after Practice stages and calibration are completed in upcoming pilot sprints.',
        })
        return render(request, self.template_name, ctx)


@login_required
@readiness_feature_required
def readiness_policy_view(request, slug):
    """
    Assessment rules, AI involvement transparency, explicit consent capture,
    and persistent +25% extended-time accessibility configuration.
    """
    cp = get_object_or_404(CareerPath, slug=slug, is_active=True)
    bp = _is_role_active_pilot(cp)
    ctx = _get_sidebar_context(request.user)
    ctx['career_path'] = cp

    if not bp:
        ctx['role_meta'] = ROLE_ARCHETYPE_META.get(cp.slug, {})
        return render(request, 'interviews/readiness_role_locked.html', ctx, status=403)

    pref, _ = ReadinessUserPreference.objects.get_or_create(user=request.user)
    requested_scope = (request.GET.get('scope') or request.POST.get('scope') or 'FULL').strip().upper()
    if requested_scope not in VALID_PRACTICE_SCOPES:
        if request.method == 'POST':
            return HttpResponseBadRequest("Invalid practice scope requested.")
        requested_scope = 'FULL'

    error_message = None
    confirmed = request.GET.get('confirmed') == '1' and (
        request.session.get('readiness_policy_consented_role') == cp.slug
    )

    if request.method == 'POST':
        accept_policy = request.POST.get('accept_policy') == 'on'
        extended_time = request.POST.get('extended_time') == 'on'

        if not accept_policy:
            error_message = (
                "Explicit consent is required. Please review the AI-era assessment policy "
                "and check the consent acknowledgment box to continue."
            )
            ctx.update({
                'blueprint': bp,
                'pref': pref,
                'draft_extended_time': extended_time,
                'requested_scope': requested_scope,
                'error_message': error_message,
                'confirmed': False,
                'durations_preview': _build_duration_preview(bp, extended_time),
            })
            return render(request, 'interviews/readiness_policy.html', ctx, status=400)

        now = timezone.now()
        pref.extended_time_enabled = extended_time
        pref.last_consent_version = bp.version
        pref.last_consented_at = now
        pref.save(update_fields=[
            'extended_time_enabled', 'last_consent_version', 'last_consented_at', 'updated_at'
        ])

        request.session['readiness_policy_consented_role'] = cp.slug
        request.session['readiness_policy_version'] = bp.version
        request.session['readiness_extended_time'] = extended_time
        request.session['readiness_practice_scope'] = requested_scope

        return redirect(
            f"{reverse('interviews:readiness_policy', args=[cp.slug])}?scope={requested_scope}&confirmed=1"
        )

    ctx.update({
        'blueprint': bp,
        'pref': pref,
        'draft_extended_time': pref.extended_time_enabled,
        'requested_scope': requested_scope,
        'error_message': error_message,
        'confirmed': confirmed,
        'durations_preview': _build_duration_preview(bp, pref.extended_time_enabled),
    })
    return render(request, 'interviews/readiness_policy.html', ctx)


def _build_duration_preview(bp: RoleAssessmentBlueprint, extended_time: bool):
    mult = 1.25 if extended_time else 1.0
    durations = bp.stage_durations or {'1': 900, '2': 900, '3': 720}
    s1 = round((int(durations.get('1', 900)) * mult) / 60, 1)
    s2 = round((int(durations.get('2', 900)) * mult) / 60, 1)
    s3 = round((int(durations.get('3', 720)) * mult) / 60, 1)
    return {
        'stage_1_min': s1,
        'stage_2_min': s2,
        'stage_3_min': s3,
        'total_min': round(s1 + s2 + s3, 1),
        'multiplier_label': '+25% Extended Time Active' if extended_time else 'Standard Timing',
    }


@method_decorator([login_required, readiness_feature_required], name='dispatch')
class PracticeHistoryView(TemplateView):
    """
    Practice history & daily AI quota dashboard backed strictly by real database rows.
    Separates Practice runs cleanly from Certification runs.
    """
    template_name = 'interviews/readiness_history.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context.update(_get_sidebar_context(user))

        quota = AssessmentSessionService.check_practice_daily_quota(user)
        practice_sessions = list(
            AssessmentSession.objects.filter(user=user, mode='PRACTICE')
            .select_related('career_path')
            .order_by('-created_at')[:25]
        )
        certification_sessions = list(
            AssessmentSession.objects.filter(user=user, mode='CERTIFICATION')
            .select_related('career_path')
            .order_by('-created_at')[:10]
        )
        pref = ReadinessUserPreference.objects.filter(user=user).first()
        legacy_attempts_count = UserAttempt.objects.filter(user=user).count()

        context.update({
            'quota': quota,
            'practice_sessions': practice_sessions,
            'certification_sessions': certification_sessions,
            'readiness_pref': pref,
            'legacy_attempts_count': legacy_attempts_count,
        })
        return context
