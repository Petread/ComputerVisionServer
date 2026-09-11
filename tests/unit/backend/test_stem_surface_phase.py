from __future__ import annotations

import hashlib
import json
import math

import numpy as np
import pytest
from cv_contracts.runtime import (
    RuntimeStemSurfacePhaseCalibrationCalculateRequest,
    RuntimeStemSurfacePhaseCalibrationObservation,
    RuntimeStemSurfacePhaseCalibrationProfile,
)
from cv_runtime.stem_rotation import StemAxisObservation, SurfaceFeatureMatch
from cv_runtime.stem_surface_phase import (
    CylindricalSurfacePhaseConfig,
    PhaseRelockCandidate,
    calculate_stem_surface_phase_calibration,
    estimate_cylindrical_surface_phase_from_matches,
    estimate_raw_surface_phase_from_matches,
    phase_calibration_runtime_reasons,
    select_phase_relock_candidate,
    stem_surface_phase_calibration_hash,
    validate_stem_surface_phase_calibration,
)


def _axis(
    *, center_x: float = 320, center_y: float = 240, angle_deg: float = 0
) -> StemAxisObservation:
    angle = np.deg2rad(angle_deg)
    return StemAxisObservation(
        center_x_px=center_x,
        center_y_px=center_y,
        direction_x=float(np.cos(angle)),
        direction_y=float(np.sin(angle)),
        length_px=400,
        median_width_px=160,
        image_width=640,
        image_height=480,
    )


def _matches(
    previous: StemAxisObservation,
    current: StemAxisObservation,
    *,
    raw_delta: float,
    count: int = 30,
) -> list[SurfaceFeatureMatch]:
    matches: list[SurfaceFeatureMatch] = []
    for index in range(count):
        s = -0.4 + 0.8 * index / max(count - 1, 1)
        u = -0.25 + 0.5 * ((index * 7) % count) / max(count - 1, 1)
        previous_point = (
            previous.center
            + s * previous.length_px * previous.direction
            + u * previous.median_width_px * previous.normal
        )
        current_point = (
            current.center
            + s * current.length_px * current.direction
            + (u + raw_delta) * current.median_width_px * current.normal
        )
        matches.append(
            SurfaceFeatureMatch(
                previous_x_px=float(previous_point[0]),
                previous_y_px=float(previous_point[1]),
                current_x_px=float(current_point[0]),
                current_y_px=float(current_point[1]),
                tracking_error_px=0.2,
            )
        )
    return matches


def _request() -> RuntimeStemSurfacePhaseCalibrationCalculateRequest:
    pairs = [(-0.5, -180.0), (-0.25, -90.0), (0.25, 90.0), (0.5, 180.0)]
    return RuntimeStemSurfacePhaseCalibrationCalculateRequest(
        calibration_id="surface-phase-line-1",
        calibration_version="1",
        image_width=640,
        image_height=480,
        intrinsic_calibration_hash="a" * 64,
        process_calibration_hash="b" * 64,
        rotation_reference_view="LOOKING_IN_CONVEYOR_DIRECTION",
        positive_rotation="CLOCKWISE",
        method="MARKED_TEST_STEM",
        observations=[
            RuntimeStemSurfacePhaseCalibrationObservation(
                observation_id=f"pair-{index}",
                raw_phase=raw,
                known_angle_deg=angle,
                diameter_mm=380 + index * 10,
                image_center_x_px=300 + index * 10,
                image_center_y_px=225 + index * 10,
            )
            for index, (raw, angle) in enumerate(pairs, start=1)
        ],
    )


def _profile() -> RuntimeStemSurfacePhaseCalibrationProfile:
    computation = calculate_stem_surface_phase_calibration(
        source_channel_id="chn_stem",
        request=_request(),
        operator_user_id="usr_test",
        generated_at="2026-08-04T12:00:00Z",
    )
    assert computation.profile is not None
    return computation.profile


