from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import dataclass, field, replace

from cv_runtime.stem_rotation_geometry import normalize_ellipse_angle_rad
from cv_runtime.stem_rotation_types import (
    STEM_SLICE_PREPROCESSING_VERSION,
    StemSliceGeometry,
    StemSliceModel,
)

OUTLIER_REASON = "PREPROCESSING_OUTLIER"
LOW_QUALITY_REASON = "PREPROCESSING_QUALITY_LOW"
INTERPOLATED_REASON = "INTERPOLATED_SHORT_GAP"
UNSUPPORTED_GRID_REASON = "NO_VALID_INTERPOLATION_SUPPORT"


@dataclass(frozen=True)
class StemSlicePreprocessingConfig:
    slice_spacing_mm: float = 50.0
    max_interpolation_gap_mm: float = 100.0
    smoothing_window_slices: int = 3
    smoothing_strength: float = 0.25
    minimum_slice_quality: float = 0.0
    interpolation_quality_factor: float = 0.8
    outlier_mad_scale: float = 6.0
    outlier_minimum_deviation_mm: float = 5.0
    outlier_quality_ceiling: float = 0.75
    x_eval_start_mm: float | None = None
    x_eval_end_mm: float | None = None

    def validate(self) -> StemSlicePreprocessingConfig:
        finite_values = {
            "slice_spacing_mm": self.slice_spacing_mm,
            "max_interpolation_gap_mm": self.max_interpolation_gap_mm,
            "smoothing_strength": self.smoothing_strength,
            "minimum_slice_quality": self.minimum_slice_quality,
            "interpolation_quality_factor": self.interpolation_quality_factor,
            "outlier_mad_scale": self.outlier_mad_scale,
            "outlier_minimum_deviation_mm": self.outlier_minimum_deviation_mm,
            "outlier_quality_ceiling": self.outlier_quality_ceiling,
        }
        if not all(math.isfinite(value) for value in finite_values.values()):
            raise ValueError("preprocessing configuration values must be finite")
        if self.slice_spacing_mm <= 0:
            raise ValueError("slice_spacing_mm must be positive")
        if self.max_interpolation_gap_mm < 0:
            raise ValueError("max_interpolation_gap_mm must not be negative")
        if self.smoothing_window_slices < 1 or self.smoothing_window_slices % 2 == 0:
            raise ValueError("smoothing_window_slices must be a positive odd number")
        for name in (
            "smoothing_strength",
            "minimum_slice_quality",
            "interpolation_quality_factor",
            "outlier_quality_ceiling",
        ):
            value = float(getattr(self, name))
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between zero and one")
        if self.outlier_mad_scale <= 0 or self.outlier_minimum_deviation_mm < 0:
            raise ValueError("outlier thresholds must be non-negative and use a positive scale")
        for name, optional_value in (
            ("x_eval_start_mm", self.x_eval_start_mm),
            ("x_eval_end_mm", self.x_eval_end_mm),
        ):
            if optional_value is not None and not math.isfinite(optional_value):
                raise ValueError(f"{name} must be finite when provided")
        if (
            self.x_eval_start_mm is not None
            and self.x_eval_end_mm is not None
            and self.x_eval_end_mm < self.x_eval_start_mm
        ):
            raise ValueError("x evaluation range must not be reversed")
        return self


@dataclass(frozen=True)
class StemSlicePreprocessingResult:
    model: StemSliceModel
    input_slice_count: int
    valid_input_slice_count: int
    rejected_outlier_count: int
    interpolated_slice_count: int
    valid_output_slice_count: int
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    algorithm_version: str = STEM_SLICE_PREPROCESSING_VERSION


def preprocess_stem_slice_model(
    model: StemSliceModel,
    *,
    config: StemSlicePreprocessingConfig | None = None,
) -> StemSlicePreprocessingResult:
    """Resample, bridge bounded gaps, and robustly smooth a raw slice model.

    The function never extrapolates past the first or last measured station. Automated
    outlier rejection is deliberately restricted to lower-quality multi-parameter
    anomalies so that a high-quality local bottleneck is not silently removed.
    """
    source = model.validate()
    resolved = (config or StemSlicePreprocessingConfig()).validate()
    ordered = _deduplicate_slices(source.slices)
    valid_input_count = sum(item.valid for item in ordered)
    quality_filtered = tuple(
        replace(
            item,
            valid=False,
            reason_codes=_append_reason(item.reason_codes, LOW_QUALITY_REASON),
        )
        if item.valid and item.quality < resolved.minimum_slice_quality
        else item
        for item in ordered
    )
    filtered, rejected_count = _reject_lower_quality_outliers(quality_filtered, resolved)
    gridded, interpolated_count = _resample_to_regular_grid(filtered, resolved)
    smoothed = _smooth_contiguous_valid_runs(gridded, resolved)
    x_start = (
        resolved.x_eval_start_mm if resolved.x_eval_start_mm is not None else source.x_eval_start_mm
    )
    x_end = resolved.x_eval_end_mm if resolved.x_eval_end_mm is not None else source.x_eval_end_mm
    output_model = StemSliceModel(
        slices=smoothed,
        model_quality=source.model_quality,
        x_eval_start_mm=x_start,
        x_eval_end_mm=x_end,
        reason_codes=source.reason_codes,
    ).validate()
    valid_output_count = sum(item.valid for item in smoothed)
    reasons: list[str] = []
    if rejected_count:
        reasons.append(OUTLIER_REASON)
    if interpolated_count:
        reasons.append(INTERPOLATED_REASON)
    if not valid_output_count:
        reasons.append(UNSUPPORTED_GRID_REASON)
    return StemSlicePreprocessingResult(
        model=output_model,
        input_slice_count=len(source.slices),
        valid_input_slice_count=valid_input_count,
        rejected_outlier_count=rejected_count,
        interpolated_slice_count=interpolated_count,
        valid_output_slice_count=valid_output_count,
        reason_codes=tuple(reasons),
    )


