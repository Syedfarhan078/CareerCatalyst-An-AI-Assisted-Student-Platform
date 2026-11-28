"""
Sprint 4 Views: Stage 2 (Engineering Scenario & Constraint Pivot Workbench).

Strictly Practice Mode for the Software Engineer pilot, gated behind
settings.ENABLE_READINESS_ASSESSMENT (default False). Implements a strict two-phase
one-way workflow (Phase A Baseline -> Lock -> Phase B Constraint Pivot -> Evaluate),
never exposes hidden grading metadata or Phase B pivot before Phase A submission,
consumes 0 AI quota, never executes candidate code, and never activates Certification Mode.
"""

import json
from datetime import timedelta
from typing import Any, Dict

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
from .services.stage2_evaluator import Stage2DeterministicEvaluator
from .views_readiness import (
    ROLE_ARCHETYPE_META,
    _get_sidebar_context,
    _is_role_active_pilot,
    readiness_feature_required,
)


def _sanitize_part_a_payload(raw: Any) -> Dict[str, str]:
    if not isinstance(raw, dict):
        raw = {}
    return {
        'initial_approach': str(raw.get('initial_approach', '') or '').strip()[:6000],
        'data_and_concurrency': str(raw.get('data_and_concurrency', '') or '').strip()[:4000],
        'initial_tradeoffs': str(raw.get('initial_tradeoffs', '') or '').strip()[:3000],
    }


def _sanitize_part_b_payload(raw: Any) -> Dict[str, str]:
    if not isinstance(raw, dict):
        raw = {}
    return {
        'updated_approach': str(raw.get('updated_approach', '') or '').strip()[:6000],
        'what_changes': str(raw.get('what_changes', '') or '').strip()[:4000],
        'what_stays_unchanged': str(raw.get('what_stays_unchanged', '') or '').strip()[:3000],
        'tradeoffs': str(raw.get('tradeoffs', '') or '').strip()[:4000],
        'risks_and_failures': str(raw.get('risks_and_failures', '') or '').strip()[:4000],
        'validation_and_testing': str(raw.get('validation_and_testing', '') or '').strip()[:4000],
    }


def _check_stage2_access_or_403(request, session: AssessmentSession):
    if session.user_id != request.user.id:
        return HttpResponseForbidden("You do not have permission to access this assessment session.")
    if session.career_path.slug not in PILOT_ACTIVE_ROLE_SLUGS:
        return HttpResponseForbidden("This career role assessment is not active.")
    if session.mode != 'PRACTICE' or session.practice_scope != 'STAGE_2':
        return HttpResponseForbidden("Invalid session mode or scope for Stage 2 Practice.")
    return None


@login_required
@readiness_feature_required
@require_POST
def stage2_practice_start(request, slug):
    """
    Creates (or resumes active unexpired) Stage 2 Practice drill after verifying
    active Software Engineer pilot role and explicit policy consent.
    """
    cp = get_object_or_404(CareerPath, slug=slug, is_active=True)
    bp = _is_role_active_pilot(cp)
    if not bp:
        ctx = _get_sidebar_context(request.user)
        ctx['career_path'] = cp
        ctx['role_meta'] = ROLE_ARCHETYPE_META.get(cp.slug, {})
        return render(request, 'interviews/readiness_role_locked.html', ctx, status=403)

    pref = ReadinessUserPreference.objects.filter(user=request.user).first()
    has_db_consent = bool(pref and pref.last_consented_at and pref.last_consent_version == bp.version)
    has_session_consent = (
        request.session.get('readiness_policy_consented_role') == cp.slug
        and request.session.get('readiness_policy_version') == bp.version
    )
    if not (has_db_consent or has_session_consent):
        return redirect(f"{reverse('interviews:readiness_policy', args=[cp.slug])}?scope=STAGE_2")

    now = timezone.now()
    existing_active = AssessmentSession.objects.filter(
        user=request.user,
        career_path=cp,
        mode='PRACTICE',
        practice_scope='STAGE_2',
        status='STAGE_2',
    ).order_by('-created_at').first()

    if existing_active:
        if existing_active.stage_expires_at and now > existing_active.stage_expires_at + timedelta(seconds=30):
            AssessmentSessionService.mark_expired(existing_active.session_uuid)
        else:
            return redirect('interviews:stage2_workbench', session_uuid=existing_active.session_uuid)

    extended_time = bool(pref.extended_time_enabled) if pref else False
    session = AssessmentSessionService.create_session(
        user=request.user,
        career_path=cp,
        mode='PRACTICE',
        practice_scope='STAGE_2',
        extended_time=extended_time,
    )
    AssessmentSessionService.accept_policy_and_start(request.user, session.session_uuid)
    return redirect('interviews:stage2_workbench', session_uuid=session.session_uuid)


