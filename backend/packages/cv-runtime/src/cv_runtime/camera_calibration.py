# ruff: noqa: ANN401 -- OpenCV exposes version-dependent, dynamically typed APIs.
from __future__ import annotations

import base64
import hashlib
import json
import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from cv_contracts.runtime import (
    RuntimeCameraCalibrationBoardDefinition,
    RuntimeCameraCalibrationObservation,
    RuntimeCameraCalibrationObservationResult,
    RuntimeCameraCalibrationRoi,
    RuntimeCameraIntrinsicCalibrationProfile,
    RuntimeLiveFrame,
)
from cv_utils import utc_now_iso


@dataclass(frozen=True)
class CameraCalibrationComputation:
    status: Literal["valid", "invalid"]
    profile: RuntimeCameraIntrinsicCalibrationProfile | None
    observations: list[RuntimeCameraCalibrationObservationResult]
    rms_reprojection_error_px: float | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    preview_before: RuntimeLiveFrame | None = None
    preview_after: RuntimeLiveFrame | None = None


@dataclass(frozen=True)
class CameraUndistortRemap:
    """Reusable OpenCV maps for one validated intrinsic-calibration profile."""

    map_x: Any
    map_y: Any


@dataclass(frozen=True)
class CameraDistortRemap:
    """Reusable map that places an undistorted image back into raw camera space."""

    map_x: Any
    map_y: Any


@dataclass(frozen=True)
class _DetectedObservation:
    observation: RuntimeCameraCalibrationObservation
    image: Any
    image_points: Any
    object_points: Any | None = None
    charuco_ids: Any | None = None


def create_identity_camera_calibration(
    *,
    source_channel_id: str,
    image_width: int,
    image_height: int,
    board_definition: RuntimeCameraCalibrationBoardDefinition,
    generated_at: str | None = None,
) -> CameraCalibrationComputation:
    """Create an explicit, versioned no-lens-correction profile.

    This is intentionally distinguishable from a measured intrinsic calibration. It lets
    authoring and runtime continue with unchanged pixels until a calibration board is
    available, without silently claiming that lens distortion was measured.
    """
    width = int(image_width)
    height = int(image_height)
    if width < 1 or height < 1:
        raise ValueError("identity passthrough requires a positive image resolution")
    focal_length = float(max(width, height))
    principal_x = (float(width) - 1.0) / 2.0
    principal_y = (float(height) - 1.0) / 2.0
    camera_matrix = [
        focal_length,
        0.0,
        principal_x,
        0.0,
        focal_length,
        principal_y,
        0.0,
        0.0,
        1.0,
    ]
    profile = RuntimeCameraIntrinsicCalibrationProfile(
        status="valid",
        source_channel_id=source_channel_id,
        image_width=width,
        image_height=height,
        calibration_method="IDENTITY_PASSTHROUGH",
        camera_matrix=camera_matrix,
        output_camera_matrix=list(camera_matrix),
        distortion_coefficients=[0.0] * 5,
        rms_reprojection_error_px=0.0,
        per_observation_reprojection_error_px={},
        accepted_observation_count=0,
        board_definition=board_definition,
        valid_roi=RuntimeCameraCalibrationRoi(
            x=0,
            y=0,
            width=width,
            height=height,
        ),
        generated_at=generated_at or utc_now_iso(),
        calibration_hash="0" * 64,
        perspective_rectified=False,
    )
    profile = profile.model_copy(
        update={"calibration_hash": camera_calibration_profile_hash(profile)}
    )
    return CameraCalibrationComputation(
        status="valid",
        profile=profile,
        observations=[],
        rms_reprojection_error_px=None,
    )


