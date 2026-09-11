from __future__ import annotations

from collections.abc import Callable

import cv2
import numpy as np
import pytest
from cv_runtime.stem_geometry import (
    StemCrossSectionRejectionCode,
    StemGeometryConfig,
    StemGeometryErrorCode,
    StemGeometryQualityFlag,
    StemGeometryResult,
    analyze_stem_geometry,
)


def _stem_mask(
    *,
    image_width: int = 640,
    image_height: int = 480,
    start_x: float = 100.0,
    length: float = 420.0,
    center_y: float = 240.0,
    curve: Callable[[np.ndarray], np.ndarray] | None = None,
    half_width: Callable[[np.ndarray], np.ndarray] | None = None,
) -> np.ndarray:
    normalized_s = np.linspace(0.0, 1.0, 421)
    x = start_x + length * normalized_s
    y = center_y + (curve(normalized_s) if curve is not None else np.zeros_like(normalized_s))
    radii = half_width(normalized_s) if half_width is not None else np.full_like(normalized_s, 16.0)
    dx = np.gradient(x)
    dy = np.gradient(y)
    tangent_length = np.hypot(dx, dy)
    normal_x = -dy / tangent_length
    normal_y = dx / tangent_length
    upper = np.column_stack((x + normal_x * radii, y + normal_y * radii))
    lower = np.column_stack((x - normal_x * radii, y - normal_y * radii))[::-1]
    polygon = np.rint(np.vstack((upper, lower))).astype(np.int32)
    mask = np.zeros((image_height, image_width), dtype=np.uint8)
    cv2.fillPoly(mask, [polygon], 255)
    return mask


def _c_curve(amplitude: float) -> Callable[[np.ndarray], np.ndarray]:
    return lambda normalized_s: amplitude * 4.0 * normalized_s * (1.0 - normalized_s)


def _analyze(mask: np.ndarray) -> StemGeometryResult:
    return analyze_stem_geometry(
        mask,
        image_width=mask.shape[1],
        image_height=mask.shape[0],
    )


def test_straight_stem_and_normalized_polygon_are_near_zero() -> None:
    mask = _stem_mask()
    cv2.rectangle(mask, (20, 20), (25, 25), 255, thickness=-1)

    result = _analyze(mask)
    repeated = _analyze(mask)

    assert result.valid is True
    assert repeated == result
    assert result.reference_axis is not None
    assert result.visible_endpoints is not None
    assert len(result.normalized_s) == 32
    assert len(result.centerline_image_xy) == 32
    assert len(result.residual_profile_px) == 32
    assert len(result.residual_profile_normalized) == 32
    assert len(result.width_profile_px) == 16
    assert result.normalized_s[0] == pytest.approx(0.0, abs=0.01)
    assert result.normalized_s[-1] == pytest.approx(1.0, abs=0.01)
    assert result.signed_curvature == pytest.approx(0.0, abs=0.025)
    assert result.rms_residual_normalized == pytest.approx(0.0, abs=0.025)
    assert result.shape_class == "straight"
    assert StemGeometryQualityFlag.FRAGMENTS_REMOVED in result.quality_flags
    assert not any(name.endswith("_mm") for name in result.__dataclass_fields__)

    normalized_polygon = [
        {"x": 0.15, "y": 0.45},
        {"x": 0.85, "y": 0.45},
        {"x": 0.85, "y": 0.55},
        {"x": 0.15, "y": 0.55},
    ]
    polygon_result = analyze_stem_geometry(
        normalized_polygon,
        image_width=640,
        image_height=480,
    )
    assert polygon_result.valid is True
    assert polygon_result.signed_curvature == pytest.approx(0.0, abs=0.025)


def test_c_bows_have_opposite_signed_curvature() -> None:
    positive = _analyze(_stem_mask(curve=_c_curve(36.0)))
    negative = _analyze(_stem_mask(curve=_c_curve(-36.0)))

    assert positive.valid is True
    assert negative.valid is True
    assert positive.shape_class == "c_bow"
    assert negative.shape_class == "c_bow"
    assert positive.signed_curvature is not None
    assert negative.signed_curvature is not None
    assert positive.signed_curvature > 0.25
    assert negative.signed_curvature < -0.25
    assert positive.signed_curvature == pytest.approx(
        -negative.signed_curvature,
        rel=0.12,
        abs=0.03,
    )


