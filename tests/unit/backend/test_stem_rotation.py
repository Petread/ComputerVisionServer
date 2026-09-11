from __future__ import annotations

import math

import numpy as np
import pytest
from cv_runtime.stem_rotation import (
    StemAxisObservation,
    StemRotationCalibrationMethod,
    StemRotationCalibrationObservation,
    StemRotationCalibrationProfile,
    SurfaceFeatureMatch,
    SurfaceRotationEstimationConfig,
    calibrate_stem_rotation,
    estimate_surface_rotation_from_matches,
    stem_rotation_calibration_from_config,
)


def _calibration(
    method: StemRotationCalibrationMethod = (StemRotationCalibrationMethod.KNOWN_ANGLE_POSITIONS),
) -> StemRotationCalibrationProfile:
    observations = [
        StemRotationCalibrationObservation(
            observed_normalized_displacement=displacement,
            known_rotation_deg=180.0 * displacement,
            stem_width_fraction=width,
            image_center_x=center_x,
            image_center_y=center_y,
        )
        for displacement, width, center_x, center_y in (
            (-0.25, 0.06, 0.30, 0.40),
            (-0.125, 0.07, 0.45, 0.50),
            (0.125, 0.075, 0.55, 0.52),
            (0.25, 0.08, 0.70, 0.60),
        )
    ]
    return calibrate_stem_rotation(
        observations,
        calibration_id="stem-rotation-cal-01",
        version="1.0.0",
        method=method,
        source_intrinsic_calibration_hash="intrinsic-sha256",
        rotation_viewpoint_convention="viewed_from_configured_log_end_a",
        positive_rotation_direction="clockwise_viewed_from_configured_log_end",
    )


def _axis(
    *,
    center: tuple[float, float],
    axis_rotation_deg: float = 0.0,
) -> StemAxisObservation:
    radians = math.radians(axis_rotation_deg)
    return StemAxisObservation(
        center_x_px=center[0],
        center_y_px=center[1],
        direction_x=math.cos(radians),
        direction_y=math.sin(radians),
        length_px=400.0,
        median_width_px=80.0,
        image_width=1000,
        image_height=600,
    )


def _point(
    axis: StemAxisObservation,
    longitudinal: float,
    transverse: float,
) -> np.ndarray:
    center = axis.center
    return (
        center
        + longitudinal * axis.length_px * axis.direction
        + transverse * axis.median_width_px * axis.normal
    )


def _matches(
    previous_axis: StemAxisObservation,
    current_axis: StemAxisObservation,
    *,
    angle_deg: float,
    outlier_count: int = 0,
) -> list[SurfaceFeatureMatch]:
    normalized_delta = angle_deg / 180.0
    matches: list[SurfaceFeatureMatch] = []
    for index in range(24):
        longitudinal = -0.42 + index * 0.84 / 23
        transverse = -0.22 + (index % 7) * 0.07
        current_transverse = transverse + normalized_delta
        if index < outlier_count:
            current_transverse += 0.55 if index % 2 else -0.45
        previous = _point(previous_axis, longitudinal, transverse)
        current = _point(current_axis, longitudinal, current_transverse)
        matches.append(
            SurfaceFeatureMatch(
                previous_x_px=float(previous[0]),
                previous_y_px=float(previous[1]),
                current_x_px=float(current[0]),
                current_y_px=float(current[1]),
                tracking_error_px=0.3,
            )
        )
    return matches


def test_known_angle_calibration_is_versioned_hashed_and_round_trips() -> None:
    profile = _calibration()

    assert profile.valid is True
    assert profile.method == "known_angle_positions"
    assert profile.degrees_per_normalized_displacement == pytest.approx(180.0)
    assert profile.mean_abs_error_deg == pytest.approx(0.0)
    assert profile.max_abs_error_deg == pytest.approx(0.0)
    assert len(profile.calibration_hash) == 64

    parsed = stem_rotation_calibration_from_config(profile.runtime_config())
    assert parsed.valid is True
    assert parsed == profile