def calculate_camera_calibration(
    *,
    source_channel_id: str,
    board_definition: RuntimeCameraCalibrationBoardDefinition,
    observations: list[RuntimeCameraCalibrationObservation],
    minimum_observation_count: int,
    maximum_rms_reprojection_error_px: float,
    preview_observation_id: str | None = None,
    generated_at: str | None = None,
) -> CameraCalibrationComputation:
    cv2, np = _opencv_modules()
    results: list[RuntimeCameraCalibrationObservationResult] = []
    detected: list[_DetectedObservation] = []
    image_size: tuple[int, int] | None = None

    for observation in observations:
        try:
            image = _decode_observation_frame(observation, cv2=cv2, np=np)
        except ValueError as exc:
            results.append(
                RuntimeCameraCalibrationObservationResult(
                    observation_id=observation.observation_id,
                    accepted=False,
                    reason_code="image_decode_failed",
                    message=str(exc),
                )
            )
            continue
        current_size = (int(image.shape[1]), int(image.shape[0]))
        if image_size is None:
            image_size = current_size
        elif current_size != image_size:
            results.append(
                RuntimeCameraCalibrationObservationResult(
                    observation_id=observation.observation_id,
                    accepted=False,
                    reason_code="calibration_resolution_mismatch",
                    message=(
                        f"Observation resolution {current_size[0]}x{current_size[1]} does not "
                        f"match {image_size[0]}x{image_size[1]}."
                    ),
                )
            )
            continue

        try:
            detection = _detect_board(
                observation,
                image,
                board_definition=board_definition,
                cv2=cv2,
                np=np,
            )
        except ValueError as exc:
            return CameraCalibrationComputation(
                status="invalid",
                profile=None,
                observations=results,
                failure_code="calibration_method_unavailable",
                failure_message=str(exc),
            )
        if detection is None:
            results.append(
                RuntimeCameraCalibrationObservationResult(
                    observation_id=observation.observation_id,
                    accepted=False,
                    reason_code="calibration_board_not_detected",
                    message="The configured calibration board was not detected reliably.",
                )
            )
            continue
        detected.append(detection)
        results.append(
            RuntimeCameraCalibrationObservationResult(
                observation_id=observation.observation_id,
                accepted=True,
                detected_point_count=int(len(detection.image_points)),
            )
        )

    if image_size is None:
        return CameraCalibrationComputation(
            status="invalid",
            profile=None,
            observations=results,
            failure_code="calibration_images_invalid",
            failure_message="None of the supplied calibration images could be decoded.",
        )
    if len(detected) < minimum_observation_count:
        return CameraCalibrationComputation(
            status="invalid",
            profile=None,
            observations=results,
            failure_code="calibration_observations_insufficient",
            failure_message=(
                f"Detected the board in {len(detected)} observations; "
                f"at least {minimum_observation_count} are required."
            ),
        )

    try:
        rms, camera_matrix, distortion_coefficients, rotation_vectors, translation_vectors = (
            _calibrate_detected_observations(
                detected,
                image_size=image_size,
                cv2=cv2,
            )
        )
    except Exception as exc:
        return CameraCalibrationComputation(
            status="invalid",
            profile=None,
            observations=results,
            failure_code="camera_calibration_failed",
            failure_message=f"OpenCV could not calculate a camera calibration: {exc}",
        )

    if not _calibration_numbers_valid(camera_matrix, distortion_coefficients, rms, np=np):
        return CameraCalibrationComputation(
            status="invalid",
            profile=None,
            observations=results,
            failure_code="camera_calibration_non_finite",
            failure_message="OpenCV returned invalid or non-finite intrinsic parameters.",
        )

    output_camera_matrix, roi = cv2.getOptimalNewCameraMatrix(
        camera_matrix,
        distortion_coefficients,
        image_size,
        0.0,
        image_size,
    )
    per_observation_errors = _per_observation_reprojection_errors(
        detected,
        rotation_vectors=rotation_vectors,
        translation_vectors=translation_vectors,
        camera_matrix=camera_matrix,
        distortion_coefficients=distortion_coefficients,
        cv2=cv2,
        np=np,
    )
    error_by_id = {
        item.observation.observation_id: float(error)
        for item, error in zip(detected, per_observation_errors, strict=True)
    }
    results = [
        item.model_copy(update={"reprojection_error_px": error_by_id.get(item.observation_id)})
        if item.accepted
        else item
        for item in results
    ]

    profile_status = "valid" if float(rms) <= maximum_rms_reprojection_error_px else "invalid"
    roi_x, roi_y, roi_width, roi_height = (int(value) for value in roi)
    profile = RuntimeCameraIntrinsicCalibrationProfile(
        status=profile_status,
        source_channel_id=source_channel_id,
        image_width=image_size[0],
        image_height=image_size[1],
        calibration_method="CHECKERBOARD_OR_CHARUCO",
        camera_matrix=[float(value) for value in camera_matrix.reshape(-1)],
        output_camera_matrix=[float(value) for value in output_camera_matrix.reshape(-1)],
        distortion_coefficients=[float(value) for value in distortion_coefficients.reshape(-1)],
        rms_reprojection_error_px=float(rms),
        per_observation_reprojection_error_px=error_by_id,
        accepted_observation_count=len(detected),
        board_definition=board_definition,
        valid_roi=RuntimeCameraCalibrationRoi(
            x=max(roi_x, 0),
            y=max(roi_y, 0),
            width=max(roi_width, 0),
            height=max(roi_height, 0),
        ),
        generated_at=generated_at or utc_now_iso(),
        calibration_hash="0" * 64,
        perspective_rectified=False,
    )
    profile = profile.model_copy(
        update={"calibration_hash": camera_calibration_profile_hash(profile)}
    )

    preview = next(
        (item for item in detected if item.observation.observation_id == preview_observation_id),
        detected[0],
    )
    preview_after = _undistorted_preview(
        preview,
        camera_matrix=camera_matrix,
        distortion_coefficients=distortion_coefficients,
        output_camera_matrix=output_camera_matrix,
        calibration_hash=profile.calibration_hash,
        cv2=cv2,
    )
    if profile_status == "invalid":
        failure_code = "calibration_reprojection_error_exceeded"
        failure_message = (
            f"RMS reprojection error {float(rms):.4f}px exceeds the configured "
            f"limit {maximum_rms_reprojection_error_px:.4f}px."
        )
    else:
        failure_code = None
        failure_message = None
    return CameraCalibrationComputation(
        status=profile_status,
        profile=profile,
        observations=results,
        rms_reprojection_error_px=float(rms),
        failure_code=failure_code,
        failure_message=failure_message,
        preview_before=preview.observation.frame,
        preview_after=preview_after,
    )