def _deduplicate_slices(slices: tuple[StemSliceGeometry, ...]) -> tuple[StemSliceGeometry, ...]:
    selected: dict[float, StemSliceGeometry] = {}
    for stem_slice in slices:
        existing = selected.get(stem_slice.x_mm)
        if existing is None or (stem_slice.valid, stem_slice.quality) > (
            existing.valid,
            existing.quality,
        ):
            selected[stem_slice.x_mm] = stem_slice
    return tuple(selected[x_mm] for x_mm in sorted(selected))


def _reject_lower_quality_outliers(
    slices: tuple[StemSliceGeometry, ...],
    config: StemSlicePreprocessingConfig,
) -> tuple[tuple[StemSliceGeometry, ...], int]:
    valid = [item for item in slices if item.valid]
    if len(valid) < 5:
        return slices, 0
    attributes = ("cy_mm", "cz_mm", "a_mm", "b_mm")
    bounds: dict[str, tuple[float, float]] = {}
    for attribute in attributes:
        values = [float(getattr(item, attribute)) for item in valid]
        median_value = statistics.median(values)
        mad = statistics.median(abs(value - median_value) for value in values)
        threshold = max(
            config.outlier_minimum_deviation_mm,
            config.outlier_mad_scale * 1.4826 * mad,
        )
        bounds[attribute] = (median_value, threshold)

    rejected = 0
    output: list[StemSliceGeometry] = []
    for stem_slice in slices:
        outlying_fields = sum(
            abs(float(getattr(stem_slice, attribute)) - median_value) > threshold
            for attribute, (median_value, threshold) in bounds.items()
        )
        should_reject = (
            stem_slice.valid
            and stem_slice.quality <= config.outlier_quality_ceiling
            and outlying_fields >= 2
        )
        if should_reject:
            rejected += 1
            output.append(
                replace(
                    stem_slice,
                    valid=False,
                    reason_codes=_append_reason(stem_slice.reason_codes, OUTLIER_REASON),
                )
            )
        else:
            output.append(stem_slice)
    return tuple(output), rejected


def _resample_to_regular_grid(
    slices: tuple[StemSliceGeometry, ...],
    config: StemSlicePreprocessingConfig,
) -> tuple[tuple[StemSliceGeometry, ...], int]:
    if not slices:
        return (), 0
    start = slices[0].x_mm
    end = slices[-1].x_mm
    count = int(math.floor((end - start) / config.slice_spacing_mm + 1.0e-9)) + 1
    grid = [start + index * config.slice_spacing_mm for index in range(max(count, 1))]
    valid = [item for item in slices if item.valid]
    valid_x = [item.x_mm for item in valid]
    all_x = [item.x_mm for item in slices]
    tolerance = max(1.0e-9, config.slice_spacing_mm * 1.0e-9)
    output: list[StemSliceGeometry] = []
    interpolated_count = 0
    for x_mm in grid:
        exact_index = bisect.bisect_left(all_x, x_mm)
        exact = (
            slices[exact_index]
            if exact_index < len(slices) and abs(slices[exact_index].x_mm - x_mm) <= tolerance
            else None
        )
        if exact is not None and exact.valid:
            output.append(replace(exact, x_mm=x_mm))
            continue

        right_index = bisect.bisect_right(valid_x, x_mm)
        left = valid[right_index - 1] if right_index > 0 else None
        right = valid[right_index] if right_index < len(valid) else None
        missing_span = (
            max(0.0, right.x_mm - left.x_mm - config.slice_spacing_mm)
            if left is not None and right is not None
            else math.inf
        )
        if (
            left is not None
            and right is not None
            and left.x_mm < x_mm < right.x_mm
            and missing_span <= config.max_interpolation_gap_mm + tolerance
        ):
            output.append(_interpolate_slice(left, right, x_mm, config))
            interpolated_count += 1
            continue

        support = exact or _nearest_slice(slices, x_mm)
        output.append(
            replace(
                support,
                x_mm=x_mm,
                valid=False,
                interpolated=False,
                reason_codes=_append_reason(support.reason_codes, UNSUPPORTED_GRID_REASON),
            )
        )
    return tuple(output), interpolated_count


