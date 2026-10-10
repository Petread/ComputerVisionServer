from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

import cv2
import numpy as np
from cv_contracts.runtime import RuntimeStemCurvatureProcessCalibrationProfile

from cv_runtime.frame_analysis_cache import immutable_roi_mask
from cv_runtime.stem_process_coordinates import (
    validate_stem_curvature_process_calibration,
)

ALGORITHM_VERSION = "stem-horizontal-curvature.robust-inner-chord.v1"
REFERENCE_METHOD = "ROBUST_INNER_ENDPOINT_CHORD_V1"


@dataclass(frozen=True)
class StemHorizontalCurvatureConfig:
    station_count: int = 96
    inner_longitudinal_fraction: float = 0.8
    minimum_visible_length_ratio: float = 0.55
    minimum_common_segment_ratio: float = 0.4
    minimum_valid_station_ratio: float = 0.65
    minimum_valid_station_count: int = 24
    morphology_kernel_px: int = 3
    minimum_component_area_px: int = 120
    meaningful_component_fraction: float = 0.08
    maximum_meaningful_components: int = 1
    minimum_selected_component_fraction: float = 0.85
    minimum_width_mm: float = 10.0
    maximum_local_width_deviation_ratio: float = 0.15
    width_outlier_mad_scale: float = 4.0
    centerline_outlier_mad_scale: float = 4.5
    smoothing_window: int = 7
    endpoint_window_fraction: float = 0.12
    straight_bow_threshold_mm: float = 2.0
    s_bow_secondary_lobe_ratio: float = 0.35
    roi_boundary_margin_px: int = 2

    def validate(self) -> StemHorizontalCurvatureConfig:
        if self.station_count < 16:
            raise ValueError("station_count must be at least 16")
        for name, value in (
            ("inner_longitudinal_fraction", self.inner_longitudinal_fraction),
            ("minimum_visible_length_ratio", self.minimum_visible_length_ratio),
            ("minimum_common_segment_ratio", self.minimum_common_segment_ratio),
            ("minimum_valid_station_ratio", self.minimum_valid_station_ratio),
            ("meaningful_component_fraction", self.meaningful_component_fraction),
            (
                "minimum_selected_component_fraction",
                self.minimum_selected_component_fraction,
            ),
            ("endpoint_window_fraction", self.endpoint_window_fraction),
            ("s_bow_secondary_lobe_ratio", self.s_bow_secondary_lobe_ratio),
        ):
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        if self.minimum_valid_station_count < 8:
            raise ValueError("minimum_valid_station_count must be at least 8")
        if self.morphology_kernel_px < 1 or self.morphology_kernel_px > 31:
            raise ValueError("morphology_kernel_px must be in [1, 31]")
        if self.minimum_component_area_px < 1:
            raise ValueError("minimum_component_area_px must be positive")
        if self.maximum_meaningful_components != 1:
            raise ValueError("V1 forbids reconstruction from multiple meaningful fragments")
        if self.minimum_width_mm <= 0:
            raise ValueError("minimum_width_mm must be positive")
        if self.maximum_local_width_deviation_ratio <= 0:
            raise ValueError("maximum_local_width_deviation_ratio must be positive")
        if self.width_outlier_mad_scale <= 0 or self.centerline_outlier_mad_scale <= 0:
            raise ValueError("outlier MAD scales must be positive")
        if self.smoothing_window < 1 or self.smoothing_window > 31:
            raise ValueError("smoothing_window must be in [1, 31]")
        if self.straight_bow_threshold_mm < 0:
            raise ValueError("straight_bow_threshold_mm must be non-negative")
        if self.roi_boundary_margin_px < 0:
            raise ValueError("roi_boundary_margin_px must be non-negative")
        return self


@dataclass(frozen=True)
class HorizontalCurvatureStation:
    x_mm: float
    x_normalized: float
    y_center_mm: float | None
    width_mm: float | None
    valid: bool
    exclusion_reason: str | None = None
    y_reference_mm: float | None = None
    residual_mm: float | None = None
    weight: float = 0.0