def camera_calibration_profile_hash(
    profile: RuntimeCameraIntrinsicCalibrationProfile | dict[str, Any],
) -> str:
    payload = (
        profile.model_dump(mode="json")
        if isinstance(profile, RuntimeCameraIntrinsicCalibrationProfile)
        else dict(profile)
    )
    payload.pop("calibration_hash", None)
    # Keep hashes of pre-existing measured profiles stable. The method discriminator was
    # added later and its default describes exactly the legacy behavior.
    if payload.get("calibration_method") == "CHECKERBOARD_OR_CHARUCO":
        payload.pop("calibration_method", None)
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_camera_intrinsic_calibration(
    value: object,
    *,
    require_valid: bool = True,
) -> RuntimeCameraIntrinsicCalibrationProfile:
    profile = RuntimeCameraIntrinsicCalibrationProfile.model_validate(value)
    if require_valid and profile.status != "valid":
        raise ValueError("camera calibration profile is not valid")
    numeric_values = [
        *profile.camera_matrix,
        *profile.output_camera_matrix,
        *profile.distortion_coefficients,
        profile.rms_reprojection_error_px,
    ]
    if not all(math.isfinite(value) for value in numeric_values):
        raise ValueError("camera calibration profile contains non-finite values")
    if profile.camera_matrix[0] <= 0 or profile.camera_matrix[4] <= 0:
        raise ValueError("camera calibration focal lengths must be positive")
    if profile.output_camera_matrix[0] <= 0 or profile.output_camera_matrix[4] <= 0:
        raise ValueError("camera calibration output focal lengths must be positive")
    if len(profile.distortion_coefficients) not in {4, 5, 8, 12, 14}:
        raise ValueError("camera calibration distortion vector has an unsupported length")
    if profile.calibration_method == "IDENTITY_PASSTHROUGH":
        if profile.accepted_observation_count != 0:
            raise ValueError("identity passthrough must not contain accepted observations")
        if profile.per_observation_reprojection_error_px:
            raise ValueError("identity passthrough must not contain reprojection observations")
        if any(value != 0 for value in profile.distortion_coefficients):
            raise ValueError("identity passthrough distortion coefficients must be zero")
        if profile.valid_roi.model_dump() != {
            "x": 0,
            "y": 0,
            "width": profile.image_width,
            "height": profile.image_height,
        }:
            raise ValueError("identity passthrough ROI must cover the complete image")
    if profile.valid_roi.x + profile.valid_roi.width > profile.image_width:
        raise ValueError("camera calibration ROI exceeds image width")
    if profile.valid_roi.y + profile.valid_roi.height > profile.image_height:
        raise ValueError("camera calibration ROI exceeds image height")
    if camera_calibration_profile_hash(profile) != profile.calibration_hash:
        raise ValueError("camera calibration hash does not match the profile")
    return profile


