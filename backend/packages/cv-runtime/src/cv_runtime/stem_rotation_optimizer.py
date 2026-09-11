from __future__ import annotations

import math
from dataclasses import dataclass

from cv_runtime.stem_rotation_geometry import score_curve_for_angles
from cv_runtime.stem_rotation_types import (
    AngleScorePoint,
    PriorityMode,
    RotationConstraints,
    StemRotationOptimizationResult,
    StemRotationReasonCode,
    StemSliceGeometry,
    StemSliceModel,
    priority_axis_for_mode,
)
from cv_runtime.stem_slice_preprocessing import (
    StemSlicePreprocessingConfig,
    preprocess_stem_slice_model,
)


@dataclass(frozen=True)
class StemRotationOptimizationConfig:
    coarse_angle_step_deg: float = 1.0
    fine_angle_step_deg: float = 0.1
    fine_search_half_width_deg: float = 2.0
    score_tolerance_mm: float = 1.0
    min_gain_mm: float = 3.0
    min_rotation_deg: float = 1.0
    minimum_model_quality: float = 0.0
    minimum_valid_slice_ratio: float = 0.5
    minimum_valid_slice_count: int = 1
    diagnostic_quantile: float | None = 0.05
    numerical_tolerance: float = 1.0e-9

    def validate(self) -> StemRotationOptimizationConfig:
        values = {
            "coarse_angle_step_deg": self.coarse_angle_step_deg,
            "fine_angle_step_deg": self.fine_angle_step_deg,
            "fine_search_half_width_deg": self.fine_search_half_width_deg,
            "score_tolerance_mm": self.score_tolerance_mm,
            "min_gain_mm": self.min_gain_mm,
            "min_rotation_deg": self.min_rotation_deg,
            "minimum_model_quality": self.minimum_model_quality,
            "minimum_valid_slice_ratio": self.minimum_valid_slice_ratio,
            "numerical_tolerance": self.numerical_tolerance,
        }
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError("optimization configuration values must be finite")
        if not 0 < self.coarse_angle_step_deg <= 180:
            raise ValueError("coarse_angle_step_deg must be in (0, 180]")
        if not 0 < self.fine_angle_step_deg <= self.coarse_angle_step_deg:
            raise ValueError("fine_angle_step_deg must be positive and no larger than coarse step")
        if self.fine_search_half_width_deg < 0:
            raise ValueError("fine_search_half_width_deg must not be negative")
        for name in ("score_tolerance_mm", "min_gain_mm", "min_rotation_deg"):
            if float(getattr(self, name)) < 0:
                raise ValueError(f"{name} must not be negative")
        for name in ("minimum_model_quality", "minimum_valid_slice_ratio"):
            value = float(getattr(self, name))
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between zero and one")
        if self.minimum_valid_slice_count < 1:
            raise ValueError("minimum_valid_slice_count must be positive")
        if self.diagnostic_quantile is not None and not 0 <= self.diagnostic_quantile <= 1:
            raise ValueError("diagnostic_quantile must be between zero and one")
        if self.numerical_tolerance <= 0:
            raise ValueError("numerical_tolerance must be positive")
        return self


