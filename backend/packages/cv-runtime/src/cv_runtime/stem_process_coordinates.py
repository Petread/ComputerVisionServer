from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from cv_contracts.runtime import (
    RuntimeStemCurvatureImageGate,
    RuntimeStemCurvatureImagePoint,
    RuntimeStemCurvatureImagePolygon,
    RuntimeStemCurvatureProcessBounds,
    RuntimeStemCurvatureProcessCalibrationCalculateRequest,
    RuntimeStemCurvatureProcessCalibrationProfile,
    RuntimeStemCurvatureProcessCalibrationProvenance,
    RuntimeStemCurvatureProcessGate,
    RuntimeStemCurvatureProcessPoint,
    RuntimeStemCurvatureProcessPolygon,
)
from cv_utils import utc_now_iso


@dataclass(frozen=True)
class StemCurvatureProcessCalibrationComputation:
    status: Literal["valid", "invalid"]
    profile: RuntimeStemCurvatureProcessCalibrationProfile | None
    rms_reprojection_error_px: float | None = None
    maximum_reprojection_error_px: float | None = None
    failure_code: str | None = None
    failure_message: str | None = None


def calculate_stem_curvature_process_calibration(
    *,
    source_channel_id: str,
    request: RuntimeStemCurvatureProcessCalibrationCalculateRequest,
    operator_user_id: str | None = None,
    generated_at: str | None = None,
) -> StemCurvatureProcessCalibrationComputation:
    cv2, np = _opencv_modules()
    image_points = np.asarray(
        [
            [item.image_point.x_px, item.image_point.y_px]
            for item in request.reference_points
        ],
        dtype=np.float64,
    )
    process_points = np.asarray(
        [
            [item.process_point.x_mm, item.process_point.y_mm]
            for item in request.reference_points
        ],
        dtype=np.float64,
    )
    try:
        _validate_reference_points(
            image_points,
            process_points,
            image_width=request.image_width,
            image_height=request.image_height,
            cv2=cv2,
            np=np,
        )
        homography, _mask = cv2.findHomography(image_points, process_points, method=0)
        if homography is None:
            raise _CalibrationError(
                "stem_process_homography_degenerate",
                "The reference points do not define a homography.",
            )
        homography = _normalized_homography(homography, np=np)
        inverse = _normalized_homography(np.linalg.inv(homography), np=np)
        _validate_homography_pair(homography, inverse, np=np)

        reprojected_image = _project_array(process_points, inverse, cv2=cv2, np=np)
        errors = np.linalg.norm(reprojected_image - image_points, axis=1)
        rms_error = float(np.sqrt(np.mean(errors**2)))
        maximum_error = float(np.max(errors))
        if not math.isfinite(rms_error) or not math.isfinite(maximum_error):
            raise _CalibrationError(
                "stem_process_reprojection_non_finite",
                "The process calibration produced non-finite reprojection errors.",
            )
        if rms_error > request.maximum_rms_reprojection_error_px:
            raise _CalibrationError(
                "stem_process_reprojection_error_exceeded",
                (
                    f"RMS reprojection error {rms_error:.4f}px exceeds the configured "
                    f"limit {request.maximum_rms_reprojection_error_px:.4f}px."
                ),
                rms_error=rms_error,
                maximum_error=maximum_error,
            )

        process_bounds = RuntimeStemCurvatureProcessBounds(
            x_min_mm=float(np.min(process_points[:, 0])),
            x_max_mm=float(np.max(process_points[:, 0])),
            y_min_mm=float(np.min(process_points[:, 1])),
            y_max_mm=float(np.max(process_points[:, 1])),
        )
        candidate_roi = _map_polygon(request.candidate_roi, homography, cv2=cv2, np=np)
        curvature_roi = _map_polygon(
            request.curvature_observation_roi,
            homography,
            cv2=cv2,
            np=np,
        )
        surface_roi = _map_polygon(
            request.surface_tracking_roi,
            homography,
            cv2=cv2,
            np=np,
        )
        session_gate = _map_gate(request.session_start_gate, homography, cv2=cv2, np=np)
        freeze_gate = _map_gate(request.curvature_freeze_gate, homography, cv2=cv2, np=np)
        decision_gate = _map_gate(request.decision_gate, homography, cv2=cv2, np=np)
        if request.measurement_geometry_mode == "CANONICAL_VIEW_CORRIDOR_V1":
            corridor_process_points = np.asarray(
                [
                    [point.x_mm, point.y_mm]
                    for point in candidate_roi.process_points
                ],
                dtype=np.float64,
            )
            bounded_process_points = np.vstack(
                [process_points, corridor_process_points]
            )
            process_bounds = RuntimeStemCurvatureProcessBounds(
                x_min_mm=float(np.min(bounded_process_points[:, 0])),
                x_max_mm=float(np.max(bounded_process_points[:, 0])),
                y_min_mm=float(np.min(bounded_process_points[:, 1])),
                y_max_mm=float(np.max(bounded_process_points[:, 1])),
            )
        _validate_process_geometry(
            image_width=request.image_width,
            image_height=request.image_height,
            process_bounds=process_bounds,
            candidate_roi=candidate_roi,
            curvature_roi=curvature_roi,
            surface_roi=surface_roi,
            session_gate=session_gate,
            freeze_gate=freeze_gate,
            decision_gate=decision_gate,
            decision_margin_mm=request.decision_margin_mm,
            conveyor_direction=request.conveyor_direction,
            measurement_geometry_mode=request.measurement_geometry_mode,
            cv2=cv2,
            np=np,
        )
    except _CalibrationError as exc:
        return StemCurvatureProcessCalibrationComputation(
            status="invalid",
            profile=None,
            rms_reprojection_error_px=exc.rms_error,
            maximum_reprojection_error_px=exc.maximum_error,
            failure_code=exc.code,
            failure_message=exc.message,
        )
    except (ValueError, ArithmeticError) as exc:
        return StemCurvatureProcessCalibrationComputation(
            status="invalid",
            profile=None,
            failure_code="stem_process_calibration_invalid",
            failure_message=str(exc),
        )

    profile = RuntimeStemCurvatureProcessCalibrationProfile(
        calibration_id=request.calibration_id,
        status="valid",
        source_channel_id=source_channel_id,
        image_width=request.image_width,
        image_height=request.image_height,
        intrinsic_calibration_hash=request.intrinsic_calibration_hash,
        reference_points=request.reference_points,
        coordinate_frame_definition=request.coordinate_frame_definition,
        measurement_geometry_mode=request.measurement_geometry_mode,
        canonical_view_profile_id=request.canonical_view_profile_id,
        canonical_view_profile_checksum=request.canonical_view_profile_checksum,
        homography_image_to_process=[float(value) for value in homography.reshape(-1)],
        homography_process_to_image=[float(value) for value in inverse.reshape(-1)],
        process_bounds=process_bounds,
        reference_plane=request.reference_plane,
        candidate_roi=candidate_roi,
        curvature_observation_roi=curvature_roi,
        surface_tracking_roi=surface_roi,
        session_start_gate=session_gate,
        curvature_freeze_gate=freeze_gate,
        decision_gate=decision_gate,
        decision_margin_mm=request.decision_margin_mm,
        conveyor_direction=request.conveyor_direction,
        rotation_reference_view=request.rotation_reference_view,
        positive_rotation=request.positive_rotation,
        diameter_class=request.diameter_class,
        height_class=request.height_class,
        rms_reprojection_error_px=rms_error,
        maximum_reprojection_error_px=maximum_error,
        maximum_allowed_reprojection_error_px=request.maximum_rms_reprojection_error_px,
        provenance=RuntimeStemCurvatureProcessCalibrationProvenance(
            created_at=generated_at or utc_now_iso(),
            operator_user_id=operator_user_id,
            reference_frame_timestamp=request.reference_frame_timestamp,
            reference_frame_sha256=request.reference_frame_sha256,
            notes=request.notes,
        ),
        calibration_hash="0" * 64,
    )
    profile = profile.model_copy(
        update={"calibration_hash": stem_curvature_process_calibration_hash(profile)}
    )
    validate_stem_curvature_process_calibration(profile)
    return StemCurvatureProcessCalibrationComputation(
        status="valid",
        profile=profile,
        rms_reprojection_error_px=rms_error,
        maximum_reprojection_error_px=maximum_error,
    )