def test_temporary_circumference_marker_dataset_uses_the_same_validated_mapping() -> None:
    profile = _calibration(StemRotationCalibrationMethod.TEMPORARY_CIRCUMFERENCE_MARKERS)

    assert profile.valid is True
    assert profile.method == "temporary_circumference_markers"
    assert profile.degrees_per_normalized_displacement == pytest.approx(180.0)
    assert stem_rotation_calibration_from_config(profile.runtime_config()) == profile


def test_relative_rotation_compensates_translation_and_axis_pose() -> None:
    calibration = _calibration()
    previous_axis = _axis(center=(400.0, 300.0))
    current_axis = _axis(center=(430.0, 285.0), axis_rotation_deg=8.0)

    estimate = estimate_surface_rotation_from_matches(
        _matches(previous_axis, current_axis, angle_deg=12.0),
        previous_axis=previous_axis,
        current_axis=current_axis,
        calibration=calibration,
    )

    assert estimate.valid is True
    assert estimate.delta_rotation_deg == pytest.approx(12.0, abs=0.15)
    assert estimate.translation_x_px == pytest.approx(30.0)
    assert estimate.translation_y_px == pytest.approx(-15.0)
    assert estimate.axis_rotation_deg == pytest.approx(8.0)
    assert estimate.inlier_ratio == pytest.approx(1.0)


def test_pure_translation_does_not_create_rotation() -> None:
    calibration = _calibration()
    previous_axis = _axis(center=(380.0, 285.0), axis_rotation_deg=-7.0)
    current_axis = _axis(center=(460.0, 335.0), axis_rotation_deg=-7.0)

    estimate = estimate_surface_rotation_from_matches(
        _matches(previous_axis, current_axis, angle_deg=0.0),
        previous_axis=previous_axis,
        current_axis=current_axis,
        calibration=calibration,
    )

    assert estimate.valid is True
    assert estimate.delta_rotation_deg == pytest.approx(0.0, abs=0.05)
    assert estimate.translation_magnitude_px == pytest.approx(math.hypot(80.0, 50.0))


def test_multiple_features_limit_short_surface_slip_outliers() -> None:
    calibration = _calibration()
    axes = [
        _axis(center=(400.0 + 12.0 * index, 300.0), axis_rotation_deg=2.0 * index)
        for index in range(4)
    ]
    cumulative = 0.0
    for index, expected_step in enumerate((10.0, 12.0, 8.0)):
        estimate = estimate_surface_rotation_from_matches(
            _matches(
                axes[index],
                axes[index + 1],
                angle_deg=expected_step,
                outlier_count=5 if index == 1 else 0,
            ),
            previous_axis=axes[index],
            current_axis=axes[index + 1],
            calibration=calibration,
        )
        assert estimate.valid is True
        assert estimate.delta_rotation_deg == pytest.approx(expected_step, abs=0.2)
        assert estimate.inlier_feature_count >= 19
        cumulative += float(estimate.delta_rotation_deg)

    assert cumulative == pytest.approx(30.0, abs=0.4)


def test_feature_loss_and_invalid_calibration_fail_closed() -> None:
    calibration = _calibration()
    previous_axis = _axis(center=(400.0, 300.0))
    current_axis = _axis(center=(410.0, 300.0))

    lost = estimate_surface_rotation_from_matches(
        _matches(previous_axis, current_axis, angle_deg=10.0)[:3],
        previous_axis=previous_axis,
        current_axis=current_axis,
        calibration=calibration,
        config=SurfaceRotationEstimationConfig(minimum_matched_features=8),
    )
    assert lost.valid is False
    assert lost.delta_rotation_deg is None
    assert "rotation_feature_count_insufficient" in lost.reason_codes

    invalid_config = calibration.runtime_config()
    invalid_config["rotation_calibration_hash"] = "tampered"
    invalid = stem_rotation_calibration_from_config(invalid_config)
    estimate = estimate_surface_rotation_from_matches(
        _matches(previous_axis, current_axis, angle_deg=10.0),
        previous_axis=previous_axis,
        current_axis=current_axis,
        calibration=invalid,
    )
    assert estimate.valid is False
    assert estimate.delta_rotation_deg is None
    assert "rotation_calibration_hash_mismatch" in estimate.reason_codes
