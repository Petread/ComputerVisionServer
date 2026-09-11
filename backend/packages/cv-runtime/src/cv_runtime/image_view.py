from __future__ import annotations

# OpenCV and NumPy are optional runtime dependencies loaded lazily; their module and ndarray
# types intentionally remain dynamic at this package boundary.
# ruff: noqa: ANN401
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from cv_contracts.runtime import (
    RuntimeImageViewProfile,
    RuntimeImageViewProfileCalculateRequest,
    RuntimeImageViewProfileProvenance,
)
from cv_utils import utc_now_iso


@dataclass(frozen=True)
class ImageViewProfileComputation:
    status: Literal["valid", "invalid"]
    profile: RuntimeImageViewProfile | None
    failure_code: str | None = None
    failure_message: str | None = None


class ImageViewValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class _PreparedImageView:
    matrix: Any
    polygon_mask: Any | None


def calculate_image_view_profile(
    *,
    source_channel_id: str,
    request: RuntimeImageViewProfileCalculateRequest,
    operator_user_id: str | None = None,
    generated_at: str | None = None,
) -> ImageViewProfileComputation:
    cv2, np = _opencv_modules()
    try:
        source = _source_pixel_points(request, np=np)
        _validate_quadrilateral(source, request=request, cv2=cv2, np=np)
        if request.transform_type == "POLYGON_MASKED_CROP":
            source_to_view = _perspective_preserving_crop_homography(
                source,
                output_width=request.output_width,
                output_height=request.output_height,
                np=np,
            )
        else:
            target = _output_corner_points(request.output_width, request.output_height, np=np)
            source_to_view = cv2.getPerspectiveTransform(
                source.astype(np.float32), target.astype(np.float32)
            ).astype(np.float64)
        source_to_view = _normalized_homography(source_to_view, np=np)
        view_to_source = _normalized_homography(np.linalg.inv(source_to_view), np=np)
        _validate_homography_pair(source_to_view, view_to_source, np=np)
    except ImageViewValidationError as exc:
        return ImageViewProfileComputation(
            status="invalid",
            profile=None,
            failure_code=exc.code,
            failure_message=exc.message,
        )
    except (ArithmeticError, ValueError) as exc:
        return ImageViewProfileComputation(
            status="invalid",
            profile=None,
            failure_code="image_view_homography_invalid",
            failure_message=str(exc),
        )

    observed_at = generated_at or utc_now_iso()
    profile = RuntimeImageViewProfile(
        view_profile_id=request.view_profile_id,
        status="valid",
        source_channel_id=source_channel_id,
        source_image_width=request.source_image_width,
        source_image_height=request.source_image_height,
        intrinsic_calibration_hash=request.intrinsic_calibration_hash,
        source_quadrilateral=request.source_quadrilateral,
        output_width=request.output_width,
        output_height=request.output_height,
        transform_type=request.transform_type,
        interpolation=request.interpolation,
        padding_mode=request.padding_mode,
        padding_value=request.padding_value,
        enabled=request.enabled,
        source_to_view_homography=[float(item) for item in source_to_view.reshape(-1)],
        view_to_source_homography=[float(item) for item in view_to_source.reshape(-1)],
        profile_checksum="0" * 64,
        validation_status="VALID",
        validated_at=observed_at,
        provenance=RuntimeImageViewProfileProvenance(
            created_at=observed_at,
            operator_user_id=operator_user_id,
            source_frame_timestamp=request.source_frame_timestamp,
            source_frame_sha256=request.source_frame_sha256,
            pipeline_version_id=request.pipeline_version_id,
            notes=request.notes,
        ),
        metadata=request.metadata,
    )
    profile = profile.model_copy(update={"profile_checksum": image_view_profile_checksum(profile)})
    validate_image_view_profile(profile)
    return ImageViewProfileComputation(status="valid", profile=profile)


