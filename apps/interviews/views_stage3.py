"""
Sprint 5 Views: Stage 3 (Adaptive AI Ownership Defense Practice Workbench).

Strictly Practice Mode for the Software Engineer pilot, gated behind
settings.ENABLE_READINESS_ASSESSMENT (default False). Reuses AssessmentSession,
AdaptiveInterviewTurn, AssessmentAIService, PAUSED_OUTAGE / PENDING_EVALUATION
recovery, AIUsageLog quota accounting (8/24h), EvaluationResult, and CompetencyScore.
"""

import json
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.roadmaps.models import CareerPath, Topic
from .models import (
    AdaptiveInterviewTurn,
    AssessmentSession,
    AssessmentStageResponse,
    ReadinessUserPreference,
)
from .services.assessment_session import (
    AssessmentSessionService,
    InvalidStateTransitionError,
    StageLockedError,
)
from .services.blueprint_loader import BlueprintRegistryService, PILOT_ACTIVE_ROLE_SLUGS
from .services.stage3_engine import QuotaExhaustedError, Stage3OwnershipEngine
from .services.stage3_evaluator import Stage3OwnershipEvaluator
from .views_readiness import (
    ROLE_ARCHETYPE_META,
    _get_sidebar_context,
    _is_role_active_pilot,
    readiness_feature_required,
)


def _check_stage3_access_or_403(request, session: AssessmentSession):
    if session.user_id != request.user.id:
        return HttpResponseForbidden("You do not have permission to access this assessment session.")
    if session.career_path.slug not in PILOT_ACTIVE_ROLE_SLUGS:
        return HttpResponseForbidden("This career role assessment is not active.")
    if session.mode != 'PRACTICE' or session.practice_scope != 'STAGE_3':
        return HttpResponseForbidden("Invalid session mode or scope for Stage 3 Practice.")
    return None