def stem_curvature_process_calibration_hash(
    profile: RuntimeStemCurvatureProcessCalibrationProfile | dict[str, Any],
) -> str:
    payload = (
        profile.model_dump(mode="json")
        if isinstance(profile, RuntimeStemCurvatureProcessCalibrationProfile)
        else dict(profile)
    )
    payload.pop("calibration_hash", None)
    if payload.get("coordinate_frame_definition") is None:
        payload.pop("coordinate_frame_definition", None)
    if payload.get("measurement_geometry_mode") == "EXPLICIT_GEOMETRY_V1":
        payload.pop("measurement_geometry_mode", None)
    if payload.get("canonical_view_profile_id") is None:
        payload.pop("canonical_view_profile_id", None)
    if payload.get("canonical_view_profile_checksum") is None:
        payload.pop("canonical_view_profile_checksum", None)
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_stem_curvature_process_calibration(
    value: object,
    *,
    require_valid: bool = True,
) -> RuntimeStemCurvatureProcessCalibrationProfile:
    cv2, np = _opencv_modules()
    profile = RuntimeStemCurvatureProcessCalibrationProfile.model_validate(value)
    if require_valid and profile.status != "valid":
        raise ValueError("stem curvature process calibration profile is not valid")
    if profile.rms_reprojection_error_px > profile.maximum_allowed_reprojection_error_px:
        raise ValueError("stem process calibration reprojection error exceeds its limit")
    image_to_process = np.asarray(
        profile.homography_image_to_process,
        dtype=np.float64,
    ).reshape(3, 3)
    process_to_image = np.asarray(
        profile.homography_process_to_image,
        dtype=np.float64,
    ).reshape(3, 3)
    _validate_homography_pair(image_to_process, process_to_image, np=np)
    reference_image = np.asarray(
        [
            [item.image_point.x_px, item.image_point.y_px]
            for item in profile.reference_points
        ],
        dtype=np.float64,
    )
    reference_process = np.asarray(
        [
            [item.process_point.x_mm, item.process_point.y_mm]
            for item in profile.reference_points
        ],
        dtype=np.float64,
    )
    _validate_reference_points(
        reference_image,
        reference_process,
        image_width=profile.image_width,
        image_height=profile.image_height,
        cv2=cv2,
        np=np,
    )
    _validate_profile_geometry_consistency(
        profile,
        image_to_process=image_to_process,
        process_to_image=process_to_image,
        cv2=cv2,
        np=np,
    )
    _validate_process_geometry(
        image_width=profile.image_width,
        image_height=profile.image_height,
        process_bounds=profile.process_bounds,
        candidate_roi=profile.candidate_roi,
        curvature_roi=profile.curvature_observation_roi,
        surface_roi=profile.surface_tracking_roi,
        session_gate=profile.session_start_gate,
        freeze_gate=profile.curvature_freeze_gate,
        decision_gate=profile.decision_gate,
        decision_margin_mm=profile.decision_margin_mm,
        conveyor_direction=profile.conveyor_direction,
        measurement_geometry_mode=profile.measurement_geometry_mode,
        cv2=cv2,
        np=np,
    )
    if stem_curvature_process_calibration_hash(profile) != profile.calibration_hash:
        raise ValueError("stem process calibration hash does not match the profile")
    return profile