def image_view_profile_checksum(profile: RuntimeImageViewProfile | dict[str, Any]) -> str:
    value = (
        profile.model_dump(mode="json")
        if isinstance(profile, RuntimeImageViewProfile)
        else dict(profile)
    )
    compatibility = {
        key: value.get(key)
        for key in (
            "schema_version",
            "view_profile_id",
            "source_space",
            "source_channel_id",
            "source_image_width",
            "source_image_height",
            "intrinsic_calibration_hash",
            "source_quadrilateral",
            "output_width",
            "output_height",
            "transform_type",
            "interpolation",
            "padding_mode",
            "padding_value",
            "enabled",
            "source_to_view_homography",
            "view_to_source_homography",
        )
    }
    canonical = json.dumps(
        compatibility,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_image_view_profile(
    value: object,
    *,
    require_enabled: bool = True,
) -> RuntimeImageViewProfile:
    cv2, np = _opencv_modules()
    profile = RuntimeImageViewProfile.model_validate(value)
    if profile.status != "valid" or profile.validation_status != "VALID":
        raise ImageViewValidationError(
            "image_view_profile_invalid", "Image view profile is not valid."
        )
    if require_enabled and not profile.enabled:
        raise ImageViewValidationError(
            "image_view_profile_disabled", "Image view profile is disabled."
        )
    request = RuntimeImageViewProfileCalculateRequest(
        view_profile_id=profile.view_profile_id,
        source_image_width=profile.source_image_width,
        source_image_height=profile.source_image_height,
        intrinsic_calibration_hash=profile.intrinsic_calibration_hash,
        source_quadrilateral=profile.source_quadrilateral,
        output_width=profile.output_width,
        output_height=profile.output_height,
        transform_type=profile.transform_type,
        interpolation=profile.interpolation,
        padding_mode=profile.padding_mode,
        padding_value=profile.padding_value,
        enabled=profile.enabled,
    )
    source = _source_pixel_points(request, np=np)
    _validate_quadrilateral(source, request=request, cv2=cv2, np=np)
    source_to_view = np.asarray(profile.source_to_view_homography, dtype=np.float64).reshape(3, 3)
    view_to_source = np.asarray(profile.view_to_source_homography, dtype=np.float64).reshape(3, 3)
    _validate_homography_pair(source_to_view, view_to_source, np=np)
    expected = (
        _perspective_preserving_crop_homography(
            source,
            output_width=profile.output_width,
            output_height=profile.output_height,
            np=np,
        )
        if profile.transform_type == "POLYGON_MASKED_CROP"
        else cv2.getPerspectiveTransform(
            source.astype(np.float32),
            _output_corner_points(profile.output_width, profile.output_height, np=np).astype(
                np.float32
            ),
        ).astype(np.float64)
    )
    expected = _normalized_homography(expected, np=np)
    if not np.allclose(source_to_view, expected, rtol=1e-7, atol=1e-7):
        raise ImageViewValidationError(
            "image_view_homography_geometry_mismatch",
            "Stored image view homography does not match the selected transformation.",
        )
    if image_view_profile_checksum(profile) != profile.profile_checksum:
        raise ImageViewValidationError(
            "image_view_profile_checksum_mismatch",
            "Image view profile checksum does not match the preprocessing geometry.",
        )
    return profile


def validate_image_view_frame(
    profile: RuntimeImageViewProfile,
    *,
    image_width: int,
    image_height: int,
    intrinsic_calibration_hash: str | None,
    source_channel_id: str | None,
) -> None:
    if (image_width, image_height) != (
        profile.source_image_width,
        profile.source_image_height,
    ):
        raise ImageViewValidationError(
            "image_view_source_resolution_mismatch",
            "Source frame resolution does not match the image view profile.",
        )
    if intrinsic_calibration_hash != profile.intrinsic_calibration_hash:
        raise ImageViewValidationError(
            "image_view_intrinsic_calibration_mismatch",
            "Undistort calibration does not match the image view profile.",
        )
    if source_channel_id is not None and source_channel_id != profile.source_channel_id:
        raise ImageViewValidationError(
            "image_view_channel_mismatch",
            "Image view profile belongs to a different runtime channel.",
        )


def apply_image_view(image: Any, profile: RuntimeImageViewProfile, *, mask: bool = False) -> Any:
    cv2, np = _opencv_modules()
    array = np.asarray(image)
    if array.ndim not in {2, 3}:
        raise ImageViewValidationError(
            "image_view_source_image_invalid", "Image view input must be a 2D or 3D image."
        )
    height, width = int(array.shape[0]), int(array.shape[1])
    if (width, height) != (profile.source_image_width, profile.source_image_height):
        raise ImageViewValidationError(
            "image_view_source_resolution_mismatch",
            "Source image resolution does not match the image view profile.",
        )
    interpolation = cv2.INTER_NEAREST if mask else _interpolation(profile.interpolation, cv2=cv2)
    prepared = _prepared_image_view(
        profile.profile_checksum,
        profile.source_image_width,
        profile.source_image_height,
        profile.output_width,
        profile.output_height,
        profile.transform_type,
        tuple(float(value) for value in profile.source_to_view_homography),
        tuple((float(point.x), float(point.y)) for point in profile.source_quadrilateral),
    )
    warp_arguments = {
        "flags": interpolation,
        "borderMode": cv2.BORDER_CONSTANT,
        "borderValue": 0 if mask else profile.padding_value,
    }
    transformed = cv2.warpPerspective(
        array,
        prepared.matrix,
        (profile.output_width, profile.output_height),
        **warp_arguments,
    )
    if prepared.polygon_mask is not None:
        padding_value = 0 if mask else profile.padding_value
        if padding_value == 0:
            transformed = cv2.bitwise_and(
                transformed,
                transformed,
                mask=prepared.polygon_mask,
            )
        else:
            masked = np.full_like(transformed, padding_value)
            cv2.copyTo(transformed, prepared.polygon_mask, masked)
            transformed = masked
    return transformed


@lru_cache(maxsize=128)
def _prepared_image_view(
    profile_checksum: str,
    source_width: int,
    source_height: int,
    output_width: int,
    output_height: int,
    transform_type: str,
    matrix_values: tuple[float, ...],
    source_quadrilateral: tuple[tuple[float, float], ...],
) -> _PreparedImageView:
    """Prepare immutable matrices and masks once for a versioned image view profile."""

    del profile_checksum  # The checksum intentionally participates in the cache key.
    cv2, np = _opencv_modules()
    matrix = np.asarray(matrix_values, dtype=np.float64).reshape(3, 3)
    matrix.setflags(write=False)
    polygon_mask = None
    if transform_type == "POLYGON_MASKED_CROP":
        source_polygon = np.asarray(
            [
                [x * max(source_width - 1, 1), y * max(source_height - 1, 1)]
                for x, y in source_quadrilateral
            ],
            dtype=np.float64,
        )
        view_polygon = transform_points(source_polygon, matrix)
        polygon_mask = np.zeros((output_height, output_width), dtype=np.uint8)
        cv2.fillPoly(polygon_mask, [np.rint(view_polygon).astype(np.int32)], 255)
        polygon_mask.setflags(write=False)
    return _PreparedImageView(
        matrix=matrix,
        polygon_mask=polygon_mask,
    )


def restore_view_mask(mask: Any, profile: RuntimeImageViewProfile) -> Any:
    cv2, np = _opencv_modules()
    array = np.asarray(mask)
    if array.ndim != 2 or array.shape != (profile.output_height, profile.output_width):
        raise ImageViewValidationError(
            "image_view_mask_resolution_mismatch",
            "View mask resolution does not match the image view profile.",
        )
    matrix = np.asarray(profile.view_to_source_homography, dtype=np.float64).reshape(3, 3)
    return np.ascontiguousarray(
        cv2.warpPerspective(
            array,
            matrix,
            (profile.source_image_width, profile.source_image_height),
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
    )


def transform_points(points: Sequence[Sequence[float]] | Any, homography: Any) -> Any:
    cv2, np = _opencv_modules()
    array = np.asarray(points, dtype=np.float64).reshape(-1, 1, 2)
    projected = cv2.perspectiveTransform(
        array,
        np.asarray(homography, dtype=np.float64).reshape(3, 3),
    ).reshape(-1, 2)
    if not np.isfinite(projected).all():
        raise ImageViewValidationError(
            "image_view_projection_non_finite", "Image view projection is non-finite."
        )
    return projected


def source_points_to_view(
    points: Sequence[Sequence[float]], profile: RuntimeImageViewProfile
) -> Any:
    return transform_points(points, profile.source_to_view_homography)


def view_points_to_source(
    points: Sequence[Sequence[float]], profile: RuntimeImageViewProfile
) -> Any:
    return transform_points(points, profile.view_to_source_homography)


def view_overlay_geometry_to_source(
    geometry: Mapping[str, Any],
    profile: RuntimeImageViewProfile,
) -> dict[str, Any]:
    """Project vector-overlay geometry from canonical view to undistorted full image.

    Mask pixels use :func:`restore_view_mask`; this helper intentionally handles vector
    primitives and returns normalized source coordinates so preview renderers stay resolution
    independent.
    """

    coordinate_system = str(geometry.get("coordinate_system") or "normalized_xy")

    def source_point(value: object) -> dict[str, float]:
        if not isinstance(value, Mapping):
            raise ImageViewValidationError(
                "image_view_overlay_geometry_invalid",
                "Overlay point must be an object containing x and y.",
            )
        try:
            x = float(value.get("x"))
            y = float(value.get("y"))
        except (TypeError, ValueError) as exc:
            raise ImageViewValidationError(
                "image_view_overlay_geometry_invalid",
                "Overlay point coordinates must be numeric.",
            ) from exc
        if coordinate_system in {
            "normalized",
            "normalized_xy",
            "normalized_xyxy",
            "normalized_polygon",
        }:
            x *= max(profile.output_width - 1, 1)
            y *= max(profile.output_height - 1, 1)
        elif coordinate_system not in {"image_pixels", "pixel", "pixels", "pixel_xy"}:
            raise ImageViewValidationError(
                "image_view_overlay_coordinate_system_unsupported",
                f"Unsupported overlay coordinate system: {coordinate_system}",
            )
        projected = view_points_to_source([(x, y)], profile)[0]
        return {
            "x": float(projected[0]) / max(profile.source_image_width - 1, 1),
            "y": float(projected[1]) / max(profile.source_image_height - 1, 1),
        }

    projected = dict(geometry)
    if all(key in geometry for key in ("x_min", "y_min", "x_max", "y_max")):
        try:
            x_min = float(geometry["x_min"])
            y_min = float(geometry["y_min"])
            x_max = float(geometry["x_max"])
            y_max = float(geometry["y_max"])
        except (TypeError, ValueError) as exc:
            raise ImageViewValidationError(
                "image_view_overlay_geometry_invalid",
                "Overlay box boundaries must be numeric.",
            ) from exc
        projected["points"] = [
            source_point({"x": x_min, "y": y_min}),
            source_point({"x": x_max, "y": y_min}),
            source_point({"x": x_max, "y": y_max}),
            source_point({"x": x_min, "y": y_max}),
        ]
        for key in ("x_min", "y_min", "x_max", "y_max"):
            projected.pop(key, None)
    if all(key in geometry for key in ("x", "y", "width", "height")):
        try:
            x = float(geometry["x"])
            y = float(geometry["y"])
            width = float(geometry["width"])
            height = float(geometry["height"])
        except (TypeError, ValueError) as exc:
            raise ImageViewValidationError(
                "image_view_overlay_geometry_invalid",
                "Overlay box coordinates must be numeric.",
            ) from exc
        projected["points"] = [
            source_point({"x": x, "y": y}),
            source_point({"x": x + width, "y": y}),
            source_point({"x": x + width, "y": y + height}),
            source_point({"x": x, "y": y + height}),
        ]
        for key in ("x", "y", "width", "height"):
            projected.pop(key, None)
    if isinstance(geometry.get("points"), list):
        projected["points"] = [source_point(item) for item in geometry["points"]]
    for key in ("start", "end", "origin", "tip"):
        if key in geometry:
            projected[key] = source_point(geometry[key])
    if "x" in geometry and "y" in geometry:
        projected.update(source_point({"x": geometry["x"], "y": geometry["y"]}))
    projected["coordinate_system"] = "normalized_xy"
    projected["projected_from_view_profile_id"] = profile.view_profile_id
    return projected


def _source_pixel_points(request: RuntimeImageViewProfileCalculateRequest, *, np: Any) -> Any:
    return np.asarray(
        [
            [
                point.x * max(request.source_image_width - 1, 1),
                point.y * max(request.source_image_height - 1, 1),
            ]
            for point in request.source_quadrilateral
        ],
        dtype=np.float64,
    )


def _validate_quadrilateral(
    points: Any,
    *,
    request: RuntimeImageViewProfileCalculateRequest,
    cv2: Any,
    np: Any,
) -> None:
    if not np.isfinite(points).all() or len(np.unique(points, axis=0)) != 4:
        raise ImageViewValidationError(
            "image_view_quadrilateral_degenerate",
            "Source quadrilateral must contain four unique finite points.",
        )
    if _segments_intersect(points[0], points[1], points[2], points[3]) or _segments_intersect(
        points[1], points[2], points[3], points[0]
    ):
        raise ImageViewValidationError(
            "image_view_quadrilateral_self_intersecting",
            "Source quadrilateral must not self-intersect.",
        )
    contour = points.astype(np.float32).reshape(-1, 1, 2)
    if not bool(cv2.isContourConvex(contour)):
        raise ImageViewValidationError(
            "image_view_quadrilateral_degenerate",
            "Source quadrilateral must be convex and ordered around its boundary.",
        )
    normalized_area = abs(float(cv2.contourArea(contour))) / float(
        max((request.source_image_width - 1) * (request.source_image_height - 1), 1)
    )
    if normalized_area < 0.005:
        raise ImageViewValidationError(
            "image_view_quadrilateral_area_too_small",
            "Source quadrilateral covers less than the supported minimum image area.",
        )


def _segments_intersect(a: Any, b: Any, c: Any, d: Any) -> bool:
    def orientation(p: Any, q: Any, r: Any) -> float:
        return float((q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]))

    return (
        orientation(a, b, c) * orientation(a, b, d) <= 0
        and orientation(c, d, a) * orientation(c, d, b) <= 0
    )


def _validate_homography_pair(forward: Any, inverse: Any, *, np: Any) -> None:
    values = np.concatenate((forward.reshape(-1), inverse.reshape(-1)))
    if not np.isfinite(values).all() or abs(float(np.linalg.det(forward))) <= 1e-12:
        raise ImageViewValidationError(
            "image_view_homography_invalid", "Image view homography is degenerate."
        )
    if float(np.linalg.cond(forward)) > 1e12:
        raise ImageViewValidationError(
            "image_view_homography_invalid", "Image view homography is numerically unstable."
        )
    identity = forward @ inverse
    identity = identity / identity[2, 2]
    if not np.allclose(identity, np.eye(3), rtol=1e-7, atol=1e-7):
        raise ImageViewValidationError(
            "image_view_homography_invalid", "Image view inverse homography is inconsistent."
        )


def _normalized_homography(value: Any, *, np: Any) -> Any:
    matrix = np.asarray(value, dtype=np.float64).reshape(3, 3)
    scale = float(matrix[2, 2])
    if not math.isfinite(scale) or abs(scale) <= 1e-15:
        raise ImageViewValidationError(
            "image_view_homography_invalid", "Image view homography cannot be normalized."
        )
    return matrix / scale


def _output_corner_points(width: int, height: int, *, np: Any) -> Any:
    return np.asarray(
        [
            [0.0, 0.0],
            [float(width - 1), 0.0],
            [float(width - 1), float(height - 1)],
            [0.0, float(height - 1)],
        ],
        dtype=np.float64,
    )


def _perspective_preserving_crop_homography(
    source: Any,
    *,
    output_width: int,
    output_height: int,
    np: Any,
) -> Any:
    """Fit the polygon bounding box with uniform scale and no projective deformation."""
    x_min = float(np.min(source[:, 0]))
    x_max = float(np.max(source[:, 0]))
    y_min = float(np.min(source[:, 1]))
    y_max = float(np.max(source[:, 1]))
    span_x = x_max - x_min
    span_y = y_max - y_min
    if span_x <= 1e-9 or span_y <= 1e-9:
        raise ImageViewValidationError(
            "image_view_quadrilateral_degenerate",
            "Source quadrilateral bounding box is degenerate.",
        )
    scale = min(
        float(output_width - 1) / span_x,
        float(output_height - 1) / span_y,
    )
    pad_x = (float(output_width - 1) - scale * span_x) / 2.0
    pad_y = (float(output_height - 1) - scale * span_y) / 2.0
    return np.asarray(
        [
            [scale, 0.0, pad_x - scale * x_min],
            [0.0, scale, pad_y - scale * y_min],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )


def _interpolation(value: str, *, cv2: Any) -> int:
    return {
        "NEAREST": cv2.INTER_NEAREST,
        "LINEAR": cv2.INTER_LINEAR,
        "CUBIC": cv2.INTER_CUBIC,
    }[value]


def _opencv_modules() -> tuple[Any, Any]:
    try:
        import cv2
        import numpy as np
    except Exception as exc:  # pragma: no cover - declared runtime dependencies
        raise RuntimeError("Image view rectification requires OpenCV and NumPy.") from exc
    return cv2, np