def _interpolate_slice(
    left: StemSliceGeometry,
    right: StemSliceGeometry,
    x_mm: float,
    config: StemSlicePreprocessingConfig,
) -> StemSliceGeometry:
    fraction = (x_mm - left.x_mm) / (right.x_mm - left.x_mm)

    def interpolate(left_value: float, right_value: float) -> float:
        return left_value + fraction * (right_value - left_value)

    phi_delta = normalize_ellipse_angle_rad(right.phi_rad - left.phi_rad)
    phi = normalize_ellipse_angle_rad(left.phi_rad + fraction * phi_delta)
    first_axis = interpolate(left.a_mm, right.a_mm)
    second_axis = interpolate(left.b_mm, right.b_mm)
    return StemSliceGeometry(
        x_mm=x_mm,
        cy_mm=interpolate(left.cy_mm, right.cy_mm),
        cz_mm=interpolate(left.cz_mm, right.cz_mm),
        a_mm=max(first_axis, second_axis),
        b_mm=min(first_axis, second_axis),
        phi_rad=phi,
        quality=min(left.quality, right.quality) * config.interpolation_quality_factor,
        valid=True,
        interpolated=True,
        reason_codes=(INTERPOLATED_REASON,),
    )


def _nearest_slice(
    slices: tuple[StemSliceGeometry, ...],
    x_mm: float,
) -> StemSliceGeometry:
    return min(slices, key=lambda item: (abs(item.x_mm - x_mm), item.x_mm))


def _smooth_contiguous_valid_runs(
    slices: tuple[StemSliceGeometry, ...],
    config: StemSlicePreprocessingConfig,
) -> tuple[StemSliceGeometry, ...]:
    if config.smoothing_window_slices == 1 or config.smoothing_strength == 0:
        return slices
    output = list(slices)
    run_start = 0
    while run_start < len(slices):
        if not slices[run_start].valid:
            run_start += 1
            continue
        run_end = run_start + 1
        while run_end < len(slices) and slices[run_end].valid:
            run_end += 1
        run = slices[run_start:run_end]
        smoothed = _smooth_run(run, config)
        output[run_start:run_end] = smoothed
        run_start = run_end
    return tuple(output)


def _smooth_run(
    run: tuple[StemSliceGeometry, ...],
    config: StemSlicePreprocessingConfig,
) -> list[StemSliceGeometry]:
    radius = config.smoothing_window_slices // 2
    unwrapped_phi = _unwrap_ellipse_angles([item.phi_rad for item in run])
    output: list[StemSliceGeometry] = []
    for index, stem_slice in enumerate(run):
        start = max(0, index - radius)
        end = min(len(run), index + radius + 1)
        window = run[start:end]
        phi_median = statistics.median(unwrapped_phi[start:end])
        phi = unwrapped_phi[index] + config.smoothing_strength * (phi_median - unwrapped_phi[index])
        first_axis = _blend_with_window_median(
            stem_slice,
            window,
            "a_mm",
            config.smoothing_strength,
        )
        second_axis = _blend_with_window_median(
            stem_slice,
            window,
            "b_mm",
            config.smoothing_strength,
        )
        output.append(
            replace(
                stem_slice,
                cy_mm=_blend_with_window_median(
                    stem_slice,
                    window,
                    "cy_mm",
                    config.smoothing_strength,
                ),
                cz_mm=_blend_with_window_median(
                    stem_slice,
                    window,
                    "cz_mm",
                    config.smoothing_strength,
                ),
                a_mm=max(first_axis, second_axis),
                b_mm=min(first_axis, second_axis),
                phi_rad=normalize_ellipse_angle_rad(phi),
            )
        )
    return output


def _blend_with_window_median(
    stem_slice: StemSliceGeometry,
    window: tuple[StemSliceGeometry, ...],
    attribute: str,
    strength: float,
) -> float:
    original = float(getattr(stem_slice, attribute))
    median_value = statistics.median(float(getattr(item, attribute)) for item in window)
    return original + strength * (median_value - original)


def _unwrap_ellipse_angles(angles: list[float]) -> list[float]:
    if not angles:
        return []
    unwrapped = [angles[0]]
    for angle in angles[1:]:
        delta = normalize_ellipse_angle_rad(angle - unwrapped[-1])
        unwrapped.append(unwrapped[-1] + delta)
    return unwrapped


def _append_reason(reasons: tuple[str, ...], reason: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*reasons, reason)))