def test_raw_phase_compensates_translation_and_axis_change() -> None:
    previous = _axis()
    translated = _axis(center_x=347, center_y=226)
    translated_result = estimate_raw_surface_phase_from_matches(
        _matches(previous, translated, raw_delta=0),
        previous_axis=previous,
        current_axis=translated,
    )
    combined = _axis(center_x=335, center_y=250, angle_deg=11)
    combined_result = estimate_raw_surface_phase_from_matches(
        _matches(previous, combined, raw_delta=-0.12),
        previous_axis=previous,
        current_axis=combined,
    )

    assert translated_result.valid
    assert translated_result.delta_raw_phase == pytest.approx(0, abs=1e-9)
    assert translated_result.translation_x_px == pytest.approx(27)
    assert translated_result.translation_y_px == pytest.approx(-14)
    assert combined_result.valid
    assert combined_result.delta_raw_phase == pytest.approx(-0.12, abs=1e-9)
    assert combined_result.axis_rotation_deg == pytest.approx(11, abs=1e-9)


def test_known_rotation_signs_remain_distinct() -> None:
    axis = _axis()
    positive = estimate_raw_surface_phase_from_matches(
        _matches(axis, axis, raw_delta=0.2), previous_axis=axis, current_axis=axis
    )
    negative = estimate_raw_surface_phase_from_matches(
        _matches(axis, axis, raw_delta=-0.2), previous_axis=axis, current_axis=axis
    )

    assert positive.valid and positive.delta_raw_phase == pytest.approx(0.2)
    assert negative.valid and negative.delta_raw_phase == pytest.approx(-0.2)


def test_cylindrical_v2_separates_translation_from_surface_rotation() -> None:
    axis = _axis()
    delta_theta = 0.08
    transverse_translation = 0.045
    matches: list[SurfaceFeatureMatch] = []
    angles = np.linspace(-1.05, 1.05, 48)
    for index, angle in enumerate(angles):
        s = -0.42 + 0.84 * index / max(len(angles) - 1, 1)
        previous_u = 0.5 * math.sin(float(angle))
        current_u = 0.5 * math.sin(float(angle) + delta_theta) + transverse_translation
        previous_point = (
            axis.center
            + s * axis.length_px * axis.direction
            + previous_u * axis.median_width_px * axis.normal
        )
        current_point = (
            axis.center
            + (s * axis.length_px + 4.0) * axis.direction
            + current_u * axis.median_width_px * axis.normal
        )
        matches.append(
            SurfaceFeatureMatch(
                previous_x_px=float(previous_point[0]),
                previous_y_px=float(previous_point[1]),
                current_x_px=float(current_point[0]),
                current_y_px=float(current_point[1]),
                tracking_error_px=0.15,
            )
        )

    estimate = estimate_cylindrical_surface_phase_from_matches(
        matches,
        previous_axis=axis,
        current_axis=axis,
        config=CylindricalSurfacePhaseConfig(
            minimum_matched_features=12,
            minimum_feature_span_ratio=0.35,
        ),
    )

    assert estimate.valid
    assert estimate.delta_raw_phase == pytest.approx(delta_theta, abs=0.01)
    assert estimate.translation_x_px == pytest.approx(4.0, abs=0.5)
    assert estimate.translation_y_px == pytest.approx(
        transverse_translation * axis.median_width_px,
        abs=0.5,
    )


def test_cylindrical_v2_compensates_axis_translation_and_rotation() -> None:
    previous = _axis()
    current = _axis(center_x=338, center_y=229, angle_deg=7)
    delta_theta = -0.06
    matches: list[SurfaceFeatureMatch] = []
    angles = np.linspace(-1.0, 1.0, 48)
    for index, angle in enumerate(angles):
        s = -0.42 + 0.84 * index / max(len(angles) - 1, 1)
        previous_u = 0.5 * math.sin(float(angle))
        current_u = 0.5 * math.sin(float(angle) + delta_theta)
        previous_point = (
            previous.center
            + s * previous.length_px * previous.direction
            + previous_u * previous.median_width_px * previous.normal
        )
        current_point = (
            current.center
            + s * current.length_px * current.direction
            + current_u * current.median_width_px * current.normal
        )
        matches.append(
            SurfaceFeatureMatch(
                previous_x_px=float(previous_point[0]),
                previous_y_px=float(previous_point[1]),
                current_x_px=float(current_point[0]),
                current_y_px=float(current_point[1]),
                tracking_error_px=0.15,
            )
        )

    estimate = estimate_cylindrical_surface_phase_from_matches(
        matches,
        previous_axis=previous,
        current_axis=current,
    )

    assert estimate.valid
    assert estimate.delta_raw_phase == pytest.approx(delta_theta, abs=0.01)
    assert estimate.translation_x_px == pytest.approx(18.0, abs=0.5)
    assert estimate.translation_y_px == pytest.approx(-11.0, abs=0.5)
    assert estimate.axis_rotation_deg == pytest.approx(7.0, abs=0.1)