def create_camera_undistort_remap(
    profile: RuntimeCameraIntrinsicCalibrationProfile,
) -> CameraUndistortRemap | None:
    """Build the same intrinsic remap used by the runtime node.

    Identity-passthrough profiles deliberately return ``None`` so callers can keep
    the original pixels without an unnecessary OpenCV interpolation pass.
    """

    if profile.calibration_method == "IDENTITY_PASSTHROUGH":
        return None
    cv2, np = _opencv_modules()
    map_x, map_y = cv2.initUndistortRectifyMap(
        np.asarray(profile.camera_matrix, dtype=np.float64).reshape(3, 3),
        np.asarray(profile.distortion_coefficients, dtype=np.float64),
        None,
        np.asarray(profile.output_camera_matrix, dtype=np.float64).reshape(3, 3),
        (profile.image_width, profile.image_height),
        cv2.CV_32FC1,
    )
    return CameraUndistortRemap(map_x=map_x, map_y=map_y)


def apply_camera_undistort(
    image: Any,
    profile: RuntimeCameraIntrinsicCalibrationProfile,
    *,
    interpolation: str = "linear",
    remap: CameraUndistortRemap | Any | None = None,
) -> Any:
    """Apply versioned intrinsic preprocessing to a decoded image array.

    Runtime nodes and continuous image-view publication share this array-level
    implementation, preventing preview streams from acquiring a subtly different
    lens correction than model inference and dataset capture.
    """

    cv2, np = _opencv_modules()
    array = np.asarray(image)
    if array.ndim not in {2, 3}:
        raise ValueError("camera undistort input must be a 2D or 3D image")
    height, width = int(array.shape[0]), int(array.shape[1])
    if (width, height) != (profile.image_width, profile.image_height):
        raise ValueError(
            f"frame resolution {width}x{height} does not match intrinsic calibration "
            f"resolution {profile.image_width}x{profile.image_height}"
        )
    interpolation_flag = {
        "nearest": cv2.INTER_NEAREST,
        "linear": cv2.INTER_LINEAR,
        "cubic": cv2.INTER_CUBIC,
    }.get(str(interpolation).strip().lower())
    if interpolation_flag is None:
        raise ValueError("camera undistort interpolation must be nearest, linear, or cubic")
    if profile.calibration_method == "IDENTITY_PASSTHROUGH":
        return np.ascontiguousarray(array)
    active_remap = remap or create_camera_undistort_remap(profile)
    if active_remap is None:  # Defensive guard for malformed profile implementations.
        raise ValueError("camera undistort remap is unavailable")
    output = cv2.remap(
        array,
        active_remap.map_x,
        active_remap.map_y,
        interpolation_flag,
        borderMode=cv2.BORDER_CONSTANT,
    )
    return np.ascontiguousarray(output)


def create_camera_distort_remap(
    profile: RuntimeCameraIntrinsicCalibrationProfile,
) -> CameraDistortRemap | None:
    """Build a raw-destination map for textures defined in undistorted pixels."""

    if profile.calibration_method == "IDENTITY_PASSTHROUGH":
        return None
    return _cached_camera_distort_remap(
        profile.calibration_hash,
        profile.image_width,
        profile.image_height,
        tuple(float(value) for value in profile.camera_matrix),
        tuple(float(value) for value in profile.output_camera_matrix),
        tuple(float(value) for value in profile.distortion_coefficients),
    )