@login_required
@readiness_feature_required
def stage2_workbench(request, session_uuid):
    """
    Renders Stage 2 Engineering Scenario & Constraint Pivot workbench cleanly across
    Phase A (Initial Situation) and Phase B (Constraint Pivot & Final Adaptation).
    """
    session = get_object_or_404(
        AssessmentSession.objects.select_related('career_path', 'blueprint'),
        session_uuid=session_uuid,
    )
    denied = _check_stage2_access_or_403(request, session)
    if denied:
        return denied

    if session.status == 'EVALUATED':
        return redirect('interviews:stage2_result', session_uuid=session.session_uuid)

    ctx = _get_sidebar_context(request.user)
    ctx['session'] = session
    ctx['career_path'] = session.career_path

    if session.status in ('EXPIRED', 'CANCELLED'):
        ctx['closed_reason'] = session.status
        return render(request, 'interviews/stage2_closed.html', ctx)

    now = timezone.now()
    if session.stage_expires_at and now > session.stage_expires_at + timedelta(seconds=30):
        AssessmentSessionService.mark_expired(session.session_uuid)
        session.refresh_from_db()
        ctx['closed_reason'] = 'EXPIRED'
        return render(
            request,
            'interviews/stage2_closed.html',
            ctx,
            status=400 if request.method == 'POST' else 200,
        )

    stage_resp = get_object_or_404(AssessmentStageResponse, session=session, stage_number=2)
    if stage_resp.is_locked or session.status != 'STAGE_2':
        if hasattr(session, 'evaluation_result'):
            return redirect('interviews:stage2_result', session_uuid=session.session_uuid)
        ctx['closed_reason'] = session.status
        return render(
            request,
            'interviews/stage2_closed.html',
            ctx,
            status=400 if request.method == 'POST' else 200,
        )

    remaining_seconds = (
        max(0, int((session.stage_expires_at - now).total_seconds()))
        if session.stage_expires_at else 600
    )

    payload = stage_resp.response_payload or {}
    draft_a = dict(payload.get('part_a') or {})
    draft_b = dict(payload.get('part_b') or {})
    error_message = None
    phase_a_locked = stage_resp.part_a_submitted_at is not None

    if request.method == 'POST':
        action = (request.POST.get('action') or '').strip()

        if not phase_a_locked:
            # Must complete Phase A before submitting Phase B
            if action == 'submit_final':
                error_message = "Submit and lock your Initial Approach (Phase A) before submitting final evaluation."
            else:
                part_a_data = _sanitize_part_a_payload({
                    'initial_approach': request.POST.get('initial_approach'),
                    'data_and_concurrency': request.POST.get('data_and_concurrency'),
                    'initial_tradeoffs': request.POST.get('initial_tradeoffs'),
                })
                draft_a = part_a_data
                if len(part_a_data['initial_approach']) < 15 or len(part_a_data['data_and_concurrency']) < 10:
                    error_message = (
                        "Provide both your initial engineering approach and data/concurrency handling (at least a sentence each) before locking Phase A."
                    )
                else:
                    try:
                        AssessmentSessionService.submit_stage_2_part_a(
                            request.user,
                            session.session_uuid,
                            part_a_data,
                        )
                        return redirect('interviews:stage2_workbench', session_uuid=session.session_uuid)
                    except (StageLockedError, InvalidStateTransitionError) as exc:
                        error_message = str(exc)
        else:
            # Phase A already locked -> only final Phase B submission is permitted
            if action == 'lock_part_a':
                error_message = "Phase A is already locked and cannot be modified."
            else:
                part_b_data = _sanitize_part_b_payload({
                    'updated_approach': request.POST.get('updated_approach'),
                    'what_changes': request.POST.get('what_changes'),
                    'what_stays_unchanged': request.POST.get('what_stays_unchanged'),
                    'tradeoffs': request.POST.get('tradeoffs'),
                    'risks_and_failures': request.POST.get('risks_and_failures'),
                    'validation_and_testing': request.POST.get('validation_and_testing'),
                })
                draft_b = part_b_data
                if len(part_b_data['updated_approach']) < 15 or not (
                    part_b_data['what_changes'] or part_b_data['tradeoffs'] or part_b_data['validation_and_testing']
                ):
                    error_message = (
                        "Describe your updated engineering approach along with changes, trade-offs, or validation steps before final submission."
                    )
                else:
                    try:
                        Stage2DeterministicEvaluator.submit_and_evaluate_stage2_practice(
                            user=request.user,
                            session_uuid=session.session_uuid,
                            part_b_payload=part_b_data,
                        )
                        return redirect('interviews:stage2_result', session_uuid=session.session_uuid)
                    except (StageLockedError, InvalidStateTransitionError):
                        return redirect('interviews:stage2_result', session_uuid=session.session_uuid)

    # Fetch candidate-safe context AFTER state mutations (strip grading_meta & pre-lock part_b_twist)
    stage_resp.refresh_from_db()
    candidate_task = BlueprintRegistryService.get_candidate_stage_context(stage_resp)

    ctx.update({
        'task': candidate_task,
        'phase': candidate_task.get('phase', 'PHASE_A'),
        'part_a_locked': stage_resp.part_a_submitted_at is not None,
        'part_a_submitted_at': stage_resp.part_a_submitted_at,
        'remaining_seconds': remaining_seconds,
        'draft_a': draft_a,
        'draft_b': draft_b,
        'error_message': error_message,
    })
    return render(
        request,
        'interviews/stage2_workbench.html',
        ctx,
        status=400 if error_message else 200,
    )


