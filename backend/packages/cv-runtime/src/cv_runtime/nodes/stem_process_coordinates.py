from __future__ import annotations

# OpenCV and NumPy are optional runtime dependencies loaded lazily; their module and ndarray
# types intentionally remain dynamic at this package boundary.
# ruff: noqa: ANN401
import copy
import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from cv_contracts.runtime import RuntimeNodeSpec

from cv_runtime.context import RuntimeExecutionContext
from cv_runtime.errors import RuntimeNodeExecutionError
from cv_runtime.handlers import NodeExecutionResult
from cv_runtime.image_view import (
    ImageViewValidationError,
    apply_image_view,
    validate_image_view_frame,
    validate_image_view_profile,
)
from cv_runtime.payloads import (
    FramePayload,
    ProvenanceLink,
    RuntimeImageContext,
    RuntimeImageTransform,
)
from cv_runtime.stem_process_coordinates import (
    rectified_image_homography,
    validate_stem_curvature_process_calibration,
    validate_stem_curvature_process_frame,
)

NodeInputs = dict[str, object]


@dataclass(frozen=True)
class _ValidatedProfile:
    model: Any
    json_value: dict[str, Any]
    serialized: str


class StemProcessCoordinateRectifyHandler:
    node_type = "preprocessing.process_coordinate_rectify"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        frame = inputs.get("frame")
        if not isinstance(frame, FramePayload):
            raise _node_error(
                node,
                "stem_process_frame_input_missing",
                "Process coordinate rectification requires a frame input.",
            )
        config = node.config or {}
        raw_view_profile = config.get("view_profile")
        view_profile = None
        cached_view_profile = None
        if raw_view_profile is not None:
            try:
                cached_view_profile = _cached_image_view_profile(
                    _canonical_config_json(raw_view_profile)
                )
                view_profile = cached_view_profile.model
            except Exception as exc:
                raise _node_error(
                    node,
                    getattr(exc, "code", "image_view_profile_invalid"),
                    "Rectified view requires a valid versioned image view profile.",
                    metadata={"detail": str(exc)},
                ) from exc
            configured_id = str(config.get("view_profile_id") or "").strip()
            if configured_id and configured_id != view_profile.view_profile_id:
                raise _node_error(
                    node,
                    "image_view_profile_id_mismatch",
                    "Configured view profile ID does not match the embedded profile.",
                )

        profile = None
        cached_process_profile = None
        if config.get("calibration") is not None:
            try:
                cached_process_profile = _cached_process_profile(
                    _canonical_config_json(config.get("calibration"))
                )
                profile = cached_process_profile.model
            except Exception as exc:
                raise _node_error(
                    node,
                    "stem_process_calibration_invalid",
                    "Process-coordinate output requires a valid calibration profile.",
                    metadata={"detail": str(exc)},
                ) from exc
        if view_profile is None and profile is None:
            raise _node_error(
                node,
                "image_view_profile_missing",
                "Rectified view requires an image view profile (or a legacy process calibration).",
            )
        if (
            profile is not None
            and profile.measurement_geometry_mode == "CANONICAL_VIEW_CORRIDOR_V1"
        ):
            if view_profile is None:
                raise _node_error(
                    node,
                    "stem_process_canonical_view_binding_missing",
                    "The process calibration uses the canonical view as its measurement corridor.",
                )
            if (
                profile.canonical_view_profile_id != view_profile.view_profile_id
                or profile.canonical_view_profile_checksum != view_profile.profile_checksum
            ):
                raise _node_error(
                    node,
                    "stem_process_canonical_view_binding_mismatch",
                    "The process calibration was created for a different canonical view profile.",
                    metadata={
                        "expected_view_profile_id": profile.canonical_view_profile_id,
                        "actual_view_profile_id": view_profile.view_profile_id,
                        "expected_view_profile_checksum": (profile.canonical_view_profile_checksum),
                        "actual_view_profile_checksum": view_profile.profile_checksum,
                    },
                )

        source_context = frame.image_context
        intrinsic_hash = _intrinsic_calibration_hash(frame)
        if source_context is None or source_context.transform.transform_type != "undistort":
            raise _node_error(
                node,
                "stem_process_intrinsic_undistort_required",
                (
                    "Process coordinate rectification must directly consume an intrinsically "
                    "undistorted frame."
                ),
                metadata={
                    "input_transform_type": (
                        source_context.transform.transform_type if source_context else None
                    )
                },
            )

        cv2, np = _opencv_modules(node)
        image = _frame_cv_image(frame, node=node, cv2=cv2, np=np)
        height, width = int(image.shape[0]), int(image.shape[1])
        if frame.width is not None and int(frame.width) != width:
            raise _node_error(
                node,
                "stem_process_frame_resolution_metadata_mismatch",
                "Frame width metadata does not match the decoded image.",
            )
        if frame.height is not None and int(frame.height) != height:
            raise _node_error(
                node,
                "stem_process_frame_resolution_metadata_mismatch",
                "Frame height metadata does not match the decoded image.",
            )
        try:
            if view_profile is not None:
                validate_image_view_frame(
                    view_profile,
                    image_width=width,
                    image_height=height,
                    intrinsic_calibration_hash=intrinsic_hash,
                    source_channel_id=context.channel_id,
                )
            if profile is not None:
                validate_stem_curvature_process_frame(
                    profile,
                    image_width=width,
                    image_height=height,
                    intrinsic_calibration_hash=intrinsic_hash,
                    source_channel_id=context.channel_id,
                )
                if (
                    view_profile is not None
                    and view_profile.intrinsic_calibration_hash
                    != profile.intrinsic_calibration_hash
                ):
                    raise ValueError("image view and process calibration intrinsic hashes differ")
        except (ValueError, ImageViewValidationError) as exc:
            code = (
                "stem_process_camera_pose_binding_mismatch"
                if "intrinsic calibration hash" in str(exc)
                else getattr(exc, "code", "stem_process_frame_calibration_mismatch")
            )
            raise _node_error(
                node,
                code,
                "The frame does not match the versioned process calibration.",
                metadata={
                    "detail": str(exc),
                    "frame_width": width,
                    "frame_height": height,
                    "calibration_width": profile.image_width if profile else None,
                    "calibration_height": profile.image_height if profile else None,
                    "frame_intrinsic_calibration_hash": intrinsic_hash,
                    "expected_intrinsic_calibration_hash": (
                        profile.intrinsic_calibration_hash
                        if profile
                        else view_profile.intrinsic_calibration_hash
                    ),
                },
            ) from exc

        matrix_values = (
            view_profile.source_to_view_homography
            if view_profile is not None
            else rectified_image_homography(profile)
        )
        output_width = view_profile.output_width if view_profile is not None else width
        output_height = view_profile.output_height if view_profile is not None else height
        try:
            rectified = (
                apply_image_view(image, view_profile)
                if view_profile is not None
                else cv2.warpPerspective(
                    image,
                    np.asarray(matrix_values, dtype=np.float64).reshape(3, 3),
                    (output_width, output_height),
                    flags=cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_CONSTANT,
                )
            )
        except Exception as exc:
            raise _node_error(
                node,
                "stem_process_rectification_failed",
                "OpenCV could not rectify the frame into process coordinates.",
                metadata={"detail": str(exc)},
            ) from exc

        profile_json = (
            copy.deepcopy(cached_process_profile.json_value)
            if cached_process_profile is not None
            else None
        )
        view_profile_json = (
            copy.deepcopy(cached_view_profile.json_value)
            if cached_view_profile is not None
            else None
        )
        try:
            process_transform = copy.deepcopy(
                _cached_process_transform_metadata(
                    cached_process_profile.serialized if cached_process_profile is not None else "",
                    cached_view_profile.serialized if cached_view_profile is not None else "",
                    tuple(float(value) for value in matrix_values),
                )
            )
        except ValueError as exc:
            raise _node_error(
                node,
                "image_view_process_reference_plane_mismatch",
                (
                    "The canonical image view must describe an axis-aligned rectangle on "
                    "the calibrated X/Y reference plane."
                ),
                metadata={"detail": str(exc)},
            ) from exc
        derivation_hash = (
            view_profile.profile_checksum if view_profile is not None else profile.calibration_hash
        )
        preserves_source_perspective = bool(
            view_profile is not None and view_profile.transform_type == "POLYGON_MASKED_CROP"
        )
        runtime_transform_type = (
            "masked_crop" if preserves_source_perspective else "perspective_rectify"
        )
        perspective_rectified = not preserves_source_perspective
        image_context = RuntimeImageContext(
            context_id=_derived_context_id(
                context,
                node=node,
                parent_context_id=source_context.context_id,
                calibration_hash=derivation_hash,
            ),
            kind="transformed",
            width=output_width,
            height=output_height,
            source_uri=frame.source_uri,
            source_node_key=node.node_key,
            source_image_context_id=(
                source_context.source_image_context_id or source_context.context_id
            ),
            parent_image_context_id=source_context.context_id,
            transform=RuntimeImageTransform(
                transform_type=runtime_transform_type,
                coordinate_projection="homography",
                parameters={
                    "view_profile_id": view_profile.view_profile_id if view_profile else None,
                    "view_profile_checksum": (
                        view_profile.profile_checksum if view_profile else None
                    ),
                    "intrinsic_calibration_hash": (
                        view_profile.intrinsic_calibration_hash
                        if view_profile
                        else profile.intrinsic_calibration_hash
                    ),
                    "source_to_view_homography": matrix_values,
                    "view_to_source_homography": (
                        view_profile.view_to_source_homography
                        if view_profile
                        else _inverse_homography(matrix_values, np=np)
                    ),
                    "output_width": output_width,
                    "output_height": output_height,
                    "image_view_transform_type": (
                        view_profile.transform_type if view_profile else "PERSPECTIVE_RECTIFY"
                    ),
                    "preserves_source_perspective": preserves_source_perspective,
                    **process_transform,
                    "perspective_rectified": perspective_rectified,
                    "resolution_policy": "exact",
                },
            ),
            provenance=[
                ProvenanceLink(
                    source_kind="image_context",
                    source_id=source_context.context_id,
                    relationship="derived_by_process_coordinate_rectification",
                    node_key=node.node_key,
                ),
                *(
                    [
                        ProvenanceLink(
                            source_kind="operator_config",
                            source_id=view_profile.profile_checksum,
                            relationship="uses_versioned_image_view_profile",
                            node_key=node.node_key,
                            metadata={"view_profile_id": view_profile.view_profile_id},
                        )
                    ]
                    if view_profile is not None
                    else []
                ),
                *(
                    [
                        ProvenanceLink(
                            source_kind="operator_config",
                            source_id=profile.calibration_hash,
                            relationship="uses_stem_curvature_process_calibration",
                            node_key=node.node_key,
                            metadata={"calibration_id": profile.calibration_id},
                        )
                    ]
                    if profile is not None
                    else []
                ),
            ],
            metadata={
                "correlation_id": context.correlation_id,
                "view_profile_id": view_profile.view_profile_id if view_profile else None,
                "view_profile_checksum": view_profile.profile_checksum if view_profile else None,
                "process_calibration_id": profile.calibration_id if profile else None,
                "process_calibration_hash": profile.calibration_hash if profile else None,
                "intrinsic_calibration_hash": intrinsic_hash,
                "perspective_rectified": perspective_rectified,
                "preserves_source_perspective": preserves_source_perspective,
            },
        )
        output = FramePayload.from_cv2_frame(
            rectified,
            mime_type="image/jpeg",
            jpeg_quality=frame.jpeg_quality(),
            source_uri=frame.source_uri,
            timestamp=frame.timestamp,
            image_context=image_context,
            metadata={
                **frame.metadata,
                "operation": "process_coordinate_rectify",
                "image_view_preprocessing_version": "image.view-runtime.v1",
                "undistort_preprocessing_version": frame.metadata.get(
                    "undistort_preprocessing_version", "camera.undistort.v1"
                ),
                "color_space": "BGR",
                "view_profile_id": view_profile.view_profile_id if view_profile else None,
                "view_profile_checksum": view_profile.profile_checksum if view_profile else None,
                "view_profile_schema_version": (
                    view_profile.schema_version if view_profile else None
                ),
                "image_view_transform_type": (
                    view_profile.transform_type if view_profile else "PERSPECTIVE_RECTIFY"
                ),
                "source_frame_context_id": source_context.context_id,
                "source_frame_timestamp": frame.timestamp,
                "camera_calibration_id": intrinsic_hash,
                "process_calibration_id": profile.calibration_id if profile else None,
                "process_calibration_hash": profile.calibration_hash if profile else None,
                "intrinsic_calibration_hash": intrinsic_hash,
                "perspective_rectified": perspective_rectified,
                "preserves_source_perspective": preserves_source_perspective,
                "canonical_model_view": view_profile is not None,
                "image_view_profile": view_profile_json,
                **(
                    {"stem_curvature_process_calibration": profile_json}
                    if profile_json is not None
                    else {}
                ),
            },
        )
        return NodeExecutionResult(outputs={"frame": output})


