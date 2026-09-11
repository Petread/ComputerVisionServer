from __future__ import annotations

import numpy as np
import pytest
from cv_contracts.runtime import (
    RuntimeStemCurvatureCoordinateFrameDefinition,
    RuntimeStemCurvatureImageGate,
    RuntimeStemCurvatureImagePoint,
    RuntimeStemCurvatureImagePolygon,
    RuntimeStemCurvatureProcessCalibrationCalculateRequest,
    RuntimeStemCurvatureProcessPoint,
    RuntimeStemCurvatureProcessReferencePoint,
    RuntimeStemCurvatureReferencePlane,
)
from cv_runtime.stem_process_coordinates import (
    calculate_stem_curvature_process_calibration,
    gate_crossed,
    image_points_to_process,
    normalized_image_point_to_pixel,
    pixel_image_point_to_normalized,
    point_in_image_polygon,
    point_in_process_polygon,
    process_points_to_image,
    stem_curvature_process_calibration_hash,
    validate_stem_curvature_process_calibration,
    validate_stem_curvature_process_frame,
    warp_mask_to_rectified,
)


def process_calibration_request(
    *,
    conveyor_direction: str = "POSITIVE_X",
    gate_x: tuple[float, float, float] = (80.0, 360.0, 560.0),
) -> RuntimeStemCurvatureProcessCalibrationCalculateRequest:
    reference_pairs = [
        ("top-left", (0.0, 0.0), (0.0, -600.0)),
        ("top-right", (639.0, 0.0), (3000.0, -600.0)),
        ("bottom-right", (639.0, 479.0), (3000.0, 600.0)),
        ("bottom-left", (0.0, 479.0), (0.0, 600.0)),
        ("center", (319.5, 239.5), (1500.0, 0.0)),
    ]
    references = [
        RuntimeStemCurvatureProcessReferencePoint(
            point_id=point_id,
            image_point=RuntimeStemCurvatureImagePoint(x_px=image[0], y_px=image[1]),
            process_point=RuntimeStemCurvatureProcessPoint(
                x_mm=process[0],
                y_mm=process[1],
            ),
        )
        for point_id, image, process in reference_pairs
    ]

    def polygon(points: list[tuple[float, float]]) -> RuntimeStemCurvatureImagePolygon:
        return RuntimeStemCurvatureImagePolygon(
            points=[RuntimeStemCurvatureImagePoint(x_px=x, y_px=y) for x, y in points]
        )

    def gate(x: float) -> RuntimeStemCurvatureImageGate:
        return RuntimeStemCurvatureImageGate(
            start=RuntimeStemCurvatureImagePoint(x_px=x, y_px=40),
            end=RuntimeStemCurvatureImagePoint(x_px=x, y_px=439),
        )

    return RuntimeStemCurvatureProcessCalibrationCalculateRequest(
        calibration_id="stem-process-line-1-v1",
        image_width=640,
        image_height=480,
        intrinsic_calibration_hash="a" * 64,
        reference_points=references,
        reference_plane=RuntimeStemCurvatureReferencePlane(
            plane_id="longitudinal-conveyor",
            z_mm=0,
            z_positive_direction="TOWARD_CAMERA",
            x_axis_description="X increases along the longitudinal conveyor.",
            y_axis_description="Y increases horizontally across the conveyor.",
            z_axis_description="Z is normal to the reference plane toward the camera.",
        ),
        candidate_roi=polygon([(20, 50), (170, 50), (170, 430), (20, 430)]),
        curvature_observation_roi=polygon(
            [(100, 70), (380, 70), (380, 410), (100, 410)]
        ),
        surface_tracking_roi=polygon(
            [(60, 40), (520, 40), (520, 440), (60, 440)]
        ),
        session_start_gate=gate(gate_x[0]),
        curvature_freeze_gate=gate(gate_x[1]),
        decision_gate=gate(gate_x[2]),
        decision_margin_mm=50,
        conveyor_direction=conveyor_direction,  # type: ignore[arg-type]
        rotation_reference_view="LOOKING_IN_CONVEYOR_DIRECTION",
        positive_rotation="CLOCKWISE",
        diameter_class="medium",
        maximum_rms_reprojection_error_px=0.25,
        reference_frame_timestamp="2026-08-04T10:00:00Z",
        notes="Synthetic planar reference.",
    )


