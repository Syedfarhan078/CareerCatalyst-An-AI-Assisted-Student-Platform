"""
Sprint 3 Views: Stage 1 (Applied Diagnosis & Safe AST Code Review Workbench).

Strictly Practice Mode for the Software Engineer pilot, gated behind
settings.ENABLE_READINESS_ASSESSMENT (default False). Never executes candidate code,
never consumes AI quota, never exposes hidden grading metadata pre-submission,
and never activates Certification Mode or certificates.
"""

import json
from datetime import timedelta
from typing import Any, Dict, List

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.roadmaps.models import CareerPath, Topic
from .models import AssessmentSession, AssessmentStageResponse, ReadinessUserPreference
from .services.assessment_session import (
    AssessmentSessionService,
    InvalidStateTransitionError,
    StageLockedError,
)
from .services.blueprint_loader import BlueprintRegistryService, PILOT_ACTIVE_ROLE_SLUGS
from .services.stage1_evaluator import Stage1DeterministicEvaluator
from .views_readiness import (
    ROLE_ARCHETYPE_META,
    _get_sidebar_context,
    _is_role_active_pilot,
    readiness_feature_required,
)


ALLOWED_CATEGORIES = {code for code, _ in BlueprintRegistryService.CANDIDATE_ISSUE_CATEGORIES}
ALLOWED_SEVERITIES = {code for code, _ in BlueprintRegistryService.CANDIDATE_SEVERITY_LEVELS}


def _sanitize_findings_payload(raw_findings: Any) -> List[Dict[str, str]]:
    """Normalizes and bounds candidate-submitted findings safely."""
    if not isinstance(raw_findings, list):
        return []
    cleaned: List[Dict[str, str]] = []
    for item in raw_findings[:Stage1DeterministicEvaluator.MAX_FINDINGS]:
        if not isinstance(item, dict):
            continue
        title = str(item.get('title', '') or '').strip()[:240]
        impact = str(item.get('impact', '') or '').strip()[:1500]
        fix = str(item.get('fix_suggestion', '') or '').strip()[:1500]
        line_ref = str(item.get('line_ref', '') or '').strip()[:40]
        category = str(item.get('category', 'LOGIC_ERROR') or 'LOGIC_ERROR').strip().upper()
        if category not in ALLOWED_CATEGORIES:
            category = 'LOGIC_ERROR'
        severity = str(item.get('severity', 'HIGH') or 'HIGH').strip().upper()
        if severity not in ALLOWED_SEVERITIES:
            severity = 'HIGH'

        if not (title or impact or fix):
            continue
        cleaned.append({
            'title': title or '(Untitled Finding)',
            'category': category,
            'severity': severity,
            'line_ref': line_ref,
            'impact': impact,
            'fix_suggestion': fix,
        })
    return cleaned


def _parse_stage1_post(request) -> Dict[str, Any]:
    """Extracts and bounds Stage 1 form or JSON payload safely."""
    raw_findings_json = request.POST.get('findings_json', '[]')
    try:
        raw_findings = json.loads(raw_findings_json)
    except (TypeError, ValueError):
        raw_findings = []

    return {
        'findings': _sanitize_findings_payload(raw_findings),
        'proposed_fix_code': str(request.POST.get('proposed_fix_code', '') or '')[:12000],
        'test_plan': str(request.POST.get('test_plan', '') or '')[:4000],
        'overall_summary': str(request.POST.get('overall_summary', '') or '')[:2500],
    }


def _check_session_access_or_403(request, session: AssessmentSession):
    if session.user_id != request.user.id:
        return HttpResponseForbidden("You do not have permission to access this assessment session.")
    if session.career_path.slug not in PILOT_ACTIVE_ROLE_SLUGS:
        return HttpResponseForbidden("This career role assessment is not active.")
    if session.mode != 'PRACTICE' or session.practice_scope != 'STAGE_1':
        return HttpResponseForbidden("Invalid session mode or scope for Stage 1 Practice.")
    return None


