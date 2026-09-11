# ruff: noqa: ANN401 -- OpenCV remap arrays are provided through dynamic bindings.
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from cv_contracts.runtime import RuntimeNodeSpec

from cv_runtime.camera_calibration import (
    apply_camera_undistort,
    validate_camera_intrinsic_calibration,
)
from cv_runtime.context import NodeInputs, RuntimeExecutionContext
from cv_runtime.errors import RuntimeNodeExecutionError
from cv_runtime.handlers import NodeExecutionResult
from cv_runtime.payloads import (
    FramePayload,
    ProvenanceLink,
    RuntimeImageContext,
    RuntimeImageTransform,
)


@dataclass(frozen=True)
class _RemapEntry:
    map_x: Any
    map_y: Any


class CameraUndistortHandler:
    node_type = "preprocessing.camera_undistort"

    def __init__(self, *, max_cache_entries: int = 16) -> None:
        self._max_cache_entries = max(int(max_cache_entries), 1)
        self._cache: OrderedDict[str, _RemapEntry] = OrderedDict()
        self._cache_lock = threading.RLock()

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
                "frame_input_missing",
                "Camera undistort requires a frame input.",
            )
        config = node.config or {}
        try:
            profile = validate_camera_intrinsic_calibration(config.get("calibration"))
        except Exception as exc:
            raise _node_error(
                node,
                "camera_calibration_invalid",
                "Camera undistort requires a valid intrinsic calibration profile.",
                metadata={"detail": str(exc)},
            ) from exc
        if context.channel_id is not None and profile.source_channel_id != context.channel_id:
            raise _node_error(
                node,
                "camera_calibration_channel_mismatch",
                "The intrinsic calibration profile belongs to a different runtime channel.",
                metadata={
                    "runtime_channel_id": context.channel_id,
                    "calibration_channel_id": profile.source_channel_id,
                    "calibration_hash": profile.calibration_hash,
                },
            )
        resolution_policy = str(config.get("resolution_policy") or "exact")
        if resolution_policy != "exact":
            raise _node_error(
                node,
                "camera_calibration_resolution_policy_unsupported",
                "Camera undistort currently supports only exact calibration resolution matching.",
                metadata={"resolution_policy": resolution_policy},
            )

        cv2, np = _opencv_modules(node)
        image = _frame_cv_image(frame, node=node, cv2=cv2, np=np)
        height, width = int(image.shape[0]), int(image.shape[1])
        if frame.width is not None and int(frame.width) != width:
            raise _node_error(
                node,
                "camera_frame_resolution_metadata_mismatch",
                "Frame width metadata does not match the decoded image.",
                metadata={"metadata_width": frame.width, "decoded_width": width},
            )
        if frame.height is not None and int(frame.height) != height:
            raise _node_error(
                node,
                "camera_frame_resolution_metadata_mismatch",
                "Frame height metadata does not match the decoded image.",
                metadata={"metadata_height": frame.height, "decoded_height": height},
            )
        if (width, height) != (profile.image_width, profile.image_height):
            raise _node_error(
                node,
                "camera_calibration_resolution_mismatch",
                (
                    f"Frame resolution {width}x{height} does not match intrinsic "
                    f"calibration resolution {profile.image_width}x{profile.image_height}."
                ),
                metadata={
                    "frame_width": width,
                    "frame_height": height,
                    "calibration_width": profile.image_width,
                    "calibration_height": profile.image_height,
                    "resolution_policy": "exact",
                },
            )

        interpolation_name = str(config.get("interpolation") or "linear")
        if interpolation_name not in {"nearest", "linear", "cubic"}:
            raise _node_error(
                node,
                "camera_undistort_interpolation_invalid",
                "Camera undistort interpolation must be nearest, linear, or cubic.",
                metadata={"interpolation": interpolation_name},
            )

        intrinsic_calibrated = profile.calibration_method != "IDENTITY_PASSTHROUGH"
        if intrinsic_calibrated:
            cache_key = f"{profile.calibration_hash}:{width}x{height}"
            remap = self._remap_for_context(
                context,
                node=node,
                cache_key=cache_key,
                camera_matrix=np.asarray(profile.camera_matrix, dtype=np.float64).reshape(3, 3),
                output_camera_matrix=np.asarray(
                    profile.output_camera_matrix,
                    dtype=np.float64,
                ).reshape(3, 3),
                distortion_coefficients=np.asarray(
                    profile.distortion_coefficients,
                    dtype=np.float64,
                ),
                image_size=(width, height),
                cv2=cv2,
            )
            try:
                output_image = apply_camera_undistort(
                    image,
                    profile,
                    interpolation=interpolation_name,
                    remap=remap,
                )
            except Exception as exc:
                raise _node_error(
                    node,
                    "camera_undistort_remap_failed",
                    "OpenCV could not remap the camera frame.",
                    metadata={"detail": str(exc)},
                ) from exc
        else:
            output_image = apply_camera_undistort(
                image,
                profile,
                interpolation=interpolation_name,
            )

        source_context = _source_image_context(context, node, frame, width=width, height=height)
        valid_roi = profile.valid_roi.model_dump(mode="json")
        image_context = RuntimeImageContext(
            context_id=_derived_context_id(
                context,
                node=node,
                parent_context_id=source_context.context_id,
                calibration_hash=profile.calibration_hash,
            ),
            kind="processed",
            width=width,
            height=height,
            source_uri=frame.source_uri,
            source_node_key=node.node_key,
            source_image_context_id=source_context.context_id,
            parent_image_context_id=source_context.context_id,
            transform=RuntimeImageTransform(
                transform_type="undistort",
                coordinate_projection="not_projectable",
                parameters={
                    "calibration_hash": profile.calibration_hash,
                    "calibration_method": profile.calibration_method,
                    "intrinsic_calibrated": intrinsic_calibrated,
                    "distortion_model": profile.distortion_model,
                    "calibration_resolution": {
                        "width": profile.image_width,
                        "height": profile.image_height,
                    },
                    "output_resolution": {"width": width, "height": height},
                    "valid_roi": valid_roi,
                    "resolution_policy": "exact",
                    "interpolation": interpolation_name,
                    "perspective_rectified": False,
                    "homography_applied": False,
                },
            ),
            provenance=[
                ProvenanceLink(
                    source_kind="image_context",
                    source_id=source_context.context_id,
                    relationship="derived_by_undistort",
                    node_key=node.node_key,
                ),
                ProvenanceLink(
                    source_kind="operator_config",
                    source_id=profile.calibration_hash,
                    relationship="uses_intrinsic_camera_calibration",
                    node_key=node.node_key,
                    metadata={"source_channel_id": profile.source_channel_id},
                ),
            ],
            metadata={
                "correlation_id": context.correlation_id,
                "calibration_status": profile.status,
                "calibration_hash": profile.calibration_hash,
                "calibration_method": profile.calibration_method,
                "intrinsic_calibrated": intrinsic_calibrated,
                "reason_codes": (
                    [] if intrinsic_calibrated else ["CAMERA_INTRINSIC_CALIBRATION_BYPASSED"]
                ),
                "valid_roi": valid_roi,
                "perspective_rectified": False,
            },
        )
        output = FramePayload.from_cv2_frame(
            np.ascontiguousarray(output_image),
            mime_type="image/jpeg",
            jpeg_quality=frame.jpeg_quality(),
            source_uri=frame.source_uri,
            timestamp=frame.timestamp,
            image_context=image_context,
            metadata={
                **frame.metadata,
                "operation": "camera_undistort",
                "undistort_preprocessing_version": "camera.undistort.v1",
                "color_space": "BGR",
                "calibration_hash": profile.calibration_hash,
                "calibration_method": profile.calibration_method,
                "intrinsic_calibrated": intrinsic_calibrated,
                "reason_codes": (
                    [] if intrinsic_calibrated else ["CAMERA_INTRINSIC_CALIBRATION_BYPASSED"]
                ),
                "valid_roi": valid_roi,
                "perspective_rectified": False,
            },
        )
        return NodeExecutionResult(outputs={"frame": output})

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    def cache_size(self) -> int:
        with self._cache_lock:
            return len(self._cache)

    def _remap_for_context(
        self,
        context: RuntimeExecutionContext,
        *,
        node: RuntimeNodeSpec,
        cache_key: str,
        camera_matrix: Any,
        output_camera_matrix: Any,
        distortion_coefficients: Any,
        image_size: tuple[int, int],
        cv2: Any,
    ) -> _RemapEntry:
        # Ordered realtime overlap shares ``context.state`` while two pure frame prefixes may
        # execute concurrently.  The remap itself is immutable; protect only the cache metadata
        # update so preprocessing can safely remain ahead of the stateful frame barrier.
        with self._cache_lock:
            raw_node_state = context.state.setdefault(node.node_key, {})
            node_state = raw_node_state if isinstance(raw_node_state, dict) else {}
            if node_state is not raw_node_state:
                context.state[node.node_key] = node_state
            state_entry = node_state.get("camera_undistort_remap")
            if (
                isinstance(state_entry, dict)
                and state_entry.get("cache_key") == cache_key
                and isinstance(state_entry.get("entry"), _RemapEntry)
            ):
                node_state["cache_hits"] = int(node_state.get("cache_hits") or 0) + 1
                return state_entry["entry"]

            entry, shared_hit = self._shared_remap(
                cache_key=cache_key,
                camera_matrix=camera_matrix,
                output_camera_matrix=output_camera_matrix,
                distortion_coefficients=distortion_coefficients,
                image_size=image_size,
                cv2=cv2,
            )
            node_state["camera_undistort_remap"] = {
                "cache_key": cache_key,
                "entry": entry,
            }
            node_state["cache_hits"] = int(node_state.get("cache_hits") or 0) + int(shared_hit)
            node_state["cache_misses"] = int(node_state.get("cache_misses") or 0) + int(
                not shared_hit
            )
            return entry

    def _shared_remap(
        self,
        *,
        cache_key: str,
        camera_matrix: Any,
        output_camera_matrix: Any,
        distortion_coefficients: Any,
        image_size: tuple[int, int],
        cv2: Any,
    ) -> tuple[_RemapEntry, bool]:
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._cache.move_to_end(cache_key)
                return cached, True
            map_x, map_y = cv2.initUndistortRectifyMap(
                camera_matrix,
                distortion_coefficients,
                None,
                output_camera_matrix,
                image_size,
                cv2.CV_32FC1,
            )
            try:
                map_x.setflags(write=False)
                map_y.setflags(write=False)
            except AttributeError:
                pass
            entry = _RemapEntry(map_x=map_x, map_y=map_y)
            self._cache[cache_key] = entry
            self._cache.move_to_end(cache_key)
            while len(self._cache) > self._max_cache_entries:
                self._cache.popitem(last=False)
            return entry, False