def test_cylindrical_v2_rejects_features_concentrated_on_one_surface_strip() -> None:
    axis = _axis()
    matches = _matches(axis, axis, raw_delta=0.04, count=24)
    concentrated = [
        SurfaceFeatureMatch(
            previous_x_px=item.previous_x_px,
            previous_y_px=axis.center_y_px + 0.05 * axis.median_width_px,
            current_x_px=item.current_x_px,
            current_y_px=axis.center_y_px + 0.06 * axis.median_width_px,
            tracking_error_px=item.tracking_error_px,
        )
        for item in matches
    ]

    estimate = estimate_cylindrical_surface_phase_from_matches(
        concentrated,
        previous_axis=axis,
        current_axis=axis,
    )

    assert not estimate.valid
    assert "PHASE_FEATURE_COVERAGE_INSUFFICIENT" in estimate.reason_codes


def test_calibration_roundtrip_fit_and_runtime_binding() -> None:
    profile = _profile()
    restored = type(profile).model_validate_json(profile.model_dump_json())
    legacy = profile.model_dump(mode="json")
    for observation in legacy["observations"]:
        for key in ("actual_marking", "calculated_phase_deg", "error_deg", "accepted"):
            observation.pop(key)
    legacy.pop("calibration_hash")
    legacy_hash = hashlib.sha256(
        json.dumps(
            legacy,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    legacy["calibration_hash"] = legacy_hash
    pre_validity_inputs = profile.model_dump(mode="json")
    for key in (
        "requested_valid_diameter_min_mm",
        "requested_valid_diameter_max_mm",
        "diameter_tolerance_ratio",
        "image_center_tolerance_x_ratio",
        "image_center_tolerance_y_ratio",
    ):
        pre_validity_inputs.pop(key)
    pre_validity_inputs.pop("calibration_hash")
    pre_validity_inputs_hash = hashlib.sha256(
        json.dumps(
            pre_validity_inputs,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    pre_validity_inputs["calibration_hash"] = pre_validity_inputs_hash

    assert profile.degrees_per_raw_phase == pytest.approx(360)
    assert profile.accepted_observation_count == 4
    assert [item.calculated_phase_deg for item in profile.observations] == pytest.approx(
        [-180.0, -90.0, 90.0, 180.0]
    )
    assert all(item.error_deg == pytest.approx(0.0) for item in profile.observations)
    assert all(item.accepted is True for item in profile.observations)
    assert stem_surface_phase_calibration_hash(profile) == profile.calibration_hash
    assert validate_stem_surface_phase_calibration(restored) == restored
    assert validate_stem_surface_phase_calibration(legacy).calibration_hash == legacy_hash
    assert (
        validate_stem_surface_phase_calibration(pre_validity_inputs).calibration_hash
        == pre_validity_inputs_hash
    )
    assert (
        phase_calibration_runtime_reasons(
            profile,
            image_width=640,
            image_height=480,
            intrinsic_calibration_hash="a" * 64,
            process_calibration_hash="b" * 64,
            source_channel_id="chn_stem",
            diameter_mm=400,
            center_x_px=320,
            center_y_px=240,
            rotation_reference_view="LOOKING_IN_CONVEYOR_DIRECTION",
            positive_rotation="CLOCKWISE",
        )
        == ()
    )
    assert profile.valid_diameter_min_mm < min(
        item.diameter_mm for item in profile.observations
    )
    assert profile.valid_diameter_max_mm > max(
        item.diameter_mm for item in profile.observations
    )
    assert profile.valid_image_center_x_min_px < min(
        item.image_center_x_px for item in profile.observations
    )


def test_calibration_uses_explicit_product_diameter_range() -> None:
    request = _request().model_copy(
        update={
            "valid_diameter_min_mm": 250.0,
            "valid_diameter_max_mm": 650.0,
            "image_center_tolerance_x_ratio": 0.3,
            "image_center_tolerance_y_ratio": 0.25,
        }
    )
    computation = calculate_stem_surface_phase_calibration(
        source_channel_id="chn_stem",
        request=request,
        generated_at="2026-08-18T12:00:00Z",
    )

    assert computation.profile is not None
    assert computation.profile.valid_diameter_min_mm == pytest.approx(250.0)
    assert computation.profile.valid_diameter_max_mm == pytest.approx(650.0)
    assert computation.profile.requested_valid_diameter_min_mm == pytest.approx(250.0)
    assert computation.profile.requested_valid_diameter_max_mm == pytest.approx(650.0)
    assert computation.profile.image_center_tolerance_x_ratio == pytest.approx(0.3)
    assert computation.profile.image_center_tolerance_y_ratio == pytest.approx(0.25)
    assert computation.profile.valid_image_center_x_min_px == pytest.approx(118.0)
    assert computation.profile.valid_image_center_x_max_px == pytest.approx(532.0)


def test_calibration_hash_survives_browser_signed_zero_roundtrip() -> None:
    request = _request()
    pairs = [(-0.3, 90.0), (-0.2, 60.0), (-0.1, 30.0), (0.0, 0.0)]
    observations = [
        observation.model_copy(
            update={"raw_phase": raw_phase, "known_angle_deg": known_angle_deg}
        )
        for observation, (raw_phase, known_angle_deg) in zip(
            request.observations,
            pairs,
            strict=True,
        )
    ]
    computation = calculate_stem_surface_phase_calibration(
        source_channel_id="chn_stem",
        request=request.model_copy(update={"observations": observations}),
        generated_at="2026-08-12T12:00:00Z",
    )

    assert computation.profile is not None
    profile = computation.profile
    calculated_zero = profile.observations[-1].calculated_phase_deg
    assert calculated_zero is not None
    assert math.copysign(1.0, calculated_zero) < 0

    browser_payload = profile.model_dump(mode="json")
    browser_payload["observations"][-1]["calculated_phase_deg"] = 0.0

    assert validate_stem_surface_phase_calibration(browser_payload).calibration_hash == (
        profile.calibration_hash
    )

    old_payload = profile.model_dump(mode="json")
    old_payload.pop("calibration_hash")
    old_hash = hashlib.sha256(
        json.dumps(
            old_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    browser_payload["calibration_hash"] = old_hash

    assert validate_stem_surface_phase_calibration(browser_payload).calibration_hash == old_hash


def test_inconsistent_phase_fit_reports_actionable_error_metrics() -> None:
    raw_values = [0.0, -0.1582, -0.1918, -0.1327]
    known_angles = [0.0, 30.0, 60.0, 90.0]
    request = _request().model_copy(
        update={
            "observations": [
                item.model_copy(
                    update={"raw_phase": raw, "known_angle_deg": angle}
                )
                for item, raw, angle in zip(
                    _request().observations,
                    raw_values,
                    known_angles,
                    strict=True,
                )
            ]
        }
    )

    computation = calculate_stem_surface_phase_calibration(
        source_channel_id="chn_stem",
        request=request,
    )

    assert computation.status == "invalid"
    assert computation.failure_code == "phase_calibration_error_too_high"
    assert computation.mean_abs_error_deg == pytest.approx(19.2864, rel=1e-4)
    assert computation.maximum_abs_error_deg == pytest.approx(42.8889, rel=1e-4)
    assert "backward roll" in str(computation.failure_message)


def test_four_phase_pairs_report_fit_error_instead_of_rejecting_a_sample() -> None:
    raw_values = [0.0, -0.0472, -0.0671, -0.1048]
    known_angles = [0.0, 45.0, 90.0, 135.0]
    request = _request().model_copy(
        update={
            "observations": [
                item.model_copy(
                    update={
                        "raw_phase": raw,
                        "known_angle_deg": angle,
                        "actual_marking": f"Frame {frame}",
                        "weight": weight,
                    }
                )
                for item, raw, angle, frame, weight in zip(
                    _request().observations,
                    raw_values,
                    known_angles,
                    [6741, 6749, 6765, 6778],
                    [1.0, 0.86, 0.86, 0.88],
                    strict=True,
                )
            ]
        }
    )

    computation = calculate_stem_surface_phase_calibration(
        source_channel_id="chn_stem",
        request=request,
    )

    assert computation.status == "invalid"
    assert computation.failure_code == "phase_calibration_error_too_high"
    assert computation.mean_abs_error_deg == pytest.approx(5.73, abs=0.02)
    assert computation.maximum_abs_error_deg == pytest.approx(14.47, abs=0.05)
    assert "Frame 6749" in str(computation.failure_message)
    assert "59.47 deg" in str(computation.failure_message)


def test_missing_calibration_explicitly_blocks_degrees_only() -> None:
    reasons = phase_calibration_runtime_reasons(
        None,
        image_width=640,
        image_height=480,
        intrinsic_calibration_hash="a" * 64,
        process_calibration_hash="b" * 64,
        source_channel_id="chn_stem",
        diameter_mm=400,
        center_x_px=320,
        center_y_px=240,
        rotation_reference_view="LOOKING_IN_CONVEYOR_DIRECTION",
        positive_rotation="CLOCKWISE",
    )

    assert reasons == ("PHASE_CALIBRATION_MISSING",)


def test_v1_degree_calibration_is_rejected_for_v2_estimator() -> None:
    reasons = phase_calibration_runtime_reasons(
        _profile(),
        image_width=640,
        image_height=480,
        intrinsic_calibration_hash="a" * 64,
        process_calibration_hash="b" * 64,
        source_channel_id="chn_stem",
        diameter_mm=400,
        center_x_px=320,
        center_y_px=240,
        rotation_reference_view="LOOKING_IN_CONVEYOR_DIRECTION",
        positive_rotation="CLOCKWISE",
        phase_estimator_version="CYLINDRICAL_SURFACE_V2",
    )

    assert "PHASE_CALIBRATION_ESTIMATOR_MISMATCH" in reasons


def test_v2_calibration_records_estimator_binding() -> None:
    computation = calculate_stem_surface_phase_calibration(
        source_channel_id="chn_stem",
        request=_request().model_copy(
            update={"phase_estimator_version": "CYLINDRICAL_SURFACE_V2"}
        ),
    )

    assert computation.status == "valid"
    assert computation.profile is not None
    assert computation.profile.phase_estimator_version == "CYLINDRICAL_SURFACE_V2"


def test_relock_requires_unique_plausible_keyframe() -> None:
    selected = select_phase_relock_candidate(
        [
            PhaseRelockCandidate("kf-right", 0.42, 0.82, 24, 0.9),
            PhaseRelockCandidate("kf-weak", 0.35, 0.55, 12, 0.6),
        ],
        last_safe_raw_phase=0.4,
    )
    ambiguous = select_phase_relock_candidate(
        [
            PhaseRelockCandidate("kf-a", 0.42, 0.82, 24, 0.9),
            PhaseRelockCandidate("kf-b", 0.44, 0.78, 22, 0.88),
        ],
        last_safe_raw_phase=0.4,
    )
    impact_without_match = select_phase_relock_candidate(
        [PhaseRelockCandidate("kf-far", 2.0, 0.95, 30, 0.95)],
        last_safe_raw_phase=0.4,
    )

    assert selected.status == "RELOCKED" and selected.candidate is not None
    assert selected.candidate.keyframe_id == "kf-right"
    assert ambiguous.status == "AMBIGUOUS" and "PHASE_TRACK_LOST" in ambiguous.reason_codes
    assert impact_without_match.status == "FAILED"
    assert "PHASE_TRACK_LOST" in impact_without_match.reason_codes