@login_required
@readiness_feature_required
@require_POST
def stage1_practice_start(request, slug):
    """
    Creates (or resumes active unexpired) Stage 1 Practice drill after verifying
    active pilot role and explicit policy consent.
    """
    cp = get_object_or_404(CareerPath, slug=slug, is_active=True)
    bp = _is_role_active_pilot(cp)
    if not bp:
        ctx = _get_sidebar_context(request.user)
        ctx['career_path'] = cp
        ctx['role_meta'] = ROLE_ARCHETYPE_META.get(cp.slug, {})
        return render(request, 'interviews/readiness_role_locked.html', ctx, status=403)

    # Require explicit recorded policy consent (never silently assumed)
    pref = ReadinessUserPreference.objects.filter(user=request.user).first()
    has_db_consent = bool(pref and pref.last_consented_at and pref.last_consent_version == bp.version)
    has_session_consent = (
        request.session.get('readiness_policy_consented_role') == cp.slug
        and request.session.get('readiness_policy_version') == bp.version
    )
    if not (has_db_consent or has_session_consent):
        return redirect(f"{reverse('interviews:readiness_policy', args=[cp.slug])}?scope=STAGE_1")

    now = timezone.now()
    existing_active = AssessmentSession.objects.filter(
        user=request.user,
        career_path=cp,
        mode='PRACTICE',
        practice_scope='STAGE_1',
        status='STAGE_1',
    ).order_by('-created_at').first()

    if existing_active:
        if existing_active.stage_expires_at and now > existing_active.stage_expires_at + timedelta(seconds=30):
            AssessmentSessionService.mark_expired(existing_active.session_uuid)
        else:
            return redirect('interviews:stage1_workbench', session_uuid=existing_active.session_uuid)

    extended_time = bool(pref.extended_time_enabled) if pref else False
    session = AssessmentSessionService.create_session(
        user=request.user,
        career_path=cp,
        mode='PRACTICE',
        practice_scope='STAGE_1',
        extended_time=extended_time,
    )
    AssessmentSessionService.accept_policy_and_start(request.user, session.session_uuid)
    return redirect('interviews:stage1_workbench', session_uuid=session.session_uuid)


@login_required
@readiness_feature_required
def stage1_workbench(request, session_uuid):
    """
    Renders Stage 1 split-pane Applied Diagnosis workbench and processes
    final atomic deterministic submission.
    """
    session = get_object_or_404(
        AssessmentSession.objects.select_related('career_path', 'blueprint'),
        session_uuid=session_uuid,
    )
    denied = _check_session_access_or_403(request, session)
    if denied:
        return denied

    if session.status == 'EVALUATED':
        return redirect('interviews:stage1_result', session_uuid=session.session_uuid)

    ctx = _get_sidebar_context(request.user)
    ctx['session'] = session
    ctx['career_path'] = session.career_path

    if session.status in ('EXPIRED', 'CANCELLED'):
        ctx['closed_reason'] = session.status
        return render(request, 'interviews/stage1_closed.html', ctx)

    now = timezone.now()
    if session.stage_expires_at and now > session.stage_expires_at + timedelta(seconds=30):
        AssessmentSessionService.mark_expired(session.session_uuid)
        session.refresh_from_db()
        ctx['closed_reason'] = 'EXPIRED'
        return render(
            request,
            'interviews/stage1_closed.html',
            ctx,
            status=400 if request.method == 'POST' else 200,
        )

    stage_resp = get_object_or_404(AssessmentStageResponse, session=session, stage_number=1)
    if stage_resp.is_locked or session.status != 'STAGE_1':
        if hasattr(session, 'evaluation_result'):
            return redirect('interviews:stage1_result', session_uuid=session.session_uuid)
        ctx['closed_reason'] = session.status
        return render(request, 'interviews/stage1_closed.html', ctx, status=400 if request.method == 'POST' else 200)

    candidate_task = BlueprintRegistryService.get_candidate_stage_context(stage_resp)
    remaining_seconds = (
        max(0, int((session.stage_expires_at - now).total_seconds()))
        if session.stage_expires_at else 600
    )

    draft = stage_resp.response_payload or {}
    error_message = None

    if request.method == 'POST':
        payload = _parse_stage1_post(request)
        if not payload['findings']:
            error_message = (
                "Add at least one concrete engineering issue finding before submitting for evaluation."
            )
            draft = payload
        else:
            try:
                Stage1DeterministicEvaluator.submit_and_evaluate_stage1_practice(
                    user=request.user,
                    session_uuid=session.session_uuid,
                    response_payload=payload,
                )
                return redirect('interviews:stage1_result', session_uuid=session.session_uuid)
            except (StageLockedError, InvalidStateTransitionError):
                return redirect('interviews:stage1_result', session_uuid=session.session_uuid)

    ctx.update({
        'task': candidate_task,
        'remaining_seconds': remaining_seconds,
        'draft': draft,
        'draft_findings_json': json.dumps(draft.get('findings') or []),
        'error_message': error_message,
        'status_code_flag': 400 if error_message else 200,
    })
    return render(
        request,
        'interviews/stage1_workbench.html',
        ctx,
        status=400 if error_message else 200,
    )