def _profile():
    computation = calculate_stem_curvature_process_calibration(
        source_channel_id="chn_stem",
        request=process_calibration_request(),
        operator_user_id="usr_test",
        generated_at="2026-08-04T10:01:00Z",
    )
    assert computation.status == "valid"
    assert computation.profile is not None
    return computation.profile


def test_known_point_mapping_round_trip_serialization_and_hash() -> None:
    profile = _profile()

    center = image_points_to_process([(319.5, 239.5)], profile)[0]
    restored = process_points_to_image([center], profile)[0]
    serialized = profile.model_dump_json()
    reparsed = type(profile).model_validate_json(serialized)

    assert center.x_mm == pytest.approx(1500.0, abs=1e-6)
    assert center.y_mm == pytest.approx(0.0, abs=1e-6)
    assert restored.x_px == pytest.approx(319.5, abs=1e-6)
    assert restored.y_px == pytest.approx(239.5, abs=1e-6)
    assert reparsed == profile
    assert stem_curvature_process_calibration_hash(profile) == profile.calibration_hash
    assert validate_stem_curvature_process_calibration(reparsed) == profile


def test_process_projection_can_clip_diagnostic_points_to_image_bounds() -> None:
    profile = _profile().model_copy(
        update={
            "homography_process_to_image": [
                1.0,
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
                0.0,
                0.0,
                1.0,
            ]
        }
    )

    projected = process_points_to_image(
        [(-18.4, -7.0), (profile.image_width + 20.0, profile.image_height + 30.0)],
        profile,
        clip_to_image=True,
    )

    assert projected[0].x_px == 0.0
    assert projected[0].y_px == 0.0
    assert projected[1].x_px == profile.image_width - 1
    assert projected[1].y_px == profile.image_height - 1


def test_axis_assistant_builds_metric_xy_homography_and_documents_z_separately() -> None:
    request = process_calibration_request()
    origin = RuntimeStemCurvatureImagePoint(x_px=0, y_px=0)
    x_endpoint = RuntimeStemCurvatureImagePoint(x_px=639, y_px=0)
    y_endpoint = RuntimeStemCurvatureImagePoint(x_px=0, y_px=479)
    xy_corner = RuntimeStemCurvatureImagePoint(x_px=639, y_px=479)
    definition = RuntimeStemCurvatureCoordinateFrameDefinition(
        origin_image_point=origin,
        x_axis_image_endpoint=x_endpoint,
        x_length_mm=3000,
        y_axis_image_endpoint=y_endpoint,
        y_length_mm=1200,
        xy_corner_image_point=xy_corner,
        z_axis_image_endpoint=RuntimeStemCurvatureImagePoint(x_px=40, y_px=40),
        z_direction_semantics="UP_FROM_REFERENCE_PLANE",
        z_reference_length_mm=250,
    )
    references = [
        RuntimeStemCurvatureProcessReferencePoint(
            point_id="axis-origin",
            image_point=origin,
            process_point=RuntimeStemCurvatureProcessPoint(x_mm=0, y_mm=0),
        ),
        RuntimeStemCurvatureProcessReferencePoint(
            point_id="axis-x-end",
            image_point=x_endpoint,
            process_point=RuntimeStemCurvatureProcessPoint(x_mm=3000, y_mm=0),
        ),
        RuntimeStemCurvatureProcessReferencePoint(
            point_id="axis-y-end",
            image_point=y_endpoint,
            process_point=RuntimeStemCurvatureProcessPoint(x_mm=0, y_mm=1200),
        ),
        RuntimeStemCurvatureProcessReferencePoint(
            point_id="axis-xy-corner",
            image_point=xy_corner,
            process_point=RuntimeStemCurvatureProcessPoint(x_mm=3000, y_mm=1200),
        ),
    ]
    computation = calculate_stem_curvature_process_calibration(
        source_channel_id="chn_stem",
        request=request.model_copy(
            update={
                "reference_points": references,
                "coordinate_frame_definition": definition,
                "measurement_geometry_mode": "CANONICAL_VIEW_CORRIDOR_V1",
                "canonical_view_profile_id": "stem-measurement-view-v1",
                "canonical_view_profile_checksum": "c" * 64,
            }
        ),
        generated_at="2026-08-06T12:00:00Z",
    )

    assert computation.status == "valid", computation.failure_message
    assert computation.profile is not None
    assert computation.profile.coordinate_frame_definition == definition
    assert computation.profile.measurement_geometry_mode == "CANONICAL_VIEW_CORRIDOR_V1"
    assert computation.profile.canonical_view_profile_id == "stem-measurement-view-v1"
    mapped = image_points_to_process([(639, 479)], computation.profile)[0]
    assert mapped.x_mm == pytest.approx(3000, abs=1e-6)
    assert mapped.y_mm == pytest.approx(1200, abs=1e-6)
    assert computation.profile.reference_points == references