def optimize_stem_rotation(
    model: StemSliceModel,
    mode: PriorityMode | str,
    *,
    constraints: RotationConstraints | None = None,
    config: StemRotationOptimizationConfig | None = None,
    preprocessing_config: StemSlicePreprocessingConfig | None = None,
) -> StemRotationOptimizationResult:
    """Optimize a relative rotation around machine +x for one priority direction.

    The model is assumed to be preprocessed unless ``preprocessing_config`` is supplied.
    Supplying it runs the same public preprocessing function before scoring.
    """
    resolved = (config or StemRotationOptimizationConfig()).validate()
    try:
        mode_value = PriorityMode(mode)
    except ValueError:
        return _invalid_result(
            mode=PriorityMode.NONE,
            model_quality=_safe_model_quality(model),
            reason=StemRotationReasonCode.INVALID_PRIORITY_MODE,
        )
    if mode_value is PriorityMode.NONE:
        return _invalid_result(
            mode=mode_value,
            model_quality=_safe_model_quality(model),
            reason=StemRotationReasonCode.INVALID_PRIORITY_MODE,
        )

    resolved_constraints = constraints or RotationConstraints()
    try:
        resolved_constraints.validate()
    except ValueError:
        return _invalid_result(
            mode=mode_value,
            model_quality=_safe_model_quality(model),
            reason=StemRotationReasonCode.ROTATION_CONSTRAINT_CONFLICT,
        )
    try:
        prepared = model.validate()
        if preprocessing_config is not None:
            prepared = preprocess_stem_slice_model(
                prepared,
                config=preprocessing_config,
            ).model
    except ValueError:
        return _invalid_result(
            mode=mode_value,
            model_quality=_safe_model_quality(model),
            reason=StemRotationReasonCode.INVALID_MODEL,
        )

    evaluated_slices = _slices_in_evaluation_range(prepared)
    valid_slices = tuple(item for item in evaluated_slices if item.valid)
    evaluated_count = len(evaluated_slices)
    valid_count = len(valid_slices)
    if not evaluated_count or not valid_count:
        return _invalid_result(
            mode=mode_value,
            model_quality=prepared.model_quality,
            reason=StemRotationReasonCode.NO_VALID_SLICES_IN_EVALUATION_RANGE,
            valid_slice_count=valid_count,
            evaluated_slice_count=evaluated_count,
        )
    if valid_count < resolved.minimum_valid_slice_count:
        return _invalid_result(
            mode=mode_value,
            model_quality=prepared.model_quality,
            reason=StemRotationReasonCode.VALID_SLICE_COUNT_INSUFFICIENT,
            valid_slice_count=valid_count,
            evaluated_slice_count=evaluated_count,
        )
    if valid_count / evaluated_count < resolved.minimum_valid_slice_ratio:
        return _invalid_result(
            mode=mode_value,
            model_quality=prepared.model_quality,
            reason=StemRotationReasonCode.VALID_SLICE_RATIO_LOW,
            valid_slice_count=valid_count,
            evaluated_slice_count=evaluated_count,
        )
    if prepared.model_quality < resolved.minimum_model_quality:
        return _invalid_result(
            mode=mode_value,
            model_quality=prepared.model_quality,
            reason=StemRotationReasonCode.MODEL_QUALITY_LOW,
            valid_slice_count=valid_count,
            evaluated_slice_count=evaluated_count,
        )

    axis = priority_axis_for_mode(mode_value)
    lower_deg, upper_deg = effective_search_bounds(resolved_constraints)
    coarse_angles = build_candidate_angles(
        lower_deg,
        upper_deg,
        resolved.coarse_angle_step_deg,
        required_angles=(0.0,),
    )
    score_by_angle: dict[float, AngleScorePoint] = {}

    def evaluate(angles: tuple[float, ...]) -> None:
        missing = tuple(angle for angle in angles if _angle_key(angle) not in score_by_angle)
        points = score_curve_for_angles(
            valid_slices,
            axis,
            missing,
            x_eval_start_mm=prepared.x_eval_start_mm,
            x_eval_end_mm=prepared.x_eval_end_mm,
            diagnostic_quantile=resolved.diagnostic_quantile,
        )
        for point in points:
            score_by_angle[_angle_key(point.angle_deg)] = point

    evaluate(coarse_angles)
    coarse_points = tuple(score_by_angle[_angle_key(angle)] for angle in coarse_angles)
    valid_coarse = [point for point in coarse_points if point.valid and point.score_mm is not None]
    if not valid_coarse:
        return _invalid_result(
            mode=mode_value,
            model_quality=prepared.model_quality,
            reason=StemRotationReasonCode.NO_VALID_SLICES_IN_EVALUATION_RANGE,
            valid_slice_count=valid_count,
            evaluated_slice_count=evaluated_count,
            score_curve=_sorted_curve(score_by_angle),
        )

    coarse_maximum = max(_score_value(point) for point in valid_coarse)
    seed_threshold = coarse_maximum - resolved.score_tolerance_mm
    seeds = [
        point.angle_deg
        for point in valid_coarse
        if _score_value(point) >= seed_threshold - resolved.numerical_tolerance
    ]
    seeds.append(0.0)
    fine_angles: set[float] = set()
    for seed in seeds:
        local_start = max(lower_deg, seed - resolved.fine_search_half_width_deg)
        local_end = min(upper_deg, seed + resolved.fine_search_half_width_deg)
        fine_angles.update(
            build_candidate_angles(
                local_start,
                local_end,
                resolved.fine_angle_step_deg,
                required_angles=(seed,),
            )
        )
    fine_angles.update((lower_deg, 0.0, upper_deg))
    evaluate(tuple(sorted(fine_angles)))

    curve = _sorted_curve(score_by_angle)
    valid_points = [point for point in curve if point.valid and point.score_mm is not None]
    selected = _select_candidate(valid_points, resolved)
    current = score_by_angle[_angle_key(0.0)]
    if selected is None or current.score_mm is None or selected.score_mm is None:
        return _invalid_result(
            mode=mode_value,
            model_quality=prepared.model_quality,
            reason=StemRotationReasonCode.NUMERICAL_ERROR,
            valid_slice_count=valid_count,
            evaluated_slice_count=evaluated_count,
            score_curve=curve,
        )
    if selected.score_mm <= resolved.numerical_tolerance:
        return _invalid_result(
            mode=mode_value,
            model_quality=prepared.model_quality,
            reason=StemRotationReasonCode.NO_USABLE_GEOMETRY,
            valid_slice_count=valid_count,
            evaluated_slice_count=evaluated_count,
            score_current_mm=current.score_mm,
            limiting_x_current_mm=current.limiting_x_mm,
            score_curve=curve,
        )

    raw_gain = selected.score_mm - current.score_mm
    if raw_gain < -resolved.numerical_tolerance:
        return _invalid_result(
            mode=mode_value,
            model_quality=prepared.model_quality,
            reason=StemRotationReasonCode.NUMERICAL_ERROR,
            valid_slice_count=valid_count,
            evaluated_slice_count=evaluated_count,
            score_current_mm=current.score_mm,
            limiting_x_current_mm=current.limiting_x_mm,
            score_curve=curve,
        )
    gain_mm = max(0.0, raw_gain)
    angle_is_too_small = (
        abs(selected.angle_deg) < resolved.min_rotation_deg
        or abs(selected.angle_deg) <= resolved.numerical_tolerance
    )
    if gain_mm < resolved.min_gain_mm:
        should_rotate = False
        reason = StemRotationReasonCode.LOW_GAIN
    elif angle_is_too_small:
        should_rotate = False
        reason = StemRotationReasonCode.ROTATION_BELOW_MINIMUM
    else:
        should_rotate = True
        reason = StemRotationReasonCode.OK

    maximum_score = max(_score_value(point) for point in valid_points)
    plateau_width = _plateau_width_deg(
        curve,
        selected.angle_deg,
        maximum_score - resolved.score_tolerance_mm,
        resolved.fine_angle_step_deg,
        resolved.numerical_tolerance,
    )
    return StemRotationOptimizationResult(
        valid=True,
        mode=mode_value,
        delta_angle_deg=selected.angle_deg if should_rotate else 0.0,
        gain_mm=gain_mm,
        should_rotate=should_rotate,
        limiting_x_current_mm=current.limiting_x_mm,
        limiting_x_optimal_mm=selected.limiting_x_mm,
        score_current_mm=current.score_mm,
        score_optimal_mm=selected.score_mm,
        model_quality=prepared.model_quality,
        reason_code=reason,
        optimal_candidate_angle_deg=selected.angle_deg,
        plateau_width_deg=plateau_width,
        valid_slice_count=valid_count,
        evaluated_slice_count=evaluated_count,
        score_curve=curve,
    )