@dataclass(frozen=True)
class HorizontalCommonSegment:
    x_start_mm: float | None = None
    x_end_mm: float | None = None
    length_mm: float = 0.0
    visible_x_start_mm: float | None = None
    visible_x_end_mm: float | None = None
    visible_length_mm: float = 0.0
    configured_inner_fraction: float = 0.0
    common_segment_ratio: float = 0.0
    minimum_common_segment_ratio: float = 0.0
    comparable: bool = False


@dataclass(frozen=True)
class StemHorizontalCurvatureResult:
    valid: bool
    observation_state: Literal[
        "accepted",
        "outside_curvature_roi",
        "insufficient_common_segment",
        "fragmented",
        "invalid",
    ]
    signed_bow_mm: float | None = None
    curvature_rms_mm: float | None = None
    curvature_maxabs_mm: float | None = None
    curvature_peak_to_peak_mm: float | None = None
    signed_bow_normalized_length: float | None = None
    signed_bow_normalized_width: float | None = None
    curvature_rms_normalized_length: float | None = None
    curvature_rms_normalized_width: float | None = None
    curvature_maxabs_normalized_length: float | None = None
    curvature_maxabs_normalized_width: float | None = None
    curvature_peak_to_peak_normalized_length: float | None = None
    curvature_peak_to_peak_normalized_width: float | None = None
    stations: tuple[HorizontalCurvatureStation, ...] = ()
    visible_length_ratio: float = 0.0
    valid_station_ratio: float = 0.0
    common_segment: HorizontalCommonSegment = field(
        default_factory=HorizontalCommonSegment
    )
    median_width_mm: float | None = None
    geometry_confidence: float = 0.0
    bow_shape_hint: Literal[
        "straight",
        "positive_c_bow",
        "negative_c_bow",
        "s_bow",
        "complex",
        "uncertain",
    ] = "uncertain"
    fragment_status: Literal[
        "single_component",
        "minor_noise_removed",
        "multiple_meaningful_fragments",
        "empty",
    ] = "empty"
    meaningful_component_count: int = 0
    selected_component_fraction: float = 0.0
    curvature_roi_clipped: bool = False
    touches_curvature_roi_boundary: bool = False
    quality_flags: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()


def measure_stem_horizontal_curvature(
    mask: Any,
    process_calibration: RuntimeStemCurvatureProcessCalibrationProfile | object,
    *,
    config: StemHorizontalCurvatureConfig | None = None,
    internal_aligned_process_grid: bool = False,
) -> StemHorizontalCurvatureResult:
    resolved = (config or StemHorizontalCurvatureConfig()).validate()
    profile = (
        RuntimeStemCurvatureProcessCalibrationProfile.model_validate(process_calibration)
        if internal_aligned_process_grid
        else validate_stem_curvature_process_calibration(process_calibration)
    )
    binary = _binary_mask(mask, profile)
    roi_mask = _curvature_roi_mask(profile)
    clipped = cv2.bitwise_and(binary, roi_mask)
    roi_clipped = bool(np.any(binary != clipped))
    if not np.any(clipped):
        return _invalid_result(
            "outside_curvature_roi",
            "curvature_observation_roi_empty",
            curvature_roi_clipped=roi_clipped,
        )

    cleaned = _clean_mask(clipped, resolved)
    component = _select_component(cleaned, resolved)
    if component[0] is None:
        state = "fragmented" if component[2] > 1 else "invalid"
        reason = (
            "multiple_meaningful_fragments"
            if component[2] > 1
            else "active_mask_component_missing"
        )
        return _invalid_result(
            state,
            reason,
            fragment_status=(
                "multiple_meaningful_fragments" if component[2] > 1 else "empty"
            ),
            meaningful_component_count=component[2],
            selected_component_fraction=component[1],
            curvature_roi_clipped=roi_clipped,
        )
    selected_mask, selected_fraction, meaningful_count, removed_noise = component
    touches_boundary = _touches_roi_boundary(
        selected_mask,
        roi_mask,
        margin_px=resolved.roi_boundary_margin_px,
    )
    stations = _sample_stations(selected_mask, profile, resolved)
    result = _measure_from_stations(
        stations,
        profile,
        resolved,
        selected_component_fraction=selected_fraction,
        meaningful_component_count=meaningful_count,
        fragment_status=("minor_noise_removed" if removed_noise else "single_component"),
        roi_clipped=roi_clipped,
        touches_boundary=touches_boundary,
    )
    return result