@login_required
@readiness_feature_required
@require_POST
def stage3_practice_start(request, slug):
    """
    Resumes active/paused Stage 3 Practice session or initializes a new one after
    verifying Software Engineer pilot status, explicit consent, and 24h AI quota.
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
        return redirect(f"{reverse('interviews:readiness_policy', args=[cp.slug])}?scope=STAGE_3")

    now = timezone.now()
    existing_active = AssessmentSession.objects.filter(
        user=request.user,
        career_path=cp,
        mode='PRACTICE',
        practice_scope='STAGE_3',
        status__in=['STAGE_3', 'PAUSED_OUTAGE', 'PENDING_EVALUATION'],
    ).order_by('-created_at').first()

    if existing_active:
        if (
            existing_active.status == 'STAGE_3'
            and existing_active.stage_expires_at
            and now > existing_active.stage_expires_at + timedelta(seconds=30)
        ):
            AssessmentSessionService.mark_expired(existing_active.session_uuid)
        else:
            return redirect('interviews:stage3_workbench', session_uuid=existing_active.session_uuid)

    # Server-side daily quota enforcement before starting a new AI-assisted session
    quota = AssessmentSessionService.check_practice_daily_quota(request.user)
    if not quota['allowed']:
        ctx = _get_sidebar_context(request.user)
        ctx.update({
            'career_path': cp,
            'quota': quota,
            'closed_reason': 'QUOTA_EXHAUSTED',
        })
        return render(request, 'interviews/stage3_closed.html', ctx, status=429)

    extended_time = bool(pref.extended_time_enabled) if pref else False
    session = AssessmentSessionService.create_session(
        user=request.user,
        career_path=cp,
        mode='PRACTICE',
        practice_scope='STAGE_3',
        extended_time=extended_time,
    )
    AssessmentSessionService.accept_policy_and_start(request.user, session.session_uuid)
    Stage3OwnershipEngine.ensure_initial_turn(session)
    return redirect('interviews:stage3_workbench', session_uuid=session.session_uuid)


@login_required
@readiness_feature_required
def stage3_workbench(request, session_uuid):
    """
    Renders bounded Stage 3 Ownership Defense panel (1 initial prompt + up to 3 adaptive
    follow-ups, max 4 candidate responses) and processes turn replies & final completion.
    """
    session = get_object_or_404(
        AssessmentSession.objects.select_related('career_path', 'blueprint'),
        session_uuid=session_uuid,
    )
    denied = _check_stage3_access_or_403(request, session)
    if denied:
        return denied

    if session.status == 'EVALUATED':
        return redirect('interviews:stage3_result', session_uuid=session.session_uuid)

    ctx = _get_sidebar_context(request.user)
    ctx['session'] = session
    ctx['career_path'] = session.career_path
    ctx['quota'] = AssessmentSessionService.check_practice_daily_quota(request.user)

    if session.status in ('EXPIRED', 'CANCELLED'):
        ctx['closed_reason'] = session.status
        return render(request, 'interviews/stage3_closed.html', ctx)

    now = timezone.now()
    if (
        session.status == 'STAGE_3'
        and session.stage_expires_at
        and now > session.stage_expires_at + timedelta(seconds=30)
    ):
        AssessmentSessionService.mark_expired(session.session_uuid)
        session.refresh_from_db()
        ctx['closed_reason'] = 'EXPIRED'
        return render(
            request,
            'interviews/stage3_closed.html',
            ctx,
            status=400 if request.method == 'POST' else 200,
        )

    stage_resp = get_object_or_404(AssessmentStageResponse, session=session, stage_number=3)
    min_turns, max_turns = Stage3OwnershipEngine.get_turn_bounds(session)

    if session.status == 'STAGE_3':
        Stage3OwnershipEngine.ensure_initial_turn(session)

    turns = list(AdaptiveInterviewTurn.objects.filter(session=session).order_by('turn_number'))
    answered_turns = [t for t in turns if t.replied_at is not None]
    open_turns = [t for t in turns if t.replied_at is None]

    error_message = None
    draft_text = ''
    status_code = 200

    if request.method == 'POST':
        action = (request.POST.get('action') or 'submit_turn').strip()

        if session.status in ('PAUSED_OUTAGE', 'PENDING_EVALUATION'):
            error_message = "Use Resume Session or Retry Evaluation below to continue safely."
            status_code = 409
        elif action == 'complete_stage3':
            if len(answered_turns) < min_turns:
                error_message = (
                    f"Answer at least {min_turns} defense turns before finishing evaluation."
                )
                status_code = 400
            else:
                try:
                    eval_res = Stage3OwnershipEvaluator.evaluate_and_finalize(
                        user=request.user,
                        session_uuid=session.session_uuid,
                    )
                    if eval_res['status'] == 'EVALUATED':
                        return redirect('interviews:stage3_result', session_uuid=session.session_uuid)
                    # PENDING_EVALUATION outage state
                    return redirect('interviews:stage3_workbench', session_uuid=session.session_uuid)
                except QuotaExhaustedError:
                    ctx['closed_reason'] = 'QUOTA_EXHAUSTED'
                    return render(request, 'interviews/stage3_closed.html', ctx, status=429)
                except (ValidationError, InvalidStateTransitionError) as exc:
                    error_message = str(exc)
                    status_code = 400
        else:
            if not open_turns:
                error_message = "Defense turns are already complete. Confirm completion to generate feedback."
                status_code = 400
            else:
                active_turn = open_turns[0]
                raw_reply = request.POST.get('candidate_reply', '')
                draft_text = raw_reply
                try:
                    # Strictly ignore any client-supplied turn_number if manipulated -> use POST value for validation
                    submitted_turn_num = int(request.POST.get('turn_number') or active_turn.turn_number)
                except (TypeError, ValueError):
                    submitted_turn_num = -1

                try:
                    Stage3OwnershipEngine.submit_turn_reply(
                        user=request.user,
                        session_uuid=session.session_uuid,
                        turn_number=submitted_turn_num,
                        reply_text=raw_reply,
                    )
                    return redirect('interviews:stage3_workbench', session_uuid=session.session_uuid)
                except QuotaExhaustedError:
                    ctx['closed_reason'] = 'QUOTA_EXHAUSTED'
                    return render(request, 'interviews/stage3_closed.html', ctx, status=429)
                except (ValidationError, InvalidStateTransitionError, StageLockedError) as exc:
                    error_message = str(exc)
                    status_code = 400

        # Reload turns cleanly after POST handling
        session.refresh_from_db()
        turns = list(AdaptiveInterviewTurn.objects.filter(session=session).order_by('turn_number'))
        answered_turns = [t for t in turns if t.replied_at is not None]
        open_turns = [t for t in turns if t.replied_at is None]

    active_turn = open_turns[0] if open_turns else None
    ready_for_completion = (
        session.status == 'STAGE_3'
        and active_turn is None
        and len(answered_turns) >= min_turns
    )

    if active_turn and not draft_text:
        saved_drafts = (stage_resp.response_payload or {}).get('turn_drafts') or {}
        draft_text = str(saved_drafts.get(str(active_turn.turn_number), '') or '')

    candidate_task = BlueprintRegistryService.get_candidate_stage_context(stage_resp)
    timer_anchor = session.paused_at if (session.status == 'PAUSED_OUTAGE' and session.paused_at) else now
    remaining_seconds = (
        max(0, int((session.stage_expires_at - timer_anchor).total_seconds()))
        if session.stage_expires_at else 420
    )

    # Expose only candidate-safe turn display fields (NEVER expose expected_reasoning_direction!)
    safe_turns = []
    for t in turns:
        meta = t.turn_analysis or {}
        safe_turns.append({
            'turn_number': t.turn_number,
            'interviewer_prompt': t.interviewer_prompt,
            'probe_type_label': str(meta.get('probe_type', 'OWNERSHIP')).replace('_', ' ').title(),
            'candidate_reply': t.candidate_reply,
            'replied_at': t.replied_at,
            'is_answered': t.replied_at is not None,
        })

    prior_context = (turns[0].context_summary_used if turns else '') or ''
    ctx.update({
        'task': candidate_task,
        'turns': safe_turns,
        'active_turn': active_turn,
        'answered_count': len(answered_turns),
        'min_turns': min_turns,
        'max_turns': max_turns,
        'ready_for_completion': ready_for_completion,
        'draft_text': draft_text,
        'prior_context': prior_context,
        'remaining_seconds': remaining_seconds,
        'error_message': error_message,
        'is_paused_outage': session.status == 'PAUSED_OUTAGE',
        'is_pending_eval': session.status == 'PENDING_EVALUATION',
    })
    return render(request, 'interviews/stage3_workbench.html', ctx, status=status_code)


@login_required
@readiness_feature_required
@require_POST
def stage3_save_draft(request, session_uuid):
    """JSON autosave preserving active Stage 3 turn draft without calling AI."""
    session = get_object_or_404(AssessmentSession, session_uuid=session_uuid)
    denied = _check_stage3_access_or_403(request, session)
    if denied:
        return JsonResponse({'ok': False, 'error': 'Forbidden'}, status=403)

    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'Invalid JSON payload.'}, status=400)

    turn_num = str(body.get('turn_number', '')).strip()
    reply_draft = str(body.get('candidate_reply', '') or '')[:Stage3OwnershipEngine.MAX_REPLY_CHARS]
    if not turn_num.isdigit():
        return JsonResponse({'ok': False, 'error': 'Invalid turn number.'}, status=400)

    stage_resp = AssessmentStageResponse.objects.filter(session=session, stage_number=3).first()
    existing_drafts = dict(((stage_resp.response_payload or {}).get('turn_drafts') if stage_resp else {}) or {})
    existing_drafts[turn_num] = reply_draft

    try:
        saved = AssessmentSessionService.save_stage_draft(
            request.user, session.session_uuid, 3, {'turn_drafts': existing_drafts}
        )
        return JsonResponse({
            'ok': True,
            'saved_at': saved.draft_saved_at.isoformat() if saved.draft_saved_at else None,
        })
    except PermissionDenied:
        return JsonResponse({'ok': False, 'error': 'Permission denied.'}, status=403)
    except (StageLockedError, InvalidStateTransitionError, ValidationError) as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=409)


@login_required
@readiness_feature_required
@require_POST
def stage3_resume(request, session_uuid):
    """Safely resumes a PAUSED_OUTAGE Stage 3 session and retries follow-up generation."""
    session = get_object_or_404(AssessmentSession, session_uuid=session_uuid)
    denied = _check_stage3_access_or_403(request, session)
    if denied:
        return denied

    try:
        Stage3OwnershipEngine.resume_from_outage_and_recover(
            user=request.user,
            session_uuid=session.session_uuid,
        )
    except QuotaExhaustedError:
        ctx = _get_sidebar_context(request.user)
        ctx.update({
            'career_path': session.career_path,
            'quota': AssessmentSessionService.check_practice_daily_quota(request.user),
            'closed_reason': 'QUOTA_EXHAUSTED',
        })
        return render(request, 'interviews/stage3_closed.html', ctx, status=429)
    except InvalidStateTransitionError:
        pass
    return redirect('interviews:stage3_workbench', session_uuid=session.session_uuid)


@login_required
@readiness_feature_required
@require_POST
def stage3_retry_evaluation(request, session_uuid):
    """Retries evaluation for a Stage 3 session safely parked at PENDING_EVALUATION."""
    session = get_object_or_404(AssessmentSession, session_uuid=session_uuid)
    denied = _check_stage3_access_or_403(request, session)
    if denied:
        return denied

    try:
        res = Stage3OwnershipEvaluator.evaluate_and_finalize(
            user=request.user,
            session_uuid=session.session_uuid,
        )
        if res['status'] == 'EVALUATED':
            return redirect('interviews:stage3_result', session_uuid=session.session_uuid)
    except QuotaExhaustedError:
        ctx = _get_sidebar_context(request.user)
        ctx.update({
            'career_path': session.career_path,
            'quota': AssessmentSessionService.check_practice_daily_quota(request.user),
            'closed_reason': 'QUOTA_EXHAUSTED',
        })
        return render(request, 'interviews/stage3_closed.html', ctx, status=429)
    except (ValidationError, InvalidStateTransitionError):
        pass
    return redirect('interviews:stage3_workbench', session_uuid=session.session_uuid)


@login_required
@readiness_feature_required
@require_POST
def stage3_abandon(request, session_uuid):
    """Safely exits an open or paused Stage 3 session without cooldown or fail penalty."""
    with transaction.atomic():
        session = get_object_or_404(
            AssessmentSession.objects.select_for_update(),
            session_uuid=session_uuid,
        )
        denied = _check_stage3_access_or_403(request, session)
        if denied:
            return denied

        if session.status == 'EVALUATED':
            return redirect('interviews:stage3_result', session_uuid=session.session_uuid)

        if session.status in ('STAGE_3', 'PAUSED_OUTAGE', 'PENDING_EVALUATION'):
            session.status = 'CANCELLED'
            session.stage_expires_at = None
            session.cooldown_until = None
            session.save(update_fields=['status', 'stage_expires_at', 'cooldown_until', 'updated_at'])

    return redirect('interviews:readiness_history')


@login_required
@readiness_feature_required
def stage3_result(request, session_uuid):
    """
    Displays evidence-based Stage 3 hybrid evaluation, competency breakdown,
    contradictions/weak areas, turn transcript review, and roadmap remediation.
    """
    session = get_object_or_404(
        AssessmentSession.objects.select_related('career_path', 'blueprint'),
        session_uuid=session_uuid,
    )
    denied = _check_stage3_access_or_403(request, session)
    if denied:
        return denied

    if session.status != 'EVALUATED' or not hasattr(session, 'evaluation_result'):
        return redirect('interviews:stage3_workbench', session_uuid=session.session_uuid)

    stage_resp = get_object_or_404(AssessmentStageResponse, session=session, stage_number=3)
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

    turns = [
        {
            'turn_number': t.turn_number,
            'probe_type_label': str((t.turn_analysis or {}).get('probe_type', 'OWNERSHIP')).replace('_', ' ').title(),
            'interviewer_prompt': t.interviewer_prompt,
            'candidate_reply': t.candidate_reply,
            'replied_at': t.replied_at,
        }
        for t in AdaptiveInterviewTurn.objects.filter(session=session, replied_at__isnull=False).order_by('turn_number')
    ]

    ctx = _get_sidebar_context(request.user)
    ctx.update({
        'session': session,
        'career_path': session.career_path,
        'evaluation': eval_res,
        'evidence': eval_res.scoring_evidence or {},
        'competency_cards': competency_cards,
        'remediations': eval_res.remediation_recommendations or [],
        'candidate_task': BlueprintRegistryService.get_candidate_stage_context(stage_resp),
        'turns': turns,
        'quota': AssessmentSessionService.check_practice_daily_quota(request.user),
    })
    return render(request, 'interviews/stage3_result.html', ctx)
