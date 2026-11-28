"""
Hybrid Evidence-Based Evaluator for Stage 3 (Adaptive AI Ownership Defense).

Combines deterministic server-side behavioral/technical/consistency checks with
schema-validated AI rubric assessment through AssessmentAIService. Prevents AI
alone from awarding inflated scores on prompt injections, buzzwords, contradictions,
or empty replies, and preserves progress under PENDING_EVALUATION on AI outages.
"""

import re
from decimal import Decimal
from typing import Any, Dict, List, Set

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.interviews.models import (
    AdaptiveInterviewTurn,
    AssessmentSession,
    AssessmentStageResponse,
    CompetencyScore,
    EvaluationResult,
)
from apps.interviews.services.ai_gateway import (
    AIGatewayOutageError,
    AISchemaValidationError,
    AssessmentAIService,
)
from apps.interviews.services.assessment_session import (
    AssessmentSessionService,
    InvalidStateTransitionError,
    StageLockedError,
)
from apps.interviews.services.stage3_engine import QuotaExhaustedError, Stage3OwnershipEngine
from apps.roadmaps.models import Topic


WORD_RE = re.compile(r'[a-zA-Z0-9_+#^/-]+')

REASONING_MARKERS = [
    'because', 'therefore', 'since', 'trade-off', 'tradeoff', 'instead of',
    'at the cost', 'mitigat', 'fallback', 'rollback', 'monitor', 'alert',
    'atomic', 'idempoten', 'timeout', 'circuit', 'ttl', 'reconcil',
    'fail closed', 'fail-closed', 'cursor', 'stream', 'dlq', 'dead-letter',
    'lock', 'queue', 'latency', 'p95', 'isolation', 'invariant',
]

TESTING_MARKERS = [
    'test', 'assert', 'mock', 'pytest', 'integration', 'load test', 'replay',
    'concurren', 'fault', 'timeout test', 'boundary', 'edge case', 'regression',
]

LIMITATION_HONESTY_MARKERS = [
    'limit', 'limitation', 'risk', 'assumption', 'would check', 'would verify',
    'would measure', 'monitor', 'trade-off', 'tradeoff', 'uncertain', 'depends on',
    'stale', 'lag',
]

BUZZWORD_ONLY_TERMS = [
    'microservices', 'blockchain', 'web3', 'kubernetes magic', 'ai magic',
    'serverless fixes everything', 'service mesh fixes all',
]


