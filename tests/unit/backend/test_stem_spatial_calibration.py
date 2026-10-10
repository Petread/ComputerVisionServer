from __future__ import annotations

import numpy as np
import pytest
from cv_contracts.runtime import (
    RuntimeCameraCalibrationBoardDefinition,
    RuntimeStemCurvatureCoordinateFrameDefinition,
    RuntimeStemCurvatureImageGate,
    RuntimeStemCurvatureImagePoint,
    RuntimeStemCurvatureImagePolygon,
    RuntimeStemCurvatureProcessCalibrationCalculateRequest,
    RuntimeStemCurvatureProcessCalibrationProfile,
    RuntimeStemCurvatureProcessPoint,
    RuntimeStemCurvatureProcessReferencePoint,
)
from cv_runtime.camera_calibration import (
    camera_calibration_profile_hash,
    create_identity_camera_calibration,
)
from cv_runtime.stem_process_coordinates import (
    StemCurvatureProcessCalibrationComputation,
    calculate_stem_curvature_process_calibration,
    process_profile_for_aligned_measurement_grid,
    process_profile_for_rectified_view,
    stem_curvature_process_calibration_hash,
    validate_stem_curvature_process_calibration,
)
from cv_runtime.stem_spatial_calibration import (
    image_points_to_height_plane,
    spatial_points_to_image,
)
from tests.unit.backend.test_stem_process_coordinates import process_calibration_request


def spatial_request(
    *, noise_px: float = 0, reference_z: float = 0
) -> tuple[RuntimeStemCurvatureProcessCalibrationCalculateRequest, np.ndarray, np.ndarray]:
    """Independent perspective rendering, including different input/output intrinsics."""
    intrinsic = create_identity_camera_calibration(
        source_channel_id="chn_stem",
        image_width=1280,
        image_height=720,
        board_definition=RuntimeCameraCalibrationBoardDefinition(),
    ).profile
    k = np.array([[850.0, 0, 640], [0, 850.0, 360], [0, 0, 1]])
    intrinsic = intrinsic.model_copy(
        update={
            "calibration_method": "CHECKERBOARD_OR_CHARUCO",
            "accepted_observation_count": 8,
            "camera_matrix": [920, 0, 640, 0, 920, 360, 0, 0, 1],
            "output_camera_matrix": k.reshape(-1).tolist(),
            "distortion_coefficients": [-0.15, 0.02, 0, 0, 0],
        }
    )
    intrinsic = intrinsic.model_copy(
        update={"calibration_hash": camera_calibration_profile_hash(intrinsic)}
    )
    center = np.array([-600.0, -800.0, 1600.0 + reference_z])
    forward = np.array([450.0, 250.0, reference_z]) - center
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, [0, 0, 1])
    right /= np.linalg.norm(right)
    rotation = np.stack([right, np.cross(forward, right), forward])
    translation = -rotation @ center

    def pixel(x: float, y: float, z: float = reference_z) -> RuntimeStemCurvatureImagePoint:
        camera = rotation @ np.array([x, y, z]) + translation
        value = k @ camera
        return RuntimeStemCurvatureImagePoint(
            x_px=float(value[0] / value[2]), y_px=float(value[1] / value[2])
        )

    points = [pixel(0, 0), pixel(900, 0), pixel(0, 500), pixel(900, 500)]
    if noise_px:
        offsets = np.random.default_rng(12).normal(0, noise_px, (4, 2))
        points = [
            p.model_copy(update={"x_px": p.x_px + dx, "y_px": p.y_px + dy})
            for p, (dx, dy) in zip(points, offsets, strict=True)
        ]
    definition = RuntimeStemCurvatureCoordinateFrameDefinition(
        origin_image_point=points[0],
        x_axis_image_endpoint=points[1],
        x_length_mm=900,
        y_axis_image_endpoint=points[2],
        y_length_mm=500,
        xy_corner_image_point=points[3],
        z_axis_image_endpoint=pixel(0, 0, reference_z + 300),
        z_reference_length_mm=300,
    )
    refs = [
        RuntimeStemCurvatureProcessReferencePoint(
            point_id=key,
            image_point=p,
            process_point=RuntimeStemCurvatureProcessPoint(x_mm=x, y_mm=y),
        )
        for key, p, (x, y) in zip(
            ["axis-origin", "axis-x-end", "axis-y-end", "axis-xy-corner"],
            points,
            [(0, 0), (900, 0), (0, 500), (900, 500)],
            strict=True,
        )
    ]
    polygon = RuntimeStemCurvatureImagePolygon(
        points=[pixel(x, y) for x, y in [(20, 20), (880, 20), (880, 480), (20, 480)]]
    )
    base = process_calibration_request()
    request = base.model_copy(
        update={
            "image_width": 1280,
            "image_height": 720,
            "intrinsic_calibration": intrinsic,
            "intrinsic_calibration_hash": intrinsic.calibration_hash,
            "spatial_calibration_enabled": True,
            "reference_points": refs,
            "coordinate_frame_definition": definition,
            "reference_plane": base.reference_plane.model_copy(update={"z_mm": reference_z}),
            "candidate_roi": polygon,
            "curvature_observation_roi": polygon,
            "surface_tracking_roi": polygon,
            "session_start_gate": RuntimeStemCurvatureImageGate(
                start=pixel(100, 50), end=pixel(100, 450)
            ),
            "curvature_freeze_gate": RuntimeStemCurvatureImageGate(
                start=pixel(650, 50), end=pixel(650, 450)
            ),
            "decision_gate": RuntimeStemCurvatureImageGate(
                start=pixel(850, 50), end=pixel(850, 450)
            ),
            "maximum_rms_reprojection_error_px": 2.0,
        }
    )
    return request, center, rotation