def rectified_process_point_to_pixel(
    x_mm: float,
    y_mm: float,
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
) -> tuple[float, float]:
    bounds = profile.process_bounds
    return (
        (x_mm - bounds.x_min_mm)
        / (bounds.x_max_mm - bounds.x_min_mm)
        * (profile.image_width - 1),
        (y_mm - bounds.y_min_mm)
        / (bounds.y_max_mm - bounds.y_min_mm)
        * (profile.image_height - 1),
    )


def rectified_pixel_to_process_point(
    x_px: float,
    y_px: float,
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
) -> tuple[float, float]:
    bounds = profile.process_bounds
    return (
        bounds.x_min_mm
        + x_px / max(profile.image_width - 1, 1) * (bounds.x_max_mm - bounds.x_min_mm),
        bounds.y_min_mm
        + y_px
        / max(profile.image_height - 1, 1)
        * (bounds.y_max_mm - bounds.y_min_mm),
    )


def mask_leading_x_mm(
    mask: Any,
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
) -> float | None:
    binary = _binary_mask(mask, profile)
    xs = np.flatnonzero(np.any(binary, axis=0))
    if len(xs) == 0:
        return None
    x_px = float(np.max(xs) if profile.conveyor_direction == "POSITIVE_X" else np.min(xs))
    return rectified_pixel_to_process_point(x_px, 0.0, profile)[0]


def freeze_gate_reached(
    leading_x_mm: float,
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
) -> bool:
    gate = profile.curvature_freeze_gate
    gate_x = (gate.process_start.x_mm + gate.process_end.x_mm) / 2.0
    if profile.conveyor_direction == "POSITIVE_X":
        return leading_x_mm >= gate_x
    return leading_x_mm <= gate_x


def _binary_mask(
    mask: Any,
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
) -> Any:
    array = np.asarray(mask)
    if array.ndim != 2:
        raise ValueError("horizontal curvature requires a 2D active-stem mask")
    if array.shape != (profile.image_height, profile.image_width):
        raise ValueError("active-stem mask resolution does not match process calibration")
    return np.multiply(array > 0, np.uint8(255))


def _curvature_roi_mask(
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
) -> Any:
    points = np.asarray(
        [
            rectified_process_point_to_pixel(point.x_mm, point.y_mm, profile)
            for point in profile.curvature_observation_roi.process_points
        ],
        dtype=np.float32,
    )
    return immutable_roi_mask(profile.calibration_hash, profile.image_width, profile.image_height,
                            tuple(map(tuple, np.rint(points).astype(np.int32).tolist())))


def _clean_mask(mask: Any, config: StemHorizontalCurvatureConfig) -> Any:
    size = config.morphology_kernel_px
    if size <= 1:
        return mask.copy()
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    opened = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    return cv2.morphologyEx(opened, cv2.MORPH_CLOSE, kernel)


def _select_component(
    mask: Any,
    config: StemHorizontalCurvatureConfig,
) -> tuple[Any | None, float, int, bool]:
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
    areas = [int(stats[index, cv2.CC_STAT_AREA]) for index in range(1, count)]
    total = sum(areas)
    if total <= 0:
        return None, 0.0, 0, False
    meaningful_threshold = max(
        config.minimum_component_area_px,
        int(math.ceil(total * config.meaningful_component_fraction)),
    )
    meaningful = [
        index + 1 for index, area in enumerate(areas) if area >= meaningful_threshold
    ]
    if len(meaningful) > config.maximum_meaningful_components:
        return None, max(areas) / total, len(meaningful), True
    if not meaningful:
        return None, max(areas) / total, 0, bool(len(areas) > 1)
    selected_label = meaningful[0]
    selected_area = areas[selected_label - 1]
    fraction = selected_area / total
    if fraction < config.minimum_selected_component_fraction:
        return None, fraction, len(meaningful), True
    selected = np.where(labels == selected_label, 255, 0).astype(np.uint8)
    return selected, fraction, len(meaningful), len(areas) > 1