@lru_cache(maxsize=8)
def _cached_camera_distort_remap(
    calibration_hash: str,
    width: int,
    height: int,
    camera_matrix_values: tuple[float, ...],
    output_camera_matrix_values: tuple[float, ...],
    distortion_values: tuple[float, ...],
) -> CameraDistortRemap:
    del calibration_hash  # The versioned hash intentionally participates in the cache key.
    cv2, np = _opencv_modules()
    raw_x, raw_y = np.meshgrid(
        np.arange(width, dtype=np.float32),
        np.arange(height, dtype=np.float32),
    )
    raw_points = np.stack((raw_x, raw_y), axis=-1).reshape(-1, 1, 2)
    undistorted = cv2.undistortPoints(
        raw_points,
        np.asarray(camera_matrix_values, dtype=np.float64).reshape(3, 3),
        np.asarray(distortion_values, dtype=np.float64),
        P=np.asarray(output_camera_matrix_values, dtype=np.float64).reshape(3, 3),
    ).reshape(height, width, 2)
    map_x = np.ascontiguousarray(undistorted[:, :, 0], dtype=np.float32)
    map_y = np.ascontiguousarray(undistorted[:, :, 1], dtype=np.float32)
    map_x.setflags(write=False)
    map_y.setflags(write=False)
    return CameraDistortRemap(map_x=map_x, map_y=map_y)


def apply_camera_distortion(
    image: Any,
    profile: RuntimeCameraIntrinsicCalibrationProfile,
    *,
    interpolation: str = "nearest",
    remap: CameraDistortRemap | None = None,
) -> Any:
    """Project an undistorted full-frame texture into the raw camera image space."""

    cv2, np = _opencv_modules()
    array = np.asarray(image)
    if array.ndim not in {2, 3}:
        raise ValueError("camera distortion input must be a 2D or 3D image")
    height, width = int(array.shape[0]), int(array.shape[1])
    if (width, height) != (profile.image_width, profile.image_height):
        raise ValueError(
            f"frame resolution {width}x{height} does not match intrinsic calibration "
            f"resolution {profile.image_width}x{profile.image_height}"
        )
    interpolation_flag = {
        "nearest": cv2.INTER_NEAREST,
        "linear": cv2.INTER_LINEAR,
        "cubic": cv2.INTER_CUBIC,
    }.get(str(interpolation).strip().lower())
    if interpolation_flag is None:
        raise ValueError("camera distortion interpolation must be nearest, linear, or cubic")
    if profile.calibration_method == "IDENTITY_PASSTHROUGH":
        return np.ascontiguousarray(array)
    active_remap = remap or create_camera_distort_remap(profile)
    if active_remap is None:
        raise ValueError("camera distortion remap is unavailable")
    return np.ascontiguousarray(
        cv2.remap(
            array,
            active_remap.map_x,
            active_remap.map_y,
            interpolation_flag,
            borderMode=cv2.BORDER_CONSTANT,
        )
    )


def undistorted_points_to_raw(
    points: Any,
    profile: RuntimeCameraIntrinsicCalibrationProfile,
) -> Any:
    """Project undistorted pixel coordinates into distorted raw-camera pixels."""

    cv2, np = _opencv_modules()
    array = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if not len(array):
        return array
    inverse_output = np.linalg.inv(
        np.asarray(profile.output_camera_matrix, dtype=np.float64).reshape(3, 3)
    )
    homogeneous = np.column_stack((array, np.ones(len(array), dtype=np.float64)))
    normalized = (inverse_output @ homogeneous.T).T
    normalized_xy = normalized[:, :2] / normalized[:, 2:3]
    object_points = np.column_stack(
        (normalized_xy, np.ones(len(normalized_xy), dtype=np.float64))
    ).reshape(-1, 1, 3)
    projected, _jacobian = cv2.projectPoints(
        object_points,
        np.zeros(3, dtype=np.float64),
        np.zeros(3, dtype=np.float64),
        np.asarray(profile.camera_matrix, dtype=np.float64).reshape(3, 3),
        np.asarray(profile.distortion_coefficients, dtype=np.float64),
    )
    projected = projected.reshape(-1, 2)
    if not np.isfinite(projected).all():
        raise ValueError("camera distortion projection produced non-finite coordinates")
    return projected