def calculate(
    request: RuntimeStemCurvatureProcessCalibrationCalculateRequest,
) -> StemCurvatureProcessCalibrationComputation:
    return calculate_stem_curvature_process_calibration(
        source_channel_id="chn_stem", request=request
    )


@pytest.mark.parametrize("reference_z", [0.0, 125.0])
def test_spatial_pose_and_known_height_round_trip(reference_z: float) -> None:
    request, center, rotation = spatial_request(reference_z=reference_z)
    result = calculate(request)
    assert result.status == "valid", result.failure_message
    profile = result.profile
    spatial = profile.spatial_calibration
    assert profile.provenance.method == spatial.method
    assert np.allclose(spatial.camera_center_mm, center, atol=1e-4)
    assert np.allclose(np.reshape(spatial.rotation_world_to_camera, (3, 3)), rotation, atol=1e-7)
    xyz = [[200, 100, reference_z + 180], [800, 400, reference_z + 180]]
    pixels = spatial_points_to_image(xyz, profile)
    assert np.allclose(
        image_points_to_height_plane(pixels, profile, z_mm=reference_z + 180), xyz, atol=1e-5
    )
    wrong_height = image_points_to_height_plane(pixels, profile, z_mm=reference_z)
    assert np.linalg.norm(wrong_height[:, :2] - np.asarray(xyz)[:, :2]) > 50
    restored = RuntimeStemCurvatureProcessCalibrationProfile.model_validate_json(
        profile.model_dump_json()
    )
    assert validate_stem_curvature_process_calibration(restored) == profile


def test_noisy_reference_fit_uses_xyz_and_reports_actual_residuals() -> None:
    request, center, _ = spatial_request(noise_px=0.15)
    result = calculate(request)
    assert result.status == "valid", result.failure_message
    assert (
        np.linalg.norm(np.asarray(result.profile.spatial_calibration.camera_center_mm) - center)
        < 15
    )
    assert 0 < result.profile.spatial_calibration.maximum_reprojection_error_px < 2
    validate_stem_curvature_process_calibration(result.profile)


@pytest.mark.parametrize(
    "case,reason",
    [
        ("no_intrinsic", "stem_spatial_intrinsic_required"),
        ("passthrough", "stem_spatial_intrinsic_not_measured"),
        ("binding", "stem_spatial_intrinsic_binding_mismatch"),
        ("no_z_length", "stem_spatial_z_reference_required"),
        ("short_z", "stem_spatial_z_reference_too_short"),
        ("outside_z", "stem_spatial_reference_outside_image"),
    ],
)
def test_unusable_spatial_inputs_do_not_fall_back_to_valid_xy(case: str, reason: str) -> None:
    request, _, _ = spatial_request()
    if case == "no_intrinsic":
        request.intrinsic_calibration = None
    elif case == "passthrough":
        request.intrinsic_calibration = create_identity_camera_calibration(
            source_channel_id="chn_stem",
            image_width=1280,
            image_height=720,
            board_definition=RuntimeCameraCalibrationBoardDefinition(),
        ).profile
    elif case == "binding":
        request.intrinsic_calibration_hash = "a" * 64
    elif case == "no_z_length":
        request.coordinate_frame_definition.z_reference_length_mm = None
    elif case == "short_z":
        request.coordinate_frame_definition.z_axis_image_endpoint = (
            request.coordinate_frame_definition.origin_image_point.model_copy(
                update={"x_px": request.coordinate_frame_definition.origin_image_point.x_px + 1}
            )
        )
    else:
        request.coordinate_frame_definition.z_axis_image_endpoint.x_px = 1500
    result = calculate(request)
    assert result.status == "invalid" and result.profile is None
    assert result.failure_code == reason