def _sample_stations(
    mask: Any,
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
    config: StemHorizontalCurvatureConfig,
) -> list[HorizontalCurvatureStation]:
    roi_pixels = [
        rectified_process_point_to_pixel(point.x_mm, point.y_mm, profile)
        for point in profile.curvature_observation_roi.process_points
    ]
    roi_x_min = max(min(point[0] for point in roi_pixels), 0.0)
    roi_x_max = min(max(point[0] for point in roi_pixels), profile.image_width - 1.0)
    centers = np.linspace(roi_x_min, roi_x_max, config.station_count)
    half_bin = max((roi_x_max - roi_x_min) / max(config.station_count - 1, 1) * 0.45, 0.5)
    bounds = profile.process_bounds
    x_span = bounds.x_max_mm - bounds.x_min_mm
    occupied = np.ascontiguousarray(np.asarray(mask).T != 0)
    visible_columns = occupied.any(axis=1)
    low_by_column = np.argmax(occupied, axis=1)
    high_by_column = mask.shape[0] - 1 - np.argmax(occupied[:, ::-1], axis=1)
    low_by_column[~visible_columns] = mask.shape[0]
    high_by_column[~visible_columns] = -1
    starts = np.maximum(np.floor(centers - half_bin).astype(np.intp), 0)
    ends = np.minimum(np.ceil(centers + half_bin).astype(np.intp) + 1, profile.image_width)
    columns = starts[:, None] + np.arange(max(int(np.max(ends - starts)), 1))
    included = columns < ends[:, None]
    columns = np.minimum(columns, profile.image_width - 1)
    station_lows = np.min(np.where(included, low_by_column[columns], mask.shape[0]), axis=1)
    station_highs = np.max(np.where(included, high_by_column[columns], -1), axis=1)
    stations: list[HorizontalCurvatureStation] = []
    for center, low, high in zip(centers, station_lows, station_highs, strict=True):
        x_mm, _unused = rectified_pixel_to_process_point(center, 0.0, profile)
        x_normalized = (x_mm - bounds.x_min_mm) / x_span
        if high < low:
            stations.append(
                HorizontalCurvatureStation(
                    x_mm=x_mm,
                    x_normalized=x_normalized,
                    y_center_mm=None,
                    width_mm=None,
                    valid=False,
                    exclusion_reason="station_not_visible",
                )
            )
            continue
        y_low_px = float(low)
        y_high_px = float(high)
        _x, y_low_mm = rectified_pixel_to_process_point(center, y_low_px, profile)
        _x, y_high_mm = rectified_pixel_to_process_point(center, y_high_px, profile)
        width_mm = abs(y_high_mm - y_low_mm)
        stations.append(
            HorizontalCurvatureStation(
                x_mm=x_mm,
                x_normalized=x_normalized,
                y_center_mm=(y_low_mm + y_high_mm) / 2.0,
                width_mm=width_mm,
                valid=True,
            )
        )
    return stations


