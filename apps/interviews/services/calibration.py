"""
Offline & Zero-Side-Effect Calibration Runner for Career Readiness Assessment.

Evaluates versioned calibration fixtures (explicitly separated as PROVISIONAL vs
HUMAN_REVIEWED) across Stage 1, Stage 2, and Stage 3 without creating candidate
AssessmentSessions, consuming candidate AI quota, or issuing certificates.
"""

import json
import random
from pathlib import Path
from typing import Any, Dict, List, Optional

from apps.interviews.services.blueprint_loader import BlueprintRegistryService
from apps.interviews.services.stage1_evaluator import Stage1DeterministicEvaluator
from apps.interviews.services.stage2_evaluator import Stage2DeterministicEvaluator
from apps.interviews.services.stage3_evaluator import Stage3OwnershipEvaluator


CALIBRATION_DIR = Path(__file__).resolve().parent.parent / 'calibration'


class CalibrationRunner:
    """Executes versioned calibration datasets and summarizes fairness/validity metrics."""

    DEFAULT_DATASET = CALIBRATION_DIR / 'se_calibration_2026_1.json'

    @classmethod
    def load_dataset(cls, path: Optional[Path] = None) -> Dict[str, Any]:
        target = path or cls.DEFAULT_DATASET
        return json.loads(Path(target).read_text(encoding='utf-8'))

    @classmethod
    def _evaluate_stage1_case(cls, case: Dict[str, Any], spec: Dict[str, Any], career_path_id: int) -> Dict[str, Any]:
        bundle = BlueprintRegistryService.get_bundle_by_id(spec, case['bundle_id'])
        snapshot = BlueprintRegistryService.build_stage_snapshot(bundle, 1)
        res = Stage1DeterministicEvaluator.evaluate_payload(
            snapshot=snapshot,
            response_payload=case['candidate_response'],
            rubric_defs=spec['competency_definitions'],
            career_path_id=career_path_id,
        )
        comp_map = {c['code']: c['score'] for c in res['competency_breakdown']}
        tags = []
        if len(res.get('matched_defects', [])) == 4:
            tags.append('matched_all_4')
        if (res.get('ast_report') or {}).get('status') == 'VALID_AST':
            tags.append('ast_valid')
        if not res['critical_gates_passed']:
            tags.append('critical_gate_failed')
        if len(res.get('unsupported_findings', [])) >= 1:
            tags.append('false_positives_flagged')
        return {
            'overall_score': res['weighted_total'],
            'critical_gates_passed': res['critical_gates_passed'],
            'competency_scores': comp_map,
            'observed_tags': tags,
            'raw': res,
        }

    @classmethod
    def _evaluate_stage2_case(cls, case: Dict[str, Any], spec: Dict[str, Any], career_path_id: int) -> Dict[str, Any]:
        bundle = BlueprintRegistryService.get_bundle_by_id(spec, case['bundle_id'])
        snapshot = BlueprintRegistryService.build_stage_snapshot(bundle, 2)
        res = Stage2DeterministicEvaluator.evaluate_payload(
            snapshot=snapshot,
            response_payload=case['candidate_response'],
            rubric_defs=spec['competency_definitions'],
            career_path_id=career_path_id,
        )
        comp_map = {c['code']: c['score'] for c in res['competency_breakdown']}
        tags = []
        if res.get('pivot_ignored'):
            tags.append('pivot_ignored')
        if any('fail-open' in m.lower() or 'fail open' in m.lower() for m in res.get('weak_tradeoff_reasoning', [])):
            tags.append('technical_trap_caught')
        if any('contradiction' in m.lower() for m in res.get('weak_tradeoff_reasoning', [])):
            tags.append('contradiction_detected')
        return {
            'overall_score': res['weighted_total'],
            'critical_gates_passed': res['critical_gates_passed'],
            'competency_scores': comp_map,
            'observed_tags': tags,
            'raw': res,
        }

    @classmethod
    def _evaluate_stage3_offline_case(cls, case: Dict[str, Any], spec: Dict[str, Any]) -> Dict[str, Any]:
        """
        Pure offline Stage 3 signal + AI-guardrail calibration (zero HTTP calls or DB writes).
        """
        from types import SimpleNamespace
        from django.utils import timezone

        bundle = BlueprintRegistryService.get_bundle_by_id(spec, case['bundle_id'])
        snapshot = BlueprintRegistryService.build_stage_snapshot(bundle, 3)
        payload = case['candidate_response']

        if payload.get('simulate_outage'):
            return {
                'overall_score': None,
                'critical_gates_passed': None,
                'competency_scores': {},
                'observed_tags': ['outage_no_penalty'],
                'raw': {'status': 'PENDING_EVALUATION', 'passed': None, 'cooldown_until': None},
            }

        turns = []
        for idx, t in enumerate(payload.get('turns', []), start=1):
            turns.append(SimpleNamespace(
                turn_number=t.get('turn_number', idx),
                interviewer_prompt=f"Probe {idx}",
                candidate_reply=t['reply'],
                turn_analysis={
                    'probe_type': t.get('probe_type', 'OWNERSHIP'),
                    'injection_flags': t.get('injection_flags', []),
                },
                replied_at=timezone.now(),
            ))

        det = Stage3OwnershipEvaluator.analyze_deterministic_signals(turns, snapshot)
        comp_weights = det['comp_weights']

        # Use either adversarial mocked AI score (if testing AI inflation resistance) or clean simulated rubric alignment
        mock_ai = payload.get('mock_ai_base_score')
        blended_scores: Dict[str, float] = {}
        crit_ok = True
        for code, w in comp_weights.items():
            det_val = float(det['det_scores'].get(code, 0.0))
            ai_val = float(mock_ai) if mock_ai is not None else det_val
            blended = (0.40 * det_val) + (0.60 * ai_val)
            if det['injection_attempts'] > 0 or det.get('repeated_turns', 0) > 0 or det_val < 35.0:
                blended = min(blended, det_val + 12.0)
            blended = round(max(0.0, min(100.0, blended)), 2)
            blended_scores[code] = blended
            cdef = next((c for c in spec['competency_definitions'] if c['code'] == code), {})
            if cdef.get('is_critical'):
                floor = float(cdef.get('critical_floor') or 60.0)
                if blended < floor:
                    crit_ok = False

        overall = round(sum(float(comp_weights[c]) * blended_scores[c] for c in comp_weights), 2)
        tags = []
        if det.get('acknowledges_limitations'):
            tags.append('acknowledges_limitations')
        if det.get('injection_attempts', 0) > 0:
            tags.append('injection_capped')
        if det.get('contradiction_flags'):
            tags.append('contradiction_detected')
        if det.get('repeated_turns', 0) > 0:
            tags.append('repeated_turn_penalized')
        if not crit_ok:
            tags.append('testing_critical_gate_failed')

        return {
            'overall_score': overall,
            'critical_gates_passed': crit_ok,
            'competency_scores': blended_scores,
            'observed_tags': tags,
            'raw': det,
        }

    @classmethod
    def run_suite(cls, dataset_path: Optional[Path] = None, repeat_stage3_trials: int = 5) -> Dict[str, Any]:
        from apps.roadmaps.models import CareerPath

        ds = cls.load_dataset(dataset_path)
        spec = BlueprintRegistryService.get_active_spec_for_role(ds['role_slug'])
        se_cp = CareerPath.objects.filter(slug=ds['role_slug']).first()
        career_path_id = se_cp.id if se_cp else 0

        results: List[Dict[str, Any]] = []
        by_stage = {1: 0, 2: 0, 3: 0}
        provisional_count = 0
        human_reviewed_count = 0
        passed_cases = 0
        failed_cases = 0
        critical_gate_mismatches = 0
        false_passes = []
        false_fails = []
        alt_design_results = []
        length_bias_checks = []
        comp_discrepancies = []

        case_scores_by_id: Dict[str, float] = {}

        for case in ds['cases']:
            stage = int(case['stage'])
            by_stage[stage] = by_stage.get(stage, 0) + 1
            if case.get('human_reviewed'):
                human_reviewed_count += 1
            else:
                provisional_count += 1

            if stage == 1:
                ev = cls._evaluate_stage1_case(case, spec, career_path_id)
            elif stage == 2:
                ev = cls._evaluate_stage2_case(case, spec, career_path_id)
            else:
                ev = cls._evaluate_stage3_offline_case(case, spec)

            # Check ranges and gate expectations
            case_ok = True
            failure_reasons: List[str] = []

            exp_range = case.get('expected_overall_range')
            obs_score = ev['overall_score']
            if obs_score is not None:
                case_scores_by_id[case['case_id']] = obs_score

            if exp_range is not None:
                lo, hi = float(exp_range[0]), float(exp_range[1])
                if obs_score is None or not (lo <= obs_score <= hi):
                    case_ok = False
                    failure_reasons.append(f"Overall {obs_score} outside expected [{lo}, {hi}]")
                    if obs_score is not None and obs_score > hi and hi < 65.0:
                        false_passes.append({'case_id': case['case_id'], 'observed': obs_score, 'expected_max': hi})
                    elif obs_score is not None and obs_score < lo and lo >= 70.0:
                        false_fails.append({'case_id': case['case_id'], 'observed': obs_score, 'expected_min': lo})

            exp_gate = case.get('expected_critical_gate_passed')
            if exp_gate is not None and ev['critical_gates_passed'] != bool(exp_gate):
                case_ok = False
                critical_gate_mismatches += 1
                failure_reasons.append(
                    f"Critical gate {ev['critical_gates_passed']} != expected {exp_gate}"
                )

            for comp_code, crange in (case.get('expected_competency_ranges') or {}).items():
                c_val = ev['competency_scores'].get(comp_code)
                if c_val is None or not (float(crange[0]) <= c_val <= float(crange[1])):
                    case_ok = False
                    msg = f"{comp_code}={c_val} outside [{crange[0]}, {crange[1]}]"
                    failure_reasons.append(msg)
                    comp_discrepancies.append({'case_id': case['case_id'], 'detail': msg})

            if case_ok:
                passed_cases += 1
            else:
                failed_cases += 1

            if case.get('category') == 'alternative_design':
                alt_design_results.append({
                    'case_id': case['case_id'],
                    'stage': stage,
                    'score': obs_score,
                    'passed_expectation': case_ok,
                })

            results.append({
                'case_id': case['case_id'],
                'title': case['title'],
                'stage': stage,
                'category': case.get('category'),
                'review_status': case.get('review_status', 'PROVISIONAL'),
                'observed_score': obs_score,
                'expected_range': exp_range,
                'observed_gate': ev['critical_gates_passed'],
                'expected_gate': exp_gate,
                'competency_scores': ev['competency_scores'],
                'observed_tags': ev['observed_tags'],
                'passed_expectation': case_ok,
                'failure_reasons': failure_reasons,
            })

        # Paired length-bias delta inspection against blueprint threshold
        max_bias_limit = float((spec.get('calibration_metadata') or {}).get('max_length_bias_delta', 6.0))
        for case in ds['cases']:
            paired_id = case.get('paired_case_id')
            if paired_id and paired_id in case_scores_by_id and case['case_id'] in case_scores_by_id:
                delta = round(abs(case_scores_by_id[case['case_id']] - case_scores_by_id[paired_id]), 2)
                length_bias_checks.append({
                    'concise_case': paired_id,
                    'verbose_case': case['case_id'],
                    'score_delta': delta,
                    'within_threshold': delta <= max_bias_limit,
                })

        # Stage 3 repeated AI evaluation variation simulation (deterministic baseline + bounded +/-4pt LLM jitter)
        rng = random.Random(2026)
        s3_det_case = next(c for c in ds['cases'] if c['case_id'] == 'CAL-S3-01')
        s3_ev_base = cls._evaluate_stage3_offline_case(s3_det_case, spec)
        det_val = s3_ev_base['overall_score']
        trial_scores = []
        for _ in range(repeat_stage3_trials):
            jittered_ai = max(0.0, min(100.0, det_val + rng.uniform(-4.0, 4.0)))
            blended = round((0.40 * det_val) + (0.60 * jittered_ai), 2)
            trial_scores.append(blended)
        s3_variation = round(max(trial_scores) - min(trial_scores), 2) if trial_scores else 0.0

        return {
            'calibration_version': ds['calibration_version'],
            'blueprint_version': ds['blueprint_version'],
            'role_slug': ds['role_slug'],
            'total_cases': len(ds['cases']),
            'by_stage': by_stage,
            'provisional_fixtures': provisional_count,
            'human_reviewed_fixtures': human_reviewed_count,
            'cases_passing_expectations': passed_cases,
            'cases_outside_expectations': failed_cases,
            'critical_gate_mismatches': critical_gate_mismatches,
            'false_passes': false_passes,
            'false_fails': false_fails,
            'competency_discrepancies': comp_discrepancies,
            'alternative_design_results': alt_design_results,
            'length_bias_checks': length_bias_checks,
            'stage3_simulated_repeat_variation': {
                'trials': repeat_stage3_trials,
                'scores': trial_scores,
                'max_spread': s3_variation,
            },
            'case_results': results,
        }