@login_required
@readiness_feature_required
@require_POST
def stage1_save_draft(request, session_uuid):
    """JSON autosave endpoint preserving candidate edits in Stage 1."""
    session = get_object_or_404(AssessmentSession, session_uuid=session_uuid)
    denied = _check_session_access_or_403(request, session)
    if denied:
        return JsonResponse({'ok': False, 'error': 'Forbidden'}, status=403)

    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'Invalid JSON payload.'}, status=400)

    cleaned_payload = {
        'findings': _sanitize_findings_payload(body.get('findings', [])),
        'proposed_fix_code': str(body.get('proposed_fix_code', '') or '')[:12000],
        'test_plan': str(body.get('test_plan', '') or '')[:4000],
        'overall_summary': str(body.get('overall_summary', '') or '')[:2500],
    }

    try:
        stage_resp = AssessmentSessionService.save_stage_draft(
            request.user, session.session_uuid, 1, cleaned_payload
        )
        return JsonResponse({
            'ok': True,
            'saved_at': stage_resp.draft_saved_at.isoformat() if stage_resp.draft_saved_at else None,
        })
    except PermissionDenied:
        return JsonResponse({'ok': False, 'error': 'Permission denied.'}, status=403)
    except (StageLockedError, InvalidStateTransitionError, ValidationError) as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=409)


@login_required
@readiness_feature_required
@require_POST
def stage1_abandon(request, session_uuid):
    """Safely exits/abandons an open Stage 1 Practice drill without penalty."""
    with transaction.atomic():
        session = get_object_or_404(
            AssessmentSession.objects.select_for_update(),
            session_uuid=session_uuid,
        )
        denied = _check_session_access_or_403(request, session)
        if denied:
            return denied

        if session.status == 'EVALUATED':
            return redirect('interviews:stage1_result', session_uuid=session.session_uuid)

        if session.status == 'STAGE_1':
            session.status = 'CANCELLED'
            session.stage_expires_at = None
            session.cooldown_until = None
            session.save(update_fields=['status', 'stage_expires_at', 'cooldown_until', 'updated_at'])

    return redirect('interviews:readiness_history')


@login_required
@readiness_feature_required
def stage1_result(request, session_uuid):
    """
    Displays deterministic Stage 1 evaluation breakdown, matched/missed/unsupported
    findings, AST static audit summary, and linked Software Engineer remediation topics.
    """
    session = get_object_or_404(
        AssessmentSession.objects.select_related('career_path', 'blueprint'),
        session_uuid=session_uuid,
    )
    denied = _check_session_access_or_403(request, session)
    if denied:
        return denied

    if session.status != 'EVALUATED' or not hasattr(session, 'evaluation_result'):
        return redirect('interviews:stage1_workbench', session_uuid=session.session_uuid)

    stage_resp = get_object_or_404(AssessmentStageResponse, session=session, stage_number=1)
    eval_res = session.evaluation_result
    comp_scores = list(eval_res.competency_scores.all().order_by('-weight', 'competency_name'))

    # Hydrate recommended roadmap Topic objects for clickable remediation
    all_topic_ids = {tid for cs in comp_scores for tid in (cs.recommended_topic_ids or [])}
    topic_map = {
        t.id: t
        for t in Topic.objects.filter(id__in=all_topic_ids).select_related('milestone')
    }
    competency_cards = []
    for cs in comp_scores:
        competency_cards.append({
            'obj': cs,
            'topics': [topic_map[tid] for tid in (cs.recommended_topic_ids or []) if tid in topic_map],
        })

    ctx = _get_sidebar_context(request.user)
    ctx.update({
        'session': session,
        'career_path': session.career_path,
        'evaluation': eval_res,
        'evidence': eval_res.scoring_evidence or {},
        'competency_cards': competency_cards,
        'remediations': eval_res.remediation_recommendations or [],
        'candidate_task': BlueprintRegistryService.get_candidate_stage_context(stage_resp),
        'candidate_response': stage_resp.response_payload or {},
    })
    return render(request, 'interviews/stage1_result.html', ctx)