@login_required
@readiness_feature_required
@require_POST
def stage2_save_draft(request, session_uuid):
    """
    JSON autosave endpoint preserving candidate edits in Stage 2.
    Strictly protects locked Phase A payload from overwrite once Phase B begins.
    """
    session = get_object_or_404(AssessmentSession, session_uuid=session_uuid)
    denied = _check_stage2_access_or_403(request, session)
    if denied:
        return JsonResponse({'ok': False, 'error': 'Forbidden'}, status=403)

    stage_resp = AssessmentStageResponse.objects.filter(session=session, stage_number=2).first()
    if not stage_resp:
        return JsonResponse({'ok': False, 'error': 'Stage 2 response not found.'}, status=404)

    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'Invalid JSON payload.'}, status=400)

    if stage_resp.part_a_submitted_at is None:
        draft_payload = {'part_a': _sanitize_part_a_payload(body.get('part_a', body))}
    else:
        draft_payload = {'part_b': _sanitize_part_b_payload(body.get('part_b', body))}

    try:
        saved = AssessmentSessionService.save_stage_draft(
            request.user, session.session_uuid, 2, draft_payload
        )
        return JsonResponse({
            'ok': True,
            'phase': 'PHASE_B' if saved.part_a_submitted_at else 'PHASE_A',
            'saved_at': saved.draft_saved_at.isoformat() if saved.draft_saved_at else None,
        })
    except PermissionDenied:
        return JsonResponse({'ok': False, 'error': 'Permission denied.'}, status=403)
    except (StageLockedError, InvalidStateTransitionError, ValidationError) as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=409)


@login_required
@readiness_feature_required
@require_POST
def stage2_abandon(request, session_uuid):
    """Safely exits/abandons an open Stage 2 Practice drill without penalty or cooldown."""
    with transaction.atomic():
        session = get_object_or_404(
            AssessmentSession.objects.select_for_update(),
            session_uuid=session_uuid,
        )
        denied = _check_stage2_access_or_403(request, session)
        if denied:
            return denied

        if session.status == 'EVALUATED':
            return redirect('interviews:stage2_result', session_uuid=session.session_uuid)

        if session.status == 'STAGE_2':
            session.status = 'CANCELLED'
            session.stage_expires_at = None
            session.cooldown_until = None
            session.save(update_fields=['status', 'stage_expires_at', 'cooldown_until', 'updated_at'])

    return redirect('interviews:readiness_history')


@login_required
@readiness_feature_required
def stage2_result(request, session_uuid):
    """
    Displays deterministic Stage 2 Scenario & Constraint Pivot feedback, competency
    breakdown, qualitative adaptation diagnostics, and linked roadmap remediation.
    """
    session = get_object_or_404(
        AssessmentSession.objects.select_related('career_path', 'blueprint'),
        session_uuid=session_uuid,
    )
    denied = _check_stage2_access_or_403(request, session)
    if denied:
        return denied

    if session.status != 'EVALUATED' or not hasattr(session, 'evaluation_result'):
        return redirect('interviews:stage2_workbench', session_uuid=session.session_uuid)

    stage_resp = get_object_or_404(AssessmentStageResponse, session=session, stage_number=2)
    eval_res = session.evaluation_result
    comp_scores = list(eval_res.competency_scores.all().order_by('-weight', 'competency_name'))

    all_topic_ids = {tid for cs in comp_scores for tid in (cs.recommended_topic_ids or [])}
    topic_map = {
        t.id: t
        for t in Topic.objects.filter(id__in=all_topic_ids).select_related('milestone')
    }
    competency_cards = [
        {
            'obj': cs,
            'topics': [topic_map[tid] for tid in (cs.recommended_topic_ids or []) if tid in topic_map],
        }
        for cs in comp_scores
    ]

    candidate_task = BlueprintRegistryService.get_candidate_stage_context(stage_resp)
    ctx = _get_sidebar_context(request.user)
    ctx.update({
        'session': session,
        'career_path': session.career_path,
        'evaluation': eval_res,
        'evidence': eval_res.scoring_evidence or {},
        'competency_cards': competency_cards,
        'remediations': eval_res.remediation_recommendations or [],
        'candidate_task': candidate_task,
        'candidate_response': stage_resp.response_payload or {},
    })
    return render(request, 'interviews/stage2_result.html', ctx)