class Stage3OwnershipEvaluator:
    """Server-authoritative hybrid evaluator for Stage 3 Ownership Defense."""

    ENGINE_VERSION = 'stage3-eval-2026.1'
    PASSING_BENCHMARK = 70.0

    @classmethod
    def _term_matches(cls, term: str, text_lower: str) -> bool:
        t = (term or '').lower().strip()
        if not t:
            return False
        if any(ch in t for ch in ('(', ')', '<', '>', '=', '.', '/', '+', '^', ' ', '-')):
            return t in text_lower
        return bool(re.search(r'(?<![a-z0-9_])' + re.escape(t), text_lower))

    @classmethod
    def _is_low_entropy(cls, text: str) -> bool:
        cleaned = (text or '').strip()
        if len(cleaned) < 12:
            return True
        tokens = WORD_RE.findall(cleaned.lower())
        if len(tokens) < 4:
            return True
        diversity = len(set(tokens)) / max(1, len(tokens))
        if len(tokens) >= 12 and diversity < 0.28:
            return True
        max_char_ratio = max(cleaned.count(ch) for ch in set(cleaned)) / len(cleaned)
        return len(cleaned) > 25 and max_char_ratio > 0.45

    @classmethod
    def analyze_deterministic_signals(
        cls,
        turns: List[AdaptiveInterviewTurn],
        snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Computes reproducible, length-neutral deterministic defense metrics from
        the candidate's answered turns. Honest limitation acknowledgment is treated
        positively (never over-penalized).
        """
        answered = [t for t in turns if t.replied_at is not None]
        grading_meta = snapshot.get('grading_meta') or {}
        red_flags = grading_meta.get('technical_red_flags') or []
        comp_weights = grading_meta.get('competency_weights') or {
            'se_ownership': 0.35,
            'se_architecture': 0.30,
            'se_testing': 0.20,
            'se_diagnosis': 0.15,
        }

        combined_replies = "\n".join((t.candidate_reply or '') for t in answered)
        combined_lower = combined_replies.lower()

        injection_hits = 0
        low_entropy_turns = 0
        repeated_turns = 0
        seen_normalized_replies: Set[str] = set()
        triggered_red_flags: List[str] = []
        contradiction_flags: List[str] = []

        for t in answered:
            analysis = t.turn_analysis or {}
            injection_hits += len(analysis.get('injection_flags') or [])
            norm_reply = re.sub(r'\s+', ' ', (t.candidate_reply or '').strip().lower())
            if cls._is_low_entropy(t.candidate_reply):
                low_entropy_turns += 1
            elif norm_reply in seen_normalized_replies:
                repeated_turns += 1
            if norm_reply:
                seen_normalized_replies.add(norm_reply)

        for rf in red_flags:
            if cls._term_matches(rf, combined_lower):
                triggered_red_flags.append(rf)

        # Basic cross-turn contradiction detection (e.g. strict fail-closed auth vs failing open auth)
        if (
            cls._term_matches('fail closed', combined_lower) or cls._term_matches('zero unauthorized', combined_lower)
        ) and (
            cls._term_matches('fail open', combined_lower) or cls._term_matches('return true on exception', combined_lower)
        ):
            contradiction_flags.append(
                "Contradicted zero-unauthorized/fail-closed requirement by suggesting fail-open bypass."
            )

        if (
            cls._term_matches('nothing changes', combined_lower) or cls._term_matches('keep synchronous lock', combined_lower)
        ) and (
            cls._term_matches('async queue', combined_lower) or cls._term_matches('redis reservation', combined_lower)
        ):
            contradiction_flags.append(
                "Contradicted earlier synchronous locking claim while introducing async reservation changes."
            )

        reasoning_hits = sum(1 for m in REASONING_MARKERS if cls._term_matches(m, combined_lower))
        testing_hits = sum(1 for m in TESTING_MARKERS if cls._term_matches(m, combined_lower))
        honesty_hits = sum(1 for m in LIMITATION_HONESTY_MARKERS if cls._term_matches(m, combined_lower))
        buzzword_hits = sum(1 for b in BUZZWORD_ONLY_TERMS if cls._term_matches(b, combined_lower))

        answered_count = len(answered)
        substantive_turns = max(0, answered_count - low_entropy_turns - repeated_turns)
        engagement_ratio = substantive_turns / max(1, min(3, answered_count or 1))

        # Length-neutral mechanistic scoring (concise accurate replies achieve full credit easily)
        mech_pct = min(100.0, (reasoning_hits / 5.0) * 100.0) if substantive_turns > 0 else 0.0
        test_pct = min(100.0, (testing_hits / 3.0) * 100.0) if substantive_turns > 0 else 0.0
        if testing_hits < 2:
            test_pct = min(test_pct, 48.0)

        honesty_bonus = 8.0 if honesty_hits >= 1 else 0.0
        penalty = (
            (18.0 * len(triggered_red_flags))
            + (15.0 * len(contradiction_flags))
            + (15.0 * repeated_turns)
            + (22.0 if injection_hits > 0 else 0.0)
            + (25.0 if (buzzword_hits >= 2 and reasoning_hits <= 1) else 0.0)
        )

        def clamp(val: float) -> float:
            return round(max(0.0, min(100.0, val)), 2)

        det_scores = {
            'se_ownership': clamp(((0.60 * mech_pct + 0.40 * (engagement_ratio * 100.0) + honesty_bonus) - penalty)),
            'se_architecture': clamp((0.75 * mech_pct + 0.25 * (engagement_ratio * 100.0)) - penalty),
            'se_testing': clamp(test_pct - (penalty * 0.5)),
            'se_diagnosis': clamp((0.70 * mech_pct + 0.30 * (engagement_ratio * 100.0)) - penalty),
        }

        # Guardrail against hollow fluent prose ("because servers work..."), prompt injection, and repeated-paste exploits
        if reasoning_hits < 2:
            det_scores = {k: min(v, 30.0) for k, v in det_scores.items()}
        if repeated_turns >= 1:
            det_scores = {k: min(v, 32.0) for k, v in det_scores.items()}
        if injection_hits > 0:
            det_scores = {k: min(v, 25.0) for k, v in det_scores.items()}
        if low_entropy_turns == answered_count and answered_count > 0:
            det_scores = {k: 5.0 for k in det_scores}

        det_weighted = round(
            sum(float(comp_weights.get(code, 0.25)) * det_scores.get(code, 0.0) for code in comp_weights),
            2,
        )

        return {
            'engine_version': cls.ENGINE_VERSION,
            'answered_turns': answered_count,
            'substantive_turns': substantive_turns,
            'low_entropy_turns': low_entropy_turns,
            'repeated_turns': repeated_turns,
            'reasoning_hits': reasoning_hits,
            'testing_hits': testing_hits,
            'acknowledges_limitations': honesty_hits >= 1,
            'injection_attempts': injection_hits,
            'triggered_red_flags': triggered_red_flags,
            'contradiction_flags': contradiction_flags,
            'det_scores': det_scores,
            'det_weighted': det_weighted,
            'comp_weights': comp_weights,
        }

    @classmethod
    def _validate_eval_schema(cls, raw: Any, required_codes: Set[str]) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise AISchemaValidationError("Evaluation payload must be a JSON object.")

        assessments = raw.get('competency_assessments')
        if not isinstance(assessments, dict):
            raise AISchemaValidationError("Missing competency_assessments dictionary.")

        cleaned_assessments: Dict[str, Dict[str, Any]] = {}
        for code in required_codes:
            entry = assessments.get(code)
            if not isinstance(entry, dict):
                raise AISchemaValidationError(f"Missing evaluation entry for competency '{code}'.")
            try:
                score_val = float(entry.get('score'))
            except (TypeError, ValueError) as exc:
                raise AISchemaValidationError(f"Invalid score for '{code}'.") from exc
            if not (0.0 <= score_val <= 100.0):
                raise AISchemaValidationError(f"Score out of [0, 100] bounds for '{code}'.")
            reasoning = str(entry.get('reasoning', '') or '').strip()[:600]
            if not reasoning:
                raise AISchemaValidationError(f"Empty reasoning for '{code}'.")
            quotes = [
                str(q)[:260]
                for q in (entry.get('evidence_quotes') or [])
                if isinstance(q, str) and q.strip()
            ][:4]
            cleaned_assessments[code] = {
                'score': round(score_val, 2),
                'reasoning': reasoning,
                'evidence_quotes': quotes,
            }

        try:
            conf = max(0.0, min(1.0, float(raw.get('evaluator_confidence', 0.75))))
        except (TypeError, ValueError) as exc:
            raise AISchemaValidationError("Invalid evaluator_confidence.") from exc

        overall_summary = str(raw.get('overall_summary', '') or '').strip()[:700]
        if not overall_summary:
            raise AISchemaValidationError("Missing overall_summary.")

        return {
            'competency_assessments': cleaned_assessments,
            'strengths': [str(x)[:260] for x in (raw.get('strengths') or []) if isinstance(x, str) and x.strip()][:5],
            'weak_areas': [str(x)[:260] for x in (raw.get('weak_areas') or []) if isinstance(x, str) and x.strip()][:5],
            'contradictions_detected': [str(x)[:260] for x in (raw.get('contradictions_detected') or []) if isinstance(x, str) and x.strip()][:4],
            'testing_feedback': [str(x)[:260] for x in (raw.get('testing_feedback') or []) if isinstance(x, str) and x.strip()][:4],
            'overall_summary': overall_summary,
            'evaluator_confidence': conf,
        }

    @classmethod
    def _resolve_topic_ids(cls, career_path_id: int, cdef: Dict[str, Any]) -> List[int]:
        ids = list(cdef.get('resolved_topic_ids') or [])
        if ids:
            return ids
        refs = cdef.get('remediation_topic_refs') or []
        if not refs:
            return []
        topics = Topic.objects.filter(milestone__career_path_id=career_path_id).select_related('milestone')
        by_key = {(t.milestone.week_number, t.title.strip().lower()): t.id for t in topics}
        by_title = {t.title.strip().lower(): t.id for t in topics}
        resolved = []
        for ref in refs:
            wk = int(ref.get('week_number', 0))
            title = str(ref.get('title', '')).strip().lower()
            tid = by_key.get((wk, title)) or by_title.get(title)
            if tid:
                resolved.append(tid)
        return resolved

    @classmethod
    def evaluate_and_finalize(
        cls,
        *,
        user,
        session_uuid,
    ) -> Dict[str, Any]:
        """
        Executes hybrid evaluation (deterministic ownership checks + AI structured rubric
        via AssessmentAIService operation='PRACTICE_EVAL') and commits EvaluationResult.
        Parks cleanly on PENDING_EVALUATION without false pass/fail on provider outage.
        """
        with transaction.atomic():
            session = AssessmentSession.objects.select_for_update().filter(
                session_uuid=session_uuid
            ).first()
            if not session:
                raise ValidationError("Assessment session not found.")
            if session.user_id != user.id:
                raise PermissionDenied("You do not have permission to access this session.")
            if session.mode != 'PRACTICE' or session.practice_scope != 'STAGE_3':
                raise ValidationError("This evaluator only grades Stage 3 Practice sessions.")
            if session.status == 'EVALUATED' and hasattr(session, 'evaluation_result'):
                return {'status': 'EVALUATED', 'evaluation': session.evaluation_result}
            if session.status not in {'STAGE_3', 'SUBMITTED', 'EVALUATING', 'PENDING_EVALUATION'}:
                raise InvalidStateTransitionError(f"Cannot evaluate from status '{session.status}'.")

            turns = list(
                AdaptiveInterviewTurn.objects.filter(session=session).order_by('turn_number')
            )
            min_turns, _ = Stage3OwnershipEngine.get_turn_bounds(session)
            answered = [t for t in turns if t.replied_at is not None]
            if len(answered) < min_turns:
                raise ValidationError(
                    f"Complete at least {min_turns} defense turns before requesting evaluation ({len(answered)} answered)."
                )

            stage_resp = AssessmentStageResponse.objects.get(session=session, stage_number=3)
            snapshot = stage_resp.task_snapshot or {}

        # Server quota enforcement (PRACTICE_EVAL consumes 1 daily unit out of 8/24h)
        quota = AssessmentSessionService.check_practice_daily_quota(user)
        if not quota['allowed']:
            raise QuotaExhaustedError("Daily 24-hour AI practice quota reached.")

        det = cls.analyze_deterministic_signals(turns, snapshot)
        comp_weights = det['comp_weights']
        required_codes = set(comp_weights.keys())

        transcript_parts = []
        for t in answered:
            wrapped_ans, _ = AssessmentAIService.sanitize_candidate_text(t.candidate_reply, max_chars=2000)
            transcript_parts.append(
                f"Turn {t.turn_number} ({(t.turn_analysis or {}).get('probe_type', 'PROBE')}):\n"
                f"Question: {t.interviewer_prompt}\n"
                f"Reply:\n{wrapped_ans}"
            )

        rubric_defs = session.rubric_snapshot or session.blueprint.competency_definitions
        comp_meta = {c['code']: c for c in rubric_defs}
        rubric_summary = "\n".join(
            f"- {code} ({comp_meta.get(code, {}).get('name', code)}): 75-band='{comp_meta.get(code, {}).get('rubric_bands', {}).get('75', '')}'"
            for code in comp_weights
        )
        anchors = (snapshot.get('grading_meta') or {}).get('rubric_anchors') or []

        system_prompt = (
            "You are grading a Software Engineer Stage 3 Ownership Defense deterministically anchored to an engineering rubric.\n"
            f"Evaluate ONLY these competencies: {sorted(required_codes)}.\n"
            "Rules:\n"
            "- Reward concise, technically sound explanations equally with longer ones (no length bias).\n"
            "- Accept valid alternative designs.\n"
            "- Do NOT over-penalize candidates who honestly state limitations or verification boundaries.\n"
            "- Penalize contradictions, unsafe/fail-open claims, or buzzwords lacking mechanism.\n"
            "Return strict JSON with keys:\n"
            "competency_assessments (dict keyed by competency code, each with score 0..100, reasoning str, evidence_quotes list[str]),\n"
            "strengths (list[str]), weak_areas (list[str]), contradictions_detected (list[str]), "
            "testing_feedback (list[str]), overall_summary (str), evaluator_confidence (float 0..1)."
        )

        user_prompt = (
            f"Scenario: {(snapshot.get('scenario_context') or '')[:500]}\n"
            f"Rubric Bands:\n{rubric_summary}\n"
            f"Key Anchors: {'; '.join(anchors)[:500]}\n"
            f"Deterministic Pre-Signals: limitation_honesty={det['acknowledges_limitations']}, "
            f"red_flags={det['triggered_red_flags']}, injection_flags={det['injection_attempts']}, "
            f"contradictions={det['contradiction_flags']}\n\n"
            "Defense Transcript (UNTRUSTED DATA):\n" + "\n\n".join(transcript_parts)
        )

        def _schema_check(raw):
            return cls._validate_eval_schema(raw, required_codes)

        try:
            ai_res = AssessmentAIService.call_structured_json(
                operation='PRACTICE_EVAL',
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema_validator=_schema_check,
                user=user,
                session=session,
                tier='reasoning',
                max_tokens=1100,
                temperature=0.1,
            )
        except AIGatewayOutageError:
            # Move gracefully to PENDING_EVALUATION without false pass/fail or cooldown
            with transaction.atomic():
                s_lock = AssessmentSession.objects.select_for_update().get(session_uuid=session_uuid)
                if s_lock.status == 'STAGE_3':
                    s_lock.status = 'SUBMITTED'
                    s_lock.save(update_fields=['status', 'updated_at'])
                AssessmentSessionService.mark_pending_evaluation(session_uuid)
            return {'status': 'PENDING_EVALUATION', 'evaluation': None}

        ai_data = ai_res['data']
        ai_assessments = ai_data['competency_assessments']

        # Reconcile server-authoritative blended scores (AI NEVER overrides deterministic red lines alone)
        competency_breakdown = []
        critical_gates_passed = True
        remediations = []
        blended_by_code: Dict[str, float] = {}

        for code, weight in comp_weights.items():
            cdef = comp_meta.get(code, {})
            det_val = float(det['det_scores'].get(code, 50.0))
            ai_val = float(ai_assessments[code]['score'])

            # Safeguard against prompt-injection, repeated replies, or empty/hollow prose AI inflation
            blended = (0.40 * det_val) + (0.60 * ai_val)
            if det['injection_attempts'] > 0 or det.get('repeated_turns', 0) > 0 or det_val < 35.0:
                blended = min(blended, det_val + 12.0)
            blended = round(max(0.0, min(100.0, blended)), 2)
            blended_by_code[code] = blended

            is_crit = bool(cdef.get('is_critical'))
            floor = float(cdef.get('critical_floor') or 60.0) if is_crit else None
            gate_ok = (blended >= floor) if (is_crit and floor is not None) else True
            if not gate_ok:
                critical_gates_passed = False

            topic_ids = cls._resolve_topic_ids(session.career_path_id, cdef)
            topics = list(
                Topic.objects.filter(id__in=topic_ids).select_related('milestone').order_by('milestone__week_number', 'order')
            ) if topic_ids else []

            comp_entry = {
                'code': code,
                'name': cdef.get('name', code),
                'weight': float(weight),
                'is_critical': is_crit,
                'critical_floor': floor,
                'score': blended,
                'deterministic_score': round(det_val, 2),
                'ai_score': round(ai_val, 2),
                'gate_passed': gate_ok,
                'reasoning': ai_assessments[code]['reasoning'],
                'evidence_quotes': ai_assessments[code]['evidence_quotes'],
                'recommended_topic_ids': [t.id for t in topics],
                'recommended_topics': [
                    {
                        'id': t.id,
                        'title': t.title,
                        'week_number': t.milestone.week_number,
                        'milestone_title': t.milestone.title,
                        'resource_url': t.resource_url,
                    }
                    for t in topics
                ],
                'rubric_criteria_snapshot': cdef.get('rubric_bands', {}),
            }
            competency_breakdown.append(comp_entry)

            if blended < cls.PASSING_BENCHMARK or not gate_ok:
                remediations.append({
                    'competency_code': code,
                    'competency_name': comp_entry['name'],
                    'score': blended,
                    'gate_passed': gate_ok,
                    'action': ai_assessments[code]['reasoning'],
                    'topics': comp_entry['recommended_topics'],
                })

        weighted_total = round(
            sum(float(comp_weights[c]) * blended_by_code[c] for c in comp_weights),
            2,
        )
        passed_flag = (weighted_total >= cls.PASSING_BENCHMARK) and critical_gates_passed

        combined_contradictions = list(dict.fromkeys(
            det['contradiction_flags'] + ai_data['contradictions_detected']
        ))

        with transaction.atomic():
            s_lock = AssessmentSession.objects.select_for_update().get(session_uuid=session_uuid)
            if s_lock.status == 'EVALUATED' and hasattr(s_lock, 'evaluation_result'):
                return {'status': 'EVALUATED', 'evaluation': s_lock.evaluation_result}

            if s_lock.status == 'STAGE_3':
                AssessmentSessionService.lock_and_advance_stage(
                    user=user,
                    session_uuid=s_lock.session_uuid,
                    stage_number=3,
                    final_payload={'answered_turns': len(answered), 'completed': True},
                    deterministic_result=det,
                )
            else:
                sr_lock = AssessmentStageResponse.objects.select_for_update().get(session=s_lock, stage_number=3)
                sr_lock.deterministic_result = det
                sr_lock.is_locked = True
                sr_lock.save(update_fields=['deterministic_result', 'is_locked'])

            score_dec = Decimal(str(weighted_total))
            AssessmentSessionService.finalize_evaluation_outcome(
                s_lock.session_uuid,
                overall_score=score_dec,
                passed=passed_flag,
            )

            eval_record = EvaluationResult.objects.create(
                session=s_lock,
                blueprint_version=s_lock.blueprint_version,
                task_bank_version=s_lock.task_bank_version,
                scoring_mode='AI_PRACTICE',
                weighted_total=score_dec,
                passing_threshold=Decimal('70.00'),
                stage_scores={'3': weighted_total},
                critical_gates_passed=critical_gates_passed,
                passed=passed_flag,
                evaluator_confidence=ai_data['evaluator_confidence'],
                borderline_cross_checked=False,
                needs_human_review=False,
                rubric_snapshot=s_lock.rubric_snapshot or [],
                scoring_evidence={
                    'overall_summary': ai_data['overall_summary'],
                    'strengths': ai_data['strengths'],
                    'weak_areas': ai_data['weak_areas'],
                    'contradictions_detected': combined_contradictions,
                    'testing_feedback': ai_data['testing_feedback'],
                    'deterministic_signals': det,
                    'answered_turns': len(answered),
                },
                raw_ai_output=ai_data,
                remediation_recommendations=remediations,
                audit_trail={
                    'engine': 'Stage3OwnershipEvaluator',
                    'engine_version': cls.ENGINE_VERSION,
                    'provider': ai_res['provider'],
                    'model': ai_res['model'],
                    'usage': ai_res.get('usage', {}),
                    'code_executed': False,
                },
            )

            for comp in competency_breakdown:
                CompetencyScore.objects.create(
                    evaluation=eval_record,
                    user=user,
                    career_path=s_lock.career_path,
                    competency_code=comp['code'],
                    competency_name=comp['name'],
                    weight=Decimal(str(comp['weight'])),
                    is_critical=comp['is_critical'],
                    critical_floor=Decimal(str(comp['critical_floor'])) if comp['critical_floor'] is not None else None,
                    score=Decimal(str(comp['score'])),
                    deterministic_score=Decimal(str(comp['deterministic_score'])),
                    ai_score=Decimal(str(comp['ai_score'])),
                    gate_passed=comp['gate_passed'],
                    rubric_criteria_snapshot=comp['rubric_criteria_snapshot'],
                    reasoning=comp['reasoning'],
                    evidence_quotes=comp['evidence_quotes'],
                    recommended_topic_ids=comp['recommended_topic_ids'],
                )

            return {'status': 'EVALUATED', 'evaluation': eval_record}