def _measure_from_stations(
    stations: list[HorizontalCurvatureStation],
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
    config: StemHorizontalCurvatureConfig,
    *,
    selected_component_fraction: float,
    meaningful_component_count: int,
    fragment_status: Literal["single_component", "minor_noise_removed"],
    roi_clipped: bool,
    touches_boundary: bool,
) -> StemHorizontalCurvatureResult:
    raw_valid = [index for index, station in enumerate(stations) if station.valid]
    if len(raw_valid) < config.minimum_valid_station_count:
        return _insufficient_result(
            stations,
            profile,
            config,
            reason="visible_station_count_insufficient",
            selected_component_fraction=selected_component_fraction,
            meaningful_component_count=meaningful_component_count,
            fragment_status=fragment_status,
            roi_clipped=roi_clipped,
            touches_boundary=touches_boundary,
        )
    first = raw_valid[0]
    last = raw_valid[-1]
    visible_start = stations[first].x_mm
    visible_end = stations[last].x_mm
    visible_length = max(visible_end - visible_start, 0.0)
    roi_x = [point.x_mm for point in profile.curvature_observation_roi.process_points]
    roi_span = max(max(roi_x) - min(roi_x), 1e-9)
    visible_ratio = min(visible_length / roi_span, 1.0)

    stations = _reject_width_outliers(stations, config)
    stations = _reject_centerline_outliers(stations, config)
    valid_after_rejection = [index for index, station in enumerate(stations) if station.valid]
    valid_station_ratio = len(valid_after_rejection) / max(len(raw_valid), 1)
    if (
        len(valid_after_rejection) < config.minimum_valid_station_count
        or valid_station_ratio < config.minimum_valid_station_ratio
        or visible_ratio < config.minimum_visible_length_ratio
    ):
        return _insufficient_result(
            stations,
            profile,
            config,
            reason="visible_or_valid_station_ratio_insufficient",
            selected_component_fraction=selected_component_fraction,
            meaningful_component_count=meaningful_component_count,
            fragment_status=fragment_status,
            roi_clipped=roi_clipped,
            touches_boundary=touches_boundary,
            visible_ratio=visible_ratio,
            valid_station_ratio=valid_station_ratio,
        )

    trim = (1.0 - config.inner_longitudinal_fraction) / 2.0
    common_start = visible_start + trim * visible_length
    common_end = visible_end - trim * visible_length
    common_indexes = [
        index
        for index in valid_after_rejection
        if common_start <= stations[index].x_mm <= common_end
    ]
    common_length = max(common_end - common_start, 0.0)
    common_ratio = min(common_length / roi_span, 1.0)
    descriptor = HorizontalCommonSegment(
        x_start_mm=common_start,
        x_end_mm=common_end,
        length_mm=common_length,
        visible_x_start_mm=visible_start,
        visible_x_end_mm=visible_end,
        visible_length_mm=visible_length,
        configured_inner_fraction=config.inner_longitudinal_fraction,
        common_segment_ratio=common_ratio,
        minimum_common_segment_ratio=config.minimum_common_segment_ratio,
        comparable=(
            common_ratio >= config.minimum_common_segment_ratio
            and len(common_indexes) >= config.minimum_valid_station_count
        ),
    )
    if not descriptor.comparable:
        return _insufficient_result(
            stations,
            profile,
            config,
            reason="common_segment_coverage_insufficient",
            selected_component_fraction=selected_component_fraction,
            meaningful_component_count=meaningful_component_count,
            fragment_status=fragment_status,
            roi_clipped=roi_clipped,
            touches_boundary=touches_boundary,
            visible_ratio=visible_ratio,
            valid_station_ratio=valid_station_ratio,
            descriptor=descriptor,
        )

    x = np.asarray([stations[index].x_mm for index in common_indexes], dtype=np.float64)
    y = np.asarray(
        [float(stations[index].y_center_mm) for index in common_indexes],
        dtype=np.float64,
    )
    y_smoothed = _smooth_profile(y, config.smoothing_window)
    endpoint_count = max(
        2,
        min(
            int(math.ceil(len(x) * config.endpoint_window_fraction)),
            max(len(x) // 3, 2),
        ),
    )
    x_start = float(np.median(x[:endpoint_count]))
    x_end = float(np.median(x[-endpoint_count:]))
    y_start = float(np.median(y_smoothed[:endpoint_count]))
    y_end = float(np.median(y_smoothed[-endpoint_count:]))
    slope = (y_end - y_start) / max(x_end - x_start, 1e-9)
    reference = y_start + slope * (x - x_start)
    residual = y_smoothed - reference
    local_s = (x - x[0]) / max(x[-1] - x[0], 1e-9)
    weights = np.maximum(np.sin(np.pi * local_s) ** 2, 0.05)
    weighted_denominator = float(np.sum(weights))
    rms = float(math.sqrt(float(np.sum(weights * residual**2)) / weighted_denominator))
    central = weights >= 0.2
    central_residual = residual[central] if np.any(central) else residual
    signed = float(central_residual[int(np.argmax(np.abs(central_residual)))])
    maxabs = float(np.max(np.abs(central_residual)))
    peak_to_peak = float(np.max(central_residual) - np.min(central_residual))
    median_width = float(
        np.median(
            [
                float(stations[index].width_mm)
                for index in common_indexes
                if stations[index].width_mm is not None
            ]
        )
    )
    length_denominator = max(common_length, 1e-9)
    width_denominator = max(median_width, 1e-9)
    shape = _shape_hint(
        central_residual,
        maxabs=maxabs,
        signed=signed,
        config=config,
    )
    updated = list(stations)
    for position, index in enumerate(common_indexes):
        updated[index] = HorizontalCurvatureStation(
            x_mm=updated[index].x_mm,
            x_normalized=updated[index].x_normalized,
            y_center_mm=float(y_smoothed[position]),
            width_mm=updated[index].width_mm,
            valid=True,
            y_reference_mm=float(reference[position]),
            residual_mm=float(residual[position]),
            weight=float(weights[position]),
        )

    quality_flags: list[str] = []
    if roi_clipped:
        quality_flags.append("mask_clipped_to_curvature_roi")
    if touches_boundary:
        quality_flags.append("stem_touches_curvature_roi_boundary")
    if fragment_status == "minor_noise_removed":
        quality_flags.append("minor_components_removed_without_merging")
    coverage_score = min(
        visible_ratio / max(config.minimum_visible_length_ratio, 1e-9),
        1.0,
    )
    common_score = min(
        common_ratio / max(config.minimum_common_segment_ratio, 1e-9),
        1.0,
    )
    boundary_score = 0.85 if touches_boundary else 1.0
    confidence = min(
        max(
            0.3 * valid_station_ratio
            + 0.25 * coverage_score
            + 0.2 * common_score
            + 0.15 * selected_component_fraction
            + 0.1 * boundary_score,
            0.0,
        ),
        1.0,
    )
    return StemHorizontalCurvatureResult(
        valid=True,
        observation_state="accepted",
        signed_bow_mm=signed,
        curvature_rms_mm=rms,
        curvature_maxabs_mm=maxabs,
        curvature_peak_to_peak_mm=peak_to_peak,
        signed_bow_normalized_length=signed / length_denominator,
        signed_bow_normalized_width=signed / width_denominator,
        curvature_rms_normalized_length=rms / length_denominator,
        curvature_rms_normalized_width=rms / width_denominator,
        curvature_maxabs_normalized_length=maxabs / length_denominator,
        curvature_maxabs_normalized_width=maxabs / width_denominator,
        curvature_peak_to_peak_normalized_length=peak_to_peak / length_denominator,
        curvature_peak_to_peak_normalized_width=peak_to_peak / width_denominator,
        stations=tuple(updated),
        visible_length_ratio=visible_ratio,
        valid_station_ratio=valid_station_ratio,
        common_segment=descriptor,
        median_width_mm=median_width,
        geometry_confidence=confidence,
        bow_shape_hint=shape,
        fragment_status=fragment_status,
        meaningful_component_count=meaningful_component_count,
        selected_component_fraction=selected_component_fraction,
        curvature_roi_clipped=roi_clipped,
        touches_curvature_roi_boundary=touches_boundary,
        quality_flags=tuple(quality_flags),
    )


def _reject_width_outliers(
    stations: list[HorizontalCurvatureStation],
    config: StemHorizontalCurvatureConfig,
) -> list[HorizontalCurvatureStation]:
    values = np.asarray(
        [station.width_mm if station.valid else np.nan for station in stations],
        dtype=np.float64,
    )
    trend = _robust_linear_trend(values)
    expected = trend
    deviations = np.abs(values - expected)
    finite = np.isfinite(deviations)
    scale = _robust_scale(deviations[finite])
    result = list(stations)
    for index, station in enumerate(stations):
        if not station.valid or station.width_mm is None:
            continue
        threshold = max(
            config.width_outlier_mad_scale * scale,
            config.maximum_local_width_deviation_ratio * max(expected[index], 1e-9),
        )
        reason = None
        if station.width_mm < config.minimum_width_mm:
            reason = "width_below_minimum"
        elif deviations[index] > threshold:
            reason = "local_width_outlier_or_branch_bulge"
        if reason:
            result[index] = HorizontalCurvatureStation(
                x_mm=station.x_mm,
                x_normalized=station.x_normalized,
                y_center_mm=station.y_center_mm,
                width_mm=station.width_mm,
                valid=False,
                exclusion_reason=reason,
            )
    return result


def _reject_centerline_outliers(
    stations: list[HorizontalCurvatureStation],
    config: StemHorizontalCurvatureConfig,
) -> list[HorizontalCurvatureStation]:
    values = np.asarray(
        [station.y_center_mm if station.valid else np.nan for station in stations],
        dtype=np.float64,
    )
    expected = _robust_polynomial_trend(values, degree=2)
    signed_deviations = values - expected
    finite = np.isfinite(signed_deviations)
    center = float(np.median(signed_deviations[finite])) if np.any(finite) else 0.0
    deviations = np.abs(signed_deviations - center)
    scale = _robust_scale(signed_deviations[finite])
    threshold = max(config.centerline_outlier_mad_scale * scale, 0.5)
    result = list(stations)
    for index, station in enumerate(stations):
        if station.valid and deviations[index] > threshold:
            result[index] = HorizontalCurvatureStation(
                x_mm=station.x_mm,
                x_normalized=station.x_normalized,
                y_center_mm=station.y_center_mm,
                width_mm=station.width_mm,
                valid=False,
                exclusion_reason="local_centerline_outlier",
            )
    return result


def _rolling_nanmedian(values: Any, window: int) -> Any:
    radius = max(window // 2, 1)
    result = np.empty_like(values)
    for index in range(len(values)):
        sample = values[max(0, index - radius) : min(len(values), index + radius + 1)]
        finite = sample[np.isfinite(sample)]
        result[index] = float(np.median(finite)) if len(finite) else np.nan
    return result


def _robust_linear_trend(values: Any) -> Any:
    finite_indexes = np.flatnonzero(np.isfinite(values))
    if len(finite_indexes) < 2:
        return _rolling_nanmedian(values, 9)
    slopes: list[float] = []
    for position, first in enumerate(finite_indexes[:-1]):
        later = finite_indexes[position + 1 :]
        slopes.extend(
            ((values[later] - values[first]) / (later - first)).tolist()
        )
    slope = float(np.median(slopes)) if slopes else 0.0
    intercept = float(
        np.median(values[finite_indexes] - slope * finite_indexes.astype(np.float64))
    )
    return intercept + slope * np.arange(len(values), dtype=np.float64)


def _robust_polynomial_trend(values: Any, *, degree: int) -> Any:
    indexes = np.arange(len(values), dtype=np.float64)
    active = np.isfinite(values)
    if int(np.count_nonzero(active)) < degree + 2:
        return _rolling_nanmedian(values, 9)
    coefficients = np.polyfit(indexes[active], values[active], degree)
    for _ in range(4):
        expected = np.polyval(coefficients, indexes)
        residual = values - expected
        finite_residual = residual[active]
        center = float(np.median(finite_residual))
        scale = _robust_scale(finite_residual)
        candidate = np.isfinite(values) & (
            np.abs(residual - center) <= max(3.5 * scale, 0.5)
        )
        if int(np.count_nonzero(candidate)) < degree + 2 or np.array_equal(candidate, active):
            break
        active = candidate
        coefficients = np.polyfit(indexes[active], values[active], degree)
    return np.polyval(coefficients, indexes)


def _robust_scale(values: Any) -> float:
    if len(values) == 0:
        return 0.0
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    return max(1.4826 * mad, 1e-6)


def _smooth_profile(values: Any, window: int) -> Any:
    size = min(window if window % 2 == 1 else window + 1, len(values))
    if size <= 2:
        return values.copy()
    if size % 2 == 0:
        size -= 1
    median = _rolling_nanmedian(np.asarray(values, dtype=np.float64), size)
    radius = size // 2
    kernel = np.arange(1, radius + 2, dtype=np.float64)
    kernel = np.concatenate((kernel, kernel[-2::-1]))
    kernel /= np.sum(kernel)
    padded = np.pad(median, (radius, radius), mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def _shape_hint(
    residual: Any,
    *,
    maxabs: float,
    signed: float,
    config: StemHorizontalCurvatureConfig,
) -> Literal[
    "straight",
    "positive_c_bow",
    "negative_c_bow",
    "s_bow",
    "complex",
    "uncertain",
]:
    if maxabs <= config.straight_bow_threshold_mm:
        return "straight"
    positive = max(float(np.max(residual)), 0.0)
    negative = max(-float(np.min(residual)), 0.0)
    if min(positive, negative) >= config.s_bow_secondary_lobe_ratio * max(
        positive,
        negative,
    ):
        sign_changes = int(np.sum(np.diff(np.sign(residual)) != 0))
        return "s_bow" if sign_changes >= 1 else "complex"
    return "positive_c_bow" if signed > 0 else "negative_c_bow"


def _touches_roi_boundary(mask: Any, roi_mask: Any, *, margin_px: int) -> bool:
    if margin_px <= 0:
        return False
    kernel_size = 2 * margin_px + 1
    kernel = np.ones((kernel_size, kernel_size), dtype=np.uint8)
    interior = cv2.erode(roi_mask, kernel)
    boundary = cv2.subtract(roi_mask, interior)
    return bool(np.any(cv2.bitwise_and(mask, boundary)))


def _insufficient_result(
    stations: list[HorizontalCurvatureStation],
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
    config: StemHorizontalCurvatureConfig,
    *,
    reason: str,
    selected_component_fraction: float,
    meaningful_component_count: int,
    fragment_status: Literal["single_component", "minor_noise_removed"],
    roi_clipped: bool,
    touches_boundary: bool,
    visible_ratio: float = 0.0,
    valid_station_ratio: float = 0.0,
    descriptor: HorizontalCommonSegment | None = None,
) -> StemHorizontalCurvatureResult:
    del profile
    return StemHorizontalCurvatureResult(
        valid=False,
        observation_state="insufficient_common_segment",
        stations=tuple(stations),
        visible_length_ratio=visible_ratio,
        valid_station_ratio=valid_station_ratio,
        common_segment=descriptor
        or HorizontalCommonSegment(
            configured_inner_fraction=config.inner_longitudinal_fraction,
            minimum_common_segment_ratio=config.minimum_common_segment_ratio,
        ),
        fragment_status=fragment_status,
        meaningful_component_count=meaningful_component_count,
        selected_component_fraction=selected_component_fraction,
        curvature_roi_clipped=roi_clipped,
        touches_curvature_roi_boundary=touches_boundary,
        quality_flags=("no_curvature_value_emitted",),
        reason_codes=(reason,),
    )


def _invalid_result(
    state: Literal["outside_curvature_roi", "fragmented", "invalid"],
    reason: str,
    *,
    fragment_status: Literal[
        "single_component",
        "minor_noise_removed",
        "multiple_meaningful_fragments",
        "empty",
    ] = "empty",
    meaningful_component_count: int = 0,
    selected_component_fraction: float = 0.0,
    curvature_roi_clipped: bool = False,
) -> StemHorizontalCurvatureResult:
    return StemHorizontalCurvatureResult(
        valid=False,
        observation_state=state,
        fragment_status=fragment_status,
        meaningful_component_count=meaningful_component_count,
        selected_component_fraction=selected_component_fraction,
        curvature_roi_clipped=curvature_roi_clipped,
        quality_flags=("no_curvature_value_emitted",),
        reason_codes=(reason,),
    )