def test_canonical_corridor_expands_process_bounds_beyond_inner_reference_rectangle() -> None:
    request = process_calibration_request()
    reference_pairs = [
        ("inner-top-left", (100.0, 100.0), (0.0, 0.0)),
        ("inner-top-right", (500.0, 100.0), (2000.0, 0.0)),
        ("inner-bottom-right", (500.0, 400.0), (2000.0, 600.0)),
        ("inner-bottom-left", (100.0, 400.0), (0.0, 600.0)),
    ]
    references = [
        RuntimeStemCurvatureProcessReferencePoint(
            point_id=point_id,
            image_point=RuntimeStemCurvatureImagePoint(x_px=image[0], y_px=image[1]),
            process_point=RuntimeStemCurvatureProcessPoint(
                x_mm=process[0],
                y_mm=process[1],
            ),
        )
        for point_id, image, process in reference_pairs
    ]
    corridor = RuntimeStemCurvatureImagePolygon(
        points=[
            RuntimeStemCurvatureImagePoint(x_px=20, y_px=50),
            RuntimeStemCurvatureImagePoint(x_px=620, y_px=50),
            RuntimeStemCurvatureImagePoint(x_px=620, y_px=450),
            RuntimeStemCurvatureImagePoint(x_px=20, y_px=450),
        ]
    )

    def gate(x_px: float) -> RuntimeStemCurvatureImageGate:
        return RuntimeStemCurvatureImageGate(
            start=RuntimeStemCurvatureImagePoint(x_px=x_px, y_px=50),
            end=RuntimeStemCurvatureImagePoint(x_px=x_px, y_px=450),
        )

    computation = calculate_stem_curvature_process_calibration(
        source_channel_id="chn_stem",
        request=request.model_copy(
            update={
                "reference_points": references,
                "candidate_roi": corridor,
                "curvature_observation_roi": corridor,
                "surface_tracking_roi": corridor,
                # Canonical stem entry is a transverse side boundary and therefore is not
                # required to precede the longitudinal exit gates in X.
                "session_start_gate": gate(620),
                "curvature_freeze_gate": gate(620),
                "decision_gate": gate(620),
                "decision_margin_mm": 0,
                "measurement_geometry_mode": "CANONICAL_VIEW_CORRIDOR_V1",
                "canonical_view_profile_id": "stem-measurement-view-v1",
                "canonical_view_profile_checksum": "d" * 64,
            }
        ),
        generated_at="2026-08-06T12:30:00Z",
    )

    assert computation.status == "valid", computation.failure_message
    assert computation.profile is not None
    assert computation.profile.process_bounds.x_min_mm < 0
    assert computation.profile.process_bounds.x_max_mm > 2000
    assert computation.profile.process_bounds.y_min_mm < 0
    assert computation.profile.process_bounds.y_max_mm > 600


def test_legacy_process_profile_hash_ignores_absent_axis_assistant_definition() -> None:
    profile = _profile()
    legacy_payload = profile.model_dump(mode="json")
    legacy_payload.pop("coordinate_frame_definition", None)

    assert stem_curvature_process_calibration_hash(
        profile
    ) == stem_curvature_process_calibration_hash(legacy_payload)
    assert validate_stem_curvature_process_calibration(legacy_payload).calibration_hash == (
        profile.calibration_hash
    )