def test_incorrect_z_measurement_rejected() -> None:
    request, _, _ = spatial_request()
    request.coordinate_frame_definition.z_reference_length_mm = 850
    result = calculate(request)
    assert result.status == "invalid"
    assert result.failure_code in {
        "stem_spatial_reprojection_exceeded",
        "stem_spatial_axis_inconsistent",
    }


def test_camera_side_and_positive_z_must_agree() -> None:
    request, _, _ = spatial_request()
    request.reference_plane.z_positive_direction = "AWAY_FROM_CAMERA"
    result = calculate(request)
    assert result.status == "invalid"
    assert result.failure_code == "stem_spatial_axis_inconsistent"


def test_pose_tampering_rejected_even_with_rehashed_profile() -> None:
    profile = calculate(spatial_request()[0]).profile
    profile.spatial_calibration.camera_center_mm[2] += 100
    profile.calibration_hash = stem_curvature_process_calibration_hash(profile)
    with pytest.raises(ValueError, match="inconsistent"):
        validate_stem_curvature_process_calibration(profile)


def test_crop_and_aligned_raster_transform_spatial_projection() -> None:
    profile = calculate(spatial_request()[0]).profile
    warp = np.array([[0.7, 0, 20], [0, 0.7, 10], [0, 0, 1.0]])
    inverse = np.asarray(profile.homography_process_to_image).reshape(3, 3)
    cropped = process_profile_for_rectified_view(
        profile,
        image_width=1000,
        image_height=600,
        transform_parameters={
            "homography_process_to_view": (warp @ inverse).reshape(-1).tolist(),
            "homography_view_to_process": np.linalg.inv(warp @ inverse).reshape(-1).tolist(),
        },
    )
    xyz = [[400, 200, 150]]
    expected = spatial_points_to_image(xyz, profile) * 0.7 + [20, 10]
    assert np.allclose(spatial_points_to_image(xyz, cropped), expected)
    assert np.allclose(image_points_to_height_plane(expected, cropped, z_mm=150), xyz)
    aligned = process_profile_for_aligned_measurement_grid(cropped)
    assert np.allclose(
        image_points_to_height_plane(spatial_points_to_image(xyz, aligned), aligned, z_mm=150), xyz
    )
    assert cropped.calibration_hash == profile.calibration_hash == aligned.calibration_hash


def test_legacy_xy_profiles_keep_their_hash_and_require_explicit_spatial_opt_in() -> None:
    profile = calculate(process_calibration_request()).profile
    document = profile.model_dump(mode="json")
    document.pop("spatial_calibration")
    assert stem_curvature_process_calibration_hash(document) == profile.calibration_hash
    restored = validate_stem_curvature_process_calibration(document)
    assert restored.spatial_calibration is None
    with pytest.raises(ValueError, match="spatial"):
        image_points_to_height_plane([[200, 100]], restored, z_mm=50)


def test_runtime_handler_preserves_pose_and_projects_into_output_view() -> None:
    from cv_runtime.nodes.stem_process_coordinates import StemProcessCoordinateRectifyHandler
    from tests.unit.backend.test_runtime_stem_process_coordinates import (
        _context,
        _node,
        _undistorted_frame,
    )

    profile = calculate(spatial_request()[0]).profile
    frame = _undistorted_frame(
        intrinsic_hash=profile.intrinsic_calibration_hash,
        width=1280,
        height=720,
    )
    output = (
        StemProcessCoordinateRectifyHandler()
        .execute(
            _context(),
            _node(profile.model_dump(mode="json")),
            {"frame": frame},
        )
        .outputs["frame"]
    )
    stored = output.metadata["stem_curvature_process_calibration"]
    assert validate_stem_curvature_process_calibration(stored) == profile
    parameters = output.image_context.transform.parameters
    warp = np.asarray(parameters["homography_image_to_rectified"]).reshape(3, 3)
    expected = warp @ np.asarray(profile.spatial_calibration.projection_matrix).reshape(3, 4)
    assert np.allclose(np.reshape(parameters["spatial_projection_matrix"], (3, 4)), expected)
    assert parameters["spatial_camera_center_mm"] == profile.spatial_calibration.camera_center_mm