def test_large_production_sized_mask_keeps_deterministic_metric_shape() -> None:
    mask = _stem_mask(
        image_width=1600,
        image_height=640,
        start_x=120.0,
        length=1360.0,
        center_y=320.0,
        curve=_c_curve(54.0),
        half_width=lambda normalized_s: 78.0 - 12.0 * normalized_s,
    )
    assert cv2.countNonZero(mask) > 60_000

    result = _analyze(mask)
    repeated = _analyze(mask)

    assert result == repeated
    assert result.valid is True
    assert result.signed_curvature is not None and result.signed_curvature > 0.2
    assert result.median_width_px == pytest.approx(144.0, abs=5.0)
    assert result.valid_cross_section_fraction >= 0.9


def test_s_bow_is_classified_as_complex_without_false_dominant_direction() -> None:
    mask = _stem_mask(curve=lambda normalized_s: 34.0 * np.sin(2.0 * np.pi * normalized_s))

    result = _analyze(mask)

    assert result.valid is True
    assert result.shape_class == "s_bow_or_complex"
    assert result.signed_curvature == pytest.approx(0.0, abs=0.10)
    assert max(result.residual_profile_normalized) > 0.45
    assert min(result.residual_profile_normalized) < -0.45


def test_tapered_stem_keeps_centerline_and_width_trend_separate() -> None:
    mask = _stem_mask(half_width=lambda normalized_s: 27.0 - 13.0 * normalized_s)

    result = _analyze(mask)

    assert result.valid is True
    assert result.shape_class == "straight"
    assert result.signed_curvature == pytest.approx(0.0, abs=0.035)
    assert result.width_profile_px[0] > result.width_profile_px[-1] + 18.0
    assert sum(result.width_valid_mask) >= 14


def test_local_branch_is_rejected_without_dominating_low_frequency_bow() -> None:
    base_mask = _stem_mask(curve=_c_curve(24.0))
    branch_mask = base_mask.copy()
    branch_x = 310
    branch_y = int(round(240.0 + 24.0 * 4.0 * 0.5 * 0.5))
    cv2.rectangle(
        branch_mask,
        (branch_x - 3, branch_y),
        (branch_x + 3, branch_y + 68),
        255,
        thickness=-1,
    )

    base = _analyze(base_mask)
    branched = _analyze(branch_mask)

    assert base.valid is True
    assert branched.valid is True
    assert base.signed_curvature is not None
    assert branched.signed_curvature is not None
    assert abs(branched.signed_curvature - base.signed_curvature) <= 0.08
    assert StemGeometryQualityFlag.LOCAL_BULGE_LIMITED in branched.quality_flags
    assert any(
        issue.reason == StemCrossSectionRejectionCode.WIDTH_OUTLIER
        for issue in branched.rejected_cross_sections
    )


def test_one_clipped_stem_end_is_explicitly_marked() -> None:
    mask = _stem_mask(
        image_width=480,
        start_x=-45.0,
        length=390.0,
        center_y=220.0,
        curve=_c_curve(18.0),
    )

    result = _analyze(mask)

    assert result.valid is True
    assert StemGeometryQualityFlag.START_END_CLIPPED in result.quality_flags
    assert StemGeometryQualityFlag.FINISH_END_CLIPPED not in result.quality_flags
    assert sum(result.centerline_valid_mask[:4]) < 4
    assert result.uncertain_cross_sections


