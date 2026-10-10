"""Spatial extension of the existing XY/Z process calibration; no depth from a lone pixel."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

from cv_contracts.runtime import (
    RuntimeStemCurvatureProcessCalibrationCalculateRequest,
    RuntimeStemCurvatureProcessCalibrationProfile,
    RuntimeStemSpatialCalibration,
)

from cv_runtime.camera_calibration import validate_camera_intrinsic_calibration

if TYPE_CHECKING:
    import numpy as np


class SpatialCalibrationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _references(
    value: RuntimeStemCurvatureProcessCalibrationCalculateRequest
    | RuntimeStemCurvatureProcessCalibrationProfile,
) -> tuple[np.ndarray, np.ndarray]:
    import numpy as np

    definition = value.coordinate_frame_definition
    if definition is None or definition.z_reference_length_mm is None:
        raise SpatialCalibrationError(
            "stem_spatial_z_reference_required",
            "Spatial calibration requires the axis assistant and a measured Z length from O.",
        )
    world = [
        [p.process_point.x_mm, p.process_point.y_mm, value.reference_plane.z_mm]
        for p in value.reference_points
    ]
    pixels = [[p.image_point.x_px, p.image_point.y_px] for p in value.reference_points]
    # Z is the selected positive machine axis, including when configured toward the base.
    world.append([0.0, 0.0, value.reference_plane.z_mm + definition.z_reference_length_mm])
    pixels.append([definition.z_axis_image_endpoint.x_px, definition.z_axis_image_endpoint.y_px])
    xyz, uv = np.asarray(world, dtype=np.float64), np.asarray(pixels, dtype=np.float64)
    if (
        not np.isfinite(xyz).all()
        or not np.isfinite(uv).all()
        or not math.isfinite(definition.z_reference_length_mm)
        or definition.z_reference_length_mm <= 0
    ):
        raise SpatialCalibrationError(
            "stem_spatial_reference_invalid", "Spatial references must be finite."
        )
    if (
        np.any(uv < 0)
        or np.any(uv[:, 0] > value.image_width - 1)
        or np.any(uv[:, 1] > value.image_height - 1)
    ):
        raise SpatialCalibrationError(
            "stem_spatial_reference_outside_image",
            "Z and XY points must be inside the undistorted image.",
        )
    origin = np.asarray([definition.origin_image_point.x_px, definition.origin_image_point.y_px])
    if np.linalg.norm(uv[-1] - origin) < max(5.0, 5.0 * _error_limit(value)):
        raise SpatialCalibrationError(
            "stem_spatial_z_reference_too_short",
            "The Z reference is too short in the image; use a longer measured vertical reference.",
        )
    return xyz, uv


def _error_limit(
    value: RuntimeStemCurvatureProcessCalibrationCalculateRequest
    | RuntimeStemCurvatureProcessCalibrationProfile,
) -> float:
    return (
        value.maximum_rms_reprojection_error_px
        if isinstance(value, RuntimeStemCurvatureProcessCalibrationCalculateRequest)
        else value.maximum_allowed_reprojection_error_px
    )


def _camera_matrix(values: Sequence[float]) -> np.ndarray:
    import numpy as np

    matrix = np.asarray(values, dtype=np.float64).reshape(3, 3)
    if (
        not np.isfinite(matrix).all()
        or matrix[0, 0] <= 0
        or matrix[1, 1] <= 0
        or not np.allclose(matrix[2], [0, 0, 1], atol=1e-9)
        or abs(matrix[0, 1]) > 1e-9
        or abs(matrix[1, 0]) > 1e-9
    ):
        raise SpatialCalibrationError(
            "stem_spatial_intrinsic_invalid",
            "Spatial calibration requires a standard finite pinhole output camera matrix.",
        )
    return matrix


def _pose_metrics(
    xyz: np.ndarray, uv: np.ndarray, k: np.ndarray, rvec: np.ndarray, tvec: np.ndarray
) -> tuple[np.ndarray, float]:
    import cv2
    import numpy as np

    projected, jacobian = cv2.projectPoints(xyz, rvec, tvec, k, None)
    errors = np.linalg.norm(projected.reshape(-1, 2) - uv, axis=1)
    jacobian = jacobian[:, :6].copy()
    # Translation is scaled by scene extent before comparing it to angular columns.
    jacobian[:, 3:6] *= max(float(np.linalg.norm(np.ptp(xyz, axis=0))), 1.0)
    return errors, float(np.linalg.cond(jacobian))


def calculate_spatial_calibration(
    request: RuntimeStemCurvatureProcessCalibrationCalculateRequest,
    *,
    source_channel_id: str,
) -> RuntimeStemSpatialCalibration:
    import cv2
    import numpy as np

    if request.intrinsic_calibration is None:
        raise SpatialCalibrationError(
            "stem_spatial_intrinsic_required",
            "Connect a measured camera calibration before calculating spatial XYZ.",
        )
    intrinsic = validate_camera_intrinsic_calibration(request.intrinsic_calibration)
    if (
        intrinsic.calibration_method == "IDENTITY_PASSTHROUGH"
        or intrinsic.accepted_observation_count < 1
    ):
        raise SpatialCalibrationError(
            "stem_spatial_intrinsic_not_measured",
            "Identity passthrough cannot provide a metric camera pose. "
            "Calibrate the existing undistort node first.",
        )
    if (
        intrinsic.calibration_hash != request.intrinsic_calibration_hash
        or intrinsic.source_channel_id != source_channel_id
        or (intrinsic.image_width, intrinsic.image_height)
        != (request.image_width, request.image_height)
    ):
        raise SpatialCalibrationError(
            "stem_spatial_intrinsic_binding_mismatch",
            "Camera calibration, channel, resolution and intrinsic hash must match.",
        )
    k = _camera_matrix(intrinsic.output_camera_matrix)
    xyz, uv = _references(request)
    # Points are selected on the undistorted full frame: use output K and NO distortion.
    try:
        candidates = cv2.solvePnPGeneric(xyz[:-1], uv[:-1], k, None, flags=cv2.SOLVEPNP_IPPE)
        solutions = []
        for rvec, tvec in zip(candidates[1], candidates[2], strict=True):
            rvec, tvec = cv2.solvePnPRefineLM(xyz, uv, k, None, rvec.copy(), tvec.copy())
            rotation = cv2.Rodrigues(rvec)[0]
            camera_center = (-rotation.T @ tvec).reshape(3)
            camera_points = xyz @ rotation.T + tvec.reshape(3)
            if np.any(camera_points[:, 2] <= 1e-6):
                continue
            side = camera_center[2] - request.reference_plane.z_mm
            expected_side = (
                1 if request.reference_plane.z_positive_direction == "TOWARD_CAMERA" else -1
            )
            if side * expected_side <= 1e-6:
                continue
            errors, condition = _pose_metrics(xyz, uv, k, rvec, tvec)
            if not np.isfinite(errors).all() or not math.isfinite(condition):
                continue
            solutions.append(
                (
                    float(np.sqrt(np.mean(errors**2))),
                    errors,
                    condition,
                    rotation,
                    tvec,
                    camera_center,
                )
            )
    except cv2.error as exc:
        raise SpatialCalibrationError(
            "stem_spatial_pose_failed",
            "Camera pose could not be calculated from the XY/Z references.",
        ) from exc
    solutions.sort(key=lambda item: item[0])
    if not solutions:
        raise SpatialCalibrationError(
            "stem_spatial_axis_inconsistent",
            "No visible right-handed XYZ pose matches the references and configured camera side. "
            "Check X/Y order and the physical Z endpoint.",
        )
    rms, errors, condition, rotation, translation, center = solutions[0]
    limit = request.maximum_rms_reprojection_error_px
    if rms > limit or float(np.max(errors)) > limit:
        raise SpatialCalibrationError(
            "stem_spatial_reprojection_exceeded",
            f"Spatial references disagree: RMS {rms:.3f}px, maximum {np.max(errors):.3f}px "
            f"(limit {limit:.3f}px). Check the measured Z length and camera calibration.",
        )
    if condition > 1e6:
        raise SpatialCalibrationError(
            "stem_spatial_pose_ill_conditioned",
            "Spatial reference geometry is ill-conditioned; spread out the XY and Z references.",
        )
    for other in solutions[1:]:
        relative = other[3] @ rotation.T
        difference = math.degrees(math.acos(float(np.clip((np.trace(relative) - 1) / 2, -1, 1))))
        if difference > 1.0 and other[0] - rms < max(0.25, limit * 0.25):
            raise SpatialCalibrationError(
                "stem_spatial_pose_ambiguous",
                "More than one camera pose fits the references; "
                "use a more informative Z reference.",
            )
    definition = request.coordinate_frame_definition
    assert definition is not None
    origin = np.asarray([definition.origin_image_point.x_px, definition.origin_image_point.y_px])
    return RuntimeStemSpatialCalibration(
        camera_matrix=k.reshape(-1).tolist(),
        rotation_world_to_camera=rotation.reshape(-1).tolist(),
        translation_world_to_camera_mm=translation.reshape(-1).tolist(),
        camera_center_mm=center.tolist(),
        projection_matrix=(k @ np.column_stack((rotation, translation))).reshape(-1).tolist(),
        reference_point_count=len(xyz),
        rms_reprojection_error_px=rms,
        maximum_reprojection_error_px=float(np.max(errors)),
        z_reprojection_error_px=float(errors[-1]),
        z_reference_displacement_px=float(np.linalg.norm(uv[-1] - origin)),
        pose_condition_number=condition,
    )


def validate_spatial_calibration(profile: RuntimeStemCurvatureProcessCalibrationProfile) -> None:
    spatial = profile.spatial_calibration
    if spatial is None:
        return
    import cv2
    import numpy as np

    xyz, uv = _references(profile)
    k = _camera_matrix(spatial.camera_matrix)
    r = np.asarray(spatial.rotation_world_to_camera).reshape(3, 3)
    t = np.asarray(spatial.translation_world_to_camera_mm)
    center = np.asarray(spatial.camera_center_mm)
    projection = np.asarray(spatial.projection_matrix).reshape(3, 4)
    if (
        not np.isfinite(r).all()
        or not np.isfinite(t).all()
        or not np.isfinite(center).all()
        or not np.isfinite(projection).all()
        or not np.allclose(r.T @ r, np.eye(3), atol=1e-7)
        or not math.isclose(float(np.linalg.det(r)), 1.0, abs_tol=1e-7)
        or not np.allclose(center, -r.T @ t, atol=1e-6)
        or not np.allclose(projection, k @ np.column_stack((r, t)), atol=1e-7)
    ):
        raise ValueError("spatial calibration pose/projection is inconsistent")
    if np.any((xyz @ r.T + t)[:, 2] <= 1e-6):
        raise ValueError("spatial calibration references are behind the camera")
    side = 1 if profile.reference_plane.z_positive_direction == "TOWARD_CAMERA" else -1
    if (center[2] - profile.reference_plane.z_mm) * side <= 1e-6:
        raise ValueError("spatial calibration camera side is inconsistent")
    errors, condition = _pose_metrics(xyz, uv, k, cv2.Rodrigues(r)[0], t)
    definition = profile.coordinate_frame_definition
    assert definition is not None
    displacement = float(
        np.linalg.norm(
            uv[-1] - [definition.origin_image_point.x_px, definition.origin_image_point.y_px]
        )
    )
    actual = [
        float(np.sqrt(np.mean(errors**2))),
        float(np.max(errors)),
        float(errors[-1]),
        condition,
        displacement,
    ]
    stored = [
        spatial.rms_reprojection_error_px,
        spatial.maximum_reprojection_error_px,
        spatial.z_reprojection_error_px,
        spatial.pose_condition_number,
        spatial.z_reference_displacement_px,
    ]
    if (
        not np.isfinite(actual).all()
        or not np.allclose(actual, stored, atol=1e-6, rtol=1e-5)
        or len(xyz) != spatial.reference_point_count
        or condition > 1e6
        or max(errors) > profile.maximum_allowed_reprojection_error_px
    ):
        raise ValueError(
            "spatial calibration reprojection diagnostics are inconsistent or exceed limits"
        )
    plane_projection = np.column_stack(
        (
            projection[:, 0],
            projection[:, 1],
            projection[:, 3] + profile.reference_plane.z_mm * projection[:, 2],
        )
    )
    plane_projection /= plane_projection[2, 2]
    if not np.allclose(
        plane_projection, np.asarray(profile.homography_process_to_image).reshape(3, 3), atol=1e-6
    ):
        raise ValueError("spatial camera pose and XY plane homography disagree")


def spatial_points_to_image(
    points: Sequence[Sequence[float]], profile: RuntimeStemCurvatureProcessCalibrationProfile
) -> np.ndarray:
    """Project metric XYZ into the profile's pixel space (also supports derived crop profiles)."""
    import numpy as np

    spatial = profile.spatial_calibration
    if spatial is None:
        raise ValueError("a spatial process calibration is required")
    xyz = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    r = np.asarray(spatial.rotation_world_to_camera).reshape(3, 3)
    t = np.asarray(spatial.translation_world_to_camera_mm)
    p = np.asarray(spatial.projection_matrix).reshape(3, 4)
    projected = np.column_stack((xyz, np.ones(len(xyz)))) @ p.T
    if (
        not np.isfinite(xyz).all()
        or np.any((xyz @ r.T + t)[:, 2] <= 1e-6)
        or np.any(np.abs(projected[:, 2]) <= 1e-9)
    ):
        raise ValueError(
            "spatial point is non-finite, behind the camera or at the projection horizon"
        )
    return projected[:, :2] / projected[:, 2:3]


def image_points_to_height_plane(
    points: Sequence[Sequence[float]],
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
    *,
    z_mm: float,
) -> np.ndarray:
    """Intersect pixel rays with an explicitly KNOWN world height; does not estimate height."""
    import numpy as np

    spatial = profile.spatial_calibration
    if spatial is None or not math.isfinite(z_mm):
        raise ValueError("spatial calibration and a finite known height are required")
    pixels = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    p = np.asarray(spatial.projection_matrix).reshape(3, 4)
    rays = np.column_stack((pixels, np.ones(len(pixels)))) @ np.linalg.inv(p[:, :3]).T
    center = np.asarray(spatial.camera_center_mm)
    if not np.isfinite(pixels).all() or np.any(np.abs(rays[:, 2]) < 1e-12):
        raise ValueError("pixel ray is non-finite or parallel to the requested height plane")
    scales = (z_mm - center[2]) / rays[:, 2]
    xyz = center + scales[:, None] * rays
    spatial_points_to_image(xyz, profile)  # Reject intersections behind the camera.
    return xyz
