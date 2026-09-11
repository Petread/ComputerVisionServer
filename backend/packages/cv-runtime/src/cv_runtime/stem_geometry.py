from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from operator import attrgetter
from typing import Literal

import cv2
import numpy as np


class StemGeometryErrorCode(StrEnum):
    NO_MATCHING_SEGMENT = "no_matching_segment"
    MASK_TOO_SMALL = "mask_too_small"
    MASK_TOO_FRAGMENTED = "mask_too_fragmented"
    INSUFFICIENT_VISIBLE_LENGTH = "insufficient_visible_length"
    INSUFFICIENT_CROSS_SECTIONS = "insufficient_cross_sections"
    BOTH_ENDS_CLIPPED = "both_ends_clipped"
    GEOMETRY_UNSTABLE = "geometry_unstable"
    IMAGE_CONTEXT_MISSING = "image_context_missing"
    INVALID_MASK = "invalid_mask"


class StemGeometryQualityFlag(StrEnum):
    FRAGMENTS_REMOVED = "fragments_removed"
    HOLES_FILLED = "holes_filled"
    START_END_CLIPPED = "start_end_clipped"
    FINISH_END_CLIPPED = "finish_end_clipped"
    CROSS_SECTION_OUTLIERS = "cross_section_outliers"
    CROSS_SECTION_GAPS = "cross_section_gaps"
    LOCAL_BULGE_LIMITED = "local_bulge_limited"
    LOW_AXIS_STABILITY = "low_axis_stability"


class StemCrossSectionRejectionCode(StrEnum):
    NO_MASK_SUPPORT = "no_mask_support"
    WIDTH_OUTLIER = "width_outlier"
    CENTER_OUTLIER = "center_outlier"


StemShapeClass = Literal["straight", "c_bow", "s_bow_or_complex", "uncertain"]
PolygonCoordinateSpace = Literal["auto", "normalized", "pixel"]
PolygonRasterizationMode = Literal["full_image", "oriented_roi"]


