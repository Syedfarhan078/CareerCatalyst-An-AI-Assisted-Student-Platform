"""
Deterministic Stage 2 (Engineering Scenario & Constraint Pivot) Evaluator.

Produces versioned, reproducible, explainable, provider-independent evaluations
for Stage 2 Practice Mode without invoking external LLMs or consuming daily AI
practice quota. Supports technically valid architectural alternatives without
rewarding length or ungrounded buzzwords.
"""

import re
from decimal import Decimal
from typing import Any, Dict, List, Set, Tuple

from django.db import transaction

from apps.interviews.models import (
    AssessmentSession,
    AssessmentStageResponse,
    CompetencyScore,
    EvaluationResult,
)
from apps.interviews.services.assessment_session import AssessmentSessionService
from apps.roadmaps.models import Topic


WORD_RE = re.compile(r'[a-zA-Z0-9_+#^/-]+')

TRADEOFF_TENSION_TERMS = [
    'trade-off', 'tradeoff', 'at the cost of', 'at the expense of',
    'downside', 'overhead', 'complexity', 'versus', ' vs ', 'instead of',
    'compromise', 'sacrific', 'stale', 'lag', 'eventual', 'cost',
    'latency', 'consistency', 'availability', 'false positive', 'backpressure',
]

DISMISSAL_TRADEOFF_TERMS = [
    'no trade-off', 'no tradeoff', 'no downside', 'zero downside',
    'no negatives', 'solves everything', 'perfect solution',
]

NO_CHANGE_PATTERNS = [
    'no change', 'nothing changes', 'keep everything the same',
    'stay identical', 'no changes needed', 'keep phase a unchanged',
]