def _intrinsic_calibration_hash(frame: FramePayload) -> str | None:
    context = frame.image_context
    if context is not None:
        value = context.transform.parameters.get("calibration_hash")
        if isinstance(value, str) and value:
            return value
        value = context.metadata.get("calibration_hash")
        if isinstance(value, str) and value:
            return value
    value = frame.metadata.get("calibration_hash")
    return value if isinstance(value, str) and value else None


def _opencv_modules(node: RuntimeNodeSpec) -> tuple[Any, Any]:
    try:
        import cv2
        import numpy as np
    except Exception as exc:  # pragma: no cover - cv-runtime declares both dependencies
        raise _node_error(
            node,
            "stem_process_dependency_unavailable",
            "Process coordinate rectification requires OpenCV and NumPy.",
            metadata={"detail": str(exc)},
        ) from exc
    return cv2, np


def _frame_cv_image(frame: FramePayload, *, node: RuntimeNodeSpec, cv2: Any, np: Any) -> Any:
    cached = frame.cached_cv2_frame()
    if cached is not None:
        image = np.asarray(cached)
    else:
        try:
            encoded = np.frombuffer(frame.image_bytes(), dtype=np.uint8)
            image = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
        except Exception as exc:
            raise _node_error(
                node,
                "stem_process_image_decode_failed",
                "Process coordinate rectification could not decode the input frame.",
                metadata={"detail": str(exc)},
            ) from exc
    if image is None or image.ndim not in {2, 3}:
        raise _node_error(
            node,
            "stem_process_image_decode_failed",
            "Process coordinate rectification requires a decodable frame.",
        )
    return image