@dataclass(frozen=True)
class StemGeometryConfig:
    centerline_sample_count: int = 32
    width_sample_count: int = 16
    cross_section_count: int = 96
    min_component_area_px: int = 300
    min_visible_length_px: float = 60.0
    max_fragmentation_fraction: float = 0.35
    min_valid_cross_section_fraction: float = 0.55
    min_visible_length_fraction: float = 0.65
    min_axis_eigenvalue_ratio: float = 3.0
    cleanup_kernel_size: int = 3
    cross_section_band_scale: float = 0.7
    boundary_quantile: float = 0.02
    longitudinal_trim_quantile: float = 0.002
    local_outlier_window: int = 9
    width_outlier_ratio: float = 1.65
    outlier_mad_multiplier: float = 4.5
    center_outlier_width_fraction: float = 0.35
    smoothing_passes: int = 2
    straight_threshold_normalized: float = 0.04
    complex_lobe_threshold_normalized: float = 0.08
    border_margin_px: int = 2
    clipped_end_window_fraction: float = 0.08
    polygon_coordinate_space: PolygonCoordinateSpace = "auto"
    polygon_rasterization_mode: PolygonRasterizationMode = "full_image"
    axis_refinement_passes: int = 2

    def __post_init__(self) -> None:
        if self.centerline_sample_count < 8:
            raise ValueError("centerline_sample_count must be at least 8")
        if self.width_sample_count < 4:
            raise ValueError("width_sample_count must be at least 4")
        if self.cross_section_count < max(
            24,
            2 * self.centerline_sample_count,
            2 * self.width_sample_count,
        ):
            raise ValueError(
                "cross_section_count must provide at least two measurements per sample"
            )
        if self.min_component_area_px < 1:
            raise ValueError("min_component_area_px must be positive")
        if self.min_visible_length_px <= 0:
            raise ValueError("min_visible_length_px must be positive")
        for name, value in (
            ("max_fragmentation_fraction", self.max_fragmentation_fraction),
            ("min_valid_cross_section_fraction", self.min_valid_cross_section_fraction),
            ("min_visible_length_fraction", self.min_visible_length_fraction),
            ("clipped_end_window_fraction", self.clipped_end_window_fraction),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.min_axis_eigenvalue_ratio <= 1.0:
            raise ValueError("min_axis_eigenvalue_ratio must be greater than 1")
        if self.cleanup_kernel_size < 1 or self.cleanup_kernel_size % 2 == 0:
            raise ValueError("cleanup_kernel_size must be a positive odd number")
        if self.cross_section_band_scale <= 0:
            raise ValueError("cross_section_band_scale must be positive")
        if not 0.0 <= self.boundary_quantile < 0.25:
            raise ValueError("boundary_quantile must be in [0, 0.25)")
        if not 0.0 <= self.longitudinal_trim_quantile < 0.1:
            raise ValueError("longitudinal_trim_quantile must be in [0, 0.1)")
        if self.local_outlier_window < 3 or self.local_outlier_window % 2 == 0:
            raise ValueError("local_outlier_window must be an odd number of at least 3")
        if self.width_outlier_ratio <= 1.0:
            raise ValueError("width_outlier_ratio must be greater than 1")
        if self.outlier_mad_multiplier <= 0 or self.center_outlier_width_fraction <= 0:
            raise ValueError("outlier limits must be positive")
        if self.smoothing_passes < 0:
            raise ValueError("smoothing_passes must not be negative")
        if self.straight_threshold_normalized <= 0:
            raise ValueError("straight_threshold_normalized must be positive")
        if self.complex_lobe_threshold_normalized <= 0:
            raise ValueError("complex_lobe_threshold_normalized must be positive")
        if self.border_margin_px < 0:
            raise ValueError("border_margin_px must not be negative")
        if not 0 <= self.axis_refinement_passes <= 4:
            raise ValueError("axis_refinement_passes must be between 0 and 4")
        if self.polygon_rasterization_mode not in {"full_image", "oriented_roi"}:
            raise ValueError("polygon_rasterization_mode must be full_image or oriented_roi")


@dataclass(frozen=True)
class StemImagePoint:
    x: float
    y: float


@dataclass(frozen=True)
class StemReferenceAxis:
    start: StemImagePoint
    end: StemImagePoint
    direction: StemImagePoint
    normal: StemImagePoint
    length_px: float


@dataclass(frozen=True)
class StemCrossSectionIssue:
    normalized_s: float
    reason: StemCrossSectionRejectionCode
    observed_width_px: float | None = None
    expected_width_px: float | None = None


@dataclass(frozen=True)
class StemGeometryIssue:
    code: StemGeometryErrorCode
    message: str


@dataclass(frozen=True)
class StemGeometryResult:
    """Pixel-domain geometry of the selected visible stem component.

    ``visible_length_fraction`` describes reliable support within the selected visible
    projection. It does not estimate a hidden or physical full-stem length.
    """

    valid: bool
    image_width: int
    image_height: int
    reference_axis: StemReferenceAxis | None
    visible_endpoints: tuple[StemImagePoint, StemImagePoint] | None
    normalized_s: tuple[float, ...]
    centerline_image_xy: tuple[StemImagePoint, ...]
    centerline_valid_mask: tuple[bool, ...]
    residual_profile_px: tuple[float, ...]
    residual_profile_normalized: tuple[float, ...]
    width_normalized_s: tuple[float, ...]
    width_profile_px: tuple[float, ...]
    width_valid_mask: tuple[bool, ...]
    signed_curvature: float | None
    signed_bow_px: float | None
    rms_residual_px: float | None
    max_abs_residual_px: float | None
    peak_to_peak_px: float | None
    median_width_px: float | None
    rms_residual_normalized: float | None
    visible_length_fraction: float
    valid_cross_section_fraction: float
    rejected_cross_sections: tuple[StemCrossSectionIssue, ...]
    uncertain_cross_sections: tuple[StemCrossSectionIssue, ...]
    shape_class: StemShapeClass
    quality_score: float
    quality_flags: tuple[StemGeometryQualityFlag, ...]
    error_codes: tuple[StemGeometryErrorCode, ...]
    issues: tuple[StemGeometryIssue, ...]
    selected_component_area_px: int
    meaningful_component_count: int
    selected_component_fraction: float


@dataclass(frozen=True)
class _ComponentSelection:
    mask: np.ndarray
    area_px: int
    meaningful_component_count: int
    selected_fraction: float
    holes_filled: bool


@dataclass(frozen=True)
class _GeometrySupport:
    selection: _ComponentSelection
    points: np.ndarray


@dataclass(frozen=True)
class _CrossSection:
    normalized_s: float
    longitudinal_px: float
    transverse_center_px: float
    width_px: float
    center_xy: np.ndarray


_ERROR_MESSAGES: dict[StemGeometryErrorCode, str] = {
    StemGeometryErrorCode.NO_MATCHING_SEGMENT: "The input contains no foreground stem segment.",
    StemGeometryErrorCode.MASK_TOO_SMALL: "The largest connected stem component is too small.",
    StemGeometryErrorCode.MASK_TOO_FRAGMENTED: (
        "No connected component contains enough of the meaningful foreground area."
    ),
    StemGeometryErrorCode.INSUFFICIENT_VISIBLE_LENGTH: (
        "The visible projected stem length is insufficient for a stable profile."
    ),
    StemGeometryErrorCode.INSUFFICIENT_CROSS_SECTIONS: (
        "Too few reliable orthogonal cross-sections remain after robust filtering."
    ),
    StemGeometryErrorCode.BOTH_ENDS_CLIPPED: (
        "Both longitudinal ends touch the image boundary; the visible extent is ambiguous."
    ),
    StemGeometryErrorCode.GEOMETRY_UNSTABLE: (
        "The selected component has no sufficiently stable longitudinal direction."
    ),
    StemGeometryErrorCode.IMAGE_CONTEXT_MISSING: (
        "Positive image width and image height are required."
    ),
    StemGeometryErrorCode.INVALID_MASK: (
        "The input must be a binary image-sized mask or a polygon with at least three points."
    ),
}
_POINT_ATTRIBUTES = attrgetter("x", "y")
_MAX_CROSS_SECTION_SUPPORT_POINTS = 60_000
_MAX_ORIENTED_POLYGON_SUPPORT_POINTS = 20_000


def analyze_stem_geometry(
    mask_or_polygon: np.ndarray | Sequence[object],
    *,
    image_width: int,
    image_height: int,
    config: StemGeometryConfig | None = None,
) -> StemGeometryResult:
    """Describe one segmented stem in the 2D image projection.

    The sign convention is image based and deterministic: the longitudinal axis is oriented
    toward positive image x (or positive image y for a vertical axis), and its positive normal
    is the corresponding clockwise image normal ``(-dy, dx)``. The function never produces
    metric or 3D quantities.
    """

    resolved = config or StemGeometryConfig()
    if image_width <= 0 or image_height <= 0:
        return _invalid_result(
            image_width=image_width,
            image_height=image_height,
            config=resolved,
            error_codes=(StemGeometryErrorCode.IMAGE_CONTEXT_MISSING,),
        )

    support = (
        _oriented_polygon_support(
            mask_or_polygon,
            image_width=image_width,
            image_height=image_height,
            config=resolved,
        )
        if resolved.polygon_rasterization_mode == "oriented_roi"
        else None
    )
    if support is None:
        binary_mask = _binary_mask(
            mask_or_polygon,
            image_width=image_width,
            image_height=image_height,
            config=resolved,
        )
        if binary_mask is None:
            return _invalid_result(
                image_width=image_width,
                image_height=image_height,
                config=resolved,
                error_codes=(StemGeometryErrorCode.INVALID_MASK,),
            )
        if not np.any(binary_mask):
            return _invalid_result(
                image_width=image_width,
                image_height=image_height,
                config=resolved,
                error_codes=(StemGeometryErrorCode.NO_MATCHING_SEGMENT,),
            )
        selection = _largest_meaningful_component(binary_mask, resolved)
        points = _mask_points(selection.mask)
    else:
        selection = support.selection
        points = support.points
        if points.size == 0:
            return _invalid_result(
                image_width=image_width,
                image_height=image_height,
                config=resolved,
                error_codes=(StemGeometryErrorCode.NO_MATCHING_SEGMENT,),
                selection=selection,
            )

    quality_flags: list[StemGeometryQualityFlag] = []
    if selection.meaningful_component_count > 1:
        quality_flags.append(StemGeometryQualityFlag.FRAGMENTS_REMOVED)
    if selection.holes_filled:
        quality_flags.append(StemGeometryQualityFlag.HOLES_FILLED)

    if selection.area_px < resolved.min_component_area_px:
        return _invalid_result(
            image_width=image_width,
            image_height=image_height,
            config=resolved,
            error_codes=(StemGeometryErrorCode.MASK_TOO_SMALL,),
            quality_flags=quality_flags,
            selection=selection,
        )
    if selection.selected_fraction < 1.0 - resolved.max_fragmentation_fraction:
        return _invalid_result(
            image_width=image_width,
            image_height=image_height,
            config=resolved,
            error_codes=(StemGeometryErrorCode.MASK_TOO_FRAGMENTED,),
            quality_flags=quality_flags,
            selection=selection,
        )

    origin, direction, eigenvalue_ratio = _initial_axis(points)
    if eigenvalue_ratio < resolved.min_axis_eigenvalue_ratio:
        quality_flags.append(StemGeometryQualityFlag.LOW_AXIS_STABILITY)
        return _invalid_result(
            image_width=image_width,
            image_height=image_height,
            config=resolved,
            error_codes=(StemGeometryErrorCode.GEOMETRY_UNSTABLE,),
            quality_flags=quality_flags,
            selection=selection,
        )

    projected_length = _projected_length(points, origin, direction, resolved)
    if projected_length < resolved.min_visible_length_px:
        return _invalid_result(
            image_width=image_width,
            image_height=image_height,
            config=resolved,
            error_codes=(StemGeometryErrorCode.INSUFFICIENT_VISIBLE_LENGTH,),
            quality_flags=quality_flags,
            selection=selection,
        )

    for _iteration in range(resolved.axis_refinement_passes):
        measured, _missing = _measure_cross_sections(points, origin, direction, resolved)
        accepted, _rejected, _uncertain = _filter_cross_sections(measured, resolved)
        if len(accepted) < 4:
            break
        origin, direction = _refine_axis(origin, direction, accepted)

    measured, missing = _measure_cross_sections(points, origin, direction, resolved)
    accepted, rejected, uncertain = _filter_cross_sections(measured, resolved)
    rejected = [*missing, *rejected]
    valid_cross_section_fraction = len(accepted) / resolved.cross_section_count
    if (
        len(accepted) < 4
        or valid_cross_section_fraction < resolved.min_valid_cross_section_fraction
    ):
        quality_flags.append(StemGeometryQualityFlag.CROSS_SECTION_GAPS)
        return _invalid_result(
            image_width=image_width,
            image_height=image_height,
            config=resolved,
            error_codes=(StemGeometryErrorCode.INSUFFICIENT_CROSS_SECTIONS,),
            quality_flags=quality_flags,
            selection=selection,
            valid_cross_section_fraction=valid_cross_section_fraction,
            rejected_cross_sections=rejected,
            uncertain_cross_sections=uncertain,
        )

    reference_origin, reference_direction = _refine_axis(origin, direction, accepted)
    reference_normal = _normal(reference_direction)
    point_longitudinal = (points - reference_origin) @ reference_direction
    lower_t, upper_t = _trimmed_bounds(point_longitudinal, resolved.longitudinal_trim_quantile)
    reference_length = upper_t - lower_t
    if reference_length < resolved.min_visible_length_px:
        return _invalid_result(
            image_width=image_width,
            image_height=image_height,
            config=resolved,
            error_codes=(StemGeometryErrorCode.INSUFFICIENT_VISIBLE_LENGTH,),
            quality_flags=quality_flags,
            selection=selection,
            valid_cross_section_fraction=valid_cross_section_fraction,
            rejected_cross_sections=rejected,
            uncertain_cross_sections=uncertain,
        )

    center_points = np.vstack([section.center_xy for section in accepted])
    section_t = (center_points - reference_origin) @ reference_direction
    section_r = (center_points - reference_origin) @ reference_normal
    section_widths = np.asarray([section.width_px for section in accepted], dtype=np.float64)
    order = np.argsort(section_t)
    section_t = section_t[order]
    section_r = section_r[order]
    section_widths = section_widths[order]
    unique_t, unique_indices = np.unique(section_t, return_index=True)
    section_t = unique_t
    section_r = section_r[unique_indices]
    section_widths = section_widths[unique_indices]

    visible_length_fraction = float(
        np.clip((section_t[-1] - section_t[0]) / max(reference_length, 1e-9), 0.0, 1.0)
    )
    if visible_length_fraction < resolved.min_visible_length_fraction:
        return _invalid_result(
            image_width=image_width,
            image_height=image_height,
            config=resolved,
            error_codes=(StemGeometryErrorCode.INSUFFICIENT_VISIBLE_LENGTH,),
            quality_flags=quality_flags,
            selection=selection,
            visible_length_fraction=visible_length_fraction,
            valid_cross_section_fraction=valid_cross_section_fraction,
            rejected_cross_sections=rejected,
            uncertain_cross_sections=uncertain,
        )

    start_clipped, finish_clipped = _clipped_ends(
        points,
        reference_origin,
        reference_direction,
        lower_t,
        upper_t,
        image_width=image_width,
        image_height=image_height,
        config=resolved,
    )
    if start_clipped:
        quality_flags.append(StemGeometryQualityFlag.START_END_CLIPPED)
    if finish_clipped:
        quality_flags.append(StemGeometryQualityFlag.FINISH_END_CLIPPED)
    if start_clipped and finish_clipped:
        return _invalid_result(
            image_width=image_width,
            image_height=image_height,
            config=resolved,
            error_codes=(StemGeometryErrorCode.BOTH_ENDS_CLIPPED,),
            quality_flags=quality_flags,
            selection=selection,
            visible_length_fraction=visible_length_fraction,
            valid_cross_section_fraction=valid_cross_section_fraction,
            rejected_cross_sections=rejected,
            uncertain_cross_sections=uncertain,
        )

    if rejected:
        quality_flags.append(StemGeometryQualityFlag.CROSS_SECTION_OUTLIERS)
    if any(item.reason == StemCrossSectionRejectionCode.WIDTH_OUTLIER for item in rejected):
        quality_flags.append(StemGeometryQualityFlag.LOCAL_BULGE_LIMITED)

    normalized_s = np.linspace(0.0, 1.0, resolved.centerline_sample_count)
    centerline_t = lower_t + normalized_s * reference_length
    raw_centerline_r = np.interp(centerline_t, section_t, section_r)
    smooth_centerline_r = _smooth_profile(raw_centerline_r, resolved.smoothing_passes)

    residual_slope, residual_offset = _robust_line_fit(normalized_s, smooth_centerline_r)
    line_start = (
        reference_origin + lower_t * reference_direction + residual_offset * reference_normal
    )
    line_end = (
        reference_origin
        + upper_t * reference_direction
        + (residual_offset + residual_slope) * reference_normal
    )
    final_direction = _unit(line_end - line_start)
    if float(final_direction @ reference_direction) < 0:
        final_direction = -final_direction
        line_start, line_end = line_end, line_start
        normalized_s = normalized_s[::-1]
        centerline_t = centerline_t[::-1]
        smooth_centerline_r = smooth_centerline_r[::-1]
    final_normal = _normal(final_direction)

    centerline_xy_array = (
        reference_origin[None, :]
        + centerline_t[:, None] * reference_direction[None, :]
        + smooth_centerline_r[:, None] * reference_normal[None, :]
    )
    residual_profile = (centerline_xy_array - line_start[None, :]) @ final_normal
    axis_length = float(np.linalg.norm(line_end - line_start))
    axis_progress = (centerline_xy_array - line_start[None, :]) @ final_direction
    if axis_length > 0:
        normalized_s = np.clip(axis_progress / axis_length, 0.0, 1.0)

    median_width = float(np.median(section_widths))
    if median_width <= 0:
        return _invalid_result(
            image_width=image_width,
            image_height=image_height,
            config=resolved,
            error_codes=(StemGeometryErrorCode.GEOMETRY_UNSTABLE,),
            quality_flags=quality_flags,
            selection=selection,
            visible_length_fraction=visible_length_fraction,
            valid_cross_section_fraction=valid_cross_section_fraction,
            rejected_cross_sections=rejected,
            uncertain_cross_sections=uncertain,
        )

    residual_profile_normalized = residual_profile / median_width
    chord = residual_profile[0] * (1.0 - normalized_s) + residual_profile[-1] * normalized_s
    bow_profile = residual_profile - chord
    bow_weights = np.maximum(4.0 * normalized_s * (1.0 - normalized_s), 0.0)
    signed_bow = float(np.sum(bow_profile * bow_weights) / max(float(np.sum(bow_weights)), 1e-9))
    signed_curvature = signed_bow / median_width
    rms_residual = float(np.sqrt(np.mean(np.square(residual_profile))))
    max_abs_residual = float(np.max(np.abs(residual_profile)))
    peak_to_peak = float(np.ptp(residual_profile))

    width_s = np.linspace(0.0, 1.0, resolved.width_sample_count)
    section_s = np.clip((section_t - lower_t) / reference_length, 0.0, 1.0)
    width_profile = np.interp(width_s, section_s, section_widths)
    centerline_valid_mask = _support_validity(normalized_s, section_s, resolved)
    width_valid_mask = _support_validity(width_s, section_s, resolved)
    if start_clipped:
        centerline_valid_mask[normalized_s <= resolved.clipped_end_window_fraction] = False
        width_valid_mask[width_s <= resolved.clipped_end_window_fraction] = False
        uncertain.extend(
            _clipped_end_issues(
                normalized_s,
                StemCrossSectionRejectionCode.NO_MASK_SUPPORT,
                at_start=True,
                config=resolved,
            )
        )
    if finish_clipped:
        centerline_valid_mask[normalized_s >= 1.0 - resolved.clipped_end_window_fraction] = False
        width_valid_mask[width_s >= 1.0 - resolved.clipped_end_window_fraction] = False
        uncertain.extend(
            _clipped_end_issues(
                normalized_s,
                StemCrossSectionRejectionCode.NO_MASK_SUPPORT,
                at_start=False,
                config=resolved,
            )
        )
    if not np.all(centerline_valid_mask) or not np.all(width_valid_mask):
        quality_flags.append(StemGeometryQualityFlag.CROSS_SECTION_GAPS)

    shape_class = _shape_class(
        residual_profile_normalized,
        bow_profile / median_width,
        signed_curvature,
        resolved,
    )
    quality_score = _quality_score(
        selected_fraction=selection.selected_fraction,
        visible_length_fraction=visible_length_fraction,
        valid_cross_section_fraction=valid_cross_section_fraction,
        eigenvalue_ratio=eigenvalue_ratio,
        start_clipped=start_clipped,
        finish_clipped=finish_clipped,
    )

    return StemGeometryResult(
        valid=True,
        image_width=image_width,
        image_height=image_height,
        reference_axis=StemReferenceAxis(
            start=_point(line_start),
            end=_point(line_end),
            direction=_point(final_direction),
            normal=_point(final_normal),
            length_px=axis_length,
        ),
        visible_endpoints=(_point(centerline_xy_array[0]), _point(centerline_xy_array[-1])),
        normalized_s=_float_tuple(normalized_s),
        centerline_image_xy=tuple(_point(value) for value in centerline_xy_array),
        centerline_valid_mask=tuple(bool(value) for value in centerline_valid_mask),
        residual_profile_px=_float_tuple(residual_profile),
        residual_profile_normalized=_float_tuple(residual_profile_normalized),
        width_normalized_s=_float_tuple(width_s),
        width_profile_px=_float_tuple(width_profile),
        width_valid_mask=tuple(bool(value) for value in width_valid_mask),
        signed_curvature=float(signed_curvature),
        signed_bow_px=float(signed_bow),
        rms_residual_px=rms_residual,
        max_abs_residual_px=max_abs_residual,
        peak_to_peak_px=peak_to_peak,
        median_width_px=median_width,
        rms_residual_normalized=rms_residual / median_width,
        visible_length_fraction=visible_length_fraction,
        valid_cross_section_fraction=valid_cross_section_fraction,
        rejected_cross_sections=tuple(sorted(rejected, key=lambda item: item.normalized_s)),
        uncertain_cross_sections=tuple(
            sorted(_unique_cross_section_issues(uncertain), key=lambda item: item.normalized_s)
        ),
        shape_class=shape_class,
        quality_score=quality_score,
        quality_flags=tuple(dict.fromkeys(quality_flags)),
        error_codes=(),
        issues=(),
        selected_component_area_px=selection.area_px,
        meaningful_component_count=selection.meaningful_component_count,
        selected_component_fraction=selection.selected_fraction,
    )


def _binary_mask(
    mask_or_polygon: np.ndarray | Sequence[object],
    *,
    image_width: int,
    image_height: int,
    config: StemGeometryConfig,
) -> np.ndarray | None:
    if isinstance(mask_or_polygon, np.ndarray):
        array = np.asarray(mask_or_polygon)
        if array.shape[:2] == (image_height, image_width):
            if array.ndim == 2:
                return np.where(array > 0, 255, 0).astype(np.uint8)
            if array.ndim == 3 and array.shape[2] in {1, 3, 4}:
                return np.where(np.any(array > 0, axis=2), 255, 0).astype(np.uint8)
            return None
    polygon = _pixel_polygon(
        mask_or_polygon,
        image_width=image_width,
        image_height=image_height,
        config=config,
    )
    if polygon is None:
        return None
    integer_polygon = np.rint(polygon).astype(np.int32)
    if abs(float(cv2.contourArea(integer_polygon))) < 1.0:
        return None
    mask = np.zeros((image_height, image_width), dtype=np.uint8)
    cv2.fillPoly(mask, [integer_polygon], 255)
    return mask


def _pixel_polygon(
    mask_or_polygon: np.ndarray | Sequence[object],
    *,
    image_width: int,
    image_height: int,
    config: StemGeometryConfig,
) -> np.ndarray | None:
    if isinstance(mask_or_polygon, np.ndarray):
        array = np.asarray(mask_or_polygon)
        if array.shape[:2] == (image_height, image_width):
            return None
        if array.ndim != 2 or array.shape[1] != 2:
            return None
        polygon = array.astype(np.float64)
    else:
        polygon = _polygon_points(mask_or_polygon)
        if polygon is None:
            return None
    if polygon.shape[0] < 3 or not np.all(np.isfinite(polygon)):
        return None
    coordinate_space = config.polygon_coordinate_space
    if coordinate_space == "auto":
        coordinate_space = (
            "normalized"
            if float(np.min(polygon)) >= -1e-6 and float(np.max(polygon)) <= 1.0 + 1e-6
            else "pixel"
        )
    if coordinate_space == "normalized":
        polygon = polygon.copy()
        polygon[:, 0] *= max(image_width - 1, 1)
        polygon[:, 1] *= max(image_height - 1, 1)
    polygon[:, 0] = np.clip(polygon[:, 0], 0, image_width - 1)
    polygon[:, 1] = np.clip(polygon[:, 1], 0, image_height - 1)
    if abs(float(cv2.contourArea(np.rint(polygon).astype(np.int32)))) < 1.0:
        return None
    return polygon


def _oriented_polygon_support(
    mask_or_polygon: np.ndarray | Sequence[object],
    *,
    image_width: int,
    image_height: int,
    config: StemGeometryConfig,
) -> _GeometrySupport | None:
    """Rasterize a polygon only inside its oriented minimum-area working rectangle.

    The local raster is aligned with the long edge of OpenCV's minimum-area rectangle.
    All selected foreground pixels are mapped back to canonical image coordinates before
    the geometry calculation, so slanted stems retain their image-space direction and sign.
    Full-size bitmap masks deliberately continue through the robust full-image path.
    """

    polygon = _pixel_polygon(
        mask_or_polygon,
        image_width=image_width,
        image_height=image_height,
        config=config,
    )
    if polygon is None:
        return None

    rectangle = cv2.minAreaRect(polygon.astype(np.float32).reshape(-1, 1, 2))
    box = cv2.boxPoints(rectangle).astype(np.float64)
    edges = np.roll(box, -1, axis=0) - box
    edge_lengths = np.linalg.norm(edges, axis=1)
    longest = edges[int(np.argmax(edge_lengths))]
    if float(np.linalg.norm(longest)) <= 1e-9:
        return None
    direction = _canonical_direction(_unit(longest))
    normal = _normal(direction)
    longitudinal = polygon @ direction
    transverse = polygon @ normal
    padding = max(config.cleanup_kernel_size, 1)
    lower_longitudinal = float(np.floor(np.min(longitudinal))) - padding
    upper_longitudinal = float(np.ceil(np.max(longitudinal))) + padding
    lower_transverse = float(np.floor(np.min(transverse))) - padding
    upper_transverse = float(np.ceil(np.max(transverse))) + padding
    local_width = int(upper_longitudinal - lower_longitudinal + 1)
    local_height = int(upper_transverse - lower_transverse + 1)
    if local_width < 2 or local_height < 2:
        return None

    local_polygon = np.column_stack(
        (
            longitudinal - lower_longitudinal,
            transverse - lower_transverse,
        )
    )
    local_mask = np.zeros((local_height, local_width), dtype=np.uint8)
    cv2.fillPoly(local_mask, [np.rint(local_polygon).astype(np.int32)], 255)
    # The runtime adapter has already selected exactly one segmentation instance. A filled
    # polygon therefore does not need the generic bitmap cleanup/component/contour cycle.
    # Bitmap masks and ambiguous instance sets remain on the robust full-image path.
    area_px = int(cv2.countNonZero(local_mask))
    selection = _ComponentSelection(
        mask=local_mask,
        area_px=area_px,
        meaningful_component_count=1 if area_px else 0,
        selected_fraction=1.0 if area_px else 0.0,
        holes_filled=False,
    )
    local_points = _sample_oriented_mask_points(
        local_mask,
        local_polygon,
        area_px=area_px,
    )
    if local_points.size == 0:
        return _GeometrySupport(selection=selection, points=local_points)

    image_points = (local_points[:, 0] + lower_longitudinal)[:, None] * direction[None, :] + (
        local_points[:, 1] + lower_transverse
    )[:, None] * normal[None, :]
    image_points[:, 0] = np.clip(image_points[:, 0], 0.0, image_width - 1.0)
    image_points[:, 1] = np.clip(image_points[:, 1], 0.0, image_height - 1.0)
    return _GeometrySupport(selection=selection, points=image_points)


def _sample_oriented_mask_points(
    mask: np.ndarray,
    polygon: np.ndarray,
    *,
    area_px: int,
) -> np.ndarray:
    """Build bounded, uniform interior support plus the exact segmentation boundary."""

    sampling_step = max(
        1,
        int(np.ceil(np.sqrt(area_px / _MAX_ORIENTED_POLYGON_SUPPORT_POINTS))),
    )
    if sampling_step == 1:
        interior = _mask_points(mask)
    else:
        sampled = mask[::sampling_step, ::sampling_step]
        y, x = np.nonzero(sampled)
        interior = np.column_stack((x, y)).astype(np.float64) * sampling_step
    return np.vstack((interior, np.asarray(polygon, dtype=np.float64)))


def _polygon_points(values: Sequence[object]) -> np.ndarray | None:
    points: list[tuple[float, float]] = []
    for value in values:
        if isinstance(value, Mapping):
            x = value.get("x")
            y = value.get("y")
        elif hasattr(value, "x") and hasattr(value, "y"):
            x, y = _POINT_ATTRIBUTES(value)
        elif isinstance(value, Sequence) and not isinstance(value, str | bytes) and len(value) == 2:
            x, y = value
        else:
            return None
        try:
            points.append((float(x), float(y)))
        except (TypeError, ValueError):
            return None
    return np.asarray(points, dtype=np.float64)


def _largest_meaningful_component(
    binary_mask: np.ndarray,
    config: StemGeometryConfig,
) -> _ComponentSelection:
    foreground = cv2.findNonZero(binary_mask)
    if foreground is None:
        return _ComponentSelection(
            mask=np.zeros_like(binary_mask),
            area_px=0,
            meaningful_component_count=0,
            selected_fraction=0.0,
            holes_filled=False,
        )
    x, y, width, height = cv2.boundingRect(foreground)
    padding = max(config.cleanup_kernel_size, 1)
    x_start = max(x - padding, 0)
    y_start = max(y - padding, 0)
    x_end = min(x + width + padding, binary_mask.shape[1])
    y_end = min(y + height + padding, binary_mask.shape[0])
    working = binary_mask[y_start:y_end, x_start:x_end]
    if config.cleanup_kernel_size > 1:
        kernel = np.ones(
            (config.cleanup_kernel_size, config.cleanup_kernel_size),
            dtype=np.uint8,
        )
        working = cv2.morphologyEx(working, cv2.MORPH_CLOSE, kernel)
    component_count, labels, statistics, _centroids = cv2.connectedComponentsWithStats(
        working,
        connectivity=8,
    )
    if component_count <= 1:
        return _ComponentSelection(
            mask=np.zeros_like(binary_mask),
            area_px=0,
            meaningful_component_count=0,
            selected_fraction=0.0,
            holes_filled=False,
        )
    areas = statistics[1:, cv2.CC_STAT_AREA].astype(np.int64)
    selected_index = int(np.argmax(areas)) + 1
    selected_mask = np.where(labels == selected_index, 255, 0).astype(np.uint8)
    selected_area = int(areas[selected_index - 1])
    meaningful_area_floor = max(4, int(round(config.min_component_area_px * 0.02)))
    meaningful = areas >= meaningful_area_floor
    meaningful_count = int(np.count_nonzero(meaningful))
    meaningful_total = int(np.sum(areas[meaningful])) if meaningful_count else selected_area
    selected_fraction = selected_area / max(meaningful_total, 1)

    contours, _hierarchy = cv2.findContours(
        selected_mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    filled = np.zeros_like(selected_mask)
    if contours:
        cv2.drawContours(filled, contours, -1, 255, thickness=cv2.FILLED)
    filled_area = int(cv2.countNonZero(filled))
    holes_filled = filled_area > selected_area
    selected_full = np.zeros_like(binary_mask)
    selected_full[y_start:y_end, x_start:x_end] = filled
    return _ComponentSelection(
        mask=selected_full,
        area_px=filled_area,
        meaningful_component_count=max(meaningful_count, 1),
        selected_fraction=float(np.clip(selected_fraction, 0.0, 1.0)),
        holes_filled=holes_filled,
    )


def _mask_points(mask: np.ndarray) -> np.ndarray:
    y, x = np.nonzero(mask)
    return np.column_stack((x, y)).astype(np.float64)


def _initial_axis(points: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    origin = np.median(points, axis=0)
    centered = points - origin
    if centered.shape[0] > 100_000:
        step = int(np.ceil(centered.shape[0] / 100_000))
        centered = centered[::step]
    covariance = np.cov(centered, rowvar=False)
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    direction = _canonical_direction(eigenvectors[:, int(np.argmax(eigenvalues))])
    ordered = np.sort(np.maximum(eigenvalues, 1e-9))
    ratio = float(ordered[-1] / ordered[-2])
    return origin, direction, ratio


def _measure_cross_sections(
    points: np.ndarray,
    origin: np.ndarray,
    direction: np.ndarray,
    config: StemGeometryConfig,
) -> tuple[list[_CrossSection], list[StemCrossSectionIssue]]:
    # A filled segmentation mask can contain several hundred thousand pixels, while
    # 96 robust cross-sections need only a dense, representative support sample.
    # Keep the full point set for component, axis-length, and clipping decisions;
    # only the repeated cross-section fit is deterministically downsampled.
    if points.shape[0] > _MAX_CROSS_SECTION_SUPPORT_POINTS:
        sample_indices = np.linspace(
            0,
            points.shape[0] - 1,
            _MAX_CROSS_SECTION_SUPPORT_POINTS,
            dtype=np.int64,
        )
        support_points = points[sample_indices]
    else:
        support_points = points
    normal = _normal(direction)
    longitudinal = (support_points - origin) @ direction
    transverse = (support_points - origin) @ normal
    lower, upper = _trimmed_bounds(longitudinal, config.longitudinal_trim_quantile)
    length = upper - lower
    # Each cross-section previously scanned every foreground pixel. At production
    # model-view sizes that made this O(section_count * foreground_pixels) and was
    # the dominant realtime cost. Sorting the longitudinal projection once lets
    # every band use two binary searches while preserving inclusive band boundaries.
    order = np.argsort(longitudinal)
    longitudinal = longitudinal[order]
    transverse = transverse[order]
    targets = np.linspace(lower, upper, config.cross_section_count)
    half_band = max(
        length / max(config.cross_section_count - 1, 1) * config.cross_section_band_scale,
        0.75,
    )
    measured: list[_CrossSection] = []
    missing: list[StemCrossSectionIssue] = []
    for target in targets:
        normalized_s = float(np.clip((target - lower) / max(length, 1e-9), 0.0, 1.0))
        support_start = int(np.searchsorted(longitudinal, target - half_band, side="left"))
        support_end = int(np.searchsorted(longitudinal, target + half_band, side="right"))
        support = transverse[support_start:support_end]
        if support.size < 6:
            missing.append(
                StemCrossSectionIssue(
                    normalized_s=normalized_s,
                    reason=StemCrossSectionRejectionCode.NO_MASK_SUPPORT,
                )
            )
            continue
        low = float(np.quantile(support, config.boundary_quantile))
        high = float(np.quantile(support, 1.0 - config.boundary_quantile))
        width = high - low
        if width <= 0:
            missing.append(
                StemCrossSectionIssue(
                    normalized_s=normalized_s,
                    reason=StemCrossSectionRejectionCode.NO_MASK_SUPPORT,
                )
            )
            continue
        midpoint = (low + high) * 0.5
        measured.append(
            _CrossSection(
                normalized_s=normalized_s,
                longitudinal_px=float(target),
                transverse_center_px=midpoint,
                width_px=width,
                center_xy=origin + target * direction + midpoint * normal,
            )
        )
    return measured, missing


def _filter_cross_sections(
    measured: list[_CrossSection],
    config: StemGeometryConfig,
) -> tuple[
    list[_CrossSection],
    list[StemCrossSectionIssue],
    list[StemCrossSectionIssue],
]:
    if not measured:
        return [], [], []
    widths = np.asarray([section.width_px for section in measured], dtype=np.float64)
    centers = np.asarray(
        [section.transverse_center_px for section in measured],
        dtype=np.float64,
    )
    expected_width = _rolling_median(widths, config.local_outlier_window)
    expected_center = _rolling_median(centers, config.local_outlier_window)
    width_delta = widths - expected_width
    center_delta = centers - expected_center
    width_scale = _robust_scale(width_delta)
    center_scale = _robust_scale(center_delta)
    typical_width = max(float(np.median(expected_width)), 1.0)

    accepted: list[_CrossSection] = []
    rejected: list[StemCrossSectionIssue] = []
    uncertain: list[StemCrossSectionIssue] = []
    for index, section in enumerate(measured):
        width_limit = max(
            2.0,
            config.outlier_mad_multiplier * width_scale,
            0.22 * expected_width[index],
        )
        ratio = section.width_px / max(expected_width[index], 1e-9)
        width_outlier = (
            ratio > config.width_outlier_ratio and width_delta[index] > width_limit
        ) or (ratio < 1.0 / config.width_outlier_ratio and -width_delta[index] > width_limit)
        if width_outlier:
            rejected.append(
                StemCrossSectionIssue(
                    normalized_s=section.normalized_s,
                    reason=StemCrossSectionRejectionCode.WIDTH_OUTLIER,
                    observed_width_px=section.width_px,
                    expected_width_px=float(expected_width[index]),
                )
            )
            continue

        center_limit = max(
            2.0,
            config.outlier_mad_multiplier * center_scale,
            config.center_outlier_width_fraction * typical_width,
        )
        if abs(center_delta[index]) > center_limit:
            rejected.append(
                StemCrossSectionIssue(
                    normalized_s=section.normalized_s,
                    reason=StemCrossSectionRejectionCode.CENTER_OUTLIER,
                    observed_width_px=section.width_px,
                    expected_width_px=float(expected_width[index]),
                )
            )
            continue

        if (
            abs(width_delta[index]) > 0.7 * width_limit
            or abs(center_delta[index]) > 0.7 * center_limit
        ):
            uncertain.append(
                StemCrossSectionIssue(
                    normalized_s=section.normalized_s,
                    reason=(
                        StemCrossSectionRejectionCode.WIDTH_OUTLIER
                        if abs(width_delta[index]) > 0.7 * width_limit
                        else StemCrossSectionRejectionCode.CENTER_OUTLIER
                    ),
                    observed_width_px=section.width_px,
                    expected_width_px=float(expected_width[index]),
                )
            )
        accepted.append(section)
    return accepted, rejected, uncertain


def _refine_axis(
    origin: np.ndarray,
    direction: np.ndarray,
    sections: list[_CrossSection],
) -> tuple[np.ndarray, np.ndarray]:
    normal = _normal(direction)
    centers = np.vstack([section.center_xy for section in sections])
    longitudinal = (centers - origin) @ direction
    transverse = (centers - origin) @ normal
    slope, intercept = _robust_line_fit(longitudinal, transverse)
    refined_origin = origin + intercept * normal
    refined_direction = _unit(direction + slope * normal)
    if float(refined_direction @ direction) < 0:
        refined_direction = -refined_direction
    return refined_origin, refined_direction


def _robust_line_fit(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    x_center = float(np.median(x))
    design = np.column_stack((x - x_center, np.ones_like(x)))
    weights = np.ones_like(x)
    coefficients = np.asarray([0.0, float(np.median(y))], dtype=np.float64)
    for _iteration in range(8):
        root_weights = np.sqrt(weights)
        weighted_design = design * root_weights[:, None]
        weighted_values = y * root_weights
        coefficients = np.linalg.lstsq(weighted_design, weighted_values, rcond=None)[0]
        residual = y - design @ coefficients
        scale = max(_robust_scale(residual), 0.5)
        normalized = np.abs(residual) / (1.5 * scale)
        weights = np.where(normalized <= 1.0, 1.0, 1.0 / np.maximum(normalized, 1e-9))
    slope = float(coefficients[0])
    intercept_at_zero = float(coefficients[1] - slope * x_center)
    return slope, intercept_at_zero


def _projected_length(
    points: np.ndarray,
    origin: np.ndarray,
    direction: np.ndarray,
    config: StemGeometryConfig,
) -> float:
    projection = (points - origin) @ direction
    lower, upper = _trimmed_bounds(projection, config.longitudinal_trim_quantile)
    return float(upper - lower)


def _trimmed_bounds(values: np.ndarray, quantile: float) -> tuple[float, float]:
    if quantile <= 0:
        return float(np.min(values)), float(np.max(values))
    return float(np.quantile(values, quantile)), float(np.quantile(values, 1.0 - quantile))


def _clipped_ends(
    points: np.ndarray,
    origin: np.ndarray,
    direction: np.ndarray,
    lower_t: float,
    upper_t: float,
    *,
    image_width: int,
    image_height: int,
    config: StemGeometryConfig,
) -> tuple[bool, bool]:
    margin = float(config.border_margin_px)
    border = (
        (points[:, 0] <= margin)
        | (points[:, 0] >= image_width - 1 - margin)
        | (points[:, 1] <= margin)
        | (points[:, 1] >= image_height - 1 - margin)
    )
    if not np.any(border):
        return False, False
    border_t = (points[border] - origin) @ direction
    end_window = max((upper_t - lower_t) * config.clipped_end_window_fraction, 2.0)
    return bool(np.any(border_t <= lower_t + end_window)), bool(
        np.any(border_t >= upper_t - end_window)
    )


def _support_validity(
    targets: np.ndarray,
    measured_s: np.ndarray,
    config: StemGeometryConfig,
) -> np.ndarray:
    maximum_gap = max(2.5 / max(config.cross_section_count - 1, 1), 0.035)
    distances = np.min(np.abs(targets[:, None] - measured_s[None, :]), axis=1)
    return distances <= maximum_gap


def _clipped_end_issues(
    normalized_s: np.ndarray,
    reason: StemCrossSectionRejectionCode,
    *,
    at_start: bool,
    config: StemGeometryConfig,
) -> list[StemCrossSectionIssue]:
    if at_start:
        selected = normalized_s[normalized_s <= config.clipped_end_window_fraction]
    else:
        selected = normalized_s[normalized_s >= 1.0 - config.clipped_end_window_fraction]
    return [StemCrossSectionIssue(normalized_s=float(value), reason=reason) for value in selected]


def _shape_class(
    residual_normalized: np.ndarray,
    bow_normalized: np.ndarray,
    signed_curvature: float,
    config: StemGeometryConfig,
) -> StemShapeClass:
    rms = float(np.sqrt(np.mean(np.square(residual_normalized))))
    max_abs = float(np.max(np.abs(residual_normalized)))
    if (
        rms <= config.straight_threshold_normalized
        and max_abs <= 2.0 * config.straight_threshold_normalized
    ):
        return "straight"
    interior = bow_normalized[1:-1] if bow_normalized.size > 2 else bow_normalized
    positive_lobe = float(np.max(interior)) if interior.size else 0.0
    negative_lobe = float(np.min(interior)) if interior.size else 0.0
    if (
        positive_lobe >= config.complex_lobe_threshold_normalized
        and negative_lobe <= -config.complex_lobe_threshold_normalized
    ):
        return "s_bow_or_complex"
    if abs(signed_curvature) >= config.straight_threshold_normalized:
        return "c_bow"
    return "uncertain"


def _quality_score(
    *,
    selected_fraction: float,
    visible_length_fraction: float,
    valid_cross_section_fraction: float,
    eigenvalue_ratio: float,
    start_clipped: bool,
    finish_clipped: bool,
) -> float:
    axis_score = float(np.clip((eigenvalue_ratio - 1.0) / 12.0, 0.0, 1.0))
    score = (
        0.25 * selected_fraction
        + 0.25 * visible_length_fraction
        + 0.35 * valid_cross_section_fraction
        + 0.15 * axis_score
    )
    if start_clipped or finish_clipped:
        score *= 0.82
    return float(np.clip(score, 0.0, 1.0))


def _rolling_median(values: np.ndarray, window: int) -> np.ndarray:
    radius = window // 2
    return np.asarray(
        [
            np.median(values[max(0, index - radius) : min(values.size, index + radius + 1)])
            for index in range(values.size)
        ],
        dtype=np.float64,
    )


def _smooth_profile(values: np.ndarray, passes: int) -> np.ndarray:
    smoothed = _rolling_median(np.asarray(values, dtype=np.float64), 3)
    kernel = np.asarray([1.0, 4.0, 6.0, 4.0, 1.0], dtype=np.float64) / 16.0
    for _iteration in range(passes):
        padded = np.pad(smoothed, 2, mode="reflect")
        smoothed = np.convolve(padded, kernel, mode="valid")
    return smoothed


def _robust_scale(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=np.float64)
    median = float(np.median(values))
    return float(1.4826 * np.median(np.abs(values - median)))


def _canonical_direction(direction: np.ndarray) -> np.ndarray:
    direction = _unit(np.asarray(direction, dtype=np.float64))
    if direction[0] < -1e-9 or (abs(float(direction[0])) <= 1e-9 and direction[1] < 0):
        return -direction
    return direction


def _normal(direction: np.ndarray) -> np.ndarray:
    return np.asarray([-direction[1], direction[0]], dtype=np.float64)


def _unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-12:
        return np.asarray([1.0, 0.0], dtype=np.float64)
    return np.asarray(vector, dtype=np.float64) / norm


def _point(value: np.ndarray) -> StemImagePoint:
    return StemImagePoint(x=float(value[0]), y=float(value[1]))


def _float_tuple(values: np.ndarray) -> tuple[float, ...]:
    return tuple(float(value) for value in values)


def _unique_cross_section_issues(
    issues: list[StemCrossSectionIssue],
) -> list[StemCrossSectionIssue]:
    unique: dict[tuple[int, StemCrossSectionRejectionCode], StemCrossSectionIssue] = {}
    for issue in issues:
        key = (round(issue.normalized_s * 10_000), issue.reason)
        unique[key] = issue
    return list(unique.values())


def _invalid_result(
    *,
    image_width: int,
    image_height: int,
    config: StemGeometryConfig,
    error_codes: tuple[StemGeometryErrorCode, ...],
    quality_flags: Sequence[StemGeometryQualityFlag] = (),
    selection: _ComponentSelection | None = None,
    visible_length_fraction: float = 0.0,
    valid_cross_section_fraction: float = 0.0,
    rejected_cross_sections: Sequence[StemCrossSectionIssue] = (),
    uncertain_cross_sections: Sequence[StemCrossSectionIssue] = (),
) -> StemGeometryResult:
    return StemGeometryResult(
        valid=False,
        image_width=image_width,
        image_height=image_height,
        reference_axis=None,
        visible_endpoints=None,
        normalized_s=tuple(
            float(value) for value in np.linspace(0.0, 1.0, config.centerline_sample_count)
        ),
        centerline_image_xy=(),
        centerline_valid_mask=tuple(False for _ in range(config.centerline_sample_count)),
        residual_profile_px=(),
        residual_profile_normalized=(),
        width_normalized_s=tuple(
            float(value) for value in np.linspace(0.0, 1.0, config.width_sample_count)
        ),
        width_profile_px=(),
        width_valid_mask=tuple(False for _ in range(config.width_sample_count)),
        signed_curvature=None,
        signed_bow_px=None,
        rms_residual_px=None,
        max_abs_residual_px=None,
        peak_to_peak_px=None,
        median_width_px=None,
        rms_residual_normalized=None,
        visible_length_fraction=visible_length_fraction,
        valid_cross_section_fraction=valid_cross_section_fraction,
        rejected_cross_sections=tuple(rejected_cross_sections),
        uncertain_cross_sections=tuple(uncertain_cross_sections),
        shape_class="uncertain",
        quality_score=0.0,
        quality_flags=tuple(dict.fromkeys(quality_flags)),
        error_codes=error_codes,
        issues=tuple(
            StemGeometryIssue(code=code, message=_ERROR_MESSAGES[code]) for code in error_codes
        ),
        selected_component_area_px=selection.area_px if selection is not None else 0,
        meaningful_component_count=(
            selection.meaningful_component_count if selection is not None else 0
        ),
        selected_component_fraction=(selection.selected_fraction if selection is not None else 0.0),
    )
