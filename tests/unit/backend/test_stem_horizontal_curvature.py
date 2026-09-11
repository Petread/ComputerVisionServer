from __future__ import annotations

import cv2
import numpy as np
import pytest
from cv_runtime.stem_horizontal_curvature import (
    StemHorizontalCurvatureConfig,
    measure_stem_horizontal_curvature,
    rectified_process_point_to_pixel,
)
from cv_runtime.stem_process_coordinates import (
    calculate_stem_curvature_process_calibration,
    rectified_image_homography,
    warp_mask_to_rectified,
)
from tests.unit.backend.test_stem_process_coordinates import process_calibration_request


def _profile():
    result = calculate_stem_curvature_process_calibration(
        source_channel_id="chn_stem",
        request=process_calibration_request(),
        generated_at="2026-08-04T10:00:00Z",
    )
    assert result.profile is not None
    return result.profile


def _stem_mask(
    profile,
    *,
    bow_mm: float = 0.0,
    slope: float = 0.0,
    translation_x_mm: float = 0.0,
    translation_y_mm: float = 0.0,
    x_start_mm: float = 520.0,
    x_end_mm: float = 1700.0,
    width_mm: float = 180.0,
    s_bow: bool = False,
) -> np.ndarray:
    x_start_mm += translation_x_mm
    x_end_mm += translation_x_mm
    x_values = np.linspace(x_start_mm, x_end_mm, 240)
    s = (x_values - x_start_mm) / (x_end_mm - x_start_mm)
    if s_bow:
        bow = bow_mm * np.sin(2.0 * np.pi * s)
    else:
        bow = bow_mm * 4.0 * s * (1.0 - s)
    center = translation_y_mm + slope * (x_values - (x_start_mm + x_end_mm) / 2.0) + bow
    upper = [
        rectified_process_point_to_pixel(float(x), float(y - width_mm / 2.0), profile)
        for x, y in zip(x_values, center, strict=True)
    ]
    lower = [
        rectified_process_point_to_pixel(float(x), float(y + width_mm / 2.0), profile)
        for x, y in zip(x_values[::-1], center[::-1], strict=True)
    ]
    polygon = np.rint(np.asarray(upper + lower)).astype(np.int32)
    mask = np.zeros((profile.image_height, profile.image_width), dtype=np.uint8)
    cv2.fillPoly(mask, [polygon], 255)
    return mask


def _measure(mask, profile=None):
    return measure_stem_horizontal_curvature(
        mask,
        profile or _profile(),
        config=StemHorizontalCurvatureConfig(
            minimum_component_area_px=50,
            straight_bow_threshold_mm=2.5,
        ),
    )


def test_straight_stem_remains_near_zero_after_slant_and_translation() -> None:
    profile = _profile()
    baseline = _measure(_stem_mask(profile, slope=0.0), profile)
    transformed = [
        _measure(
            _stem_mask(
                profile,
                slope=slope,
                translation_x_mm=translation_x,
                translation_y_mm=translation_y,
            ),
            profile,
        )
        for slope, translation_x, translation_y in (
            (0.06, 60.0, 45.0),
            (-0.12, -30.0, -55.0),
            (0.14, 20.0, -35.0),
        )
    ]

    assert baseline.valid and all(item.valid for item in transformed)
    assert abs(baseline.signed_bow_mm or 0.0) < 2.5
    assert baseline.bow_shape_hint == "straight"
    assert all(abs(item.signed_bow_mm or 0.0) < 2.5 for item in transformed)
    assert all(item.bow_shape_hint == "straight" for item in transformed)


def test_opposite_simple_bows_have_stable_magnitude_and_opposite_sign() -> None:
    profile = _profile()
    positive = _measure(_stem_mask(profile, bow_mm=42.0), profile)
    negative = _measure(
        _stem_mask(
            profile,
            bow_mm=-42.0,
            slope=0.05,
            translation_x_mm=50.0,
            translation_y_mm=30.0,
        ),
        profile,
    )

    assert positive.valid and negative.valid
    assert positive.signed_bow_mm is not None and positive.signed_bow_mm > 10
    assert negative.signed_bow_mm is not None and negative.signed_bow_mm < -10
    assert abs(abs(positive.signed_bow_mm) - abs(negative.signed_bow_mm)) < 5
    assert positive.bow_shape_hint == "positive_c_bow"
    assert negative.bow_shape_hint == "negative_c_bow"