@pytest.mark.parametrize(
    ("mask", "expected_code"),
    [
        (
            np.pad(np.ones((9, 9), dtype=np.uint8) * 255, ((100, 371), (100, 531))),
            StemGeometryErrorCode.MASK_TOO_SMALL,
        ),
        (
            None,
            StemGeometryErrorCode.MASK_TOO_FRAGMENTED,
        ),
    ],
)
def test_too_small_or_strongly_fragmented_masks_fail_closed(
    mask: np.ndarray | None,
    expected_code: StemGeometryErrorCode,
) -> None:
    if mask is None:
        mask = np.zeros((480, 640), dtype=np.uint8)
        for index in range(6):
            x = 40 + index * 90
            cv2.rectangle(mask, (x, 190), (x + 30, 210), 255, thickness=-1)

    result = _analyze(mask)

    assert result.valid is False
    assert expected_code in result.error_codes
    assert result.issues
    assert all(issue.message for issue in result.issues)
    assert result.reference_axis is None


def test_rotation_and_translation_preserve_normalized_residual_profile() -> None:
    original_mask = _stem_mask(
        start_x=145.0,
        length=350.0,
        curve=_c_curve(28.0),
    )
    transform = cv2.getRotationMatrix2D((320.0, 240.0), 27.0, 1.0)
    transform[:, 2] += np.asarray([22.0, -14.0])
    transformed_mask = cv2.warpAffine(
        original_mask,
        transform,
        (640, 480),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )

    original = _analyze(original_mask)
    transformed = _analyze(transformed_mask)

    assert original.valid is True
    assert transformed.valid is True
    original_profile = np.asarray(original.residual_profile_normalized)
    transformed_profile = np.asarray(transformed.residual_profile_normalized)
    profile_delta = original_profile - transformed_profile
    assert float(np.sqrt(np.mean(np.square(profile_delta)))) <= 0.035
    assert float(np.max(np.abs(profile_delta))) <= 0.075
    assert transformed.signed_curvature == pytest.approx(
        original.signed_curvature,
        rel=0.12,
        abs=0.035,
    )


def test_oriented_polygon_roi_preserves_slanted_stem_geometry_in_image_coordinates() -> None:
    source_mask = _stem_mask(
        start_x=125.0,
        length=385.0,
        curve=_c_curve(25.0),
        half_width=lambda normalized_s: 22.0 - 4.0 * normalized_s,
    )
    transform = cv2.getRotationMatrix2D((320.0, 240.0), 31.0, 1.0)
    slanted_mask = cv2.warpAffine(
        source_mask,
        transform,
        (640, 480),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    contours, _hierarchy = cv2.findContours(
        slanted_mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    assert contours
    polygon = cv2.approxPolyDP(
        max(contours, key=cv2.contourArea),
        epsilon=1.0,
        closed=True,
    ).reshape(-1, 2)

    full = analyze_stem_geometry(
        polygon,
        image_width=640,
        image_height=480,
        config=StemGeometryConfig(polygon_coordinate_space="pixel"),
    )
    oriented = analyze_stem_geometry(
        polygon,
        image_width=640,
        image_height=480,
        config=StemGeometryConfig(
            polygon_coordinate_space="pixel",
            polygon_rasterization_mode="oriented_roi",
        ),
    )

    assert full.valid is True
    assert oriented.valid is True
    assert full.reference_axis is not None
    assert oriented.reference_axis is not None
    assert oriented.signed_curvature == pytest.approx(
        full.signed_curvature,
        rel=0.12,
        abs=0.035,
    )
    assert oriented.median_width_px == pytest.approx(full.median_width_px, abs=2.5)
    direction_alignment = abs(
        full.reference_axis.direction.x * oriented.reference_axis.direction.x
        + full.reference_axis.direction.y * oriented.reference_axis.direction.y
    )
    assert direction_alignment >= 0.995
    assert all(
        0.0 <= point.x < 640.0 and 0.0 <= point.y < 480.0 for point in oriented.centerline_image_xy
    )


def test_oriented_polygon_mode_keeps_full_bitmap_mask_on_robust_fallback() -> None:
    mask = _stem_mask(curve=_c_curve(22.0))

    full = analyze_stem_geometry(mask, image_width=640, image_height=480)
    fallback = analyze_stem_geometry(
        mask,
        image_width=640,
        image_height=480,
        config=StemGeometryConfig(polygon_rasterization_mode="oriented_roi"),
    )

    assert fallback == full
