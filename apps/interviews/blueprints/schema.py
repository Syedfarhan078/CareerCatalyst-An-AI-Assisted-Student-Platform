"""
Versioned Blueprint Schema Loader and Structural Validator.

Validates version-controlled role assessment blueprints and calibrated task
bundles before synchronization or runtime session creation.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List


BLUEPRINTS_DIR = Path(__file__).resolve().parent

VALID_ARCHETYPES = {'ENGINEERING', 'DATA_ML', 'CLOUD_SEC', 'PRODUCT_DESIGN'}
VALID_CALIBRATION_STATUSES = {'DRAFT', 'PILOT', 'CALIBRATED', 'ACTIVE', 'RETIRED'}
REQUIRED_RUBRIC_BANDS = {'0', '25', '50', '75', '100'}


class BlueprintValidationError(ValueError):
    """Raised when a role assessment blueprint fails structural or invariant validation."""


def compute_spec_checksum(spec: Dict[str, Any]) -> str:
    """Computes a deterministic SHA-256 digest of the canonical JSON blueprint spec."""
    canonical = json.dumps(spec, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def list_blueprint_files() -> List[Path]:
    """Returns all versioned JSON blueprint files in sorted order."""
    return sorted(BLUEPRINTS_DIR.glob('*.json'))


def load_blueprint_file(path: Path) -> Dict[str, Any]:
    """Loads and validates a blueprint JSON file from disk."""
    try:
        raw = path.read_text(encoding='utf-8')
        spec = json.loads(raw)
    except Exception as exc:
        raise BlueprintValidationError(f"Failed to parse blueprint file '{path.name}': {exc}") from exc
    validate_blueprint_spec(spec)
    return spec


def validate_blueprint_spec(spec: Dict[str, Any]) -> None:
    """
    Validates all mandatory metadata, weight sums, timing bounds, conservative
    Stage 3 stopping rules, observable competency rubrics, and equivalent task
    bundle invariants.
    """
    if not isinstance(spec, dict):
        raise BlueprintValidationError("Blueprint specification must be a JSON object.")

    required_top = [
        'role_slug', 'blueprint_version', 'task_bank_version',
        'archetype', 'calibration_status', 'is_active', 'is_pilot',
        'passing_score', 'borderline_low', 'borderline_high',
        'stage_weights', 'stage_durations', 'practice_durations',
        'timing_policy', 'stopping_rules', 'competency_definitions',
        'task_bundles',
    ]
    missing = [k for k in required_top if k not in spec]
    if missing:
        raise BlueprintValidationError(f"Blueprint missing required keys: {missing}")

    if spec['archetype'] not in VALID_ARCHETYPES:
        raise BlueprintValidationError(f"Invalid archetype '{spec['archetype']}'.")
    if spec['calibration_status'] not in VALID_CALIBRATION_STATUSES:
        raise BlueprintValidationError(f"Invalid calibration_status '{spec['calibration_status']}'.")

    passing = float(spec['passing_score'])
    b_low = float(spec['borderline_low'])
    b_high = float(spec['borderline_high'])
    if not (0 < b_low <= passing <= b_high <= 100):
        raise BlueprintValidationError(
            f"Invalid threshold bounds: borderline_low={b_low}, passing={passing}, borderline_high={b_high}"
        )

    # Validate stage weights sum to 1.0 across stages 1, 2, 3
    weights = spec['stage_weights']
    if not isinstance(weights, dict) or set(weights.keys()) != {'1', '2', '3'}:
        raise BlueprintValidationError("stage_weights must specify exact keys {'1', '2', '3'}.")
    weight_sum = sum(float(v) for v in weights.values())
    if abs(weight_sum - 1.0) > 1e-4 or any(float(v) <= 0 for v in weights.values()):
        raise BlueprintValidationError(f"stage_weights must be positive and sum to 1.0 (got {weight_sum}).")

    # Validate stage durations (seconds)
    durations = spec['stage_durations']
    if not isinstance(durations, dict) or set(durations.keys()) != {'1', '2', '3'}:
        raise BlueprintValidationError("stage_durations must specify exact keys {'1', '2', '3'}.")
    for stage_k, sec in durations.items():
        if not isinstance(sec, int) or sec < 120 or sec > 3600:
            raise BlueprintValidationError(f"Stage {stage_k} duration {sec}s outside [120, 3600] bounds.")

    # Validate practice durations
    p_durations = spec['practice_durations']
    if not isinstance(p_durations, dict) or not {'FULL', 'STAGE_1', 'STAGE_2', 'STAGE_3'}.issubset(p_durations.keys()):
        raise BlueprintValidationError("practice_durations must define FULL, STAGE_1, STAGE_2, STAGE_3.")

    # Validate timing policy
    timing = spec['timing_policy']
    if not isinstance(timing, dict):
        raise BlueprintValidationError("timing_policy must be an object.")
    mult = float(timing.get('accessibility_multiplier', 0))
    grace = int(timing.get('reconnect_grace_seconds', -1))
    if mult < 1.0 or mult > 2.0 or grace < 0 or grace > 600:
        raise BlueprintValidationError("Invalid timing_policy accessibility_multiplier or reconnect_grace_seconds.")

    # Validate conservative Stage 3 stopping rules (Refinement #7)
    stopping = spec['stopping_rules']
    if not isinstance(stopping, dict):
        raise BlueprintValidationError("stopping_rules must be an object.")
    min_turns = int(stopping.get('min_turns', 0))
    max_turns = int(stopping.get('max_turns', 0))
    if min_turns < 2 or max_turns < min_turns or max_turns > 6:
        raise BlueprintValidationError(f"Invalid stopping turn bounds: min={min_turns}, max={max_turns}.")
    if stopping.get('allow_early_stop_certification', True) and not stopping.get('require_all_target_competencies_covered', False):
        raise BlueprintValidationError(
            "Certification early stopping cannot rely solely on AI confidence without full competency coverage."
        )

    # Validate competencies & observable rubrics
    competencies = spec['competency_definitions']
    if not isinstance(competencies, list) or len(competencies) < 3:
        raise BlueprintValidationError("competency_definitions must define at least 3 competencies.")

    comp_codes = set()
    comp_weight_sum = 0.0
    critical_count = 0
    for comp in competencies:
        code = comp.get('code', '').strip()
        if not code or code in comp_codes:
            raise BlueprintValidationError(f"Duplicate or empty competency code '{code}'.")
        comp_codes.add(code)

        cw = float(comp.get('weight', 0))
        if cw <= 0:
            raise BlueprintValidationError(f"Competency '{code}' weight must be > 0.")
        comp_weight_sum += cw

        if comp.get('is_critical'):
            critical_count += 1
            floor = float(comp.get('critical_floor', 0))
            if floor < 50 or floor > 85:
                raise BlueprintValidationError(f"Critical competency '{code}' floor {floor} outside [50, 85].")

        bands = comp.get('rubric_bands', {})
        if not isinstance(bands, dict) or set(bands.keys()) != REQUIRED_RUBRIC_BANDS:
            raise BlueprintValidationError(
                f"Competency '{code}' must define observable rubric_bands for keys {sorted(REQUIRED_RUBRIC_BANDS)}."
            )
        if any(not str(v).strip() for v in bands.values()):
            raise BlueprintValidationError(f"Competency '{code}' has empty rubric band description.")

        topics = comp.get('remediation_topic_refs', [])
        if not isinstance(topics, list) or len(topics) == 0:
            raise BlueprintValidationError(f"Competency '{code}' must define at least 1 remediation_topic_ref.")

    if abs(comp_weight_sum - 1.0) > 1e-4:
        raise BlueprintValidationError(f"Competency weights must sum to 1.0 (got {comp_weight_sum}).")
    if critical_count < 1:
        raise BlueprintValidationError("At least 1 critical competency gate is required.")

    # Validate calibrated task bundles
    bundles = spec['task_bundles']
    if not isinstance(bundles, list) or len(bundles) < 3:
        raise BlueprintValidationError("At least 3 calibrated equivalent task_bundles are required.")

    bundle_ids = set()
    for bundle in bundles:
        bid = bundle.get('bundle_id', '').strip()
        if not bid or bid in bundle_ids:
            raise BlueprintValidationError(f"Duplicate or missing bundle_id '{bid}'.")
        bundle_ids.add(bid)

        covered = set(bundle.get('competencies_covered', []))
        if not comp_codes.issubset(covered):
            missing_cov = comp_codes - covered
            raise BlueprintValidationError(f"Bundle '{bid}' does not cover competencies: {missing_cov}")

        s1 = bundle.get('stage_1', {})
        defects = s1.get('planted_defects', [])
        if not s1.get('artifact_code') or len(defects) != 4:
            raise BlueprintValidationError(
                f"Bundle '{bid}' Stage 1 must define artifact_code and exactly 4 calibrated planted_defects."
            )
        if not s1.get('requirements') or not s1.get('expected_behavior'):
            raise BlueprintValidationError(
                f"Bundle '{bid}' Stage 1 must define candidate-facing requirements and expected_behavior."
            )
        if not any(d.get('is_critical') for d in defects):
            raise BlueprintValidationError(
                f"Bundle '{bid}' Stage 1 must designate at least 1 critical planted defect."
            )
        if not isinstance(s1.get('ast_invariants', []), list) or len(s1.get('ast_invariants', [])) < 2:
            raise BlueprintValidationError(f"Bundle '{bid}' Stage 1 must define at least 2 deterministic ast_invariants.")

        s2 = bundle.get('stage_2', {})
        if not s2.get('part_a') or not s2.get('part_b_twist'):
            raise BlueprintValidationError(f"Bundle '{bid}' Stage 2 must define both part_a and part_b_twist.")

        s3 = bundle.get('stage_3', {})
        if not s3.get('focus_competencies') or not s3.get('twist_pool'):
            raise BlueprintValidationError(f"Bundle '{bid}' Stage 3 must define focus_competencies and twist_pool.")