def test_canonical_corridor_mode_requires_an_exact_view_profile_binding() -> None:
    payload = process_calibration_request().model_dump(mode="json")
    payload["measurement_geometry_mode"] = "CANONICAL_VIEW_CORRIDOR_V1"

    with pytest.raises(ValueError, match="requires its view profile ID and checksum"):
        RuntimeStemCurvatureProcessCalibrationCalculateRequest.model_validate(payload)


def test_normalized_coordinates_rois_gates_and_mask_use_both_coordinate_systems() -> None:
    profile = _profile()
    pixel = normalized_image_point_to_pixel(
        0.5,
        0.5,
        image_width=profile.image_width,
        image_height=profile.image_height,
    )
    normalized = pixel_image_point_to_normalized(
        pixel,
        image_width=profile.image_width,
        image_height=profile.image_height,
    )
    process = image_points_to_process([pixel], profile)[0]
    image_before = RuntimeStemCurvatureImagePoint(x_px=40, y_px=240)
    image_after = RuntimeStemCurvatureImagePoint(x_px=120, y_px=240)
    process_before = image_points_to_process([image_before], profile)[0]
    process_after = image_points_to_process([image_after], profile)[0]
    mask = np.zeros((480, 640), dtype=np.uint8)
    mask[100:300, 150:350] = 255

    assert normalized == pytest.approx((0.5, 0.5))
    assert point_in_image_polygon(pixel, profile.curvature_observation_roi)
    assert point_in_process_polygon(process, profile.curvature_observation_roi)
    assert gate_crossed(
        image_before,
        image_after,
        profile.session_start_gate,
        coordinate_system="image",
    )
    assert gate_crossed(
        process_before,
        process_after,
        profile.session_start_gate,
        coordinate_system="process",
    )
    rectified = warp_mask_to_rectified(mask, profile)
    assert rectified.shape == mask.shape
    assert np.count_nonzero(rectified) > 0


def test_gate_order_and_empty_or_degenerate_geometry_fail_closed() -> None:
    wrong_order = calculate_stem_curvature_process_calibration(
        source_channel_id="chn_stem",
        request=process_calibration_request(gate_x=(560.0, 360.0, 80.0)),
    )
    degenerate_payload = process_calibration_request().model_dump(mode="json")
    degenerate_payload["reference_points"][3]["process_point"] = {"x_mm": 3000, "y_mm": -600}
    degenerate = calculate_stem_curvature_process_calibration(
        source_channel_id="chn_stem",
        request=RuntimeStemCurvatureProcessCalibrationCalculateRequest.model_validate(
            degenerate_payload
        ),
    )

    assert wrong_order.status == "invalid"
    assert wrong_order.failure_code == "stem_process_gate_order_invalid"
    assert degenerate.status == "invalid"
    assert degenerate.failure_code in {
        "stem_process_homography_degenerate",
        "stem_process_reference_duplicate",
    }


def test_resolution_intrinsic_binding_and_channel_detect_pose_context_changes() -> None:
    profile = _profile()

    with pytest.raises(ValueError, match="resolution"):
        validate_stem_curvature_process_frame(
            profile,
            image_width=800,
            image_height=600,
            intrinsic_calibration_hash=profile.intrinsic_calibration_hash,
            source_channel_id=profile.source_channel_id,
        )
    with pytest.raises(ValueError, match="camera movement or recalibration"):
        validate_stem_curvature_process_frame(
            profile,
            image_width=profile.image_width,
            image_height=profile.image_height,
            intrinsic_calibration_hash="b" * 64,
            source_channel_id=profile.source_channel_id,
        )
    with pytest.raises(ValueError, match="different runtime channel"):
        validate_stem_curvature_process_frame(
            profile,
            image_width=profile.image_width,
            image_height=profile.image_height,
            intrinsic_calibration_hash=profile.intrinsic_calibration_hash,
            source_channel_id="chn_other",
        )


def test_rehashed_but_inconsistent_gate_geometry_still_fails_closed() -> None:
    profile = _profile()
    payload = profile.model_dump(mode="json")
    payload["decision_gate"]["process_start"]["x_mm"] = 100.0
    tampered = type(profile).model_validate(payload)
    tampered = tampered.model_copy(
        update={
            "calibration_hash": stem_curvature_process_calibration_hash(tampered)
        }
    )

    with pytest.raises(ValueError, match="decision_gate process coordinates"):
        validate_stem_curvature_process_calibration(tampered)