def _derived_context_id(
    context: RuntimeExecutionContext,
    *,
    node: RuntimeNodeSpec,
    parent_context_id: str,
    calibration_hash: str,
) -> str:
    digest = hashlib.sha256(
        "|".join(
            (
                context.correlation_id,
                node.node_key,
                parent_context_id,
                calibration_hash,
            )
        ).encode("utf-8")
    ).hexdigest()[:20]
    return f"ctx_{digest}"


def _node_error(
    node: RuntimeNodeSpec,
    code: str,
    message: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> RuntimeNodeExecutionError:
    return RuntimeNodeExecutionError(
        code=code,
        message=message,
        node_key=node.node_key,
        node_type=node.node_type,
        retryable=False,
        metadata=metadata or {},
    )


preprocessing_process_coordinate_rectify = StemProcessCoordinateRectifyHandler()


def _canonical_config_json(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


@lru_cache(maxsize=128)
def _cached_image_view_profile(serialized: str) -> _ValidatedProfile:
    profile = validate_image_view_profile(json.loads(serialized))
    return _ValidatedProfile(
        model=profile,
        json_value=profile.model_dump(mode="json"),
        serialized=serialized,
    )


@lru_cache(maxsize=128)
def _cached_process_profile(serialized: str) -> _ValidatedProfile:
    profile = validate_stem_curvature_process_calibration(json.loads(serialized))
    return _ValidatedProfile(
        model=profile,
        json_value=profile.model_dump(mode="json"),
        serialized=serialized,
    )


@lru_cache(maxsize=128)
def _cached_process_transform_metadata(
    process_profile_json: str,
    view_profile_json: str,
    source_to_view: tuple[float, ...],
) -> dict[str, Any]:
    import numpy as np

    profile = _cached_process_profile(process_profile_json).model if process_profile_json else None
    view_profile = (
        _cached_image_view_profile(view_profile_json).model if view_profile_json else None
    )
    return _process_transform_metadata(
        profile,
        view_profile=view_profile,
        source_to_view=list(source_to_view),
        np=np,
    )


def _inverse_homography(values: list[float], *, np: Any) -> list[float]:
    matrix = np.asarray(values, dtype=np.float64).reshape(3, 3)
    inverse = np.linalg.inv(matrix)
    inverse = inverse / inverse[2, 2]
    return [float(item) for item in inverse.reshape(-1)]


def _process_transform_metadata(
    profile: Any,
    *,
    view_profile: Any,
    source_to_view: list[float],
    np: Any,
) -> dict[str, Any]:
    if profile is None:
        return {}
    image_to_process = np.asarray(profile.homography_image_to_process, dtype=np.float64).reshape(
        3, 3
    )
    process_to_image = np.asarray(profile.homography_process_to_image, dtype=np.float64).reshape(
        3, 3
    )
    image_to_view = np.asarray(source_to_view, dtype=np.float64).reshape(3, 3)
    view_to_image = np.linalg.inv(image_to_view)
    view_to_process = image_to_process @ view_to_image
    process_to_view = image_to_view @ process_to_image
    view_to_process /= view_to_process[2, 2]
    process_to_view /= process_to_view[2, 2]
    bounds = profile.process_bounds.model_dump(mode="json")
    if view_profile is not None:
        source_points = np.asarray(
            [
                [
                    point.x * (view_profile.source_image_width - 1),
                    point.y * (view_profile.source_image_height - 1),
                ]
                for point in view_profile.source_quadrilateral
            ],
            dtype=np.float64,
        )
        homogeneous = np.column_stack((source_points, np.ones(4)))
        projected = (image_to_process @ homogeneous.T).T
        process_points = projected[:, :2] / projected[:, 2:3]
        x_min, y_min = np.min(process_points, axis=0)
        x_max, y_max = np.max(process_points, axis=0)
        if view_profile.transform_type == "PERSPECTIVE_RECTIFY":
            expected_options = (
                np.asarray([[x_min, y_min], [x_max, y_min], [x_max, y_max], [x_min, y_max]]),
                np.asarray([[x_min, y_max], [x_max, y_max], [x_max, y_min], [x_min, y_min]]),
                np.asarray([[x_max, y_min], [x_min, y_min], [x_min, y_max], [x_max, y_max]]),
                np.asarray([[x_max, y_max], [x_min, y_max], [x_min, y_min], [x_max, y_min]]),
            )
            scale = max(float(x_max - x_min), float(y_max - y_min), 1e-9)
            maximum_corner_error = min(
                float(np.max(np.linalg.norm(process_points - item, axis=1)))
                for item in expected_options
            )
            if maximum_corner_error > scale * 0.02:
                raise ValueError(
                    "perspective-rectified image view must describe an axis-aligned "
                    "rectangle on the calibrated X/Y reference plane"
                )
        bounds = {
            "x_min_mm": float(x_min),
            "x_max_mm": float(x_max),
            "y_min_mm": float(y_min),
            "y_max_mm": float(y_max),
        }
    return {
        "process_calibration_id": profile.calibration_id,
        "process_calibration_hash": profile.calibration_hash,
        "homography_image_to_process": profile.homography_image_to_process,
        "homography_process_to_image": profile.homography_process_to_image,
        "homography_view_to_process": [float(item) for item in view_to_process.reshape(-1)],
        "homography_process_to_view": [float(item) for item in process_to_view.reshape(-1)],
        "homography_image_to_rectified": source_to_view,
        "process_bounds": bounds,
        "process_view_bounds": bounds,
        "image_view_transform_type": (
            view_profile.transform_type if view_profile is not None else "PERSPECTIVE_RECTIFY"
        ),
        "coordinate_unit": profile.coordinate_unit,
        "reference_plane": profile.reference_plane.model_dump(mode="json"),
    }