def test_perspective_round_trip_preserves_metric_curvature() -> None:
    profile = _profile()
    rectified = _stem_mask(profile, bow_mm=36.0, slope=0.04)
    image_to_rectified = np.asarray(
        rectified_image_homography(profile),
        dtype=np.float64,
    ).reshape(3, 3)
    undistorted = cv2.warpPerspective(
        rectified,
        np.linalg.inv(image_to_rectified),
        (profile.image_width, profile.image_height),
        flags=cv2.INTER_NEAREST,
    )
    restored = warp_mask_to_rectified(undistorted, profile)
    direct = _measure(rectified, profile)
    round_trip = _measure(restored, profile)

    assert direct.valid and round_trip.valid
    assert round_trip.signed_bow_mm == pytest.approx(direct.signed_bow_mm, abs=3.0)
    assert round_trip.curvature_rms_mm == pytest.approx(
        direct.curvature_rms_mm,
        abs=2.0,
    )


def test_short_branch_bulge_and_small_noise_are_removed_without_sign_change() -> None:
    profile = _profile()
    clean = _stem_mask(profile, bow_mm=38.0)
    noisy = clean.copy()
    branch_x, branch_y = rectified_process_point_to_pixel(1100.0, 130.0, profile)
    cv2.circle(noisy, (round(branch_x), round(branch_y)), 24, 255, -1)
    noise_a = rectified_process_point_to_pixel(700.0, -300.0, profile)
    noise_b = rectified_process_point_to_pixel(1500.0, 300.0, profile)
    cv2.circle(noisy, tuple(round(value) for value in noise_a), 4, 255, -1)
    cv2.circle(noisy, tuple(round(value) for value in noise_b), 4, 255, -1)
    baseline = _measure(clean, profile)
    robust = _measure(noisy, profile)

    assert baseline.valid and robust.valid
    assert robust.signed_bow_mm == pytest.approx(baseline.signed_bow_mm, abs=5.0)
    assert robust.fragment_status == "minor_noise_removed"
    assert any(
        station.exclusion_reason == "local_width_outlier_or_branch_bulge"
        for station in robust.stations
    )


def test_s_bow_has_two_sided_residual_and_dedicated_hint() -> None:
    profile = _profile()
    result = _measure(_stem_mask(profile, bow_mm=45.0, s_bow=True), profile)

    assert result.valid
    assert result.bow_shape_hint == "s_bow"
    assert result.curvature_peak_to_peak_mm is not None
    assert result.curvature_peak_to_peak_mm > 40


def test_short_visible_or_cut_segment_is_not_comparable() -> None:
    profile = _profile()
    result = _measure(
        _stem_mask(profile, bow_mm=35.0, x_start_mm=800.0, x_end_mm=1250.0),
        profile,
    )

    assert not result.valid
    assert result.observation_state == "insufficient_common_segment"
    assert result.signed_bow_mm is None
    assert not result.common_segment.comparable


def test_valid_different_visible_lengths_use_the_same_inner_fraction_strategy() -> None:
    profile = _profile()
    long = _measure(
        _stem_mask(profile, bow_mm=35.0, x_start_mm=520.0, x_end_mm=1650.0),
        profile,
    )
    shorter = _measure(
        _stem_mask(profile, bow_mm=35.0, x_start_mm=620.0, x_end_mm=1600.0),
        profile,
    )

    assert long.valid and shorter.valid
    assert long.common_segment.comparable and shorter.common_segment.comparable
    assert long.common_segment.configured_inner_fraction == 0.8
    assert shorter.common_segment.configured_inner_fraction == 0.8
    assert shorter.signed_bow_mm == pytest.approx(long.signed_bow_mm, abs=4.0)


def test_multiple_meaningful_fragments_are_rejected_without_reconstruction() -> None:
    profile = _profile()
    mask = _stem_mask(profile, bow_mm=35.0)
    gap_start, _ = rectified_process_point_to_pixel(1050.0, 0.0, profile)
    gap_end, _ = rectified_process_point_to_pixel(1160.0, 0.0, profile)
    mask[:, round(gap_start) : round(gap_end)] = 0
    result = _measure(mask, profile)

    assert not result.valid
    assert result.observation_state == "fragmented"
    assert result.fragment_status == "multiple_meaningful_fragments"
    assert result.signed_bow_mm is None


def test_roi_clipping_and_normalized_outputs_are_explicit() -> None:
    profile = _profile()
    result = _measure(
        _stem_mask(
            profile,
            bow_mm=30.0,
            x_start_mm=350.0,
            x_end_mm=1750.0,
        ),
        profile,
    )

    assert result.valid
    assert result.curvature_roi_clipped
    assert result.signed_bow_normalized_length is not None
    assert result.signed_bow_normalized_width is not None
    assert result.common_segment.comparable