def build_stem_angle_score_curve(
    model: StemSliceModel,
    mode: PriorityMode | str,
    *,
    constraints: RotationConstraints | None = None,
    angle_step_deg: float = 1.0,
) -> tuple[AngleScorePoint, ...]:
    """Build a complete bounded angle-to-score diagnostic curve."""
    prepared = model.validate()
    mode_value = PriorityMode(mode)
    axis = priority_axis_for_mode(mode_value)
    resolved_constraints = (constraints or RotationConstraints()).validate()
    lower_deg, upper_deg = effective_search_bounds(resolved_constraints)
    angles = build_candidate_angles(
        lower_deg,
        upper_deg,
        angle_step_deg,
        required_angles=(0.0,),
    )
    return score_curve_for_angles(
        prepared.slices,
        axis,
        angles,
        x_eval_start_mm=prepared.x_eval_start_mm,
        x_eval_end_mm=prepared.x_eval_end_mm,
    )


def effective_search_bounds(constraints: RotationConstraints) -> tuple[float, float]:
    """Choose at most one 180-degree physical interval while retaining delta=0."""
    resolved = constraints.validate()
    lower = resolved.negative_limit_deg
    upper = resolved.positive_limit_deg
    if upper - lower <= 180.0:
        return (lower, upper)
    effective_lower = max(lower, -90.0)
    effective_upper = effective_lower + 180.0
    if effective_upper > upper:
        effective_upper = upper
        effective_lower = effective_upper - 180.0
    return (effective_lower, effective_upper)