def validate_stem_curvature_process_frame(
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
    *,
    image_width: int,
    image_height: int,
    intrinsic_calibration_hash: str | None,
    source_channel_id: str | None,
) -> None:
    if (image_width, image_height) != (profile.image_width, profile.image_height):
        raise ValueError(
            f"frame resolution {image_width}x{image_height} does not match process "
            f"calibration resolution {profile.image_width}x{profile.image_height}"
        )
    if intrinsic_calibration_hash != profile.intrinsic_calibration_hash:
        raise ValueError(
            "the undistorted frame intrinsic calibration hash does not match the process "
            "calibration; camera movement or recalibration requires a new process profile"
        )
    if source_channel_id is not None and source_channel_id != profile.source_channel_id:
        raise ValueError("the process calibration belongs to a different runtime channel")


def process_profile_for_rectified_view(
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
    *,
    image_width: int,
    image_height: int,
    transform_parameters: dict[str, Any] | None = None,
) -> RuntimeStemCurvatureProcessCalibrationProfile:
    """Return a geometry-only profile for a canonical output without changing its identity.

    The stored calibration remains bound to the undistorted source resolution.  Runtime
    measurement helpers historically used its image dimensions as the rectified raster
    size, so canonical views with another output size use this non-persisted projection.
    """

    parameters = transform_parameters or {}
    raw_bounds = parameters.get("process_view_bounds")
    bounds = profile.process_bounds
    if isinstance(raw_bounds, dict):
        bounds = RuntimeStemCurvatureProcessBounds.model_validate(raw_bounds)
    image_to_process = parameters.get("homography_view_to_process")
    process_to_image = parameters.get("homography_process_to_view")
    updates: dict[str, Any] = {
        "image_width": int(image_width),
        "image_height": int(image_height),
        "process_bounds": bounds,
    }
    if isinstance(image_to_process, list) and len(image_to_process) == 9:
        updates["homography_image_to_process"] = image_to_process
    if isinstance(process_to_image, list) and len(process_to_image) == 9:
        updates["homography_process_to_image"] = process_to_image
    return profile.model_copy(
        update=updates
    )