def _opencv_modules() -> tuple[Any, Any]:
    try:
        import cv2
        import numpy as np
    except Exception as exc:  # pragma: no cover - dependency is mandatory for cv-runtime
        raise RuntimeError("Camera calibration requires OpenCV and NumPy.") from exc
    return cv2, np


def _decode_observation_frame(
    observation: RuntimeCameraCalibrationObservation,
    *,
    cv2: Any,
    np: Any,
) -> Any:
    frame = observation.frame
    if not frame.image_base64:
        raise ValueError("Calibration observation contains no image data.")
    try:
        encoded = base64.b64decode(frame.image_base64, validate=True)
    except Exception as exc:
        raise ValueError("Calibration observation contains invalid base64 image data.") from exc
    image = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Calibration observation image could not be decoded.")
    width = int(image.shape[1])
    height = int(image.shape[0])
    if frame.width is not None and int(frame.width) != width:
        raise ValueError("Calibration observation width does not match the encoded image.")
    if frame.height is not None and int(frame.height) != height:
        raise ValueError("Calibration observation height does not match the encoded image.")
    return image


def _detect_board(
    observation: RuntimeCameraCalibrationObservation,
    image: Any,
    *,
    board_definition: RuntimeCameraCalibrationBoardDefinition,
    cv2: Any,
    np: Any,
) -> _DetectedObservation | None:
    if board_definition.board_type == "charuco":
        return _detect_charuco(
            observation,
            image,
            board_definition=board_definition,
            cv2=cv2,
        )
    grayscale = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    pattern_size = (board_definition.columns, board_definition.rows)
    flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
    found, corners = cv2.findChessboardCorners(grayscale, pattern_size, flags)
    if not found or corners is None:
        return None
    criteria = (
        cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
        30,
        0.001,
    )
    corners = cv2.cornerSubPix(grayscale, corners, (11, 11), (-1, -1), criteria)
    object_points = np.zeros(
        (board_definition.rows * board_definition.columns, 3),
        dtype=np.float32,
    )
    object_points[:, :2] = np.mgrid[
        0 : board_definition.columns,
        0 : board_definition.rows,
    ].T.reshape(-1, 2).astype(np.float32) * float(board_definition.square_length)
    return _DetectedObservation(
        observation=observation,
        image=image,
        image_points=corners,
        object_points=object_points,
    )


def _detect_charuco(
    observation: RuntimeCameraCalibrationObservation,
    image: Any,
    *,
    board_definition: RuntimeCameraCalibrationBoardDefinition,
    cv2: Any,
) -> _DetectedObservation | None:
    aruco = getattr(cv2, "aruco", None)
    if aruco is None:
        raise ValueError(
            "This OpenCV build has no aruco module; use Checkerboard or install the "
            "matching OpenCV contrib runtime package."
        )
    dictionary_id = getattr(aruco, board_definition.dictionary_name, None)
    if dictionary_id is None:
        raise ValueError(f"Unknown ChArUco dictionary {board_definition.dictionary_name}.")
    dictionary = aruco.getPredefinedDictionary(dictionary_id)
    board = _charuco_board(board_definition, dictionary=dictionary, aruco=aruco)
    grayscale = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if hasattr(aruco, "CharucoDetector"):
        detector = aruco.CharucoDetector(board)
        charuco_corners, charuco_ids, _marker_corners, _marker_ids = detector.detectBoard(grayscale)
        detected_count = 0 if charuco_ids is None else len(charuco_ids)
    else:
        marker_corners, marker_ids, _rejected = aruco.detectMarkers(grayscale, dictionary)
        if marker_ids is None or len(marker_ids) < 2:
            return None
        detected_count, charuco_corners, charuco_ids = aruco.interpolateCornersCharuco(
            marker_corners,
            marker_ids,
            grayscale,
            board,
        )
    if (
        detected_count is None
        or int(detected_count) < 6
        or charuco_corners is None
        or charuco_ids is None
    ):
        return None
    return _DetectedObservation(
        observation=observation,
        image=image,
        image_points=charuco_corners,
        charuco_ids=charuco_ids,
        object_points=_charuco_object_points(board, charuco_ids),
    )