def build_candidate_angles(
    lower_deg: float,
    upper_deg: float,
    step_deg: float,
    *,
    required_angles: tuple[float, ...] = (),
) -> tuple[float, ...]:
    """Build an inclusive deterministic grid and retain required in-range angles."""
    if not all(math.isfinite(value) for value in (lower_deg, upper_deg, step_deg)):
        raise ValueError("candidate range values must be finite")
    if upper_deg < lower_deg or step_deg <= 0:
        raise ValueError("candidate range must be ordered and use a positive step")
    count = int(math.floor((upper_deg - lower_deg) / step_deg + 1.0e-12))
    values = [lower_deg + index * step_deg for index in range(count + 1)]
    values.extend((lower_deg, upper_deg))
    values.extend(angle for angle in required_angles if lower_deg <= angle <= upper_deg)
    unique = {_angle_key(angle): _clean_angle(angle) for angle in values}
    return tuple(sorted(unique.values()))


def _select_candidate(
    points: list[AngleScorePoint],
    config: StemRotationOptimizationConfig,
) -> AngleScorePoint | None:
    if not points:
        return None
    maximum = max(_score_value(point) for point in points if point.score_mm is not None)
    eligible = [
        point
        for point in points
        if point.score_mm is not None
        and point.score_mm >= maximum - config.score_tolerance_mm - config.numerical_tolerance
    ]
    # The final sign key is deliberately versioned behavior: +delta wins an otherwise
    # exact +/- tie after score tolerance and shortest physical path were applied.
    return min(
        eligible,
        key=lambda point: (
            abs(point.angle_deg),
            0 if point.angle_deg >= 0 else 1,
            point.angle_deg,
        ),
    )


def _score_value(point: AngleScorePoint) -> float:
    if point.score_mm is None:
        raise ValueError("angle score point has no score")
    return point.score_mm


def _plateau_width_deg(
    curve: tuple[AngleScorePoint, ...],
    selected_angle_deg: float,
    score_threshold_mm: float,
    fine_step_deg: float,
    numerical_tolerance: float,
) -> float:
    eligible = [
        point
        for point in curve
        if point.score_mm is not None and point.score_mm >= score_threshold_mm - numerical_tolerance
    ]
    if not eligible:
        return 0.0
    selected_index = min(
        range(len(eligible)),
        key=lambda index: abs(eligible[index].angle_deg - selected_angle_deg),
    )
    maximum_gap = fine_step_deg * 1.5 + numerical_tolerance
    left = selected_index
    while left > 0 and eligible[left].angle_deg - eligible[left - 1].angle_deg <= maximum_gap:
        left -= 1
    right = selected_index
    while (
        right + 1 < len(eligible)
        and eligible[right + 1].angle_deg - eligible[right].angle_deg <= maximum_gap
    ):
        right += 1
    return max(0.0, eligible[right].angle_deg - eligible[left].angle_deg)


def _slices_in_evaluation_range(model: StemSliceModel) -> tuple[StemSliceGeometry, ...]:
    return tuple(
        stem_slice
        for stem_slice in model.slices
        if (model.x_eval_start_mm is None or stem_slice.x_mm >= model.x_eval_start_mm)
        and (model.x_eval_end_mm is None or stem_slice.x_mm <= model.x_eval_end_mm)
    )


def _invalid_result(
    *,
    mode: PriorityMode,
    model_quality: float,
    reason: StemRotationReasonCode,
    valid_slice_count: int = 0,
    evaluated_slice_count: int = 0,
    score_current_mm: float | None = None,
    limiting_x_current_mm: float | None = None,
    score_curve: tuple[AngleScorePoint, ...] = (),
) -> StemRotationOptimizationResult:
    return StemRotationOptimizationResult(
        valid=False,
        mode=mode,
        delta_angle_deg=None,
        gain_mm=None,
        should_rotate=False,
        limiting_x_current_mm=limiting_x_current_mm,
        limiting_x_optimal_mm=None,
        score_current_mm=score_current_mm,
        score_optimal_mm=None,
        model_quality=model_quality,
        reason_code=reason,
        valid_slice_count=valid_slice_count,
        evaluated_slice_count=evaluated_slice_count,
        score_curve=score_curve,
    )


def _safe_model_quality(model: StemSliceModel) -> float:
    value = model.model_quality
    return value if math.isfinite(value) and 0 <= value <= 1 else 0.0


def _sorted_curve(score_by_angle: dict[float, AngleScorePoint]) -> tuple[AngleScorePoint, ...]:
    return tuple(score_by_angle[key] for key in sorted(score_by_angle))


def _angle_key(angle_deg: float) -> float:
    return round(_clean_angle(angle_deg), 12)


def _clean_angle(angle_deg: float) -> float:
    return 0.0 if abs(angle_deg) < 1.0e-12 else float(angle_deg)