def _opencv_modules(node: RuntimeNodeSpec) -> tuple[Any, Any]:
    try:
        import cv2
        import numpy as np
    except Exception as exc:  # pragma: no cover - cv-runtime declares both dependencies
        raise _node_error(
            node,
            "camera_undistort_dependency_unavailable",
            "Camera undistort requires OpenCV and NumPy.",
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
                "camera_undistort_image_decode_failed",
                "Camera undistort could not decode the input frame.",
                metadata={"detail": str(exc)},
            ) from exc
    if image is None or image.ndim not in {2, 3}:
        raise _node_error(
            node,
            "camera_undistort_image_decode_failed",
            "Camera undistort requires a decodable grayscale or color frame.",
        )
    return image


def _source_image_context(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    frame: FramePayload,
    *,
    width: int,
    height: int,
) -> RuntimeImageContext:
    if frame.image_context is not None:
        return frame.image_context
    digest = hashlib.sha256(
        "|".join(
            (
                context.correlation_id,
                node.node_key,
                "camera-undistort-source",
                frame.timestamp or "",
            )
        ).encode("utf-8")
    ).hexdigest()[:20]
    context_id = f"ctx_{digest}"
    return RuntimeImageContext(
        context_id=context_id,
        kind="original",
        width=width,
        height=height,
        source_uri=frame.source_uri,
        source_node_key=node.node_key,
        source_image_context_id=context_id,
        transform=RuntimeImageTransform(
            transform_type="identity",
            coordinate_projection="identity",
        ),
        provenance=[
            ProvenanceLink(
                source_kind="node",
                source_id=node.node_key,
                relationship="materialized_missing_source_context",
                node_key=node.node_key,
            )
        ],
    )


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


preprocessing_camera_undistort = CameraUndistortHandler()