def process_profile_for_aligned_measurement_grid(
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
) -> RuntimeStemCurvatureProcessCalibrationProfile:
    """Describe an internal X/Y-aligned mask raster without changing calibration identity."""
    _cv2, np = _opencv_modules()
    bounds = profile.process_bounds
    x_span = bounds.x_max_mm - bounds.x_min_mm
    y_span = bounds.y_max_mm - bounds.y_min_mm
    image_to_process = np.asarray(
        [
            [x_span / max(profile.image_width - 1, 1), 0.0, bounds.x_min_mm],
            [0.0, y_span / max(profile.image_height - 1, 1), bounds.y_min_mm],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    process_to_image = _normalized_homography(np.linalg.inv(image_to_process), np=np)
    return profile.model_copy(
        update={
            "homography_image_to_process": [
                float(value) for value in image_to_process.reshape(-1)
            ],
            "homography_process_to_image": [
                float(value) for value in process_to_image.reshape(-1)
            ],
        }
    )


def normalized_image_point_to_pixel(
    x_normalized: float,
    y_normalized: float,
    *,
    image_width: int,
    image_height: int,
) -> RuntimeStemCurvatureImagePoint:
    if not 0 <= x_normalized <= 1 or not 0 <= y_normalized <= 1:
        raise ValueError("normalized image coordinates must be in [0, 1]")
    return RuntimeStemCurvatureImagePoint(
        x_px=float(x_normalized) * max(image_width - 1, 0),
        y_px=float(y_normalized) * max(image_height - 1, 0),
    )


def pixel_image_point_to_normalized(
    point: RuntimeStemCurvatureImagePoint,
    *,
    image_width: int,
    image_height: int,
) -> tuple[float, float]:
    if image_width < 2 or image_height < 2:
        raise ValueError("image dimensions must be at least 2x2 for normalization")
    return (
        point.x_px / (image_width - 1),
        point.y_px / (image_height - 1),
    )


def image_points_to_process(
    points: Sequence[RuntimeStemCurvatureImagePoint | tuple[float, float]],
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
) -> list[RuntimeStemCurvatureProcessPoint]:
    cv2, np = _opencv_modules()
    array = np.asarray(
        [
            [point.x_px, point.y_px]
            if isinstance(point, RuntimeStemCurvatureImagePoint)
            else [float(point[0]), float(point[1])]
            for point in points
        ],
        dtype=np.float64,
    )
    projected = _project_array(
        array,
        np.asarray(profile.homography_image_to_process, dtype=np.float64).reshape(3, 3),
        cv2=cv2,
        np=np,
    )
    return [
        RuntimeStemCurvatureProcessPoint(x_mm=float(x), y_mm=float(y))
        for x, y in projected
    ]


def process_points_to_image(
    points: Sequence[RuntimeStemCurvatureProcessPoint | tuple[float, float]],
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
    *,
    clip_to_image: bool = False,
) -> list[RuntimeStemCurvatureImagePoint]:
    """Project process points, optionally bounding display-only image coordinates.

    Calibration and geometry callers remain strict by default. ``clip_to_image`` is
    intended for diagnostic rendering where an off-canvas projection is valid and
    must not invalidate the underlying measurement.
    """
    cv2, np = _opencv_modules()
    array = np.asarray(
        [
            [point.x_mm, point.y_mm]
            if isinstance(point, RuntimeStemCurvatureProcessPoint)
            else [float(point[0]), float(point[1])]
            for point in points
        ],
        dtype=np.float64,
    )
    projected = _project_array(
        array,
        np.asarray(profile.homography_process_to_image, dtype=np.float64).reshape(3, 3),
        cv2=cv2,
        np=np,
    )
    image_x_max = float(max(profile.image_width - 1, 0))
    image_y_max = float(max(profile.image_height - 1, 0))
    image_points: list[RuntimeStemCurvatureImagePoint] = []
    for x, y in projected:
        x_px = _clamp_projection_roundoff(float(x))
        y_px = _clamp_projection_roundoff(float(y))
        if clip_to_image:
            x_px = min(max(x_px, 0.0), image_x_max)
            y_px = min(max(y_px, 0.0), image_y_max)
        image_points.append(RuntimeStemCurvatureImagePoint(x_px=x_px, y_px=y_px))
    return image_points


def _clamp_projection_roundoff(value: float) -> float:
    return 0.0 if -1e-9 < value < 0.0 else value


def contour_image_to_process(
    contour: Sequence[Sequence[float]],
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
) -> list[RuntimeStemCurvatureProcessPoint]:
    return image_points_to_process(
        [(float(point[0]), float(point[1])) for point in contour],
        profile,
    )


def point_in_process_polygon(
    point: RuntimeStemCurvatureProcessPoint,
    polygon: RuntimeStemCurvatureProcessPolygon,
) -> bool:
    cv2, np = _opencv_modules()
    contour = np.asarray(
        [[item.x_mm, item.y_mm] for item in polygon.process_points],
        dtype=np.float32,
    )
    return bool(cv2.pointPolygonTest(contour, (point.x_mm, point.y_mm), False) >= 0)


def point_in_image_polygon(
    point: RuntimeStemCurvatureImagePoint,
    polygon: RuntimeStemCurvatureProcessPolygon,
) -> bool:
    cv2, np = _opencv_modules()
    contour = np.asarray(
        [[item.x_px, item.y_px] for item in polygon.image_points],
        dtype=np.float32,
    )
    return bool(cv2.pointPolygonTest(contour, (point.x_px, point.y_px), False) >= 0)


def gate_crossed(
    previous: RuntimeStemCurvatureImagePoint | RuntimeStemCurvatureProcessPoint,
    current: RuntimeStemCurvatureImagePoint | RuntimeStemCurvatureProcessPoint,
    gate: RuntimeStemCurvatureProcessGate,
    *,
    coordinate_system: Literal["image", "process"],
) -> bool:
    if coordinate_system == "image":
        start = (gate.image_start.x_px, gate.image_start.y_px)
        end = (gate.image_end.x_px, gate.image_end.y_px)
        previous_xy = _image_xy(previous)
        current_xy = _image_xy(current)
    else:
        start = (gate.process_start.x_mm, gate.process_start.y_mm)
        end = (gate.process_end.x_mm, gate.process_end.y_mm)
        previous_xy = _process_xy(previous)
        current_xy = _process_xy(current)
    previous_side = _line_side(previous_xy, start, end)
    current_side = _line_side(current_xy, start, end)
    return previous_side == 0 or current_side == 0 or (previous_side < 0 < current_side) or (
        current_side < 0 < previous_side
    )


def rectified_image_homography(
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
) -> list[float]:
    _cv2, np = _opencv_modules()
    bounds = profile.process_bounds
    x_span = bounds.x_max_mm - bounds.x_min_mm
    y_span = bounds.y_max_mm - bounds.y_min_mm
    process_to_rectified = np.asarray(
        [
            [
                (profile.image_width - 1) / x_span,
                0.0,
                -(profile.image_width - 1) * bounds.x_min_mm / x_span,
            ],
            [
                0.0,
                (profile.image_height - 1) / y_span,
                -(profile.image_height - 1) * bounds.y_min_mm / y_span,
            ],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    image_to_process = np.asarray(
        profile.homography_image_to_process,
        dtype=np.float64,
    ).reshape(3, 3)
    matrix = _normalized_homography(process_to_rectified @ image_to_process, np=np)
    return [float(value) for value in matrix.reshape(-1)]


def warp_mask_to_rectified(
    mask: Any,
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
) -> Any:
    cv2, np = _opencv_modules()
    array = np.asarray(mask)
    if array.ndim != 2:
        raise ValueError("process-coordinate mask transformation requires a 2D mask")
    if array.shape != (profile.image_height, profile.image_width):
        raise ValueError("mask resolution does not match the process calibration")
    matrix = np.asarray(rectified_image_homography(profile), dtype=np.float64).reshape(3, 3)
    return cv2.warpPerspective(
        array,
        matrix,
        (profile.image_width, profile.image_height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )


class _CalibrationError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        rms_error: float | None = None,
        maximum_error: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.rms_error = rms_error
        self.maximum_error = maximum_error


def _validate_reference_points(
    image_points: Any,
    process_points: Any,
    *,
    image_width: int,
    image_height: int,
    cv2: Any,
    np: Any,
) -> None:
    if not np.isfinite(image_points).all() or not np.isfinite(process_points).all():
        raise _CalibrationError(
            "stem_process_reference_non_finite",
            "Reference coordinates must be finite.",
        )
    if len(np.unique(image_points, axis=0)) != len(image_points):
        raise _CalibrationError(
            "stem_process_reference_duplicate",
            "Image reference points must be unique.",
        )
    if len(np.unique(process_points, axis=0)) != len(process_points):
        raise _CalibrationError(
            "stem_process_reference_duplicate",
            "Process reference points must be unique.",
        )
    if (
        np.any(image_points[:, 0] < 0)
        or np.any(image_points[:, 0] > image_width - 1)
        or np.any(image_points[:, 1] < 0)
        or np.any(image_points[:, 1] > image_height - 1)
    ):
        raise _CalibrationError(
            "stem_process_reference_outside_image",
            "Reference points must lie inside the undistorted calibration image.",
        )
    image_area = float(cv2.contourArea(cv2.convexHull(image_points.astype(np.float32))))
    process_area = float(cv2.contourArea(cv2.convexHull(process_points.astype(np.float32))))
    if image_area <= 4.0 or process_area <= 1e-6:
        raise _CalibrationError(
            "stem_process_homography_degenerate",
            "Reference points must span a non-degenerate polygon in both coordinate systems.",
        )


def _validate_homography_pair(image_to_process: Any, process_to_image: Any, *, np: Any) -> None:
    values = np.concatenate((image_to_process.reshape(-1), process_to_image.reshape(-1)))
    if not np.isfinite(values).all():
        raise ValueError("stem process homography contains non-finite values")
    if abs(float(np.linalg.det(image_to_process))) <= 1e-12:
        raise ValueError("stem process homography is degenerate")
    if float(np.linalg.cond(image_to_process)) > 1e12:
        raise ValueError("stem process homography is numerically unstable")
    identity = image_to_process @ process_to_image
    identity = identity / identity[2, 2]
    if not np.allclose(identity, np.eye(3), rtol=1e-7, atol=1e-7):
        raise ValueError("stem process homography inverse is inconsistent")


def _map_polygon(
    polygon: RuntimeStemCurvatureImagePolygon,
    homography: Any,
    *,
    cv2: Any,
    np: Any,
) -> RuntimeStemCurvatureProcessPolygon:
    image = np.asarray([[item.x_px, item.y_px] for item in polygon.points], dtype=np.float64)
    process = _project_array(image, homography, cv2=cv2, np=np)
    return RuntimeStemCurvatureProcessPolygon(
        image_points=polygon.points,
        process_points=[
            RuntimeStemCurvatureProcessPoint(x_mm=float(x), y_mm=float(y))
            for x, y in process
        ],
    )


def _map_gate(
    gate: RuntimeStemCurvatureImageGate,
    homography: Any,
    *,
    cv2: Any,
    np: Any,
) -> RuntimeStemCurvatureProcessGate:
    process = _project_array(
        np.asarray(
            [[gate.start.x_px, gate.start.y_px], [gate.end.x_px, gate.end.y_px]],
            dtype=np.float64,
        ),
        homography,
        cv2=cv2,
        np=np,
    )
    return RuntimeStemCurvatureProcessGate(
        image_start=gate.start,
        image_end=gate.end,
        process_start=RuntimeStemCurvatureProcessPoint(
            x_mm=float(process[0, 0]),
            y_mm=float(process[0, 1]),
        ),
        process_end=RuntimeStemCurvatureProcessPoint(
            x_mm=float(process[1, 0]),
            y_mm=float(process[1, 1]),
        ),
    )


def _validate_profile_geometry_consistency(
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
    *,
    image_to_process: Any,
    process_to_image: Any,
    cv2: Any,
    np: Any,
) -> None:
    reference_image = np.asarray(
        [
            [item.image_point.x_px, item.image_point.y_px]
            for item in profile.reference_points
        ],
        dtype=np.float64,
    )
    reference_process = np.asarray(
        [
            [item.process_point.x_mm, item.process_point.y_mm]
            for item in profile.reference_points
        ],
        dtype=np.float64,
    )
    projected_image = _project_array(
        reference_process,
        process_to_image,
        cv2=cv2,
        np=np,
    )
    errors = np.linalg.norm(projected_image - reference_image, axis=1)
    rms_error = float(np.sqrt(np.mean(errors**2)))
    maximum_error = float(np.max(errors))
    if not math.isclose(
        rms_error,
        profile.rms_reprojection_error_px,
        rel_tol=1e-7,
        abs_tol=1e-6,
    ) or not math.isclose(
        maximum_error,
        profile.maximum_reprojection_error_px,
        rel_tol=1e-7,
        abs_tol=1e-6,
    ):
        raise ValueError("stem process stored reprojection errors are inconsistent")

    for label, polygon in (
        ("candidate_roi", profile.candidate_roi),
        ("curvature_observation_roi", profile.curvature_observation_roi),
        ("surface_tracking_roi", profile.surface_tracking_roi),
    ):
        projected = _project_array(
            np.asarray(
                [[point.x_px, point.y_px] for point in polygon.image_points],
                dtype=np.float64,
            ),
            image_to_process,
            cv2=cv2,
            np=np,
        )
        stored = np.asarray(
            [[point.x_mm, point.y_mm] for point in polygon.process_points],
            dtype=np.float64,
        )
        if not np.allclose(projected, stored, rtol=1e-7, atol=1e-5):
            raise ValueError(f"{label} process coordinates do not match the homography")

    for label, gate in (
        ("session_start_gate", profile.session_start_gate),
        ("curvature_freeze_gate", profile.curvature_freeze_gate),
        ("decision_gate", profile.decision_gate),
    ):
        projected = _project_array(
            np.asarray(
                [
                    [gate.image_start.x_px, gate.image_start.y_px],
                    [gate.image_end.x_px, gate.image_end.y_px],
                ],
                dtype=np.float64,
            ),
            image_to_process,
            cv2=cv2,
            np=np,
        )
        stored = np.asarray(
            [
                [gate.process_start.x_mm, gate.process_start.y_mm],
                [gate.process_end.x_mm, gate.process_end.y_mm],
            ],
            dtype=np.float64,
        )
        if not np.allclose(projected, stored, rtol=1e-7, atol=1e-5):
            raise ValueError(f"{label} process coordinates do not match the homography")


def _validate_process_geometry(
    *,
    image_width: int,
    image_height: int,
    process_bounds: RuntimeStemCurvatureProcessBounds,
    candidate_roi: RuntimeStemCurvatureProcessPolygon,
    curvature_roi: RuntimeStemCurvatureProcessPolygon,
    surface_roi: RuntimeStemCurvatureProcessPolygon,
    session_gate: RuntimeStemCurvatureProcessGate,
    freeze_gate: RuntimeStemCurvatureProcessGate,
    decision_gate: RuntimeStemCurvatureProcessGate,
    decision_margin_mm: float,
    conveyor_direction: Literal["POSITIVE_X", "NEGATIVE_X"],
    measurement_geometry_mode: Literal[
        "EXPLICIT_GEOMETRY_V1",
        "CANONICAL_VIEW_CORRIDOR_V1",
    ],
    cv2: Any,
    np: Any,
) -> None:
    for label, polygon in (
        ("candidate_roi", candidate_roi),
        ("curvature_observation_roi", curvature_roi),
        ("surface_tracking_roi", surface_roi),
    ):
        image = np.asarray(
            [[item.x_px, item.y_px] for item in polygon.image_points],
            dtype=np.float32,
        )
        process = np.asarray(
            [[item.x_mm, item.y_mm] for item in polygon.process_points],
            dtype=np.float32,
        )
        if not _points_inside_image(
            image,
            image_width=image_width,
            image_height=image_height,
            np=np,
        ):
            raise _CalibrationError(
                "stem_process_geometry_outside_image",
                f"{label} must lie inside the undistorted image.",
            )
        if abs(float(cv2.contourArea(image))) <= 4.0 or abs(
            float(cv2.contourArea(process))
        ) <= 1e-6:
            raise _CalibrationError(
                "stem_process_measurement_region_empty",
                f"{label} must span a non-empty measurement region.",
            )
        if not _process_points_inside_bounds(process, process_bounds, np=np):
            raise _CalibrationError(
                "stem_process_geometry_outside_reference_plane",
                f"{label} must lie inside the calibrated process reference bounds.",
            )

    for label, gate in (
        ("session_start_gate", session_gate),
        ("curvature_freeze_gate", freeze_gate),
        ("decision_gate", decision_gate),
    ):
        image = np.asarray(
            [
                [gate.image_start.x_px, gate.image_start.y_px],
                [gate.image_end.x_px, gate.image_end.y_px],
            ],
            dtype=np.float64,
        )
        process = np.asarray(
            [
                [gate.process_start.x_mm, gate.process_start.y_mm],
                [gate.process_end.x_mm, gate.process_end.y_mm],
            ],
            dtype=np.float64,
        )
        if not _points_inside_image(
            image,
            image_width=image_width,
            image_height=image_height,
            np=np,
        ):
            raise _CalibrationError(
                "stem_process_geometry_outside_image",
                f"{label} must lie inside the undistorted image.",
            )
        if float(np.linalg.norm(image[1] - image[0])) <= 1.0 or float(
            np.linalg.norm(process[1] - process[0])
        ) <= 1e-6:
            raise _CalibrationError(
                "stem_process_gate_degenerate",
                f"{label} must be a non-degenerate line.",
            )
        if not _process_points_inside_bounds(process, process_bounds, np=np):
            raise _CalibrationError(
                "stem_process_geometry_outside_reference_plane",
                f"{label} must lie inside the calibrated process reference bounds.",
            )

    gate_x = [
        (session_gate.process_start.x_mm + session_gate.process_end.x_mm) / 2.0,
        (freeze_gate.process_start.x_mm + freeze_gate.process_end.x_mm) / 2.0,
        (decision_gate.process_start.x_mm + decision_gate.process_end.x_mm) / 2.0,
    ]
    if measurement_geometry_mode == "CANONICAL_VIEW_CORRIDOR_V1":
        # Freeze and decision are one canonical-view exit event. The curvature node
        # freezes when the leading mask edge reaches this boundary and the solver
        # creates the decision from that same event; no artificial X gap is required.
        ordered = True
    else:
        ordered = gate_x[0] < gate_x[1] < gate_x[2]
        if conveyor_direction == "NEGATIVE_X":
            ordered = gate_x[0] > gate_x[1] > gate_x[2]
    if not ordered:
        raise _CalibrationError(
            "stem_process_gate_order_invalid",
            (
                "Canonical corridor exit geometry is invalid."
                if measurement_geometry_mode == "CANONICAL_VIEW_CORRIDOR_V1"
                else "Gate order must be session start, curvature freeze, then decision along "
                "the conveyor."
            ),
        )
    if abs(gate_x[2] - gate_x[1]) < decision_margin_mm:
        raise _CalibrationError(
            "stem_process_decision_margin_invalid",
            "Decision margin in millimetres must fit along X between freeze and decision gates.",
        )


def _project_array(points: Any, homography: Any, *, cv2: Any, np: Any) -> Any:
    if len(points) == 0:
        return np.empty((0, 2), dtype=np.float64)
    projected = cv2.perspectiveTransform(
        np.asarray(points, dtype=np.float64).reshape(-1, 1, 2),
        np.asarray(homography, dtype=np.float64),
    ).reshape(-1, 2)
    if not np.isfinite(projected).all():
        raise _CalibrationError(
            "stem_process_projection_non_finite",
            "The homography produced non-finite coordinates.",
        )
    return projected


def _normalized_homography(value: Any, *, np: Any) -> Any:
    matrix = np.asarray(value, dtype=np.float64).reshape(3, 3)
    scale = float(matrix[2, 2])
    if not math.isfinite(scale) or abs(scale) <= 1e-15:
        norm = float(np.linalg.norm(matrix))
        if not math.isfinite(norm) or norm <= 1e-15:
            raise _CalibrationError(
                "stem_process_homography_degenerate",
                "The calculated homography is degenerate.",
            )
        return matrix / norm
    return matrix / scale


def _points_inside_image(
    points: Any,
    *,
    image_width: int,
    image_height: int,
    np: Any,
) -> bool:
    return bool(
        np.isfinite(points).all()
        and np.all(points[:, 0] >= 0)
        and np.all(points[:, 0] <= image_width - 1)
        and np.all(points[:, 1] >= 0)
        and np.all(points[:, 1] <= image_height - 1)
    )


def _process_points_inside_bounds(
    points: Any,
    bounds: RuntimeStemCurvatureProcessBounds,
    *,
    np: Any,
) -> bool:
    tolerance = 1e-6
    return bool(
        np.all(points[:, 0] >= bounds.x_min_mm - tolerance)
        and np.all(points[:, 0] <= bounds.x_max_mm + tolerance)
        and np.all(points[:, 1] >= bounds.y_min_mm - tolerance)
        and np.all(points[:, 1] <= bounds.y_max_mm + tolerance)
    )


def _image_xy(
    point: RuntimeStemCurvatureImagePoint | RuntimeStemCurvatureProcessPoint,
) -> tuple[float, float]:
    if not isinstance(point, RuntimeStemCurvatureImagePoint):
        raise TypeError("image gate checks require image points")
    return point.x_px, point.y_px


def _process_xy(
    point: RuntimeStemCurvatureImagePoint | RuntimeStemCurvatureProcessPoint,
) -> tuple[float, float]:
    if not isinstance(point, RuntimeStemCurvatureProcessPoint):
        raise TypeError("process gate checks require process points")
    return point.x_mm, point.y_mm


def _line_side(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    return (end[0] - start[0]) * (point[1] - start[1]) - (
        end[1] - start[1]
    ) * (point[0] - start[0])


def _opencv_modules() -> tuple[Any, Any]:
    try:
        import cv2
        import numpy as np
    except Exception as exc:  # pragma: no cover - cv-runtime declares both dependencies
        raise RuntimeError(
            "Stem process calibration requires OpenCV and NumPy."
        ) from exc
    return cv2, np
