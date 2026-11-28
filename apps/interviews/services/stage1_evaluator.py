"""
Deterministic Stage 1 (Applied Diagnosis) Evaluator.

Produces reproducible, explainable, provider-independent competency evaluations
for Stage 1 Practice without calling an LLM or consuming AI practice quota.
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
from apps.interviews.services.stage1_ast import Stage1ASTAnalyzer
from apps.roadmaps.models import Topic


WORD_RE = re.compile(r'[a-zA-Z0-9_+#^/]+')


class Stage1DeterministicEvaluator:
    """
    Hybrid-ready deterministic scorer for Stage 1 code diagnosis & test planning.
    Never executes candidate code and never grants inflated scores for buzzwords.
    """

    MAX_FINDINGS = 12

    @classmethod
    def _parse_line_numbers(cls, line_ref: str) -> Set[int]:
        nums: Set[int] = set()
        raw = str(line_ref or '').strip()
        if not raw:
            return nums
        # Match ranges like 4-7 or 4..7
        for m in re.finditer(r'(\d+)\s*[-–.]+\s*(\d+)', raw):
            a, b = int(m.group(1)), int(m.group(2))
            if 1 <= a <= 500 and 1 <= b <= 500:
                lo, hi = min(a, b), max(a, b)
                if hi - lo <= 30:
                    nums.update(range(lo, hi + 1))
        # Match standalone numbers like "line 11" or "4, 7"
        for m in re.finditer(r'\b(\d+)\b', raw):
            val = int(m.group(1))
            if 1 <= val <= 500:
                nums.add(val)
        return nums

    @classmethod
    def _term_matches(cls, term: str, text_lower: str) -> bool:
        t = term.lower().strip()
        if not t:
            return False
        # Symbol/operator fragments use exact substring check; word/stem concepts require left word boundary
        if any(ch in t for ch in ('(', ')', '<', '>', '=', '.', '/', '+', '^', ' ')):
            return t in text_lower
        return bool(re.search(r'(?<![a-z0-9_])' + re.escape(t), text_lower))

    @classmethod
    def _is_low_entropy_or_echo(
        cls,
        text: str,
        artifact_code: str,
        fields: Tuple[str, ...] = (),
        prompt_sources: Tuple[str, ...] = (),
    ) -> bool:
        cleaned = (text or '').strip()
        if len(cleaned) < 12:
            return True
        tokens = WORD_RE.findall(cleaned.lower())
        if len(tokens) < 3:
            return True
        diversity = len(set(tokens)) / max(1, len(tokens))
        if len(tokens) >= 12 and diversity < 0.30:
            return True
        if cleaned:
            max_char_ratio = max(cleaned.count(ch) for ch in set(cleaned)) / len(cleaned)
            if len(cleaned) > 25 and max_char_ratio > 0.45:
                return True
        norm_code = re.sub(r'\s+', ' ', (artifact_code or '').strip().lower())
        norm_prompts = [re.sub(r'\s+', ' ', (p or '').strip().lower()) for p in prompt_sources if p]
        combined_prompt_tokens = set(WORD_RE.findall(" ".join(norm_prompts))) if norm_prompts else set()
        for candidate_str in (cleaned,) + fields:
            norm_part = re.sub(r'\s+', ' ', (candidate_str or '').strip().lower())
            if len(norm_part) > 30 and norm_code and norm_part in norm_code:
                return True
            if len(norm_part) >= 24 and any(norm_part in p for p in norm_prompts if len(p) >= 24):
                return True
            part_tokens = set(WORD_RE.findall(norm_part))
            if len(part_tokens) >= 12 and combined_prompt_tokens:
                overlap = len(part_tokens & combined_prompt_tokens) / max(1, len(part_tokens))
                if overlap >= 0.88:
                    return True
        return False

    @classmethod
    def _concept_coverage(cls, text_lower: str, clusters: List[List[str]]) -> float:
        if not clusters:
            return 0.0
        matched_clusters = 0
        for cluster in clusters:
            if any(cls._term_matches(term, text_lower) for term in cluster):
                matched_clusters += 1
        return matched_clusters / len(clusters)

    @classmethod
    def _score_finding_against_defect(
        cls,
        finding: Dict[str, Any],
        defect: Dict[str, Any],
        artifact_code: str,
        prompt_sources: Tuple[str, ...] = (),
    ) -> Dict[str, Any]:
        title = str(finding.get('title', '') or '')
        impact = str(finding.get('impact', '') or '')
        fix = str(finding.get('fix_suggestion', '') or '')
        category = str(finding.get('category', '') or '').strip().upper()
        severity = str(finding.get('severity', '') or '').strip().upper()
        line_ref = str(finding.get('line_ref', '') or '')

        combined_text = f"{title} {impact} {fix}".strip()
        if cls._is_low_entropy_or_echo(combined_text, artifact_code, fields=(title, impact), prompt_sources=prompt_sources):
            return {
                'affinity': 0.0,
                'root_cause_score': 0.0,
                'line_overlap': 0.0,
                'category_score': 0.0,
                'quality_score': 0.0,
                'low_entropy': True,
            }

        combined_lower = combined_text.lower()
        impact_lower = impact.lower()
        fix_lower = fix.lower()

        # 1. Line overlap (tolerance +/- 1 line for boundary interpretations)
        cand_lines = cls._parse_line_numbers(line_ref)
        target_lines = set(int(x) for x in defect.get('line_numbers', []))
        line_overlap = 0.0
        if cand_lines and target_lines:
            if cand_lines & target_lines:
                # Penalize gigantic line spam like "1-14" covering the whole file
                specificity = min(1.0, len(target_lines) / max(1, len(cand_lines)))
                line_overlap = round(0.6 + 0.4 * specificity, 3)
            else:
                expanded = set()
                for n in target_lines:
                    expanded.update((n - 1, n, n + 1))
                if cand_lines & expanded:
                    line_overlap = 0.45

        # 2. Category alignment
        if category == defect.get('category'):
            category_score = 1.0
        elif category in set(defect.get('accepted_categories', [])):
            category_score = 0.85
        else:
            category_score = 0.0

        # 3. Root-cause, impact, and fix concept coverage
        root_cause_score = cls._concept_coverage(combined_lower, defect.get('root_cause_concepts', []))
        impact_concepts = defect.get('impact_concepts', [])
        impact_hit = any(cls._term_matches(t, impact_lower) for t in impact_concepts) if impact_concepts else False
        impact_score = 1.0 if impact_hit else (0.4 if len(impact.split()) >= 8 and root_cause_score >= 0.5 else 0.0)

        fix_concepts = defect.get('fix_concepts', [])
        fix_hit = any(cls._term_matches(t, fix_lower) for t in fix_concepts) if fix_concepts else False
        fix_score = 1.0 if fix_hit else (0.35 if len(fix.split()) >= 6 and root_cause_score >= 0.5 else 0.0)

        # Require genuine concept grounding so blind line+category guessing cannot pass
        affinity = (
            0.55 * root_cause_score
            + 0.20 * line_overlap
            + 0.15 * category_score
            + 0.10 * impact_score
        )
        is_viable_match = (
            affinity >= 0.44
            and (root_cause_score >= 0.45 or (line_overlap >= 0.55 and impact_score >= 0.7))
        )

        # Severity accuracy
        target_sev = defect.get('severity', 'HIGH')
        if severity == target_sev:
            sev_pts = 10.0
        elif {severity, target_sev} <= {'CRITICAL', 'HIGH'} or {severity, target_sev} <= {'HIGH', 'MEDIUM'}:
            sev_pts = 6.0
        else:
            sev_pts = 2.0

        quality_score = min(
            100.0,
            (35.0 * root_cause_score)
            + (15.0 * line_overlap)
            + (10.0 * category_score)
            + sev_pts
            + (15.0 * impact_score)
            + (15.0 * fix_score),
        )
        # Naming a bug with neither production impact nor fix/mitigation cannot earn strong credit
        if impact_score == 0.0 and fix_score == 0.0:
            quality_score = min(quality_score, 48.0)

        return {
            'affinity': round(affinity, 3) if is_viable_match else 0.0,
            'root_cause_score': round(root_cause_score, 3),
            'line_overlap': round(line_overlap, 3),
            'category_score': round(category_score, 3),
            'impact_score': round(impact_score, 3),
            'fix_score': round(fix_score, 3),
            'quality_score': round(quality_score, 2),
            'low_entropy': False,
        }

    @classmethod
    def evaluate_payload(
        cls,
        snapshot: Dict[str, Any],
        response_payload: Dict[str, Any],
        rubric_defs: List[Dict[str, Any]],
        career_path_id: int,
    ) -> Dict[str, Any]:
        """
        Pure deterministic Stage 1 scorer. Side-effect free and repeatable.
        """
        grading_meta = snapshot.get('grading_meta', {})
        planted_defects = grading_meta.get('planted_defects', [])
        testing_expectations = grading_meta.get('testing_expectations', [])
        ast_invariants = grading_meta.get('ast_invariants', [])
        artifact_code = snapshot.get('artifact_code', '')
        prompt_sources = (
            str(snapshot.get('scenario', '') or ''),
            str(snapshot.get('prompt', '') or ''),
            str(snapshot.get('expected_behavior', '') or ''),
            " ".join(snapshot.get('requirements', []) or []),
            " ".join(snapshot.get('constraints', []) or []),
        ) + tuple(snapshot.get('requirements', []) or [])

        raw_findings = list(response_payload.get('findings') or [])[:cls.MAX_FINDINGS]
        proposed_fix_code = str(response_payload.get('proposed_fix_code') or '')
        test_plan = str(response_payload.get('test_plan') or '')
        overall_summary = str(response_payload.get('overall_summary') or '')

        # 1. Safe AST static analysis on optional proposed Python fix
        ast_report = Stage1ASTAnalyzer.analyze(proposed_fix_code, ast_invariants)

        # 2. Order-independent greedy matching of candidate findings to planted defects
        pair_candidates: List[Tuple[float, int, int, Dict[str, Any]]] = []
        low_entropy_findings: List[int] = []

        for f_idx, finding in enumerate(raw_findings):
            if not isinstance(finding, dict):
                continue
            scored_any_nonzero = False
            for d_idx, defect in enumerate(planted_defects):
                res = cls._score_finding_against_defect(finding, defect, artifact_code, prompt_sources=prompt_sources)
                if res.get('low_entropy'):
                    low_entropy_findings.append(f_idx)
                    break
                if res['affinity'] > 0:
                    scored_any_nonzero = True
                    pair_candidates.append((res['affinity'], f_idx, d_idx, res))

        pair_candidates.sort(key=lambda item: item[0], reverse=True)
        matched_f: Set[int] = set()
        matched_d: Dict[int, Dict[str, Any]] = {}

        for affinity, f_idx, d_idx, res in pair_candidates:
            if f_idx in matched_f or d_idx in matched_d:
                continue
            matched_f.add(f_idx)
            matched_d[d_idx] = {
                'finding_index': f_idx,
                'finding_title': raw_findings[f_idx].get('title', ''),
                'defect_id': planted_defects[d_idx]['id'],
                'defect_label': planted_defects[d_idx]['label'],
                'line_range': planted_defects[d_idx]['line_range'],
                'is_critical': bool(planted_defects[d_idx].get('is_critical')),
                'explanation': planted_defects[d_idx].get('explanation', ''),
                'metrics': res,
            }

        # Group secondary findings targeting an already-matched defect vs genuine false positives
        unsupported_findings: List[Dict[str, Any]] = []
        for f_idx, finding in enumerate(raw_findings):
            if f_idx in matched_f:
                continue
            # Check if it overlaps an already-matched defect (duplicate note) vs unrelated unsupported claim
            best_dup = 0.0
            for d_idx, defect in enumerate(planted_defects):
                if d_idx in matched_d:
                    chk = cls._score_finding_against_defect(finding, defect, artifact_code, prompt_sources=prompt_sources)
                    best_dup = max(best_dup, chk['affinity'])
            if best_dup < 0.44:
                unsupported_findings.append({
                    'finding_index': f_idx,
                    'title': finding.get('title', '(Untitled finding)'),
                    'category': finding.get('category', ''),
                    'line_ref': finding.get('line_ref', ''),
                    'reason': (
                        "Low-detail, echoed artifact snippet, or repetitive text."
                        if f_idx in low_entropy_findings
                        else "Did not substantiate a planted engineering defect with accurate mechanism or location."
                    ),
                })

        missed_defects: List[Dict[str, Any]] = []
        missed_critical_defects: List[Dict[str, Any]] = []
        weighted_defect_sum = 0.0
        total_defect_weight = sum(float(d.get('weight', 0.25)) for d in planted_defects) or 1.0

        for d_idx, defect in enumerate(planted_defects):
            w = float(defect.get('weight', 0.25))
            if d_idx in matched_d:
                weighted_defect_sum += w * matched_d[d_idx]['metrics']['quality_score']
            else:
                missed_info = {
                    'defect_id': defect['id'],
                    'label': defect['label'],
                    'line_range': defect['line_range'],
                    'category': defect['category'],
                    'severity': defect.get('severity', 'HIGH'),
                    'is_critical': bool(defect.get('is_critical')),
                    'explanation': defect.get('explanation', ''),
                }
                missed_defects.append(missed_info)
                if missed_info['is_critical']:
                    missed_critical_defects.append(missed_info)

        base_diagnosis_score = weighted_defect_sum / total_defect_weight
        false_positive_penalty = min(20.0, 6.0 * len(unsupported_findings))

        # Blend AST score conservatively (only +boost/audit when candidate submits Python code)
        if ast_report['status'] == 'VALID_AST' and ast_report['total_rules'] > 0:
            ast_pct = (ast_report['passed_rules'] / ast_report['total_rules']) * 100.0
            raw_diagnosis = (0.85 * base_diagnosis_score) + (0.15 * ast_pct) - false_positive_penalty
        elif ast_report['status'] in ('SYNTAX_ERROR', 'PATHOLOGICAL_AST', 'INPUT_TOO_LARGE'):
            raw_diagnosis = base_diagnosis_score - false_positive_penalty - 8.0
        else:
            raw_diagnosis = base_diagnosis_score - false_positive_penalty

        diagnosis_score = max(0.0, min(100.0, raw_diagnosis))
        if missed_critical_defects:
            # Strict critical competency floor enforcement: missing critical security/integrity flaws caps diagnosis <60%
            diagnosis_score = min(diagnosis_score, 54.0)

        # 3. Testing & Edge-Case Rigor (se_testing)
        combined_test_text = (
            f"{test_plan}\n"
            + " ".join(str(f.get('fix_suggestion', '')) for f in raw_findings if isinstance(f, dict))
        ).lower()
        test_matched: List[str] = []
        test_missed: List[str] = []
        if cls._is_low_entropy_or_echo(test_plan, artifact_code, prompt_sources=prompt_sources) and len(test_plan.strip()) > 0:
            test_score = 10.0
            test_missed = [t['label'] for t in testing_expectations]
        else:
            for exp in testing_expectations:
                if any(cls._term_matches(term, combined_test_text) for term in exp.get('concepts', [])):
                    test_matched.append(exp['label'])
                else:
                    test_missed.append(exp['label'])
            exp_total = max(1, len(testing_expectations))
            test_score = (len(test_matched) / exp_total) * 100.0
            # Floor safeguard: fewer than 2 distinct failure/edge cases cannot reach 60%
            if len(test_matched) < 2:
                test_score = min(test_score, 48.0)

        # 4. AI Output Verification (se_ai_critique)
        caught_ratio = len(matched_d) / max(1, len(planted_defects))
        precision_ratio = len(matched_d) / max(1, len(matched_d) + len(unsupported_findings))
        summary_bonus = 10.0 if (not cls._is_low_entropy_or_echo(overall_summary, artifact_code, prompt_sources=prompt_sources) and len(overall_summary.split()) >= 10) else 0.0
        ai_critique_score = min(
            100.0,
            max(0.0, (70.0 * caught_ratio) + (20.0 * precision_ratio) + summary_bonus),
        )

        # Map into Stage 1 Practice Competency Breakdown (normalized Stage 1 weights summing to 1.0)
        comp_meta = {c['code']: c for c in rubric_defs}
        stage1_weights = {
            'se_diagnosis': 0.45,
            'se_testing': 0.30,
            'se_ai_critique': 0.25,
        }
        computed_scores = {
            'se_diagnosis': round(diagnosis_score, 2),
            'se_testing': round(test_score, 2),
            'se_ai_critique': round(ai_critique_score, 2),
        }

        weighted_total = round(
            sum(stage1_weights[code] * computed_scores[code] for code in stage1_weights),
            2,
        )

        competency_breakdown = []
        critical_gates_passed = True
        remediations = []

        for code, weight in stage1_weights.items():
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

            reasoning = cls._build_competency_reasoning(
                code=code,
                matched_count=len(matched_d),
                total_defects=len(planted_defects),
                missed_critical=missed_critical_defects,
                unsupported_count=len(unsupported_findings),
                test_matched=test_matched,
                test_missed=test_missed,
                ast_report=ast_report,
            )

            comp_entry = {
                'code': code,
                'name': cdef.get('name', code),
                'weight': weight,
                'is_critical': is_crit,
                'critical_floor': floor,
                'score': score_val,
                'gate_passed': gate_ok,
                'reasoning': reasoning,
                'evidence_quotes': [
                    f"Matched '{m['finding_title']}' -> {m['defect_label']} ({m['line_range']})"
                    for m in matched_d.values()
                ][:4] if code != 'se_testing' else [f"Covered test scenario: {lbl}" for lbl in test_matched],
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

            if score_val < 70.0 or not gate_ok:
                remediations.append({
                    'competency_code': code,
                    'competency_name': comp_entry['name'],
                    'score': score_val,
                    'gate_passed': gate_ok,
                    'action': reasoning,
                    'topics': comp_entry['recommended_topics'],
                })

        practice_benchmark_met = (weighted_total >= 70.0) and critical_gates_passed

        return {
            'weighted_total': weighted_total,
            'practice_benchmark_met': practice_benchmark_met,
            'critical_gates_passed': critical_gates_passed,
            'matched_defects': list(matched_d.values()),
            'missed_defects': missed_defects,
            'missed_critical_defects': missed_critical_defects,
            'unsupported_findings': unsupported_findings,
            'test_matched': test_matched,
            'test_missed': test_missed,
            'ast_report': ast_report,
            'competency_breakdown': competency_breakdown,
            'remediations': remediations,
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
    def _build_competency_reasoning(
        cls,
        *,
        code: str,
        matched_count: int,
        total_defects: int,
        missed_critical: List[Dict[str, Any]],
        unsupported_count: int,
        test_matched: List[str],
        test_missed: List[str],
        ast_report: Dict[str, Any],
    ) -> str:
        if code == 'se_diagnosis':
            msg = f"Identified {matched_count} of {total_defects} planted defects."
            if missed_critical:
                names = ", ".join(m['label'] for m in missed_critical)
                msg += f" Critical gate triggered: missed critical defect(s) [{names}]."
            if ast_report['status'] == 'VALID_AST' and ast_report['total_rules'] > 0:
                msg += f" AST static check passed {ast_report['passed_rules']}/{ast_report['total_rules']} structural invariants."
            elif ast_report['status'] == 'SYNTAX_ERROR':
                msg += " Submitted patch snippet failed syntax parsing."
            return msg

        if code == 'se_testing':
            if not test_matched:
                return "No concrete boundary, error, or concurrency verification cases identified."
            msg = f"Covered {len(test_matched)} verification dimension(s)."
            if test_missed:
                msg += f" Still missing: {'; '.join(test_missed[:2])}."
            return msg

        # se_ai_critique
        msg = f"Caught {matched_count}/{total_defects} subtle AI-generated issues"
        if unsupported_count > 0:
            msg += f" with {unsupported_count} unsupported/false-positive claim(s)."
        else:
            msg += " with zero unsupported claims."
        return msg

    @classmethod
    def submit_and_evaluate_stage1_practice(
        cls,
        *,
        user,
        session_uuid,
        response_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Atomically locks Stage 1, evaluates deterministically (0 AI calls),
        persists EvaluationResult + CompetencyScore rows, and finalizes session.
        """
        with transaction.atomic():
            session = AssessmentSession.objects.select_for_update().filter(
                session_uuid=session_uuid
            ).first()
            if not session:
                raise ValueError("Assessment session not found.")
            if session.mode != 'PRACTICE' or session.practice_scope != 'STAGE_1':
                raise ValueError("This endpoint only evaluates Stage 1 Practice drills.")

            stage_resp = AssessmentStageResponse.objects.select_for_update().filter(
                session=session,
                stage_number=1,
            ).first()
            if not stage_resp:
                raise ValueError("Stage 1 snapshot missing.")

            eval_output = cls.evaluate_payload(
                snapshot=stage_resp.task_snapshot,
                response_payload=response_payload,
                rubric_defs=session.rubric_snapshot or session.blueprint.competency_definitions,
                career_path_id=session.career_path_id,
            )

            # Lock stage and advance to SUBMITTED
            AssessmentSessionService.lock_and_advance_stage(
                user=user,
                session_uuid=session.session_uuid,
                stage_number=1,
                final_payload=response_payload,
                deterministic_result=eval_output,
            )

            # Finalize session as EVALUATED (Practice mode never sets cooldown_until)
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
                stage_scores={'1': eval_output['weighted_total']},
                critical_gates_passed=eval_output['critical_gates_passed'],
                passed=passed_flag,
                evaluator_confidence=1.0,
                borderline_cross_checked=False,
                needs_human_review=False,
                rubric_snapshot=session.rubric_snapshot or [],
                scoring_evidence={
                    'matched_defects': eval_output['matched_defects'],
                    'missed_defects': eval_output['missed_defects'],
                    'unsupported_findings': eval_output['unsupported_findings'],
                    'test_matched': eval_output['test_matched'],
                    'test_missed': eval_output['test_missed'],
                    'ast_report': eval_output['ast_report'],
                },
                remediation_recommendations=eval_output['remediations'],
                audit_trail={
                    'engine': 'Stage1DeterministicEvaluator',
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