def _charuco_board(
    definition: RuntimeCameraCalibrationBoardDefinition,
    *,
    dictionary: Any,
    aruco: Any,
) -> Any:
    marker_length = float(definition.marker_length or 0)
    if hasattr(aruco, "CharucoBoard"):
        return aruco.CharucoBoard(
            (definition.columns, definition.rows),
            float(definition.square_length),
            marker_length,
            dictionary,
        )
    if hasattr(aruco, "CharucoBoard_create"):
        return aruco.CharucoBoard_create(
            definition.columns,
            definition.rows,
            float(definition.square_length),
            marker_length,
            dictionary,
        )
    raise ValueError("This OpenCV aruco module does not support ChArUco boards.")


def _charuco_object_points(board: Any, charuco_ids: Any) -> Any:
    points = (
        board.getChessboardCorners()
        if hasattr(board, "getChessboardCorners")
        else board.chessboardCorners
    )
    return points[charuco_ids.reshape(-1)]


def _calibrate_detected_observations(
    detected: list[_DetectedObservation],
    *,
    image_size: tuple[int, int],
    cv2: Any,
) -> tuple[float, Any, Any, list[Any], list[Any]]:
    return cv2.calibrateCamera(
        [item.object_points for item in detected],
        [item.image_points for item in detected],
        image_size,
        None,
        None,
    )


def _per_observation_reprojection_errors(
    detected: list[_DetectedObservation],
    *,
    rotation_vectors: list[Any],
    translation_vectors: list[Any],
    camera_matrix: Any,
    distortion_coefficients: Any,
    cv2: Any,
    np: Any,
) -> list[float]:
    errors: list[float] = []
    for item, rotation, translation in zip(
        detected,
        rotation_vectors,
        translation_vectors,
        strict=True,
    ):
        object_points = item.object_points
        projected, _jacobian = cv2.projectPoints(
            object_points,
            rotation,
            translation,
            camera_matrix,
            distortion_coefficients,
        )
        observed = np.asarray(item.image_points, dtype=np.float64).reshape(-1, 2)
        predicted = np.asarray(projected, dtype=np.float64).reshape(-1, 2)
        errors.append(float(np.sqrt(np.mean(np.sum((observed - predicted) ** 2, axis=1)))))
    return errors


def _calibration_numbers_valid(
    camera_matrix: Any,
    distortion_coefficients: Any,
    rms: float,
    *,
    np: Any,
) -> bool:
    return bool(
        math.isfinite(float(rms))
        and np.isfinite(camera_matrix).all()
        and np.isfinite(distortion_coefficients).all()
        and float(camera_matrix[0, 0]) > 0
        and float(camera_matrix[1, 1]) > 0
    )


def _undistorted_preview(
    detected: _DetectedObservation,
    *,
    camera_matrix: Any,
    distortion_coefficients: Any,
    output_camera_matrix: Any,
    calibration_hash: str,
    cv2: Any,
) -> RuntimeLiveFrame:
    image = cv2.undistort(
        detected.image,
        camera_matrix,
        distortion_coefficients,
        None,
        output_camera_matrix,
    )
    success, encoded = cv2.imencode(".png", image)
    if not success:
        raise ValueError("OpenCV could not encode the undistorted calibration preview.")
    source = detected.observation.frame
    return RuntimeLiveFrame(
        image_base64=base64.b64encode(encoded.tobytes()).decode("ascii"),
        mime_type="image/png",
        width=int(image.shape[1]),
        height=int(image.shape[0]),
        source_timestamp=source.source_timestamp,
        captured_at=source.captured_at,
        sequence=source.sequence,
        image_context={
            "context_id": f"calibration-preview:{detected.observation.observation_id}:undistorted",
            "kind": "processed",
            "width": int(image.shape[1]),
            "height": int(image.shape[0]),
            "source_image_context_id": (source.image_context or {}).get("context_id"),
            "transform": {
                "transform_type": "undistort",
                "coordinate_projection": "not_projectable",
                "parameters": {
                    "calibration_hash": calibration_hash,
                    "perspective_rectified": False,
                },
            },
        },
        metadata={
            **source.metadata,
            "operation": "camera_undistort_preview",
            "calibration_hash": calibration_hash,
            "perspective_rectified": False,
        },
    )