class Stage2DeterministicEvaluator:
    """
    Versioned, deterministic two-phase Scenario & Constraint Pivot evaluator.
    Scores baseline coherence, constraint adaptation across valid alternatives,
    trade-off balance, risk/assumption hygiene, and validation rigor.
    """

    ENGINE_VERSION = 'stage2-eval-2026.1'
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
    def _is_low_entropy(cls, text: str, min_chars: int = 15, min_tokens: int = 4) -> bool:
        cleaned = (text or '').strip()
        if len(cleaned) < min_chars:
            return True
        tokens = WORD_RE.findall(cleaned.lower())
        if len(tokens) < min_tokens:
            return True
        diversity = len(set(tokens)) / max(1, len(tokens))
        if len(tokens) >= 12 and diversity < 0.28:
            return True
        max_char_ratio = max(cleaned.count(ch) for ch in set(cleaned)) / len(cleaned)
        if len(cleaned) > 25 and max_char_ratio > 0.45:
            return True
        return False

    @classmethod
    def _is_prompt_echo(cls, candidate_text: str, prompt_sources: List[str]) -> bool:
        cand_norm = re.sub(r'\s+', ' ', (candidate_text or '').strip().lower())
        if len(cand_norm) < 40:
            return False
        for src in prompt_sources:
            src_norm = re.sub(r'\s+', ' ', (src or '').strip().lower())
            if len(src_norm) >= 40 and cand_norm in src_norm:
                return True
        cand_tokens = set(WORD_RE.findall(cand_norm))
        if len(cand_tokens) < 8:
            return False
        combined_src = " ".join(prompt_sources).lower()
        src_tokens = set(WORD_RE.findall(combined_src))
        if not src_tokens:
            return False
        # Pure copy-paste has high subset ratio with minimal original candidate reasoning
        overlap_ratio = len(cand_tokens & src_tokens) / max(1, len(cand_tokens))
        return overlap_ratio >= 0.85 and len(cand_tokens) >= 7

    @classmethod
    def _token_overlap_ratio(cls, text_a: str, text_b: str) -> float:
        tok_a = set(WORD_RE.findall((text_a or '').lower()))
        tok_b = set(WORD_RE.findall((text_b or '').lower()))
        if not tok_a or not tok_b:
            return 0.0
        union = tok_a | tok_b
        return len(tok_a & tok_b) / max(1, len(union))

    @classmethod
    def _eval_concept_clusters(cls, text_lower: str, clusters: List[List[str]]) -> float:
        if not clusters:
            return 0.0
        hits = sum(
            1 for cluster in clusters
            if any(cls._term_matches(term, text_lower) for term in cluster)
        )
        return hits / len(clusters)

    @classmethod
    def _eval_adaptation_concept(cls, text_lower: str, concept: Dict[str, Any]) -> float:
        alt_paths = concept.get('alternative_paths') or []
        if not alt_paths:
            return 0.0
        best = 0.0
        for path_clusters in alt_paths:
            best = max(best, cls._eval_concept_clusters(text_lower, path_clusters))
        return best

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
    def evaluate_payload(
        cls,
        snapshot: Dict[str, Any],
        response_payload: Dict[str, Any],
        rubric_defs: List[Dict[str, Any]],
        career_path_id: int,
    ) -> Dict[str, Any]:
        """
        Pure, repeatable deterministic Stage 2 scorer.
        Evaluates Phase A baseline + Phase B pivot adaptation across valid alternatives,
        trade-offs, assumptions, failure risks, and testing strategy.
        """
        part_a_spec = snapshot.get('part_a') or {}
        part_b_spec = snapshot.get('part_b_twist') or {}
        grading_meta = snapshot.get('grading_meta') or {}

        prompt_sources = [
            str(part_a_spec.get('context', '')),
            str(part_a_spec.get('existing_design', '')),
            str(part_a_spec.get('prompt', '')),
            " ".join(part_a_spec.get('requirements', [])),
            str(part_b_spec.get('context', '')),
            str(part_b_spec.get('pivot_summary', '')),
            str(part_b_spec.get('prompt', '')),
            " ".join(part_b_spec.get('new_constraints', [])),
        ]

        part_a = response_payload.get('part_a') or {}
        part_b = response_payload.get('part_b') or {}

        init_approach = str(part_a.get('initial_approach', '') or '').strip()
        init_data = str(part_a.get('data_and_concurrency', '') or '').strip()
        init_tradeoffs = str(part_a.get('initial_tradeoffs', '') or '').strip()
        init_combined = f"{init_approach} {init_data} {init_tradeoffs}".strip()
        init_lower = init_combined.lower()

        upd_approach = str(part_b.get('updated_approach', '') or '').strip()
        what_changes = str(part_b.get('what_changes', '') or '').strip()
        what_stays = str(part_b.get('what_stays_unchanged', '') or '').strip()
        tradeoffs_text = str(part_b.get('tradeoffs', '') or '').strip()
        risks_text = str(part_b.get('risks_and_failures', '') or '').strip()
        testing_text = str(part_b.get('validation_and_testing', '') or '').strip()

        phase_b_core_text = f"{upd_approach} {what_changes} {what_stays}".strip()
        phase_b_all_text = f"{phase_b_core_text} {tradeoffs_text} {risks_text} {testing_text}".strip()
        phase_b_core_lower = phase_b_core_text.lower()
        phase_b_all_lower = phase_b_all_text.lower()

        handled_well: List[str] = []
        missed_considerations: List[str] = []
        weak_tradeoff_reasoning: List[str] = []
        unsupported_assumptions: List[str] = []
        risks_not_considered: List[str] = []
        recommended_testing: List[str] = []

        # ------------------------------------------------------------------
        # 1. Phase A Baseline Coherence (0..100)
        # ------------------------------------------------------------------
        initial_concepts = grading_meta.get('initial_concepts') or []
        if cls._is_low_entropy(init_combined, min_chars=20, min_tokens=5) or cls._is_prompt_echo(init_combined, prompt_sources):
            init_score = 0.0
            missed_considerations.append("Initial Phase A design lacked concrete technical detail beyond repeating prompt context.")
        else:
            if initial_concepts:
                init_hits = [
                    cls._eval_concept_clusters(init_lower, c.get('concept_groups', []))
                    for c in initial_concepts
                ]
                init_concept_pct = (sum(init_hits) / len(initial_concepts)) * 100.0
            else:
                init_concept_pct = 75.0
            init_has_tradeoff = 100.0 if (
                not cls._is_low_entropy(init_tradeoffs, min_chars=15, min_tokens=4)
                and any(cls._term_matches(t, init_tradeoffs.lower()) for t in TRADEOFF_TENSION_TERMS)
            ) else 45.0
            init_score = (0.75 * init_concept_pct) + (0.25 * init_has_tradeoff)
            if init_score >= 70.0:
                handled_well.append("Phase A baseline established sound data/concurrency and transaction boundaries.")

        # ------------------------------------------------------------------
        # 2. Pivot Adaptation Concepts (0..100, alternative architectural paths)
        # ------------------------------------------------------------------
        adaptation_concepts = grading_meta.get('adaptation_concepts') or []
        total_adapt_weight = sum(float(c.get('weight', 0.25)) for c in adaptation_concepts) or 1.0
        weighted_adapt_sum = 0.0
        matched_adapt_count = 0
        missed_critical_adaptation: List[str] = []

        phase_b_echo = cls._is_low_entropy(phase_b_core_text, min_chars=20, min_tokens=5) or cls._is_prompt_echo(upd_approach, prompt_sources)
        if phase_b_echo:
            weighted_adapt_sum = 0.0
            for c in adaptation_concepts:
                missed_considerations.append(f"Missing adaptation: {c['label']}")
                if c.get('is_critical'):
                    missed_critical_adaptation.append(c['label'])
        else:
            for c in adaptation_concepts:
                w = float(c.get('weight', 0.25))
                search_target = (
                    f"{what_stays} {upd_approach}".lower()
                    if 'preserved' in c.get('id', '')
                    else phase_b_core_lower
                )
                ratio = cls._eval_adaptation_concept(search_target, c)
                # Award full concept credit cleanly on concise valid match (>= 0.85)
                concept_pts = 100.0 if ratio >= 0.85 else (ratio * 88.0)
                weighted_adapt_sum += w * concept_pts
                if ratio >= 0.65:
                    matched_adapt_count += 1
                    handled_well.append(f"Adapted well: {c['label']}")
                else:
                    missed_considerations.append(f"Missed pivot consideration: {c['label']}")
                    if c.get('is_critical'):
                        missed_critical_adaptation.append(c['label'])

        adapt_score = weighted_adapt_sum / total_adapt_weight

        # ------------------------------------------------------------------
        # 3. Pivot-Ignored & Repetition Check
        # ------------------------------------------------------------------
        similarity_to_part_a = cls._token_overlap_ratio(f"{init_approach} {init_data}", f"{upd_approach} {what_changes}")
        pivot_ignored = False
        if (similarity_to_part_a >= 0.72 and adapt_score < 55.0) or (
            any(cls._term_matches(p, f"{upd_approach} {what_changes}".lower()) for p in NO_CHANGE_PATTERNS)
            and adapt_score < 45.0
        ):
            pivot_ignored = True
            adapt_score = max(0.0, adapt_score - 25.0)
            weak_tradeoff_reasoning.append(
                "Final response largely repeats the Phase A baseline instead of adapting architecture to the new constraint."
            )

        # ------------------------------------------------------------------
        # 4. Decision Change vs Preserved Invariant Clarity
        # ------------------------------------------------------------------
        changes_ok = not (cls._is_low_entropy(what_changes, min_chars=12, min_tokens=3) or cls._is_prompt_echo(what_changes, prompt_sources))
        stays_ok = not (cls._is_low_entropy(what_stays, min_chars=12, min_tokens=3) or cls._is_prompt_echo(what_stays, prompt_sources))
        clarity_score = (55.0 if changes_ok else 0.0) + (45.0 if stays_ok else 0.0)
        if not changes_ok:
            missed_considerations.append("Did not clearly articulate what changes from the Phase A design.")
        if not stays_ok:
            missed_considerations.append("Did not identify which core invariants or components remain unchanged.")

        # ------------------------------------------------------------------
        # 5. Trade-off Balance & Anti-Buzzword Guard
        # ------------------------------------------------------------------
        tradeoff_score = 0.0
        tradeoff_lower = tradeoffs_text.lower()
        buzzwords = [b for b in grading_meta.get('buzzword_terms', []) if cls._term_matches(b, phase_b_all_lower)]

        if cls._is_low_entropy(tradeoffs_text, min_chars=15, min_tokens=4) or cls._is_prompt_echo(tradeoffs_text, prompt_sources):
            weak_tradeoff_reasoning.append("Trade-off section is empty, vague, or echoed without engineering tension analysis.")
            tradeoff_score = 0.0
        elif any(cls._term_matches(d, tradeoff_lower) for d in DISMISSAL_TRADEOFF_TERMS):
            weak_tradeoff_reasoning.append("Claims the adapted solution introduces zero downsides or trade-offs.")
            tradeoff_score = 15.0
        else:
            has_tension = any(cls._term_matches(t, tradeoff_lower) for t in TRADEOFF_TENSION_TERMS)
            domain_pairs = grading_meta.get('tradeoff_pairs') or []
            domain_hits = sum(
                1 for cluster in domain_pairs
                for term in cluster
                if cls._term_matches(term, tradeoff_lower)
            )
            if has_tension and domain_hits >= 2:
                tradeoff_score = 95.0
                handled_well.append("Articulated concrete engineering trade-offs (latency/consistency/complexity costs accepted).")
            elif has_tension or domain_hits >= 2:
                tradeoff_score = 72.0
                handled_well.append("Identified relevant trade-offs introduced by the requirement pivot.")
            else:
                tradeoff_score = 38.0
                weak_tradeoff_reasoning.append(
                    "Trade-off explanation lists components without weighing concrete downsides (consistency lag, complexity, cost, or blast radius)."
                )

        if len(buzzwords) >= 2 and matched_adapt_count == 0:
            weak_tradeoff_reasoning.append(
                f"Uses ungrounded buzzwords ({', '.join(buzzwords[:4])}) without linking them to the constraint bottleneck."
            )
            adapt_score = min(adapt_score, 25.0)
            tradeoff_score = min(tradeoff_score, 25.0)

        # ------------------------------------------------------------------
        # 6. Contradictions, Technical Traps & Unsupported Assumptions
        # ------------------------------------------------------------------
        penalty_arch = 0.0
        penalty_ownership = 0.0

        for chk in grading_meta.get('contradiction_checks') or []:
            hit_a = any(cls._term_matches(t, phase_b_all_lower) for t in chk.get('phrase_a', []))
            hit_b = any(cls._term_matches(t, phase_b_all_lower) for t in chk.get('phrase_b', []))
            if hit_a and hit_b:
                pen = float(chk.get('penalty', 12.0))
                penalty_arch += pen
                weak_tradeoff_reasoning.append(chk['message'])

        # Generic internal contradiction: claims nothing changes in `what_changes` while changing architecture in `updated_approach`
        if any(cls._term_matches(p, what_changes.lower()) for p in NO_CHANGE_PATTERNS) and matched_adapt_count >= 1:
            penalty_arch += 14.0
            weak_tradeoff_reasoning.append(
                "Contradiction: states nothing changes from baseline while modifying architecture in the updated approach."
            )

        for trap in grading_meta.get('technical_traps') or []:
            if any(cls._term_matches(t, phase_b_all_lower) for t in trap.get('pattern_terms', [])):
                pen = float(trap.get('penalty', 15.0))
                penalty_arch += pen
                penalty_ownership += (pen * 0.6)
                risks_not_considered.append(f"Technical error: {trap['message']}")
                weak_tradeoff_reasoning.append(trap['message'])

        for flag in grading_meta.get('assumption_red_flags') or []:
            if any(cls._term_matches(t, phase_b_all_lower) for t in flag.get('pattern_terms', [])):
                unsupported_assumptions.append(flag['message'])
                penalty_ownership += 8.0

        # ------------------------------------------------------------------
        # 7. Engineering Ownership: Risks, Failure Modes & Rollback (0..100)
        # ------------------------------------------------------------------
        risk_concepts = grading_meta.get('risk_concepts') or []
        risk_text_lower = f"{risks_text} {upd_approach}".lower()
        risk_matched_count = 0
        if cls._is_low_entropy(risks_text, min_chars=15, min_tokens=4) or cls._is_prompt_echo(risks_text, prompt_sources) or phase_b_echo:
            risk_coverage_pct = 0.0
            for rc in risk_concepts:
                risks_not_considered.append(rc['label'])
            unsupported_assumptions.append("No explicit failure modes, blast-radius bounds, or rollback signals documented.")
        else:
            for rc in risk_concepts:
                if any(cls._term_matches(t, risk_text_lower) for t in rc.get('terms', [])):
                    risk_matched_count += 1
                    handled_well.append(f"Risk & monitoring aware: {rc['label']}")
                else:
                    risks_not_considered.append(rc['label'])
            risk_coverage_pct = (risk_matched_count / max(1, len(risk_concepts))) * 100.0

        hygiene_score = max(0.0, 100.0 - (25.0 * len(unsupported_assumptions)))
        raw_ownership = (0.55 * risk_coverage_pct) + (0.25 * hygiene_score) + (0.20 * (100.0 if stays_ok else 0.0)) - penalty_ownership
        ownership_score = max(0.0, min(100.0, raw_ownership))

        # ------------------------------------------------------------------
        # 8. Testing & Validation Strategy (se_testing, CRITICAL GATE >= 60%)
        # ------------------------------------------------------------------
        testing_expectations = grading_meta.get('testing_expectations') or []
        # Evaluate testing strictly against validation_and_testing so un-tested risk confessions do not score as tests
        test_search_lower = testing_text.lower()
        test_matched: List[str] = []
        test_missed: List[str] = []

        if cls._is_low_entropy(testing_text, min_chars=15, min_tokens=4) or cls._is_prompt_echo(testing_text, prompt_sources):
            test_score = 0.0
            test_missed = [t['label'] for t in testing_expectations]
            recommended_testing = list(test_missed)
        else:
            for exp in testing_expectations:
                if any(cls._term_matches(t, test_search_lower) for t in exp.get('concepts', [])):
                    test_matched.append(exp['label'])
                else:
                    test_missed.append(exp['label'])
            test_score = (len(test_matched) / max(1, len(testing_expectations))) * 100.0
            if len(test_matched) < 2:
                # Critical gate safeguard: generic testing or single-scenario testing cannot hit 60%
                test_score = min(test_score, 48.0)
            if test_matched:
                handled_well.append(f"Validation plan covers {len(test_matched)} key test dimension(s).")
            recommended_testing = list(test_missed)

        # ------------------------------------------------------------------
        # 9. Compute Architecture Score & Critical Floor Safeguards
        # ------------------------------------------------------------------
        raw_arch = (
            (0.20 * init_score)
            + (0.50 * adapt_score)
            + (0.15 * tradeoff_score)
            + (0.15 * clarity_score)
            - penalty_arch
        )
        arch_score = max(0.0, min(100.0, raw_arch))
        if missed_critical_adaptation:
            # Missing all or any critical pivot mechanism prevents strong architecture score
            cap = 54.0 if len(missed_critical_adaptation) >= 2 else 66.0
            arch_score = min(arch_score, cap)
        if phase_b_echo:
            arch_score = min(arch_score, 15.0)
            ownership_score = min(ownership_score, 15.0)

        # ------------------------------------------------------------------
        # 10. Competency Breakdown & Weighted Total
        # ------------------------------------------------------------------
        stage2_weights = grading_meta.get('competency_weights') or {
            'se_architecture': 0.40,
            'se_ownership': 0.30,
            'se_testing': 0.30,
        }
        computed_scores = {
            'se_architecture': round(arch_score, 2),
            'se_ownership': round(ownership_score, 2),
            'se_testing': round(test_score, 2),
        }
        weighted_total = round(
            sum(float(stage2_weights[k]) * computed_scores[k] for k in stage2_weights),
            2,
        )

        comp_meta = {c['code']: c for c in rubric_defs}
        competency_breakdown = []
        remediations = []
        critical_gates_passed = True

        for code, weight in stage2_weights.items():
            cdef = comp_meta.get(code, {})
            score_val = computed_scores[code]
            is_crit = bool(cdef.get('is_critical'))
            floor = float(cdef.get('critical_floor') or 60.0) if is_crit else None
            gate_ok = (score_val >= floor) if (is_crit and floor is not None) else True
            if not gate_ok:
                critical_gates_passed = False

            topic_ids = cls._resolve_topic_ids(career_path_id, cdef)
            topics = list(
                Topic.objects.filter(id__in=topic_ids).select_related('milestone').order_by('milestone__week_number', 'order')
            ) if topic_ids else []

            if code == 'se_architecture':
                reasoning = (
                    f"Matched {matched_adapt_count} of {len(adaptation_concepts)} pivot adaptation mechanisms "
                    f"(baseline {round(init_score)}%, trade-off balance {round(tradeoff_score)}%)."
                )
                if pivot_ignored:
                    reasoning += " Penalty applied: repeated baseline without adapting to constraint shift."
                evidence_quotes = [m for m in handled_well if m.startswith("Adapted well") or m.startswith("Articulated")][:4]
            elif code == 'se_ownership':
                reasoning = f"Addressed {risk_matched_count} of {len(risk_concepts)} operational risk & monitoring controls."
                if unsupported_assumptions:
                    reasoning += f" {len(unsupported_assumptions)} unsupported assumption flag(s) identified."
                evidence_quotes = [m for m in handled_well if m.startswith("Risk")][:4]
            else:
                if not test_matched:
                    reasoning = "No concrete load, concurrency, fault-injection, or rollback validation scenarios specified."
                else:
                    reasoning = f"Covered {len(test_matched)} of {len(testing_expectations)} validation scenarios."
                evidence_quotes = [f"Test coverage: {t}" for t in test_matched]

            comp_entry = {
                'code': code,
                'name': cdef.get('name', code),
                'weight': float(weight),
                'is_critical': is_crit,
                'critical_floor': floor,
                'score': score_val,
                'gate_passed': gate_ok,
                'reasoning': reasoning,
                'evidence_quotes': evidence_quotes,
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

            if score_val < cls.PASSING_BENCHMARK or not gate_ok:
                action_text = reasoning
                if code == 'se_architecture' and missed_considerations:
                    action_text += f" Priority gap: {missed_considerations[0]}"
                elif code == 'se_ownership' and risks_not_considered:
                    action_text += f" Priority risk gap: {risks_not_considered[0]}"
                elif code == 'se_testing' and recommended_testing:
                    action_text += f" Add verification for: {recommended_testing[0]}"
                remediations.append({
                    'competency_code': code,
                    'competency_name': comp_entry['name'],
                    'score': score_val,
                    'gate_passed': gate_ok,
                    'action': action_text,
                    'topics': comp_entry['recommended_topics'],
                })

        practice_benchmark_met = (weighted_total >= cls.PASSING_BENCHMARK) and critical_gates_passed

        return {
            'engine_version': cls.ENGINE_VERSION,
            'weighted_total': weighted_total,
            'practice_benchmark_met': practice_benchmark_met,
            'critical_gates_passed': critical_gates_passed,
            'subscores': {
                'phase_a_baseline': round(init_score, 2),
                'pivot_adaptation': round(adapt_score, 2),
                'tradeoff_balance': round(tradeoff_score, 2),
                'risk_awareness': round(risk_coverage_pct, 2),
                'validation_rigor': round(test_score, 2),
            },
            'pivot_ignored': pivot_ignored,
            'matched_adaptation_count': matched_adapt_count,
            'total_adaptation_count': len(adaptation_concepts),
            'handled_well': list(dict.fromkeys(handled_well)),
            'missed_considerations': list(dict.fromkeys(missed_considerations)),
            'weak_tradeoff_reasoning': list(dict.fromkeys(weak_tradeoff_reasoning)),
            'unsupported_assumptions': list(dict.fromkeys(unsupported_assumptions)),
            'risks_not_considered': list(dict.fromkeys(risks_not_considered)),
            'recommended_testing': list(dict.fromkeys(recommended_testing)),
            'test_matched': test_matched,
            'test_missed': test_missed,
            'competency_breakdown': competency_breakdown,
            'remediations': remediations,
        }

    @classmethod
    def submit_and_evaluate_stage2_practice(
        cls,
        *,
        user,
        session_uuid,
        part_b_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Atomically locks Stage 2 final response (preserving locked Part A),
        evaluates deterministically (0 AI calls, 0 code execution), persists
        EvaluationResult + CompetencyScore rows, and finalizes session.
        """
        with transaction.atomic():
            session = AssessmentSession.objects.select_for_update().filter(
                session_uuid=session_uuid
            ).first()
            if not session:
                raise ValueError("Assessment session not found.")
            if session.mode != 'PRACTICE' or session.practice_scope != 'STAGE_2':
                raise ValueError("This endpoint only evaluates Stage 2 Practice drills.")

            stage_resp = AssessmentStageResponse.objects.select_for_update().filter(
                session=session,
                stage_number=2,
            ).first()
            if not stage_resp:
                raise ValueError("Stage 2 snapshot missing.")
            if stage_resp.part_a_submitted_at is None:
                raise ValueError("Phase A must be completed before submitting Phase B.")

            existing_payload = dict(stage_resp.response_payload or {})
            locked_part_a = dict(existing_payload.get('part_a') or {})
            full_payload = {
                'part_a': locked_part_a,
                'part_b': dict(part_b_payload or {}),
            }

            eval_output = cls.evaluate_payload(
                snapshot=stage_resp.task_snapshot,
                response_payload=full_payload,
                rubric_defs=session.rubric_snapshot or session.blueprint.competency_definitions,
                career_path_id=session.career_path_id,
            )

            AssessmentSessionService.lock_and_advance_stage(
                user=user,
                session_uuid=session.session_uuid,
                stage_number=2,
                final_payload={'part_b': full_payload['part_b']},
                deterministic_result=eval_output,
            )

            score_dec = Decimal(str(eval_output['weighted_total']))
            passed_flag = bool(eval_output['practice_benchmark_met'])
            AssessmentSessionService.finalize_evaluation_outcome(
                session.session_uuid,
                overall_score=score_dec,
                passed=passed_flag,
            )

            eval_record = EvaluationResult.objects.create(
                session=session,
                blueprint_version=session.blueprint_version,
                task_bank_version=session.task_bank_version,
                scoring_mode='DETERMINISTIC_PRACTICE',
                weighted_total=score_dec,
                passing_threshold=Decimal('70.00'),
                stage_scores={'2': eval_output['weighted_total']},
                critical_gates_passed=eval_output['critical_gates_passed'],
                passed=passed_flag,
                evaluator_confidence=1.0,
                borderline_cross_checked=False,
                needs_human_review=False,
                rubric_snapshot=session.rubric_snapshot or [],
                scoring_evidence={
                    'subscores': eval_output['subscores'],
                    'pivot_ignored': eval_output['pivot_ignored'],
                    'matched_adaptation_count': eval_output['matched_adaptation_count'],
                    'total_adaptation_count': eval_output['total_adaptation_count'],
                    'handled_well': eval_output['handled_well'],
                    'missed_considerations': eval_output['missed_considerations'],
                    'weak_tradeoff_reasoning': eval_output['weak_tradeoff_reasoning'],
                    'unsupported_assumptions': eval_output['unsupported_assumptions'],
                    'risks_not_considered': eval_output['risks_not_considered'],
                    'recommended_testing': eval_output['recommended_testing'],
                    'test_matched': eval_output['test_matched'],
                    'test_missed': eval_output['test_missed'],
                },
                remediation_recommendations=eval_output['remediations'],
                audit_trail={
                    'engine': 'Stage2DeterministicEvaluator',
                    'engine_version': cls.ENGINE_VERSION,
                    'ai_quota_consumed': 0,
                    'code_executed': False,
                },
            )

            for comp in eval_output['competency_breakdown']:
                CompetencyScore.objects.create(
                    evaluation=eval_record,
                    user=user,
                    career_path=session.career_path,
                    competency_code=comp['code'],
                    competency_name=comp['name'],
                    weight=Decimal(str(comp['weight'])),
                    is_critical=comp['is_critical'],
                    critical_floor=Decimal(str(comp['critical_floor'])) if comp['critical_floor'] is not None else None,
                    score=Decimal(str(comp['score'])),
                    deterministic_score=Decimal(str(comp['score'])),
                    ai_score=None,
                    gate_passed=comp['gate_passed'],
                    rubric_criteria_snapshot=comp['rubric_criteria_snapshot'],
                    reasoning=comp['reasoning'],
                    evidence_quotes=comp['evidence_quotes'],
                    recommended_topic_ids=comp['recommended_topic_ids'],
                )

            return {
                'session': session,
                'evaluation': eval_record,
                'details': eval_output,
            }
