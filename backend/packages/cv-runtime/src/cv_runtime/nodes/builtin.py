from __future__ import annotations

import base64
import hashlib
import io
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv_contracts import (
    ClassificationLabelScore,
    DetectionBox,
    DetectionPoint,
    DetectionPrediction,
    RealtimeAnomalyRequest,
    RealtimeClassifyRequest,
    RealtimeDetectRequest,
    RealtimeSegmentRequest,
    SegmentationPoint,
    SegmentationPrediction,
)
from cv_contracts.runtime import RuntimeNodeSpec
from cv_status import TaskType
from PIL import Image, ImageChops, ImageStat

from cv_runtime.context import NodeInputs, RuntimeExecutionContext
from cv_runtime.errors import RuntimeNodeExecutionError
from cv_runtime.handlers import NodeExecutionResult
from cv_runtime.payloads import (
    AnomalyPayload,
    BoardCutDecisionPayload,
    CaptureIntent,
    CaptureIntentsPayload,
    ClassificationPayload,
    ClearanceZone,
    ClearanceZonesPayload,
    CoordinateFramePayload,
    CvEdgeContour,
    CvEdgesPayload,
    CvLineCandidate,
    DetectionPayload,
    DisplayOutputPayload,
    EventPayload,
    EventsPayload,
    FeatureMeasurement,
    FeatureMeasurementsPayload,
    FramePayload,
    FreeRegion,
    FreeRegionPayload,
    InspectionCoordinateFrame,
    InspectionPoint,
    InspectionVector,
    LineGeometry,
    MeasurementDecision,
    MeasurementPayload,
    MeasurementsPayload,
    OverlayPayload,
    OverlayPrimitive,
    PositionPayload,
    PositionsPayload,
    ProvenanceLink,
    QualityDecisionPayload,
    ReferenceEdge,
    ReferenceEdgesPayload,
    Region,
    RegionSetPayload,
    ResultPayload,
    RoiPayload,
    RoiSetPayload,
    RuntimeImageContext,
    RuntimeImageTransform,
    RuntimeObjectAttribute,
    RuntimeObjectEnrichment,
    RuntimeObjectReference,
    SegmentPayload,
    StemCurvaturePhaseSolutionPayload,
    StemHorizontalCurvaturePayload,
    StemSurfacePhasePayload,
)


@dataclass(frozen=True)
class BuiltinHandler:
    node_type: str

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        raise NotImplementedError


@dataclass(frozen=True)
class SpatialObject:
    key: str
    label: str | None
    score: float | None
    box: DetectionBox
    polygon: list[tuple[float, float]]
    source_image_context_id: str | None = None
    source_object_id: str | None = None
    source_node_key: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class PreparedInferenceImage:
    image_bytes: bytes
    mime_type: str
    source_width: int
    source_height: int
    input_width: int
    input_height: int
    scale: float = 1.0
    pad_left: int = 0
    pad_top: int = 0
    letterboxed: bool = False

    def image_base64(self) -> str:
        return base64.b64encode(self.image_bytes).decode("ascii")

    def metadata(self) -> dict[str, Any]:
        return {
            "source_width": self.source_width,
            "source_height": self.source_height,
            "input_width": self.input_width,
            "input_height": self.input_height,
            "letterboxed": self.letterboxed,
            "scale": round(self.scale, 8),
            "pad_left": self.pad_left,
            "pad_top": self.pad_top,
            "encoded_bytes": len(self.image_bytes),
        }


class FileTestSourceHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        frame = context.input_frame
        config = _merged_config(context, node)
        if frame is None:
            path = config.get("path")
            if not path:
                raise _node_error(
                    node,
                    "source_input_missing",
                    "File/test source requires a configured path or input frame.",
                )
            frame = _frame_from_path(Path(str(path)), timestamp=context.now_iso())
        return NodeExecutionResult(outputs={"frame": _ensure_frame_context(context, node, frame)})


class TestPhotoSourceHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        config = node.config or {}
        frame = context.input_frame
        if frame is None:
            photos = _test_photo_sequence(config)
            if not photos:
                raise _node_error(
                    node,
                    "test_photo_missing",
                    "Test photo source requires an uploaded photo.",
                )
            state_key = f"test_photo_sequence:{node.node_key}"
            photo_index = int(context.state.get(state_key, 0)) % len(photos)
            context.state[state_key] = (photo_index + 1) % len(photos)
            photo = photos[photo_index]
            data_url = str(photo.get("data_url") or "")
            try:
                header, encoded = data_url.split(",", 1)
                if header not in {
                    "data:image/jpeg;base64",
                    "data:image/png;base64",
                    "data:image/webp;base64",
                    "data:image/bmp;base64",
                }:
                    raise ValueError("unsupported data URL")
                image_bytes = base64.b64decode(encoded, validate=True)
                if len(image_bytes) > 5 * 1024 * 1024:
                    raise ValueError("image exceeds 5 MB")
                image = Image.open(io.BytesIO(image_bytes))
                image.verify()
                width, height = image.size
            except (ValueError, OSError) as exc:
                raise _node_error(
                    node,
                    "test_photo_invalid",
                    "The uploaded test photo is invalid or unsupported.",
                ) from exc
            mime_type = header[5:].split(";", 1)[0]
            frame = FramePayload.from_bytes(
                image_bytes,
                mime_type=mime_type,
                width=width,
                height=height,
                source_uri=f"pipeline-test-photo://{photo.get('name') or node.node_key}",
                timestamp=context.now_iso(),
                metadata={
                    "source_type": "test_photo",
                    "file_name": photo.get("name"),
                    "photo_index": photo_index,
                    "photo_count": len(photos),
                },
            )
        outputs: dict[str, Any] = {
            "frame": _ensure_frame_context(context, node, frame)
        }
        trigger = inputs.get("trigger") or context.trigger
        if trigger is not None:
            outputs["trigger"] = trigger
        return NodeExecutionResult(outputs=outputs)


def _test_photo_sequence(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw_photos = config.get("photos")
    if isinstance(raw_photos, list):
        photos = [item for item in raw_photos if isinstance(item, dict)]
        if len(photos) > 100:
            return []
        if photos:
            return photos
    photo = config.get("photo")
    return [photo] if isinstance(photo, dict) else []


class HttpPutTriggerHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        trigger = context.trigger or _trigger_payload(context, {"trigger_type": "http_put"})
        return NodeExecutionResult(outputs={"trigger": trigger})


class IntervalTriggerHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        trigger = context.trigger or _trigger_payload(
            context,
            {
                "trigger_type": "interval",
                "interval_ms": int((node.config or {}).get("interval_ms") or 1000),
            },
        )
        return NodeExecutionResult(outputs={"trigger": trigger})


class ChannelSnapshotSourceHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        if context.input_frame is None:
            raise _node_error(
                node,
                "channel_snapshot_missing",
                "Channel snapshot requires a frame captured from the selected channel.",
                retryable=True,
            )
        frame = _ensure_frame_context(context, node, context.input_frame)
        trigger = inputs.get("trigger") or context.trigger
        outputs: dict[str, Any] = {"frame": frame}
        if trigger is not None:
            outputs["trigger"] = trigger
        return NodeExecutionResult(outputs=outputs)


class TriggerSnapshotSourceHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        frame = context.input_frame
        trigger = context.trigger
        config = _merged_config(context, node)
        if frame is None:
            raise _node_error(
                node,
                "snapshot_input_missing",
                "Trigger snapshot source needs a frame captured by the trigger executor.",
            )
        frame = _ensure_frame_context(context, node, frame)
        return NodeExecutionResult(
            outputs={
                "frame": frame,
                "trigger": trigger
                or context.trigger
                or _trigger_payload(context, {"camera": config.get("camera")}),
            }
        )


class ChannelFrameSourceHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        if context.input_frame is None:
            raise _node_error(
                node,
                "channel_frame_missing",
                "Channel frame input requires a frame supplied by the runtime channel.",
                retryable=True,
            )
        frame = _ensure_frame_context(context, node, context.input_frame)
        outputs: dict[str, Any] = {"frame": frame}
        if context.trigger is not None:
            outputs["trigger"] = context.trigger
        return NodeExecutionResult(outputs=outputs)


class RtspSourceHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        if context.input_frame is None:
            config = _merged_config(context, node)
            if config.get("path"):
                return NodeExecutionResult(
                    outputs={
                        "frame": _ensure_frame_context(
                            context,
                            node,
                            _frame_from_path(
                                Path(str(config["path"])), timestamp=context.now_iso()
                            ),
                        )
                    }
                )
            raise _node_error(
                node,
                "rtsp_frame_missing",
                "RTSP source adapter requires a frame supplied by the realtime stream loop.",
                retryable=True,
            )
        return NodeExecutionResult(
            outputs={"frame": _ensure_frame_context(context, node, context.input_frame)}
        )


class ResizeHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        frame = _ensure_frame_context(context, node, _required_frame(node, inputs, "frame"))
        config = node.config or {}
        width = int(config.get("width") or 1280)
        height = int(config.get("height") or 720)
        keep_aspect = bool(config.get("keep_aspect", config.get("keep_aspect_ratio", True)))
        image = _open_frame(frame)
        if keep_aspect:
            image.thumbnail((width, height))
        else:
            image = image.resize((width, height))
        output_frame = _frame_from_image(
            image,
            source=frame,
            metadata={"operation": "resize"},
            image_context=_derived_image_context(
                context,
                node,
                source=frame,
                kind="transformed",
                transform_type="resize",
                parameters={
                    "width": image.width,
                    "height": image.height,
                    "keep_aspect": keep_aspect,
                },
                coordinate_projection="scale",
            ),
        )
        return NodeExecutionResult(outputs={"frame": output_frame})


class CropHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        frame = _ensure_frame_context(context, node, _required_frame(node, inputs, "frame"))
        config = node.config or {}
        image = _open_frame(frame, copy=False)
        width, height = image.size
        x = max(int(config.get("x") or 0), 0)
        y = max(int(config.get("y") or 0), 0)
        crop_width = max(int(config.get("width") or width), 1)
        crop_height = max(int(config.get("height") or height), 1)
        crop_box = (x, y, min(x + crop_width, width), min(y + crop_height, height))
        cropped = image.crop(crop_box)
        output_frame = _frame_from_image(
            cropped,
            source=frame,
            metadata={"operation": "crop"},
            image_context=_derived_image_context(
                context,
                node,
                source=frame,
                kind="crop",
                transform_type="crop",
                parameters={
                    "x": crop_box[0],
                    "y": crop_box[1],
                    "width": cropped.width,
                    "height": cropped.height,
                    "source_width": width,
                    "source_height": height,
                },
                coordinate_projection="crop_offset",
            ),
        )
        return NodeExecutionResult(outputs={"frame": output_frame})


class RoiExtractHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        frame = _ensure_frame_context(context, node, _required_frame(node, inputs, "frame"))
        regions = _regions_from_payload(
            inputs.get("regions") or inputs.get("detections") or inputs.get("segments")
        )
        if not regions:
            regions = _configured_regions(node.config or {})
        image = _open_frame(frame)
        padding = float((node.config or {}).get("padding_percent") or 0)
        rois: list[RoiPayload] = []
        for index, region in enumerate(regions, start=1):
            box = _pad_box(region.box, padding)
            cropped = _crop_normalized(image, box)
            source_object_id = region.source_object_id or region.key
            roi_context = _derived_image_context(
                context,
                node,
                source=frame,
                kind="roi",
                transform_type="roi_crop",
                parameters={
                    "box": box.model_dump(mode="json"),
                    "padding_percent": padding,
                    "source_region_key": region.key,
                },
                coordinate_projection="crop_offset",
                source_object_id=source_object_id,
            )
            roi_frame = _frame_from_image(
                cropped,
                source=frame,
                metadata={"operation": "roi_extract"},
                image_context=roi_context,
            )
            rois.append(
                RoiPayload(
                    roi_key=region.key or f"roi_{index}",
                    frame=roi_frame,
                    box=box,
                    label=region.label,
                    source_region_key=region.key,
                    source_image_context_id=_frame_context_id(frame),
                    source_object_id=source_object_id,
                    image_context=roi_context,
                    provenance=_roi_provenance(frame, source_object_id, node.node_key),
                    metadata=region.metadata,
                )
            )
        return NodeExecutionResult(outputs={"rois": RoiSetPayload(rois=rois)})


class FrameSamplerHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        frame = _required_frame(node, inputs, "frame")
        state = context.state.setdefault(node.node_key, {})
        count = int(state.get("count") or 0) + 1
        state["count"] = count
        sample_every = max(int((node.config or {}).get("sample_every_n") or 1), 1)
        if count != 1 and count % sample_every != 0:
            return NodeExecutionResult(outputs={})
        max_fps = float((node.config or {}).get("max_fps") or 0)
        if max_fps > 0:
            last_emitted_at = float(state.get("last_emitted_at") or 0.0)
            now = time.perf_counter()
            min_interval = 1.0 / max_fps
            if last_emitted_at and (now - last_emitted_at) < min_interval:
                return NodeExecutionResult(outputs={})
            state["last_emitted_at"] = now
        else:
            state["last_emitted_at"] = time.perf_counter()
        return NodeExecutionResult(outputs={"frame": frame})


class ZoneFilterHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        payload = inputs.get("items")
        zones = _zones_from_config(node.config or {})
        include = (node.config or {}).get("mode", "include") != "exclude"
        if not zones or payload is None:
            return NodeExecutionResult(outputs={"items": _result_from_payload(payload)})
        if isinstance(payload, DetectionPayload):
            detections = [
                item for item in payload.detections if _box_in_any_zone(item.box, zones) is include
            ]
            return NodeExecutionResult(
                outputs={
                    "items": DetectionPayload(
                        detections=detections,
                        image_context_id=payload.image_context_id,
                        image_width=payload.image_width,
                        image_height=payload.image_height,
                        route=payload.route,
                    )
                }
            )
        if isinstance(payload, RegionSetPayload):
            regions = [
                item for item in payload.regions if _box_in_any_zone(item.box, zones) is include
            ]
            return NodeExecutionResult(outputs={"items": RegionSetPayload(regions=regions)})
        return NodeExecutionResult(outputs={"items": _result_from_payload(payload)})


class DetectionInferenceHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        frame = _ensure_frame_context(context, node, _required_frame(node, inputs, "frame"))
        if context.inference_client is None:
            raise _node_error(
                node, "inference_client_missing", "Inference client is not configured."
            )
        resolve_started = time.perf_counter()
        binding = context.resolve_model(_required_model_ref(node))
        model_resolve_ms = _elapsed_ms(resolve_started)
        if binding.task_type not in {TaskType.DETECTION, TaskType.ORIENTED_DETECTION}:
            raise _node_error(
                node,
                "model_task_incompatible",
                "Detection node requires a detection or oriented detection model.",
            )
        timeout_ms = _inference_timeout_ms(context, node)
        max_detections = int((node.config or {}).get("max_detections") or 100)
        score_threshold = float((node.config or {}).get("score_threshold") or 0.35)
        image_prepare_started = time.perf_counter()
        prepared = _prepare_inference_image(frame, binding, task_type=binding.task_type)
        detect_bytes = getattr(context.inference_client, "detect_bytes", None)
        if callable(detect_bytes):
            request_image_prepare_ms = _elapsed_ms(image_prepare_started)
            client_started = time.perf_counter()
            response = detect_bytes(
                deployment_target=binding.deployment_target,
                deployment_release_id=binding.deployment_release_id,
                image_bytes=prepared.image_bytes,
                mime_type=prepared.mime_type,
                timeout_ms=timeout_ms,
                max_detections=max_detections,
                score_threshold=score_threshold,
            )
            binary_transport = True
        else:
            request_image_prepare_ms = _elapsed_ms(image_prepare_started)
            client_started = time.perf_counter()
            response = context.inference_client.detect(
                RealtimeDetectRequest(
                    deployment_target=binding.deployment_target,
                    deployment_release_id=binding.deployment_release_id,
                    image_base64=prepared.image_base64(),
                    timeout_ms=timeout_ms,
                    max_detections=max_detections,
                    score_threshold=score_threshold,
                )
            )
            binary_transport = False
        client_roundtrip_ms = _elapsed_ms(client_started)
        detections = _restore_detection_geometry(response.detections, prepared)
        metadata = _resolved_model_metadata(binding)
        metadata["inference_image"] = prepared.metadata()
        metadata["runtime_timings_ms"] = _runtime_inference_timings(
            model_resolve_ms=model_resolve_ms,
            request_image_prepare_ms=request_image_prepare_ms,
            client_roundtrip_ms=client_roundtrip_ms,
            response=response,
            binary_transport=binary_transport,
        )
        return NodeExecutionResult(
            outputs={
                "detections": DetectionPayload(
                    detections=detections,
                    image_context_id=_frame_context_id(frame),
                    image_width=_frame_width(frame),
                    image_height=_frame_height(frame),
                    objects=_object_refs_from_detections(context, node, frame, detections),
                    route=response.route.model_dump(mode="json"),
                    metadata=metadata,
                )
            }
        )


class ClassificationInferenceHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        frame = _ensure_frame_context(context, node, _required_frame(node, inputs, "frame"))
        if context.inference_client is None:
            raise _node_error(
                node, "inference_client_missing", "Inference client is not configured."
            )
        resolve_started = time.perf_counter()
        binding = context.resolve_model(_required_model_ref(node))
        model_resolve_ms = _elapsed_ms(resolve_started)
        if binding.task_type != TaskType.CLASSIFICATION:
            raise _node_error(
                node,
                "model_task_incompatible",
                "Classification node requires a classification model.",
            )
        timeout_ms = _inference_timeout_ms(context, node)
        top_k = int((node.config or {}).get("top_k") or 1)
        image_prepare_started = time.perf_counter()
        prepared = _prepare_inference_image(frame, binding, task_type=binding.task_type)
        classify_bytes = getattr(context.inference_client, "classify_bytes", None)
        if callable(classify_bytes):
            request_image_prepare_ms = _elapsed_ms(image_prepare_started)
            client_started = time.perf_counter()
            response = classify_bytes(
                deployment_target=binding.deployment_target,
                deployment_release_id=binding.deployment_release_id,
                image_bytes=prepared.image_bytes,
                mime_type=prepared.mime_type,
                timeout_ms=timeout_ms,
                top_k=top_k,
            )
            binary_transport = True
        else:
            request_image_prepare_ms = _elapsed_ms(image_prepare_started)
            client_started = time.perf_counter()
            response = context.inference_client.classify(
                RealtimeClassifyRequest(
                    deployment_target=binding.deployment_target,
                    deployment_release_id=binding.deployment_release_id,
                    image_base64=prepared.image_base64(),
                    timeout_ms=timeout_ms,
                    top_k=top_k,
                )
            )
            binary_transport = False
        client_roundtrip_ms = _elapsed_ms(client_started)
        metadata = _resolved_model_metadata(binding)
        metadata["inference_image"] = prepared.metadata()
        metadata["runtime_timings_ms"] = _runtime_inference_timings(
            model_resolve_ms=model_resolve_ms,
            request_image_prepare_ms=request_image_prepare_ms,
            client_roundtrip_ms=client_roundtrip_ms,
            response=response,
            binary_transport=binary_transport,
        )
        return NodeExecutionResult(
            outputs={
                "classifications": ClassificationPayload(
                    labels=response.labels,
                    image_context_id=_frame_context_id(frame),
                    source_object_id=_source_object_id(frame),
                    enrichments=_classification_enrichments(context, node, frame, response.labels),
                    route=response.route.model_dump(mode="json"),
                    metadata=metadata,
                )
            }
        )


class SegmentationInferenceHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        frame = _ensure_frame_context(context, node, _required_frame(node, inputs, "frame"))
        if context.inference_client is None:
            raise _node_error(
                node, "inference_client_missing", "Inference client is not configured."
            )
        resolve_started = time.perf_counter()
        binding = context.resolve_model(_required_model_ref(node))
        model_resolve_ms = _elapsed_ms(resolve_started)
        if binding.task_type != TaskType.SEGMENTATION:
            raise _node_error(
                node, "model_task_incompatible", "Segmentation node requires a segmentation model."
            )
        image_view_compatibility = _validate_inference_image_view(frame, binding, node=node)
        timeout_ms = _inference_timeout_ms(context, node)
        max_segments = int((node.config or {}).get("max_segments") or 50)
        score_threshold = float((node.config or {}).get("score_threshold") or 0.35)
        image_prepare_started = time.perf_counter()
        prepared = _prepare_inference_image(
            frame, binding, task_type=binding.task_type,
            lossless=bool((node.config or {}).get("return_masks", False)),
        )
        segment_bytes = getattr(context.inference_client, "segment_bytes", None)
        if callable(segment_bytes) and not (node.config or {}).get("return_masks", False):
            request_image_prepare_ms = _elapsed_ms(image_prepare_started)
            client_started = time.perf_counter()
            response = segment_bytes(
                deployment_target=binding.deployment_target,
                deployment_release_id=binding.deployment_release_id,
                image_bytes=prepared.image_bytes,
                mime_type=prepared.mime_type,
                timeout_ms=timeout_ms,
                max_segments=max_segments,
                score_threshold=score_threshold,
            )
            binary_transport = True
        else:
            request_image_prepare_ms = _elapsed_ms(image_prepare_started)
            client_started = time.perf_counter()
            response = context.inference_client.segment(
                RealtimeSegmentRequest(
                    deployment_target=binding.deployment_target,
                    deployment_release_id=binding.deployment_release_id,
                    image_base64=prepared.image_base64(),
                    timeout_ms=timeout_ms,
                    max_segments=max_segments,
                    score_threshold=score_threshold,
                    return_masks=bool((node.config or {}).get("return_masks", False)),
                )
            )
            binary_transport = False
        client_roundtrip_ms = _elapsed_ms(client_started)
        segments = _restore_segmentation_geometry(response.segments, prepared)
        metadata = _resolved_model_metadata(binding)
        metadata["inference_image"] = prepared.metadata()
        metadata["image_view_compatibility"] = image_view_compatibility
        metadata["runtime_timings_ms"] = _runtime_inference_timings(
            model_resolve_ms=model_resolve_ms,
            request_image_prepare_ms=request_image_prepare_ms,
            client_roundtrip_ms=client_roundtrip_ms,
            response=response,
            binary_transport=binary_transport,
        )
        return NodeExecutionResult(
            outputs={
                "segments": SegmentPayload(
                    segments=segments,
                    image_width=_frame_width(frame),
                    image_height=_frame_height(frame),
                    image_context_id=_frame_context_id(frame),
                    objects=_object_refs_from_segments(context, node, frame, segments),
                    route=response.route.model_dump(mode="json"),
                    metadata=metadata,
                )
            }
        )


class AnomalyInferenceHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        frame = _ensure_frame_context(context, node, _required_frame(node, inputs, "frame"))
        if context.inference_client is None:
            raise _node_error(
                node, "inference_client_missing", "Inference client is not configured."
            )
        resolve_started = time.perf_counter()
        binding = context.resolve_model(_required_model_ref(node))
        model_resolve_ms = _elapsed_ms(resolve_started)
        if binding.task_type != TaskType.ANOMALY:
            raise _node_error(
                node, "model_task_incompatible", "Anomaly node requires an anomaly model."
            )
        threshold = _optional_float((node.config or {}).get("threshold"))
        timeout_ms = _inference_timeout_ms(context, node)
        return_mask = bool((node.config or {}).get("return_mask", False))
        image_prepare_started = time.perf_counter()
        prepared = _prepare_inference_image(frame, binding, task_type=binding.task_type)
        anomaly_bytes = getattr(context.inference_client, "anomaly_bytes", None)
        if callable(anomaly_bytes):
            request_image_prepare_ms = _elapsed_ms(image_prepare_started)
            client_started = time.perf_counter()
            response = anomaly_bytes(
                deployment_target=binding.deployment_target,
                deployment_release_id=binding.deployment_release_id,
                image_bytes=prepared.image_bytes,
                mime_type=prepared.mime_type,
                timeout_ms=timeout_ms,
                threshold=threshold,
                return_mask=return_mask,
            )
            binary_transport = True
        else:
            request_image_prepare_ms = _elapsed_ms(image_prepare_started)
            client_started = time.perf_counter()
            response = context.inference_client.anomaly(
                RealtimeAnomalyRequest(
                    deployment_target=binding.deployment_target,
                    deployment_release_id=binding.deployment_release_id,
                    image_base64=prepared.image_base64(),
                    timeout_ms=timeout_ms,
                    threshold=threshold,
                    return_mask=return_mask,
                )
            )
            binary_transport = False
        client_roundtrip_ms = _elapsed_ms(client_started)
        metadata = _resolved_model_metadata(binding)
        metadata["inference_image"] = prepared.metadata()
        metadata["runtime_timings_ms"] = _runtime_inference_timings(
            model_resolve_ms=model_resolve_ms,
            request_image_prepare_ms=request_image_prepare_ms,
            client_roundtrip_ms=client_roundtrip_ms,
            response=response,
            binary_transport=binary_transport,
        )
        metadata.update(
            {
                "heatmap_opacity": _optional_float((node.config or {}).get("heatmap_opacity"))
                or 0.55,
                "mask_requested": return_mask,
            }
        )
        return NodeExecutionResult(
            outputs={
                "anomaly": AnomalyPayload(
                    anomaly_score=response.anomaly_score,
                    predicted_label=response.predicted_label,
                    is_anomalous=response.is_anomalous,
                    threshold=response.threshold,
                    heatmap=response.heatmap,
                    mask=response.mask,
                    image_width=response.image_width,
                    image_height=response.image_height,
                    image_context_id=_frame_context_id(frame),
                    route=response.route.model_dump(mode="json"),
                    metadata=metadata,
                )
            }
        )


class PositionDeterminationHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        regions = _regions_from_payload(inputs.get("objects"))
        positions = [
            PositionPayload(
                key=region.key or f"position_{index}",
                label=region.label,
                x=round((region.box.x_min + region.box.x_max) / 2, 6),
                y=round((region.box.y_min + region.box.y_max) / 2, 6),
                box=region.box,
                source_image_context_id=region.source_image_context_id,
                source_object_id=region.source_object_id,
                metadata=region.metadata,
            )
            for index, region in enumerate(regions, start=1)
        ]
        return NodeExecutionResult(outputs={"positions": PositionsPayload(positions=positions)})


class ZoneEntryExitHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        positions = _positions_from_payload(inputs.get("positions"))
        zones = _zones_from_config(node.config or {})
        node_state = context.state.setdefault(node.node_key, {})
        previous = node_state.setdefault("inside", {})
        entered_at = node_state.setdefault("entered_at", {})
        emitted_enter = node_state.setdefault("emitted_enter", {})
        min_dwell_ms = max(int((node.config or {}).get("min_dwell_ms") or 0), 0)
        now_ms = int(time.time() * 1000)
        events: list[EventPayload] = []
        for position in positions:
            inside = _point_in_any_zone(position.x, position.y, zones)
            was_inside = bool(previous.get(position.key, False))
            previous[position.key] = inside
            if inside and not was_inside:
                entered_at[position.key] = now_ms
                emitted_enter[position.key] = min_dwell_ms == 0
                if min_dwell_ms == 0:
                    events.append(
                        EventPayload(
                            event_type="zone.entered",
                            key=position.key,
                            payload=position.model_dump(mode="json"),
                        )
                    )
            elif inside and was_inside:
                first_seen_at = int(entered_at.get(position.key) or now_ms)
                has_emitted = bool(emitted_enter.get(position.key, False))
                if not has_emitted and (now_ms - first_seen_at) >= min_dwell_ms:
                    emitted_enter[position.key] = True
                    events.append(
                        EventPayload(
                            event_type="zone.entered",
                            key=position.key,
                            payload=position.model_dump(mode="json"),
                        )
                    )
            elif was_inside and not inside:
                entered_at.pop(position.key, None)
                emitted_enter.pop(position.key, None)
                events.append(
                    EventPayload(
                        event_type="zone.exited",
                        key=position.key,
                        payload=position.model_dump(mode="json"),
                    )
                )
        return NodeExecutionResult(outputs={"events": EventsPayload(events=events)})


class OpticalFlowMotionHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        frame = _required_frame(node, inputs, "frame")
        image = _open_frame(frame, copy=False).convert("L")
        state = context.state.setdefault(node.node_key, {})
        previous = state.get("previous")
        state["previous"] = frame.image_base64_value()
        value = 0.0
        if previous:
            previous_image = Image.open(
                io.BytesIO(FramePayload(image_base64=previous).image_bytes())
            ).convert("L")
            previous_image = previous_image.resize(image.size)
            diff = ImageChops.difference(image, previous_image)
            value = round(float(ImageStat.Stat(diff).mean[0]) / 255.0, 6)
        return NodeExecutionResult(
            outputs={
                "motion": MeasurementsPayload(
                    measurements=[MeasurementPayload(key="motion", value=value, unit="normalized")]
                )
            }
        )


class DetectionSizeHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        detections = _detections_from_payload(inputs.get("detections"))
        unit = str((node.config or {}).get("unit") or "percent")
        measurements = []
        for index, detection in enumerate(detections, start=1):
            width = max(detection.box.x_max - detection.box.x_min, 0)
            height = max(detection.box.y_max - detection.box.y_min, 0)
            area = width * height
            measurements.append(
                MeasurementPayload(
                    key=f"size_{index}",
                    label=detection.label,
                    value={"width": width, "height": height, "area": area},
                    unit=unit,
                )
            )
        return NodeExecutionResult(
            outputs={"sizes": MeasurementsPayload(measurements=measurements)}
        )


class ObjectSpatialSignalHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        config = node.config or {}
        primary_payload = inputs.get("primary_objects") or inputs.get("objects")
        secondary_payload = inputs.get("secondary_objects")
        primary_objects = _spatial_objects_from_payload(primary_payload)
        secondary_objects = (
            _spatial_objects_from_payload(secondary_payload)
            if secondary_payload is not None
            else primary_objects
        )
        same_object_set = secondary_payload is None or secondary_payload is primary_payload
        zones = _named_zones_from_config(config)
        rules = _spatial_rules_from_config(config)
        state = context.state.setdefault(node.node_key, {})
        previous_pairs = list(state.get("previous_pairs") or [])
        next_pairs: list[dict[str, Any]] = []
        matches: list[dict[str, Any]] = []
        rule_results: list[dict[str, Any]] = []
        primitives: list[OverlayPrimitive] = []

        for rule_index, rule in enumerate(rules, start=1):
            rule_key = _spatial_rule_key(rule, rule_index)
            relation = str(rule.get("relation") or "distance")
            enabled = bool(rule.get("enabled", True))
            if not enabled:
                rule_results.append(
                    {
                        "rule_key": rule_key,
                        "relation": relation,
                        "active": False,
                        "enabled": False,
                        "match_count": 0,
                    }
                )
                continue

            if relation == "zone_occupancy":
                zone_matches, zone_primitives = _evaluate_spatial_zone_rule(
                    rule,
                    rule_key=rule_key,
                    objects=primary_objects,
                    zones=zones,
                )
                matches.extend(zone_matches)
                primitives.extend(zone_primitives)
                rule_results.append(
                    {
                        "rule_key": rule_key,
                        "relation": relation,
                        "active": bool(zone_matches),
                        "enabled": True,
                        "match_count": len(zone_matches),
                    }
                )
                continue

            pairs = _spatial_candidate_pairs(
                primary_objects,
                secondary_objects,
                same_object_set=same_object_set,
                rule=rule,
            )
            rule_matches: list[dict[str, Any]] = []
            for pair_index, (primary, secondary) in enumerate(pairs, start=1):
                distance_percent = _spatial_distance_percent(primary, secondary)
                pair_snapshot = _spatial_pair_snapshot(
                    rule_key,
                    primary,
                    secondary,
                    distance_percent,
                )
                if relation == "approaching":
                    next_pairs.append(pair_snapshot)
                    match = _evaluate_spatial_approach(
                        rule,
                        rule_key=rule_key,
                        pair_index=pair_index,
                        primary=primary,
                        secondary=secondary,
                        distance_percent=distance_percent,
                        previous_pairs=previous_pairs,
                    )
                elif relation == "overlap":
                    match = _evaluate_spatial_overlap(
                        rule,
                        rule_key=rule_key,
                        pair_index=pair_index,
                        primary=primary,
                        secondary=secondary,
                    )
                elif relation == "distance":
                    match = _evaluate_spatial_distance(
                        rule,
                        rule_key=rule_key,
                        pair_index=pair_index,
                        primary=primary,
                        secondary=secondary,
                        distance_percent=distance_percent,
                    )
                else:
                    match = None
                if match is None:
                    continue
                rule_matches.append(match)
                primitives.extend(_spatial_pair_overlay_primitives(match, primary, secondary))

            matches.extend(rule_matches)
            rule_results.append(
                {
                    "rule_key": rule_key,
                    "relation": relation,
                    "active": bool(rule_matches),
                    "enabled": True,
                    "candidate_pair_count": len(pairs),
                    "match_count": len(rule_matches),
                }
            )

        if next_pairs:
            state["previous_pairs"] = next_pairs
        elif any(str(rule.get("relation") or "") == "approaching" for rule in rules):
            state["previous_pairs"] = []

        enabled_rule_count = sum(1 for rule in rules if bool(rule.get("enabled", True)))
        active_rule_count = sum(1 for rule in rule_results if rule.get("active"))
        min_match_count = max(int(config.get("min_match_count") or 1), 1)
        if str(config.get("match_policy") or "any") == "all":
            signal = (
                enabled_rule_count > 0
                and active_rule_count == enabled_rule_count
                and len(matches) >= min_match_count
            )
        else:
            signal = len(matches) >= min_match_count
        result = {
            "signal": signal,
            "active": signal,
            "signal_key": str(config.get("signal_key") or node.node_key),
            "match_policy": str(config.get("match_policy") or "any"),
            "min_match_count": min_match_count,
            "enabled_rule_count": enabled_rule_count,
            "active_rule_count": active_rule_count,
            "match_count": len(matches),
            "rules": rule_results,
            "matches": matches,
        }
        return NodeExecutionResult(
            outputs={
                "signal": ResultPayload(result=result, decision=signal),
                "overlay": OverlayPayload(primitives=primitives),
            }
        )


class ZoneDetectionHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        config = node.config or {}
        regions = _regions_from_payload(inputs.get("objects"))
        objects = _spatial_objects_from_payload(inputs.get("objects"))
        zones = _named_zones_from_config(config)
        zone_match = str(config.get("zone_match") or "center_inside")
        rules = _zone_detection_rules_from_config(config)
        match_policy = str(config.get("match_policy") or "any")
        min_match_count = max(int(config.get("min_match_count") or 1), 1)

        object_zone_map: dict[str, list[dict[str, Any]]] = {}
        zone_summaries: list[dict[str, Any]] = []
        primitives: list[OverlayPrimitive] = []
        for zone in zones:
            zone_box = zone["box"]
            zone_objects = [
                item
                for item in objects
                if _spatial_object_in_zone(
                    item,
                    zone_box,
                    mode=zone_match,
                    min_overlap_percent=_optional_float(config.get("min_overlap_percent")),
                )
            ]
            class_counts: dict[str, int] = {}
            for item in zone_objects:
                label = item.label or "unknown"
                class_counts[label] = class_counts.get(label, 0) + 1
                object_zone_map.setdefault(item.key, []).append(
                    {
                        "key": zone["key"],
                        "label": zone["label"],
                        "match_mode": zone_match,
                    }
                )
            zone_summaries.append(
                {
                    "key": zone["key"],
                    "label": zone["label"],
                    "box": zone_box.model_dump(mode="json"),
                    "count": len(zone_objects),
                    "class_counts": dict(sorted(class_counts.items())),
                    "objects": [_spatial_object_summary(item) for item in zone_objects],
                }
            )
            primitives.append(_spatial_zone_primitive(zone, len(zone_objects)))

        enriched_regions = [
            region.model_copy(
                update={
                    "metadata": {
                        **region.metadata,
                        "zones": object_zone_map.get(
                            region.source_object_id or region.key or f"region_{index}",
                            [],
                        ),
                        "zone_status": (
                            "inside"
                            if object_zone_map.get(
                                region.source_object_id or region.key or f"region_{index}",
                                [],
                            )
                            else "outside"
                        ),
                    }
                }
            )
            for index, region in enumerate(regions, start=1)
        ]
        outside_objects = [
            item for item in objects if not object_zone_map.get(item.key)
        ]
        rule_results: list[dict[str, Any]] = []
        matches: list[dict[str, Any]] = []
        for rule_index, rule in enumerate(rules, start=1):
            rule_key = _spatial_rule_key(rule, rule_index)
            if not bool(rule.get("enabled", True)):
                rule_results.append(
                    {
                        "rule_key": rule_key,
                        "relation": "zone_occupancy",
                        "enabled": False,
                        "active": False,
                        "match_count": 0,
                    }
                )
                continue
            zone_matches, zone_primitives = _evaluate_spatial_zone_rule(
                rule,
                rule_key=rule_key,
                objects=objects,
                zones=zones,
            )
            matches.extend(zone_matches)
            primitives.extend(zone_primitives)
            rule_results.append(
                {
                    "rule_key": rule_key,
                    "relation": "zone_occupancy",
                    "enabled": True,
                    "active": bool(zone_matches),
                    "match_count": len(zone_matches),
                }
            )

        enabled_rule_count = sum(1 for rule in rule_results if rule.get("enabled", True))
        active_rule_count = sum(1 for rule in rule_results if rule.get("active"))
        if rule_results and match_policy == "all":
            signal = (
                enabled_rule_count > 0
                and active_rule_count == enabled_rule_count
                and len(matches) >= min_match_count
            )
        elif rule_results:
            signal = len(matches) >= min_match_count
        else:
            signal = any(zone["count"] > 0 for zone in zone_summaries)

        result = {
            "signal": signal,
            "active": signal,
            "signal_key": str(config.get("signal_key") or node.node_key),
            "zone_match": zone_match,
            "match_policy": match_policy,
            "min_match_count": min_match_count,
            "zone_count": len(zones),
            "inside_count": len(objects) - len(outside_objects),
            "outside_count": len(outside_objects),
            "zones": zone_summaries,
            "outside_objects": [_spatial_object_summary(item) for item in outside_objects],
            "match_count": len(matches),
            "rules": rule_results,
            "matches": matches,
        }
        return NodeExecutionResult(
            outputs={
                "objects": RegionSetPayload(regions=enriched_regions),
                "result": ResultPayload(result=result, decision=signal),
                "overlay": OverlayPayload(primitives=primitives),
            }
        )


class ColorObjectSegmentationHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        started = time.perf_counter()
        frame = _ensure_frame_context(context, node, _required_frame(node, inputs, "frame"))
        config = node.config or {}
        image = _cv_bgr_image(node, _decode_frame_cv2(node, frame))
        height, width = int(image.shape[0]), int(image.shape[1])
        blur_kernel = _validate_odd_kernel(
            config.get("blur_kernel_size"),
            "blur_kernel_size",
            node,
            allow_zero=True,
        )
        if blur_kernel > 1:
            cv2 = _require_cv2(node)
            image = cv2.GaussianBlur(image, (blur_kernel, blur_kernel), 0)
        profiles = _color_profiles_from_config(config)
        zones = _named_zones_from_config(config)
        zone_match = str(config.get("zone_match") or "box_overlaps")
        morphology_kernel = _validate_odd_kernel(
            config.get("morphology_kernel_size"),
            "morphology_kernel_size",
            node,
            allow_zero=True,
        )
        morphology_iterations = max(int(config.get("morphology_iterations") or 0), 0)
        polygon_epsilon_percent = max(
            _optional_float(config.get("polygon_epsilon_percent")) or 0.0,
            0.0,
        )
        contour_display_mode = str(config.get("contour_display_mode") or "oriented_box")
        if contour_display_mode not in {"oriented_box", "convex_hull", "raw_contour"}:
            contour_display_mode = "oriented_box"
        max_objects = max(int(config.get("max_objects") or 80), 1)
        exclusive_profile_matching = _config_bool(
            config.get("exclusive_profile_matching"),
            default=True,
        )
        split_touching_objects = _config_bool(
            config.get("split_touching_objects"),
            default=True,
        )
        split_distance_threshold = _clamp_float(
            _optional_float(config.get("split_distance_threshold")),
            0.05,
            0.95,
            0.38,
        )
        cv2 = _require_cv2(node)
        np = _require_numpy(node)
        assigned_mask = np.zeros((height, width), dtype=np.uint8)
        candidates: list[tuple[Region, Any]] = []
        profile_summaries: list[dict[str, Any]] = []
        for profile_index, profile in enumerate(profiles, start=1):
            profile_mask = _color_profile_mask(node, image, profile)
            raw_pixel_count = int(cv2.countNonZero(profile_mask))
            if exclusive_profile_matching:
                profile_mask = cv2.bitwise_and(profile_mask, cv2.bitwise_not(assigned_mask))
            if zones:
                profile_mask = _apply_cv_zone_mask(node, profile_mask, zones, width, height)
            profile_mask = _morphology_mask(
                node,
                profile_mask,
                kernel_size=morphology_kernel,
                iterations=morphology_iterations,
            )
            assigned_pixel_count = int(cv2.countNonZero(profile_mask))
            if exclusive_profile_matching:
                assigned_mask = cv2.bitwise_or(assigned_mask, profile_mask)
            contour_entries = _color_contours_from_mask(
                node,
                image,
                profile_mask,
                split_touching_objects=split_touching_objects,
                distance_threshold=split_distance_threshold,
            )
            accepted_count = 0
            for contour_index, (contour, contour_metadata) in enumerate(contour_entries, start=1):
                region = _color_region_from_contour(
                    context,
                    node,
                    profile,
                    contour,
                    image_context_id=_frame_context_id(frame),
                    width=width,
                    height=height,
                    contour_index=contour_index,
                    polygon_epsilon_percent=polygon_epsilon_percent,
                    contour_display_mode=contour_display_mode,
                    profile_index=profile_index,
                    contour_metadata=contour_metadata,
                )
                if region is None:
                    continue
                if zones and not any(
                    _region_in_zone(region, zone["box"], mode=zone_match) for zone in zones
                ):
                    continue
                candidates.append((region, contour))
                accepted_count += 1
            profile_summaries.append(
                {
                    "profile_index": profile_index,
                    "label": profile["label"],
                    "raw_pixel_count": raw_pixel_count,
                    "assigned_pixel_count": assigned_pixel_count,
                    "candidate_count": len(contour_entries),
                    "accepted_count": accepted_count,
                }
            )
        candidates.sort(
            key=lambda item: float(item[0].metadata.get("area_percent") or 0.0),
            reverse=True,
        )
        selected_candidates = candidates[:max_objects]
        regions = [region for region, _contour in selected_candidates]
        counts_by_label = _region_counts_by_label(regions)
        source_context_id = _frame_context_id(frame)
        overlay_mode = str(config.get("overlay_output_mode") or "objects")
        if overlay_mode not in {"objects", "mask", "both"}:
            overlay_mode = "objects"
        object_mask = _mask_from_contours(
            cv2,
            np,
            height,
            width,
            [contour for _region, contour in selected_candidates],
        )
        mask_frame = (
            _frame_from_cv_mask(
                object_mask,
                source=frame,
                context=context,
                node=node,
                operation="color_object_segmentation",
                metadata={
                    "operation": "color_object_segmentation",
                    "profile_count": len(profiles),
                    "object_count": len(regions),
                    "mask_source": "accepted_objects",
                },
            )
            if overlay_mode in {"mask", "both"}
            else None
        )
        primitives = _color_object_overlay_primitives(
            regions,
            zones,
            mask_frame=mask_frame,
            source_image_context_id=source_context_id,
            overlay_mode=overlay_mode,
        )
        timing_ms = round((time.perf_counter() - started) * 1000, 3)
        outputs: dict[str, Any] = {
            "objects": RegionSetPayload(
                regions=regions,
                metadata={
                    "source": "classic_cv",
                    "algorithm": "color_object_segmentation",
                    "timing_ms": timing_ms,
                    "image_context_id": source_context_id,
                    "image_width": width,
                    "image_height": height,
                    "profile_count": len(profiles),
                    "candidate_count": len(candidates),
                    "object_count": len(regions),
                    "count_total": len(regions),
                    "counts_by_label": counts_by_label,
                    "exclusive_profile_matching": exclusive_profile_matching,
                    "split_touching_objects": split_touching_objects,
                    "split_distance_threshold": split_distance_threshold,
                    "contour_display_mode": contour_display_mode,
                    "profiles": profile_summaries,
                },
            ),
            "overlay": OverlayPayload(primitives=primitives),
            "counts": MeasurementsPayload(
                measurements=_count_measurements_from_regions(
                    regions,
                    image_context_id=source_context_id,
                ),
                metadata={
                    "source": "classic_cv",
                    "algorithm": "color_object_segmentation",
                    "count_total": len(regions),
                    "counts_by_label": counts_by_label,
                },
            ),
        }
        if mask_frame is not None:
            outputs["mask"] = mask_frame
        return NodeExecutionResult(outputs=outputs)


class ObjectOrientationSignalHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        config = node.config or {}
        regions = _regions_from_payload(inputs.get("objects"))
        labels = _orientation_label_filters(config)
        zones = _named_zones_from_config(config)
        zone_match = str(config.get("zone_match") or "center_inside")
        selected_regions = [
            region
            for region in regions
            if _orientation_label_matches(region.label, labels)
            and (
                not zones
                or any(_region_in_zone(region, zone["box"], mode=zone_match) for zone in zones)
            )
        ]
        coordinate_frame = inputs.get("coordinate_frame")
        reference_angle = _orientation_reference_angle_degrees(
            config,
            coordinate_frame if isinstance(coordinate_frame, CoordinateFramePayload) else None,
        )
        tolerance = max(_optional_float(config.get("angle_tolerance_degrees")) or 0.0, 0.0)
        min_aspect_ratio = max(_optional_float(config.get("min_aspect_ratio")) or 1.0, 1.0)
        min_major_axis_percent = max(
            _optional_float(config.get("min_major_axis_percent")) or 0.0,
            0.0,
        )
        max_minor_axis_percent = _optional_float(config.get("max_minor_axis_percent"))
        minimum_count = max(int(config.get("minimum_object_count") or 0), 0)
        maximum_count = _optional_int(config.get("maximum_object_count"))
        inspected: list[dict[str, Any]] = []
        violations: list[dict[str, Any]] = []
        primitives = _orientation_zone_primitives(zones)
        for index, region in enumerate(selected_regions, start=1):
            geometry = _region_axis_geometry(region)
            deviation = _angle_deviation_degrees(geometry["orientation_degrees"], reference_angle)
            reasons: list[str] = []
            if deviation > tolerance:
                reasons.append("angle_deviation")
            if geometry["aspect_ratio"] < min_aspect_ratio:
                reasons.append("aspect_ratio")
            if geometry["major_axis_percent"] < min_major_axis_percent:
                reasons.append("major_axis_length")
            if (
                max_minor_axis_percent is not None
                and geometry["minor_axis_percent"] > max_minor_axis_percent
            ):
                reasons.append("minor_axis_width")
            item = {
                "object_key": region.source_object_id or region.key or f"object_{index}",
                "label": region.label,
                "orientation_degrees": round(geometry["orientation_degrees"], 4),
                "reference_angle_degrees": round(reference_angle, 4),
                "angle_deviation_degrees": round(deviation, 4),
                "aspect_ratio": round(geometry["aspect_ratio"], 4),
                "major_axis_percent": round(geometry["major_axis_percent"], 4),
                "minor_axis_percent": round(geometry["minor_axis_percent"], 4),
                "valid": not reasons,
                "reasons": reasons,
                "box": region.box.model_dump(mode="json"),
            }
            inspected.append(item)
            if reasons:
                violations.append(item)
            primitives.extend(
                _orientation_object_primitives(
                    region,
                    geometry=geometry,
                    valid=not reasons,
                    label=str(config.get("signal_label") or "Orientation"),
                )
            )
        count_violation = len(selected_regions) < minimum_count
        if maximum_count is not None and len(selected_regions) > maximum_count:
            count_violation = True
        violation_active = bool(violations) or count_violation
        signal = (
            violation_active
            if str(config.get("emit_signal_on") or "violation") == "violation"
            else not violation_active
        )
        primitives.append(
            _orientation_reference_primitive(config, reference_angle, selected_regions)
        )
        result = {
            "signal": signal,
            "active": signal,
            "status": "fail" if violation_active else "pass",
            "signal_key": str(config.get("signal_key") or node.node_key),
            "signal_label": str(config.get("signal_label") or "Object orientation"),
            "emit_signal_on": str(config.get("emit_signal_on") or "violation"),
            "reference_angle_degrees": round(reference_angle, 4),
            "angle_tolerance_degrees": tolerance,
            "inspected_count": len(selected_regions),
            "valid_count": len(selected_regions) - len(violations),
            "violation_count": len(violations),
            "minimum_object_count": minimum_count,
            "maximum_object_count": maximum_count,
            "count_violation": count_violation,
            "objects": inspected,
            "violations": violations,
        }
        return NodeExecutionResult(
            outputs={
                "signal": ResultPayload(result=result, decision=signal),
                "overlay": OverlayPayload(primitives=primitives),
            }
        )


class CannyEdgeDetectionHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        started = time.perf_counter()
        frame = _ensure_frame_context(context, node, _required_frame(node, inputs, "frame"))
        image = _decode_frame_cv2(node, frame)
        if image.ndim not in {2, 3}:
            raise _node_error(
                node, "image_format_unsupported", "Canny requires a grayscale or color image."
            )
        config = node.config or {}
        grayscale_mode = str(config.get("grayscale_conversion") or "auto")
        if grayscale_mode not in {"auto", "yes", "no"}:
            raise _node_error(
                node,
                "canny_grayscale_invalid",
                "Grayscale conversion must be automatic, yes, or no.",
            )
        gray = _canny_grayscale_image(node, image, grayscale_mode)
        blur_kernel = _validate_odd_kernel(
            config.get("blur_kernel_size"), "blur_kernel_size", node, allow_zero=True
        )
        if blur_kernel > 1:
            cv2 = _require_cv2(node)
            gray = cv2.GaussianBlur(gray, (blur_kernel, blur_kernel), 0)
        lower = _required_threshold(config.get("lower_threshold"), "lower_threshold", node)
        upper = _required_threshold(config.get("upper_threshold"), "upper_threshold", node)
        if lower >= upper:
            raise _node_error(
                node,
                "canny_threshold_order_invalid",
                "Canny lower_threshold must be lower than upper_threshold.",
                metadata={"lower_threshold": lower, "upper_threshold": upper},
            )
        aperture = _validate_aperture(config.get("aperture_size"), node)
        cv2 = _require_cv2(node)
        edge_mask = cv2.Canny(
            gray,
            threshold1=lower,
            threshold2=upper,
            apertureSize=aperture,
            L2gradient=bool(config.get("l2_gradient", False)),
        )
        mask_input = inputs.get("mask")
        if isinstance(mask_input, FramePayload):
            roi_mask = _decode_frame_cv2(node, mask_input, grayscale=True)
            if roi_mask.shape[:2] != edge_mask.shape[:2]:
                raise _node_error(
                    node,
                    "canny_mask_shape_mismatch",
                    "Optional Canny mask must have the same width and height as the input frame.",
                    metadata={
                        "frame_shape": list(edge_mask.shape[:2]),
                        "mask_shape": list(roi_mask.shape[:2]),
                    },
                )
            edge_mask = cv2.bitwise_and(edge_mask, edge_mask, mask=roi_mask)
        output_mode = str(config.get("output_mode") or "both")
        if output_mode not in {"edge_mask", "edge_contours", "both"}:
            raise _node_error(
                node,
                "canny_output_mode_invalid",
                "Canny output mode must be edge_mask, edge_contours, or both.",
            )
        width = int(edge_mask.shape[1])
        height = int(edge_mask.shape[0])
        edge_mask_frame = _frame_from_cv_mask(
            edge_mask,
            source=frame,
            context=context,
            node=node,
            metadata={
                "operation": "canny_edge_detection",
                "lower_threshold": lower,
                "upper_threshold": upper,
            },
        )
        contours = (
            _cv_contours_from_mask(context, node, edge_mask)
            if output_mode in {"edge_contours", "both"}
            else []
        )
        timing_ms = round((time.perf_counter() - started) * 1000, 3)
        source_context_id = _frame_context_id(frame)
        payload = CvEdgesPayload(
            image_context_id=_frame_context_id(edge_mask_frame)
            or source_context_id
            or "ctx_unknown",
            source_image_context_id=source_context_id,
            edge_mask_frame=edge_mask_frame,
            contours=contours,
            edge_pixel_count=int(cv2.countNonZero(edge_mask)),
            width=width,
            height=height,
            metadata={
                "timing_ms": timing_ms,
                "grayscale_conversion": grayscale_mode,
                "blur_kernel_size": blur_kernel,
                "lower_threshold": lower,
                "upper_threshold": upper,
                "aperture_size": aperture,
                "l2_gradient": bool(config.get("l2_gradient", False)),
                "output_mode": output_mode,
                "contour_count": len(contours),
            },
            provenance=[
                ProvenanceLink(
                    source_kind="image_context",
                    source_id=source_context_id or "unknown",
                    relationship="canny_edges_from_image_context",
                    node_key=node.node_key,
                )
            ],
        )
        return NodeExecutionResult(outputs={"edges": payload})


class ReferenceEdgeFromCvEdgesHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        started = time.perf_counter()
        payload = inputs.get("edges")
        if not isinstance(payload, CvEdgesPayload):
            raise _node_error(
                node, "cv_edges_missing", "Reference edge extraction requires Canny CV edge output."
            )
        if payload.edge_mask_frame is None:
            raise _node_error(
                node,
                "cv_edge_mask_missing",
                "Reference edge extraction requires an edge mask artifact.",
            )
        config = node.config or {}
        method = str(config.get("line_extraction_method") or "hough_line_segments")
        if method != "hough_line_segments":
            raise _node_error(
                node,
                "cv_line_method_unsupported",
                "Only OpenCV Hough line segments are currently supported.",
            )
        edge_mask = _decode_frame_cv2(node, payload.edge_mask_frame, grayscale=True)
        global_min_length = max(
            _required_positive_float(
                config.get("minimum_line_length_px"), "minimum_line_length_px", node
            ),
            1.0,
        )
        candidates = _hough_line_candidates(
            context,
            node,
            edge_mask,
            threshold=max(
                int(
                    _required_positive_float(config.get("hough_threshold"), "hough_threshold", node)
                ),
                1,
            ),
            min_line_length=global_min_length,
            max_line_gap=max(
                _required_positive_float(config.get("max_line_gap_px"), "max_line_gap_px", node),
                0.0,
            ),
        )
        rules = _cv_line_selection_rules(config)
        if not rules:
            raise _node_error(
                node,
                "cv_line_selection_rules_missing",
                "Reference Edge From CV Edges requires at least one line selection rule.",
            )
        selected_edges: list[ReferenceEdge] = []
        selected_candidate_ids: set[str] = set()
        missing_roles: list[str] = []
        for rule in rules:
            role = str(rule.get("edge_role") or "custom")
            required = bool(rule.get("required", role in {"start_edge", "side_edge"}))
            candidate = _select_cv_line_candidate(
                candidates,
                rule,
                image_width=payload.width,
                image_height=payload.height,
                selected_candidate_ids=selected_candidate_ids,
            )
            if candidate is None:
                if required:
                    missing_roles.append(role)
                continue
            selected_candidate_ids.add(candidate.line_id)
            selected_edges.append(
                ReferenceEdge(
                    edge_id=_stable_analysis_id(context, node, "edge", candidate.line_id, role),
                    image_context_id=payload.source_image_context_id or payload.image_context_id,
                    line=candidate.line,
                    confidence=candidate.score,
                    role=role,  # type: ignore[arg-type]
                    source={
                        "source": "classic_cv",
                        "source_node_key": node.node_key,
                        "edge_mask_context_id": payload.image_context_id,
                        "source_image_context_id": payload.source_image_context_id,
                        "candidate_line_id": candidate.line_id,
                        "line_extraction_method": method,
                        "selection_rule": rule,
                        "image_width": payload.width,
                        "image_height": payload.height,
                    },
                    provenance=[
                        ProvenanceLink(
                            source_kind="image_context",
                            source_id=payload.image_context_id,
                            relationship="reference_edge_selected_from_cv_edges",
                            node_key=node.node_key,
                        )
                    ],
                )
            )
        if missing_roles and str(config.get("fallback_behavior") or "fail") == "fail":
            raise _node_error(
                node,
                "required_cv_reference_edge_missing",
                "CV edge line selection could not find required reference edge roles.",
                metadata={
                    "missing_roles": missing_roles,
                    "candidate_count": len(candidates),
                    "candidate_lines": [
                        candidate.model_dump(mode="json") for candidate in candidates[:25]
                    ],
                },
            )
        timing_ms = round((time.perf_counter() - started) * 1000, 3)
        return NodeExecutionResult(
            outputs={
                "reference_edges": ReferenceEdgesPayload(
                    edges=selected_edges,
                    image_context_id=payload.source_image_context_id or payload.image_context_id,
                    metadata={
                        "source": "classic_cv",
                        "timing_ms": timing_ms,
                        "line_extraction_method": method,
                        "candidate_count": len(candidates),
                        "candidate_lines": [
                            candidate.model_dump(mode="json") for candidate in candidates[:50]
                        ],
                        "selected_line_ids": sorted(selected_candidate_ids),
                        "missing_roles": missing_roles,
                    },
                )
            }
        )


class ReferenceEdgeFromFeaturesHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        payload = inputs.get("features")
        regions = _regions_from_payload(payload)
        image_width, image_height = _image_dimensions_from_payload(payload)
        if image_width is None or image_height is None:
            raise _node_error(
                node,
                "image_dimensions_missing",
                "Reference edge extraction requires feature detections with source image dimensions.",
            )
        config = node.config or {}
        minimum_confidence = float(config.get("minimum_confidence") or 0.0)
        mappings = _edge_role_mappings(config)
        if not mappings:
            raise _node_error(
                node,
                "edge_role_mapping_missing",
                "Reference edge extraction requires at least one class-to-edge-role mapping.",
            )
        default_mode = str(config.get("line_extraction_mode") or "from_bbox_edge")
        default_bbox_side = str(config.get("bbox_side") or "longest_side")
        edges: list[ReferenceEdge] = []
        for index, region in enumerate(regions, start=1):
            if region.score is not None and region.score < minimum_confidence:
                continue
            mapping = mappings.get(str(region.label or ""))
            if mapping is None:
                continue
            mode = str(mapping.get("line_extraction_mode") or default_mode)
            bbox_side = str(mapping.get("bbox_side") or default_bbox_side)
            line = _line_from_region(
                region, image_width, image_height, mode=mode, bbox_side=bbox_side
            )
            if line is None or _line_length(line) <= 1e-6:
                raise _node_error(
                    node,
                    "reference_edge_degenerate",
                    f"Detected feature {region.label or region.key or index} cannot form a non-degenerate edge line.",
                )
            feature_id = region.source_object_id or region.key or f"feature_{index}"
            role = str(mapping.get("edge_role") or "custom")
            edges.append(
                ReferenceEdge(
                    edge_id=_stable_analysis_id(context, node, "edge", feature_id, role),
                    image_context_id=region.source_image_context_id
                    or _payload_image_context_id(payload),
                    line=line,
                    confidence=float(region.score if region.score is not None else 1.0),
                    role=role,  # type: ignore[arg-type]
                    source={
                        "feature_id": feature_id,
                        "feature_type": region.label,
                        "source_node_key": region.source_node_key,
                        "line_extraction_mode": mode,
                        "bbox_side": bbox_side,
                        "image_width": image_width,
                        "image_height": image_height,
                    },
                    provenance=[
                        ProvenanceLink(
                            source_kind="object",
                            source_id=feature_id,
                            relationship="edge_extracted_from_feature_detection",
                            node_key=node.node_key,
                        )
                    ],
                )
            )
        required_roles = _field_list(config.get("required_roles") or ["start_edge", "side_edge"])
        missing_roles = [
            role for role in required_roles if role not in {edge.role for edge in edges}
        ]
        if missing_roles:
            raise _node_error(
                node,
                "required_reference_edge_missing",
                "Feature detections did not produce required reference edge roles.",
                metadata={"missing_roles": missing_roles},
            )
        return NodeExecutionResult(
            outputs={
                "reference_edges": ReferenceEdgesPayload(
                    edges=edges,
                    image_context_id=edges[0].image_context_id if edges else None,
                )
            }
        )


class CoordinateFrameFromEdgesHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        payload = inputs.get("reference_edges")
        if not isinstance(payload, ReferenceEdgesPayload):
            raise _node_error(
                node,
                "reference_edges_missing",
                "Coordinate frame construction requires reference edges.",
            )
        config = node.config or {}
        start_role = str(config.get("start_edge_role") or "start_edge")
        direction_role = str(config.get("direction_edge_role") or "side_edge")
        start_edge = _edge_by_role(payload.edges, start_role)
        direction_edge = _edge_by_role(payload.edges, direction_role)
        if start_edge is None or direction_edge is None:
            raise _node_error(
                node,
                "coordinate_frame_edge_missing",
                "Coordinate frame construction requires configured start and direction edge roles.",
                metadata={"start_edge_role": start_role, "direction_edge_role": direction_role},
            )
        start_vec = _line_vector(start_edge.line)
        direction_vec = _line_vector(direction_edge.line)
        if _vector_length(start_vec) <= 1e-6 or _vector_length(direction_vec) <= 1e-6:
            raise _node_error(
                node,
                "coordinate_frame_degenerate_edge",
                "Reference edge lines must be non-degenerate.",
            )
        origin_rule = str(config.get("origin_rule") or "intersection_with_side_edge")
        origin = _coordinate_origin(start_edge, direction_edge, origin_rule, config)
        if origin is None:
            raise _node_error(
                node,
                "coordinate_frame_origin_failed",
                "Coordinate frame origin could not be computed from the selected edges.",
            )
        axis_rule = str(config.get("longitudinal_axis_rule") or "along_side_edge")
        if axis_rule == "perpendicular_to_start_edge":
            longitudinal = _normalize_vector((-start_vec[1], start_vec[0]))
            direction_unit = _normalize_vector(direction_vec)
            if _dot(longitudinal, direction_unit) < 0:
                longitudinal = (-longitudinal[0], -longitudinal[1])
        else:
            longitudinal = _normalize_vector(direction_vec)
        if str(config.get("axis_orientation") or "auto") == "invert":
            longitudinal = (-longitudinal[0], -longitudinal[1])
        if _vector_length(longitudinal) <= 1e-6:
            raise _node_error(
                node, "coordinate_frame_axis_failed", "Coordinate frame axis cannot be computed."
            )
        transverse = (-longitudinal[1], longitudinal[0])
        confidence = (
            min(start_edge.confidence, direction_edge.confidence)
            if str(config.get("confidence_aggregation") or "min") == "min"
            else (start_edge.confidence + direction_edge.confidence) / 2
        )
        image_width = _first_number(
            start_edge.source.get("image_width"), direction_edge.source.get("image_width")
        )
        image_height = _first_number(
            start_edge.source.get("image_height"), direction_edge.source.get("image_height")
        )
        frame = InspectionCoordinateFrame(
            frame_id=_stable_analysis_id(
                context, node, "frame", start_edge.edge_id, direction_edge.edge_id
            ),
            image_context_id=start_edge.image_context_id,
            origin=InspectionPoint(x=origin[0], y=origin[1]),
            longitudinal_axis=InspectionVector(x=longitudinal[0], y=longitudinal[1]),
            transverse_axis=InspectionVector(x=transverse[0], y=transverse[1]),
            confidence=max(0.0, min(float(confidence), 1.0)),
            source={
                "start_edge_id": start_edge.edge_id,
                "direction_edge_id": direction_edge.edge_id,
                "origin_rule": origin_rule,
                "longitudinal_axis_rule": axis_rule,
            },
            provenance=[
                ProvenanceLink(
                    source_kind="node",
                    source_id=node.node_key,
                    relationship="coordinate_frame_from_reference_edges",
                    node_key=node.node_key,
                )
            ],
            inspected_object_id=str(config.get("inspected_object_id") or "") or None,
            camera_id=str(config.get("camera_id") or "") or None,
            view_id=str(config.get("view_id") or "") or None,
            metadata={
                "image_width": image_width,
                "image_height": image_height,
            },
        )
        return NodeExecutionResult(
            outputs={"coordinate_frame": CoordinateFramePayload(frame=frame)}
        )


class FeatureMeasurementHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        payload = inputs.get("features")
        regions = _regions_from_payload(payload)
        image_width, image_height = _image_dimensions_from_payload(payload)
        if image_width is None or image_height is None:
            raise _node_error(
                node,
                "image_dimensions_missing",
                "Feature measurement requires feature detections with source image dimensions.",
            )
        frame_payload = inputs.get("coordinate_frame")
        coordinate_frame = (
            frame_payload.frame if isinstance(frame_payload, CoordinateFramePayload) else None
        )
        config = node.config or {}
        rule_map = _measurement_rule_map(config)
        fallback_behavior = str(config.get("fallback_behavior") or "use_bbox")
        mm_per_pixel = _positive_optional_float(config.get("mm_per_pixel"))
        measurements: list[FeatureMeasurement] = []
        features: list[dict[str, Any]] = []
        for index, region in enumerate(regions, start=1):
            feature_type = str(region.label or "unknown")
            feature_id = region.source_object_id or region.key or f"feature_{index}"
            bases = rule_map.get(feature_type) or rule_map.get("*")
            if not bases:
                if fallback_behavior == "skip":
                    continue
                if fallback_behavior == "fail":
                    raise _node_error(
                        node,
                        "measurement_rule_missing",
                        f"No measurement rule exists for feature type {feature_type}.",
                    )
                bases = ["bbox_width", "bbox_height", "approximate_diameter"]
            bbox = _box_to_pixel_geometry(region.box, image_width, image_height)
            axis_projection = (
                _axis_projection_metadata(bbox["corners"], coordinate_frame)
                if coordinate_frame is not None
                else {}
            )
            feature_metadata = {
                "feature_id": feature_id,
                "feature_type": feature_type,
                "confidence": region.score,
                "bbox_px": bbox,
                "axis_projection": axis_projection,
                "source_image_context_id": region.source_image_context_id
                or _payload_image_context_id(payload),
                "source_object_id": region.source_object_id,
            }
            features.append(feature_metadata)
            for basis in bases:
                value = _measurement_value_for_basis(
                    basis,
                    bbox=bbox,
                    region=region,
                    coordinate_frame=coordinate_frame,
                    axis_projection=axis_projection,
                )
                if value is None:
                    if fallback_behavior == "fail":
                        raise _node_error(
                            node,
                            "measurement_basis_unavailable",
                            f"Feature {feature_type} does not have geometry for {basis}.",
                        )
                    continue
                measurements.append(
                    FeatureMeasurement(
                        feature_id=feature_id,
                        feature_type=feature_type,
                        measurement_name=str(basis),
                        value_px=round(float(value), 6),
                        value_mm=round(float(value) * mm_per_pixel, 6) if mm_per_pixel else None,
                        basis=str(basis),  # type: ignore[arg-type]
                        method_metadata={
                            **feature_metadata,
                            "basis": basis,
                            "method": "feature_detection_geometry",
                        },
                        confidence=float(region.score if region.score is not None else 1.0),
                        source_image_context_id=feature_metadata["source_image_context_id"],
                        source_object_id=region.source_object_id,
                        provenance=[
                            ProvenanceLink(
                                source_kind="object",
                                source_id=feature_id,
                                relationship="measured_from_feature_detection",
                                node_key=node.node_key,
                            )
                        ],
                    )
                )
        return NodeExecutionResult(
            outputs={
                "feature_measurements": FeatureMeasurementsPayload(
                    measurements=measurements,
                    features=features,
                    image_context_id=_payload_image_context_id(payload),
                )
            }
        )


class ClearanceZoneGeneratorHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        measurements_payload = inputs.get("measurements")
        frame_payload = inputs.get("coordinate_frame")
        if not isinstance(measurements_payload, FeatureMeasurementsPayload):
            raise _node_error(
                node, "measurements_missing", "Clearance zones require feature measurements."
            )
        if not isinstance(frame_payload, CoordinateFramePayload):
            raise _node_error(
                node,
                "coordinate_frame_missing",
                "Clearance zones require an inspection coordinate frame.",
            )
        config = node.config or {}
        rules = _clearance_rules(config)
        if not rules:
            raise _node_error(
                node, "clearance_rules_missing", "At least one clearance rule is required."
            )
        measurement_index: dict[tuple[str, str], FeatureMeasurement] = {
            (measurement.feature_id, measurement.basis): measurement
            for measurement in measurements_payload.measurements
        }
        zones: list[ClearanceZone] = []
        confidence_threshold = float(config.get("feature_confidence_threshold") or 0.0)
        for rule in rules:
            if not bool(rule.get("enabled", True)):
                continue
            feature_type = str(rule.get("feature_type") or "").strip()
            basis = str(rule.get("measurement_basis") or "").strip()
            if not feature_type or not basis:
                continue
            matching_feature_ids = {
                measurement.feature_id
                for measurement in measurements_payload.measurements
                if measurement.feature_type == feature_type
            }
            for feature_id in sorted(matching_feature_ids):
                measurement = measurement_index.get((feature_id, basis))
                if measurement is None:
                    raise _node_error(
                        node,
                        "clearance_measurement_missing",
                        f"Clearance rule for {feature_type} requires measurement basis {basis}, but it is not available.",
                        metadata={"feature_type": feature_type, "measurement_basis": basis},
                    )
                if measurement.confidence < confidence_threshold:
                    continue
                axis_projection = measurement.method_metadata.get("axis_projection")
                if not isinstance(axis_projection, dict):
                    raise _node_error(
                        node,
                        "clearance_axis_projection_missing",
                        "Clearance zone generation requires axis-projected feature metadata.",
                    )
                feature_start = _required_number(axis_projection.get("longitudinal_start_px"))
                feature_end = _required_number(axis_projection.get("longitudinal_end_px"))
                clearance = float(measurement.value_px) * float(rule.get("multiplier") or 0.0)
                clearance = max(clearance, float(rule.get("minimum_clearance_px") or 0.0))
                maximum = _positive_optional_float(rule.get("maximum_clearance_px"))
                if maximum is not None:
                    clearance = min(clearance, maximum)
                apply_before = bool(rule.get("apply_before", True))
                apply_after = bool(rule.get("apply_after", True))
                start = feature_start - clearance if apply_before else feature_start
                end = feature_end + clearance if apply_after else feature_end
                zones.append(
                    ClearanceZone(
                        zone_id=_stable_analysis_id(
                            context, node, "zone", feature_id, basis, start, end
                        ),
                        feature_id=feature_id,
                        feature_type=feature_type,
                        interval_start_px=round(min(start, end), 6),
                        interval_end_px=round(max(start, end), 6),
                        clearance_px=round(clearance, 6),
                        rule_metadata={
                            "measurement_basis": basis,
                            "multiplier": float(rule.get("multiplier") or 0.0),
                            "apply_before": apply_before,
                            "apply_after": apply_after,
                            "raw_feature_interval_px": [feature_start, feature_end],
                            "source_measurement": measurement.model_dump(mode="json"),
                        },
                        severity=str(rule.get("severity") or "info"),
                        category=str(rule.get("category") or "clearance"),
                    )
                )
        if any(bool(rule.get("merge_overlapping_zones", False)) for rule in rules):
            zones = _merge_clearance_zones(context, node, zones)
        return NodeExecutionResult(
            outputs={
                "clearance_zones": ClearanceZonesPayload(
                    zones=zones,
                    image_context_id=frame_payload.frame.image_context_id,
                    coordinate_frame_id=frame_payload.frame.frame_id,
                )
            }
        )


class FirstFreeRegionFinderHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        frame_payload = inputs.get("coordinate_frame")
        zones_payload = inputs.get("clearance_zones")
        if not isinstance(frame_payload, CoordinateFramePayload):
            raise _node_error(
                node,
                "coordinate_frame_missing",
                "First free region search requires a coordinate frame.",
            )
        if not isinstance(zones_payload, ClearanceZonesPayload):
            raise _node_error(
                node,
                "clearance_zones_missing",
                "First free region search requires clearance zones.",
            )
        config = node.config or {}
        mm_per_pixel = _positive_optional_float(config.get("mm_per_pixel"))
        start_offset = _distance_config_px(
            config, "start_offset", "start_offset_px", "start_offset_mm", mm_per_pixel, node
        )
        minimum_length = _distance_config_px(
            config,
            "minimum_free_length",
            "minimum_free_length_px",
            "minimum_free_length_mm",
            mm_per_pixel,
            node,
        )
        end_limit = _optional_distance_config_px(
            config, "end_limit_px", "end_limit_mm", mm_per_pixel, node
        )
        require_end_limit = bool(config.get("treat_outside_known_board_region_as_invalid", False))
        direction = str(config.get("search_direction") or "positive_longitudinal")
        intervals = [(zone.interval_start_px, zone.interval_end_px) for zone in zones_payload.zones]
        if direction == "negative_longitudinal":
            intervals = [(-end, -start) for start, end in intervals]
        if bool(config.get("merge_blocked_intervals", True)):
            intervals = _merge_intervals(intervals)
        else:
            intervals = sorted((min(start, end), max(start, end)) for start, end in intervals)
        free = _find_first_free_interval(
            intervals,
            start_offset=start_offset,
            minimum_length=minimum_length,
            end_limit=end_limit,
            require_end_limit=require_end_limit,
        )
        if direction == "negative_longitudinal" and free.found:
            start = -float(free.start_position_px or 0.0)
            end = -free.end_position_px if free.end_position_px is not None else None
            free = free.model_copy(
                update={
                    "start_position_px": start,
                    "end_position_px": end,
                    "distance_from_origin_px": abs(start),
                }
            )
        if mm_per_pixel and free.found:
            free = _metric_free_region(free, mm_per_pixel)
        return NodeExecutionResult(
            outputs={
                "free_region": FreeRegionPayload(
                    free_region=free,
                    image_context_id=frame_payload.frame.image_context_id,
                    coordinate_frame_id=frame_payload.frame.frame_id,
                )
            }
        )


class PixelToMetricHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        mm_per_pixel = _positive_optional_float((node.config or {}).get("mm_per_pixel"))
        if mm_per_pixel is None:
            raise _node_error(
                node, "metric_calibration_invalid", "mm_per_pixel must be a positive number."
            )
        apply_to = set(_field_list((node.config or {}).get("apply_to")))
        measurements = inputs.get("measurements")
        zones = inputs.get("clearance_zones")
        free = inputs.get("free_region")
        frame = inputs.get("coordinate_frame")
        edges = inputs.get("reference_edges")
        decision = inputs.get("decision")
        outputs: dict[str, Any] = {}
        metric_measurements = measurements
        metric_zones = zones
        metric_free = free
        if isinstance(measurements, FeatureMeasurementsPayload) and (
            not apply_to or "feature_measurements" in apply_to
        ):
            metric_measurements = _metric_measurements(measurements, mm_per_pixel)
            outputs["feature_measurements"] = metric_measurements
        if isinstance(zones, ClearanceZonesPayload) and (
            not apply_to or "clearance_zones" in apply_to
        ):
            metric_zones = _metric_clearance_zones(zones, mm_per_pixel)
            outputs["clearance_zones"] = metric_zones
        if isinstance(free, FreeRegionPayload) and (not apply_to or "free_region" in apply_to):
            metric_free = FreeRegionPayload(
                free_region=_metric_free_region(free.free_region, mm_per_pixel),
                image_context_id=free.image_context_id,
                coordinate_frame_id=free.coordinate_frame_id,
            )
            outputs["free_region"] = metric_free
        result = _inspection_result_payload(
            mm_per_pixel=mm_per_pixel,
            measurements=metric_measurements
            if isinstance(metric_measurements, FeatureMeasurementsPayload)
            else None,
            zones=metric_zones if isinstance(metric_zones, ClearanceZonesPayload) else None,
            free=metric_free if isinstance(metric_free, FreeRegionPayload) else None,
            frame=frame if isinstance(frame, CoordinateFramePayload) else None,
            edges=edges if isinstance(edges, ReferenceEdgesPayload) else None,
            decision=decision if isinstance(decision, QualityDecisionPayload) else None,
        )
        outputs["result"] = result
        return NodeExecutionResult(outputs=outputs)


class QualityRuleEvaluationHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        measurements = inputs.get("measurements")
        free = inputs.get("free_region")
        if not isinstance(measurements, FeatureMeasurementsPayload) and not isinstance(
            free, FreeRegionPayload
        ):
            raise _node_error(
                node,
                "quality_rule_inputs_missing",
                "Quality rule evaluation requires measurements or a free-region result.",
            )
        rules = _quality_rules(node.config or {})
        evaluated: list[dict[str, Any]] = []
        messages: list[str] = []
        status: str = "pass"
        for rule in rules:
            outcome = _evaluate_quality_rule(rule, measurements, free)
            evaluated.append(outcome)
            if not outcome["passed"]:
                messages.append(str(outcome["message"]))
                fail_status = str(rule.get("status_on_fail") or "fail")
                if fail_status == "fail":
                    status = "fail"
                elif status != "fail":
                    status = "warning"
        label = str((node.config or {}).get("result_label") or "Measurement decision")
        selected = {
            "status": status,
            "rule_count": len(evaluated),
            "failed_rule_count": sum(1 for item in evaluated if not item["passed"]),
        }
        if isinstance(free, FreeRegionPayload):
            selected["free_region_distance_px"] = free.free_region.distance_from_origin_px
            selected["free_region_distance_mm"] = free.free_region.distance_from_origin_mm
        decision = MeasurementDecision(
            status=status,  # type: ignore[arg-type]
            result_label=label,
            evaluated_rules=evaluated,
            messages=messages,
            selected_output_values=selected,
        )
        result = ResultPayload(
            result={
                "status": decision.status,
                "result_label": decision.result_label,
                "result": decision.status == "pass",
                "evaluated_rules": decision.evaluated_rules,
                "messages": decision.messages,
                **decision.selected_output_values,
                "decision": decision.model_dump(mode="json"),
            },
            decision=decision.status == "pass",
        )
        return NodeExecutionResult(
            outputs={
                "decision": QualityDecisionPayload(decision=decision),
                "result": result,
            }
        )


class ObjectCountingHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        regions = _regions_from_payload(inputs.get("objects"))
        group_by = str((node.config or {}).get("group_by") or "label")
        counts: dict[str, int] = {}
        if group_by == "total":
            counts["total"] = len(regions)
        elif group_by == "zone":
            zones = _named_zones_from_config(node.config or {})
            for region in regions:
                zone_label = _match_zone_label(region.box, zones)
                key = zone_label or "outside"
                counts[key] = counts.get(key, 0) + 1
        else:
            for region in regions:
                key = region.label or "unknown"
                counts[key] = counts.get(key, 0) + 1
        measurements = [
            MeasurementPayload(key=f"count_{key}", label=key, value=value, unit="objects")
            for key, value in sorted(counts.items())
        ]
        return NodeExecutionResult(
            outputs={"counts": MeasurementsPayload(measurements=measurements)}
        )


class MovementDirectionHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        positions = _positions_from_payload(inputs.get("motion"))
        smoothing = min(max(float((node.config or {}).get("smoothing") or 0.0), 0.0), 1.0)
        state = context.state.setdefault(node.node_key, {})
        previous_positions = state.setdefault("previous_positions", {})
        smoothed_vectors = state.setdefault("smoothed_vectors", {})
        measurements = []
        for position in positions:
            previous = previous_positions.get(position.key)
            previous_positions[position.key] = {"x": position.x, "y": position.y}
            direction = "unknown"
            magnitude = 0.0
            if previous:
                dx = position.x - float(previous["x"])
                dy = position.y - float(previous["y"])
                previous_vector = smoothed_vectors.get(position.key, {"dx": dx, "dy": dy})
                dx = (1.0 - smoothing) * dx + smoothing * float(previous_vector["dx"])
                dy = (1.0 - smoothing) * dy + smoothing * float(previous_vector["dy"])
                smoothed_vectors[position.key] = {"dx": dx, "dy": dy}
                magnitude = round(math.sqrt(dx * dx + dy * dy), 6)
                direction = _direction_from_delta(dx, dy)
            measurements.append(
                MeasurementPayload(
                    key=f"direction_{position.key}",
                    label=position.label,
                    value={"direction": direction, "magnitude": magnitude},
                    unit="normalized_delta",
                )
            )
        return NodeExecutionResult(
            outputs={"direction": MeasurementsPayload(measurements=measurements)}
        )


class RuleEvaluationHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        payload = inputs.get("result")
        expression = str((node.config or {}).get("expression") or "pass").strip()
        result = _result_from_payload(payload).result
        decision = _evaluate_condition(expression, result)
        result.setdefault("result", decision)
        result["rule_expression"] = expression
        return NodeExecutionResult(
            outputs={"result": ResultPayload(result=result, decision=decision)}
        )


class CloseDetectionHandler(ObjectSpatialSignalHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        result = super().execute(context, node, inputs)
        outputs = dict(result.outputs)
        if "signal" in outputs:
            outputs["result"] = outputs.pop("signal")
        return NodeExecutionResult(outputs=outputs)


class FullFrameCaptureHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        frame = _required_frame(node, inputs, "frame")
        projects = _project_ids(node)
        eligibility = inputs.get("eligibility")
        require_eligibility = bool((node.config or {}).get("require_eligibility", False))
        eligibility_result = (
            dict(eligibility.result) if isinstance(eligibility, ResultPayload) else None
        )
        if require_eligibility and eligibility_result is None:
            return NodeExecutionResult(outputs={"capture": CaptureIntentsPayload(intents=[])})
        if eligibility_result is not None and not bool(
            eligibility_result.get("eligible", eligibility_result.get("result", False))
        ):
            return NodeExecutionResult(outputs={"capture": CaptureIntentsPayload(intents=[])})
        if (
            eligibility_result is not None
            and eligibility_result.get("image_context_id") is not None
        ):
            if (
                frame.image_context is None
                or eligibility_result["image_context_id"] != frame.image_context.context_id
            ):
                return NodeExecutionResult(outputs={"capture": CaptureIntentsPayload(intents=[])})
        view_metadata = {
            key: frame.metadata.get(key)
            for key in (
                "view_profile_id",
                "view_profile_checksum",
                "view_profile_schema_version",
                "image_view_preprocessing_version",
                "undistort_preprocessing_version",
                "color_space",
                "camera_calibration_id",
                "process_calibration_id",
                "process_calibration_hash",
                "source_frame_context_id",
                "source_frame_timestamp",
            )
            if frame.metadata.get(key) is not None
        }
        return NodeExecutionResult(
            outputs={
                "capture": CaptureIntentsPayload(
                    intents=[
                        CaptureIntent(
                            capture_mode="full_frame",
                            target_project_ids=projects,
                            asset=frame.model_dump(mode="json"),
                            metadata={
                                "node_key": node.node_key,
                                "capture_source": "PIPELINE_IMAGE_OUTPUT",
                                "frame_eligibility": eligibility_result or {},
                                **view_metadata,
                            },
                        )
                    ]
                )
            }
        )


class RoiCaptureHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        rois = _rois_from_payload(inputs.get("rois"))
        if not rois and isinstance(inputs.get("frame"), FramePayload):
            frame = _ensure_frame_context(context, node, inputs["frame"])
            image = _open_frame(frame)
            rois = []
            regions = _regions_from_payload(inputs.get("detections"))
            for index, region in enumerate(regions, start=1):
                source_object_id = region.source_object_id or region.key or f"detection_{index}"
                roi_context = _derived_image_context(
                    context,
                    node,
                    source=frame,
                    kind="roi",
                    transform_type="roi_crop",
                    parameters={
                        "box": region.box.model_dump(mode="json"),
                        "source_region_key": region.key,
                    },
                    coordinate_projection="crop_offset",
                    source_object_id=source_object_id,
                )
                roi_frame = _frame_from_image(
                    _crop_normalized(image, region.box),
                    source=frame,
                    metadata={"operation": "detection_roi_capture"},
                    image_context=roi_context,
                )
                rois.append(
                    RoiPayload(
                        roi_key=region.key or f"detection_{index}",
                        frame=roi_frame,
                        box=region.box,
                        label=region.label,
                        source_region_key=region.key or f"detection_{index}",
                        source_image_context_id=_frame_context_id(frame),
                        source_object_id=source_object_id,
                        image_context=roi_context,
                        provenance=_roi_provenance(frame, source_object_id, node.node_key),
                        metadata={"score": region.score, "source": "detection_derived_roi"},
                    )
                )
        projects = _project_ids(node)
        return NodeExecutionResult(
            outputs={
                "capture": CaptureIntentsPayload(
                    intents=[
                        CaptureIntent(
                            capture_mode="roi",
                            target_project_ids=projects,
                            asset=roi.model_dump(mode="json"),
                            metadata={"node_key": node.node_key},
                        )
                        for roi in rois
                    ]
                )
            }
        )


class ConditionalCaptureHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        result = _result_from_payload(inputs.get("result"))
        condition = str((node.config or {}).get("condition") or "result == true")
        if not _evaluate_condition(condition, result.result):
            return NodeExecutionResult(outputs={"capture": CaptureIntentsPayload(intents=[])})
        frame = inputs.get("frame")
        asset = (
            frame.model_dump(mode="json")
            if isinstance(frame, FramePayload)
            else result.model_dump(mode="json")
        )
        return NodeExecutionResult(
            outputs={
                "capture": CaptureIntentsPayload(
                    intents=[
                        CaptureIntent(
                            capture_mode="conditional",
                            target_project_ids=_project_ids(node),
                            asset=asset,
                            condition=condition,
                            metadata={"node_key": node.node_key},
                        )
                    ]
                )
            }
        )


class MultiProjectCaptureHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        asset = inputs.get("asset")
        projects = _project_ids(node)
        return NodeExecutionResult(
            outputs={
                "capture": CaptureIntentsPayload(
                    intents=[
                        CaptureIntent(
                            capture_mode="multi_project",
                            target_project_ids=projects,
                            asset=asset.model_dump(mode="json") if asset else {},
                            metadata={"node_key": node.node_key},
                        )
                    ]
                )
            }
        )


class OverlayOutputHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        payload = inputs.get("result")
        display_context_id = str((node.config or {}).get("display_context_id") or "") or None
        layer_filter = _overlay_layer_filter(node.config or {})
        if isinstance(payload, OverlayPayload):
            return NodeExecutionResult(
                outputs={
                    "overlay": OverlayPayload(
                        primitives=_filter_overlay_primitives(
                            list(payload.primitives),
                            layer_filter,
                        ),
                        display_context_id=display_context_id or payload.display_context_id,
                        projection_policy=payload.projection_policy,
                        metadata={
                            **payload.metadata,
                            "source_overlay_passthrough": True,
                        },
                    )
                }
            )
        primitives = _overlay_primitives(payload)
        primitives = _filter_overlay_primitives(primitives, layer_filter)
        return NodeExecutionResult(
            outputs={
                "overlay": OverlayPayload(
                    primitives=primitives,
                    display_context_id=display_context_id,
                    metadata={
                        "source_payload_type": getattr(payload, "payload_type", None),
                        "configured_layer_filter": sorted(layer_filter),
                    },
                )
            }
        )


class DisplayOutputHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        frame = _ensure_frame_context(context, node, _required_frame(node, inputs, "frame"))
        image_context = frame.image_context
        if image_context is None:
            raise _node_error(
                node,
                "display_context_missing",
                "Display output requires a frame with an image context.",
            )
        return NodeExecutionResult(
            outputs={
                "display": DisplayOutputPayload(
                    display_context_id=image_context.context_id,
                    display_context=image_context,
                    frame=frame,
                    layer_policy={
                        "display_name": str((node.config or {}).get("display_name") or "Live view"),
                        "show_enriched_labels": bool(
                            (node.config or {}).get("show_enriched_labels", True)
                        ),
                    },
                    object_layer_keys=_field_list((node.config or {}).get("object_layers")),
                    overlay_layer_keys=_field_list((node.config or {}).get("overlay_layers")),
                    render_enriched_labels=bool(
                        (node.config or {}).get("show_enriched_labels", True)
                    ),
                )
            }
        )


class HttpTriggerResponseOutputHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        result = _result_from_payload(inputs.get("result"))
        config = dict(node.config or {})
        config["include_decision"] = bool(config.get("include_decision", False))
        response = _mapped_result_output(result, config)
        return NodeExecutionResult(
            outputs={"response": ResultPayload(result=response, decision=result.decision)}
        )


class ResultOutputHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        result = _result_from_payload(inputs.get("result"))
        payload = _mapped_result_output(result, node.config or {})
        if bool((node.config or {}).get("include_runtime_metadata", True)):
            payload.setdefault("correlation_id", context.correlation_id)
            payload.setdefault("channel_id", context.channel_id)
            payload.setdefault("pipeline_version_id", context.pipeline_version_id)
        return NodeExecutionResult(
            outputs={"result": ResultPayload(result=payload, decision=result.decision)}
        )


class PersistedResultOutputHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        result = _result_from_payload(inputs.get("result"))
        payload = dict(result.result)
        payload.setdefault("correlation_id", context.correlation_id)
        payload.setdefault("channel_id", context.channel_id)
        payload.setdefault("pipeline_version_id", context.pipeline_version_id)
        return NodeExecutionResult(
            outputs={"persisted": ResultPayload(result=payload, decision=result.decision)}
        )


class InternalEventOutputHandler(BuiltinHandler):
    def execute(
        self, context: RuntimeExecutionContext, node: RuntimeNodeSpec, inputs: NodeInputs
    ) -> NodeExecutionResult:
        event_type = str((node.config or {}).get("event_type") or "runtime.pipeline.result.v1")
        result = _result_from_payload(inputs.get("result"))
        return NodeExecutionResult(
            outputs={
                "event": EventsPayload(
                    events=[
                        EventPayload(
                            event_type=event_type,
                            key=context.correlation_id,
                            payload=result.result,
                        )
                    ]
                )
            }
        )


def _stable_analysis_id(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    prefix: str,
    *parts: object,
) -> str:
    raw = "|".join(str(part) for part in (context.correlation_id, node.node_key, prefix, *parts))
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _payload_image_context_id(payload: object) -> str:
    if isinstance(payload, DetectionPayload | SegmentPayload):
        return payload.image_context_id or "ctx_unknown"
    if isinstance(payload, RegionSetPayload):
        return next(
            (
                region.source_image_context_id
                for region in payload.regions
                if region.source_image_context_id
            ),
            "ctx_unknown",
        )
    return "ctx_unknown"


def _image_dimensions_from_payload(payload: object) -> tuple[int | None, int | None]:
    if isinstance(payload, DetectionPayload):
        return payload.image_width, payload.image_height
    if isinstance(payload, SegmentPayload):
        return payload.image_width, payload.image_height
    if isinstance(payload, RegionSetPayload):
        for region in payload.regions:
            width = region.metadata.get("image_width")
            height = region.metadata.get("image_height")
            if isinstance(width, int) and isinstance(height, int):
                return width, height
    return None, None


def _frame_width(frame: FramePayload) -> int | None:
    if frame.width is not None:
        return frame.width
    if frame.image_context is not None and frame.image_context.width is not None:
        return frame.image_context.width
    try:
        return _open_frame(frame).width
    except Exception:
        return None


def _frame_height(frame: FramePayload) -> int | None:
    if frame.height is not None:
        return frame.height
    if frame.image_context is not None and frame.image_context.height is not None:
        return frame.image_context.height
    try:
        return _open_frame(frame).height
    except Exception:
        return None


def _require_cv2(node: RuntimeNodeSpec):
    try:
        import cv2  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - exercised only when dependency is missing
        raise _node_error(
            node,
            "opencv_unavailable",
            "This Computer Vision node requires OpenCV (cv2) to be installed.",
            metadata={"error": str(exc)},
        ) from exc
    return cv2


def _require_numpy(node: RuntimeNodeSpec):
    try:
        import numpy as np  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - exercised only when dependency is missing
        raise _node_error(
            node,
            "numpy_unavailable",
            "This Computer Vision node requires NumPy to be installed.",
            metadata={"error": str(exc)},
        ) from exc
    return np


def _decode_frame_cv2(node: RuntimeNodeSpec, frame: FramePayload, *, grayscale: bool = False):
    cv2 = _require_cv2(node)
    np = _require_numpy(node)
    encoded = np.frombuffer(frame.image_bytes(), dtype=np.uint8)
    flag = cv2.IMREAD_GRAYSCALE if grayscale else cv2.IMREAD_UNCHANGED
    decoded = cv2.imdecode(encoded, flag)
    if decoded is None:
        raise _node_error(
            node, "image_decode_failed", "Computer Vision node could not decode the input image."
        )
    return decoded


def _cv_bgr_image(node: RuntimeNodeSpec, image: Any):
    cv2 = _require_cv2(node)
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.ndim != 3:
        raise _node_error(
            node,
            "image_format_unsupported",
            "Color object segmentation requires a grayscale or color image.",
        )
    channel_count = int(image.shape[2])
    if channel_count == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    if channel_count == 3:
        return image
    if channel_count == 1:
        return cv2.cvtColor(image[:, :, 0], cv2.COLOR_GRAY2BGR)
    raise _node_error(
        node,
        "image_format_unsupported",
        "Color object segmentation requires one, three, or four image channels.",
    )


def _canny_grayscale_image(node: RuntimeNodeSpec, image: Any, grayscale_mode: str):
    cv2 = _require_cv2(node)
    if image.ndim == 2:
        return image
    if image.ndim != 3:
        raise _node_error(
            node, "image_format_unsupported", "Canny requires a grayscale or color image."
        )
    channel_count = int(image.shape[2])
    if channel_count == 1:
        return image[:, :, 0]
    if grayscale_mode == "no":
        raise _node_error(
            node,
            "canny_grayscale_required",
            "Canny can run without grayscale conversion only when the input is already single-channel.",
        )
    if channel_count == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def _validate_odd_kernel(
    value: object,
    field_name: str,
    node: RuntimeNodeSpec,
    *,
    allow_zero: bool = False,
) -> int:
    try:
        parsed = int(value if value is not None and value != "" else 0)
    except (TypeError, ValueError) as exc:
        raise _node_error(node, "kernel_size_invalid", f"{field_name} must be an integer.") from exc
    if allow_zero and parsed in {0, 1}:
        return parsed
    if parsed < 1 or parsed % 2 == 0:
        raise _node_error(
            node, "kernel_size_invalid", f"{field_name} must be an odd positive integer."
        )
    return parsed


def _required_threshold(value: object, field_name: str, node: RuntimeNodeSpec) -> float:
    parsed = _optional_float(value)
    if parsed is None or parsed < 0:
        raise _node_error(
            node, "canny_threshold_invalid", f"{field_name} must be a non-negative number."
        )
    return parsed


def _validate_aperture(value: object, node: RuntimeNodeSpec) -> int:
    try:
        parsed = int(value if value is not None and value != "" else 3)
    except (TypeError, ValueError) as exc:
        raise _node_error(
            node, "canny_aperture_invalid", "Canny aperture size must be 3, 5, or 7."
        ) from exc
    if parsed not in {3, 5, 7}:
        raise _node_error(node, "canny_aperture_invalid", "Canny aperture size must be 3, 5, or 7.")
    return parsed


def _required_positive_float(value: object, field_name: str, node: RuntimeNodeSpec) -> float:
    parsed = _optional_float(value)
    if parsed is None or parsed < 0:
        raise _node_error(
            node, "cv_parameter_invalid", f"{field_name} must be a non-negative number."
        )
    return parsed


def _frame_from_cv_mask(
    mask: Any,
    *,
    source: FramePayload,
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    operation: str = "canny_edge_detection",
    metadata: dict[str, Any] | None = None,
) -> FramePayload:
    image = Image.fromarray(mask).convert("L")
    image_context = _derived_image_context(
        context,
        node,
        source=source,
        kind="processed",
        transform_type="mask",
        parameters={
            "operation": operation,
            "width": image.width,
            "height": image.height,
            **(metadata or {}),
        },
        coordinate_projection="identity",
    )
    return _frame_from_image(
        image,
        source=source,
        metadata=metadata,
        image_context=image_context,
        image_format="PNG",
        mime_type="image/png",
    )


def _cv_contours_from_mask(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    mask: Any,
) -> list[CvEdgeContour]:
    cv2 = _require_cv2(node)
    contours, _hierarchy = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    result: list[CvEdgeContour] = []
    for index, contour in enumerate(contours, start=1):
        length = float(cv2.arcLength(contour, closed=False))
        if length <= 0:
            continue
        x, y, width, height = cv2.boundingRect(contour)
        points = [
            InspectionPoint(x=float(point[0][0]), y=float(point[0][1])) for point in contour[:200]
        ]
        result.append(
            CvEdgeContour(
                contour_id=_stable_analysis_id(
                    context, node, "contour", index, x, y, width, height
                ),
                points=points,
                bounding_box={
                    "x_min": float(x),
                    "y_min": float(y),
                    "x_max": float(x + width),
                    "y_max": float(y + height),
                    "width": float(width),
                    "height": float(height),
                },
                area_px=float(cv2.contourArea(contour)),
                length_px=round(length, 6),
                score=min(length / max(mask.shape[0], mask.shape[1], 1), 1.0),
                metadata={"point_count": int(len(contour))},
            )
        )
    return result


def _color_profiles_from_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw_profiles = config.get("profiles") or config.get("color_profiles") or []
    if not isinstance(raw_profiles, list):
        return []
    profiles: list[dict[str, Any]] = []
    for index, item in enumerate(raw_profiles, start=1):
        if not isinstance(item, dict) or item.get("enabled") is False:
            continue
        label = str(item.get("label") or item.get("class_name") or f"color_object_{index}").strip()
        if not label:
            continue
        profiles.append({**item, "label": label})
    return profiles


def _color_profile_mask(node: RuntimeNodeSpec, image: Any, profile: dict[str, Any]):
    color_space = str(profile.get("color_space") or "hsv")
    if color_space != "hsv":
        raise _node_error(
            node,
            "color_profile_space_unsupported",
            "Color object segmentation currently supports HSV color profiles.",
            metadata={"color_space": color_space},
        )
    cv2 = _require_cv2(node)
    np = _require_numpy(node)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hue_min = int(_clamp_float(_optional_float(profile.get("hue_min")), 0, 179, 0))
    hue_max = int(_clamp_float(_optional_float(profile.get("hue_max")), 0, 179, 179))
    sat_min = int(_clamp_float(_optional_float(profile.get("saturation_min")), 0, 255, 0))
    sat_max = int(_clamp_float(_optional_float(profile.get("saturation_max")), 0, 255, 255))
    val_min = int(_clamp_float(_optional_float(profile.get("value_min")), 0, 255, 0))
    val_max = int(_clamp_float(_optional_float(profile.get("value_max")), 0, 255, 255))
    lower = np.array([hue_min, sat_min, val_min], dtype=np.uint8)
    upper = np.array([hue_max, sat_max, val_max], dtype=np.uint8)
    if hue_min <= hue_max:
        return cv2.inRange(hsv, lower, upper)
    lower_a = np.array([0, sat_min, val_min], dtype=np.uint8)
    upper_a = np.array([hue_max, sat_max, val_max], dtype=np.uint8)
    lower_b = np.array([hue_min, sat_min, val_min], dtype=np.uint8)
    upper_b = np.array([179, sat_max, val_max], dtype=np.uint8)
    return cv2.bitwise_or(cv2.inRange(hsv, lower_a, upper_a), cv2.inRange(hsv, lower_b, upper_b))


def _color_contours_from_mask(
    node: RuntimeNodeSpec,
    image: Any,
    mask: Any,
    *,
    split_touching_objects: bool,
    distance_threshold: float,
) -> list[tuple[Any, dict[str, Any]]]:
    cv2 = _require_cv2(node)
    np = _require_numpy(node)

    def external_contours(component_mask: Any) -> list[Any]:
        contours, _hierarchy = cv2.findContours(
            component_mask,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        return [contour for contour in contours if cv2.contourArea(contour) > 0]

    component_count, labels = cv2.connectedComponents(
        (mask > 0).astype(np.uint8),
        connectivity=8,
    )
    contour_entries: list[tuple[Any, dict[str, Any]]] = []
    for component_index in range(1, component_count):
        component_mask = np.where(labels == component_index, 255, 0).astype(np.uint8)
        component_contours = external_contours(component_mask)
        if not component_contours:
            continue
        base_contour = max(component_contours, key=cv2.contourArea)
        base_area_px = float(cv2.contourArea(base_contour))
        if not split_touching_objects:
            contour_entries.append(
                (
                    base_contour,
                    {
                        "component_index": component_index,
                        "split_method": "external_contour",
                        "split_count": 1,
                    },
                )
            )
            continue

        (_center, (rect_width, rect_height), _angle) = cv2.minAreaRect(base_contour)
        major_axis = max(float(rect_width), float(rect_height))
        minor_axis = max(min(float(rect_width), float(rect_height)), 1e-6)
        component_aspect_ratio = major_axis / minor_axis
        if component_aspect_ratio > 6.0:
            contour_entries.append(
                (
                    base_contour,
                    {
                        "component_index": component_index,
                        "split_method": "external_contour",
                        "split_count": 1,
                        "split_skipped_reason": "elongated_component",
                        "aspect_ratio": round(component_aspect_ratio, 6),
                    },
                )
            )
            continue

        def compact_seed_markers(seed_mask: Any, *, min_area_px: int) -> tuple[Any, int]:
            marker_count, marker_labels, marker_stats, _centroids = (
                cv2.connectedComponentsWithStats(seed_mask, connectivity=8)
            )
            compact = np.zeros_like(marker_labels, dtype=np.int32)
            next_label = 1
            for marker_index in range(1, marker_count):
                marker_area = int(marker_stats[marker_index, cv2.CC_STAT_AREA])
                if marker_area < min_area_px:
                    continue
                compact[marker_labels == marker_index] = next_label
                next_label += 1
            return compact, next_label - 1

        distance = cv2.distanceTransform(component_mask, cv2.DIST_L2, 5)
        max_distance = float(distance.max())
        if max_distance <= 1.0:
            contour_entries.append(
                (
                    base_contour,
                    {
                        "component_index": component_index,
                        "split_method": "external_contour",
                        "split_count": 1,
                    },
                )
            )
            continue

        _threshold, sure_foreground = cv2.threshold(
            distance,
            max_distance * distance_threshold,
            255,
            cv2.THRESH_BINARY,
        )
        sure_foreground = sure_foreground.astype(np.uint8)
        min_marker_area_px = max(int(base_area_px * 0.01), 3)
        compact_markers, split_count = compact_seed_markers(
            sure_foreground,
            min_area_px=min_marker_area_px,
        )
        if split_count <= 1:
            peak_kernel = np.ones((3, 3), dtype=np.uint8)
            local_peaks = (
                (distance >= max_distance * distance_threshold)
                & (distance == cv2.dilate(distance, peak_kernel))
            )
            local_peak_mask = np.where(local_peaks, 255, 0).astype(np.uint8)
            local_peak_mask = cv2.dilate(local_peak_mask, peak_kernel, iterations=1)
            compact_markers, split_count = compact_seed_markers(
                local_peak_mask,
                min_area_px=1,
            )
        if split_count <= 1:
            contour_entries.append(
                (
                    base_contour,
                    {
                        "component_index": component_index,
                        "split_method": "external_contour",
                        "split_count": 1,
                        "distance_peak_px": round(max_distance, 3),
                    },
                )
            )
            continue

        foreground = np.where(compact_markers > 0, 255, 0).astype(np.uint8)
        markers = compact_markers + 1
        markers[component_mask == 0] = 1
        markers[(component_mask > 0) & (foreground == 0)] = 0
        watershed_markers = cv2.watershed(image.copy(), markers.astype(np.int32))
        split_entries: list[tuple[Any, dict[str, Any]]] = []
        for marker_index in range(2, split_count + 2):
            object_mask = np.where(watershed_markers == marker_index, 255, 0).astype(np.uint8)
            object_mask = cv2.bitwise_and(object_mask, component_mask)
            split_contours = external_contours(object_mask)
            if not split_contours:
                continue
            split_contour = max(split_contours, key=cv2.contourArea)
            split_entries.append(
                (
                    split_contour,
                    {
                        "component_index": component_index,
                        "split_index": marker_index - 1,
                        "split_count": split_count,
                        "split_method": "distance_watershed",
                        "distance_peak_px": round(max_distance, 3),
                        "distance_threshold": round(distance_threshold, 6),
                    },
                )
            )
        if len(split_entries) <= 1:
            contour_entries.append(
                (
                    base_contour,
                    {
                        "component_index": component_index,
                        "split_method": "external_contour",
                        "split_count": 1,
                        "distance_peak_px": round(max_distance, 3),
                    },
                )
            )
        else:
            contour_entries.extend(split_entries)
    return contour_entries


def _morphology_mask(
    node: RuntimeNodeSpec,
    mask: Any,
    *,
    kernel_size: int,
    iterations: int,
):
    if kernel_size <= 1 or iterations <= 0:
        return mask
    cv2 = _require_cv2(node)
    np = _require_numpy(node)
    kernel = np.ones((kernel_size, kernel_size), dtype=np.uint8)
    opened = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=iterations)
    return cv2.morphologyEx(opened, cv2.MORPH_CLOSE, kernel, iterations=iterations)


def _apply_cv_zone_mask(
    node: RuntimeNodeSpec,
    mask: Any,
    zones: list[dict[str, Any]],
    width: int,
    height: int,
):
    cv2 = _require_cv2(node)
    np = _require_numpy(node)
    zone_mask = np.zeros((height, width), dtype=np.uint8)
    for zone in zones:
        box = zone["box"]
        x_min = max(int(math.floor(box.x_min * width)), 0)
        y_min = max(int(math.floor(box.y_min * height)), 0)
        x_max = min(int(math.ceil(box.x_max * width)), width)
        y_max = min(int(math.ceil(box.y_max * height)), height)
        angle_degrees = float(zone.get("angle_degrees") or 0.0)
        if abs(angle_degrees) > 1e-9:
            points = _rotated_box_points_px(np, box, width, height, angle_degrees)
            if points is not None:
                cv2.fillPoly(zone_mask, [points], 255)
        elif x_max > x_min and y_max > y_min:
            zone_mask[y_min:y_max, x_min:x_max] = 255
    return cv2.bitwise_and(mask, zone_mask)


def _rotated_box_points_px(
    np: Any,
    box: DetectionBox,
    width: int,
    height: int,
    angle_degrees: float,
):
    box_width = max((box.x_max - box.x_min) * width, 0.0)
    box_height = max((box.y_max - box.y_min) * height, 0.0)
    if box_width <= 0.0 or box_height <= 0.0:
        return None
    center_x = ((box.x_min + box.x_max) / 2.0) * width
    center_y = ((box.y_min + box.y_max) / 2.0) * height
    half_width = box_width / 2.0
    half_height = box_height / 2.0
    radians = math.radians(angle_degrees)
    cos_angle = math.cos(radians)
    sin_angle = math.sin(radians)
    corners = []
    for offset_x, offset_y in (
        (-half_width, -half_height),
        (half_width, -half_height),
        (half_width, half_height),
        (-half_width, half_height),
    ):
        corners.append(
            (
                round(center_x + offset_x * cos_angle - offset_y * sin_angle),
                round(center_y + offset_x * sin_angle + offset_y * cos_angle),
            )
        )
    return np.array(corners, dtype=np.int32)


def _normalized_cv_contour_points(
    contour: Any,
    width: int,
    height: int,
    *,
    limit: int,
) -> list[dict[str, float]]:
    points: list[dict[str, float]] = []
    for point in contour[:limit]:
        try:
            x = float(point[0][0])
            y = float(point[0][1])
        except (TypeError, ValueError, IndexError):
            continue
        points.append(_normalized_cv_point(x, y, width, height))
    return points


def _normalized_cv_point(x: float, y: float, width: int, height: int) -> dict[str, float]:
    normalized_x = min(max(x / max(width, 1), 0.0), 1.0)
    normalized_y = min(max(y / max(height, 1), 0.0), 1.0)
    return {"x": round(normalized_x, 6), "y": round(normalized_y, 6)}


def _normalized_sorted_cv_box_points(
    points: Any,
    width: int,
    height: int,
) -> list[dict[str, float]]:
    raw_points: list[tuple[float, float]] = []
    for point in points:
        try:
            raw_points.append((float(point[0]), float(point[1])))
        except (TypeError, ValueError, IndexError):
            continue
    if len(raw_points) != 4:
        return [_normalized_cv_point(x, y, width, height) for x, y in raw_points]
    center_x = sum(point[0] for point in raw_points) / 4.0
    center_y = sum(point[1] for point in raw_points) / 4.0
    sorted_points = sorted(
        raw_points,
        key=lambda point: math.atan2(point[1] - center_y, point[0] - center_x),
    )
    return [_normalized_cv_point(x, y, width, height) for x, y in sorted_points]


def _display_contour_polygon(
    mode: str,
    *,
    contour_polygon: list[dict[str, float]],
    hull_polygon: list[dict[str, float]],
    oriented_box_polygon: list[dict[str, float]],
) -> list[dict[str, float]]:
    if mode == "raw_contour" and len(contour_polygon) >= 3:
        return contour_polygon
    if mode == "convex_hull" and len(hull_polygon) >= 3:
        return hull_polygon
    if len(oriented_box_polygon) >= 3:
        return oriented_box_polygon
    if len(hull_polygon) >= 3:
        return hull_polygon
    return contour_polygon


def _mask_from_contours(
    cv2: Any,
    np: Any,
    height: int,
    width: int,
    contours: list[Any],
):
    mask = np.zeros((height, width), dtype=np.uint8)
    if contours:
        cv2.drawContours(mask, contours, -1, 255, thickness=cv2.FILLED)
    return mask


def _color_region_from_contour(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    profile: dict[str, Any],
    contour: Any,
    *,
    image_context_id: str | None,
    width: int,
    height: int,
    contour_index: int,
    polygon_epsilon_percent: float,
    contour_display_mode: str,
    profile_index: int,
    contour_metadata: dict[str, Any] | None = None,
) -> Region | None:
    cv2 = _require_cv2(node)
    area_px = float(cv2.contourArea(contour))
    image_area = max(float(width * height), 1.0)
    area_percent = (area_px / image_area) * 100.0
    min_area_percent = _optional_float(profile.get("min_area_percent"))
    max_area_percent = _optional_float(profile.get("max_area_percent"))
    if min_area_percent is not None and area_percent < min_area_percent:
        return None
    if max_area_percent is not None and area_percent > max_area_percent:
        return None
    x, y, box_width, box_height = cv2.boundingRect(contour)
    if box_width <= 0 or box_height <= 0:
        return None
    rect = cv2.minAreaRect(contour)
    (rect_center_x, rect_center_y), (rect_width, rect_height), rect_angle = rect
    major_axis = max(float(rect_width), float(rect_height))
    minor_axis = min(float(rect_width), float(rect_height))
    if major_axis <= 0 or minor_axis <= 0:
        return None
    aspect_ratio = major_axis / max(minor_axis, 1e-6)
    min_aspect_ratio = _optional_float(profile.get("min_aspect_ratio"))
    if min_aspect_ratio is not None and aspect_ratio < min_aspect_ratio:
        return None
    orientation = float(rect_angle)
    if rect_width < rect_height:
        orientation += 90.0
    orientation %= 180.0
    diagonal = math.hypot(width, height) or 1.0
    center_x = float(rect_center_x)
    center_y = float(rect_center_y)
    axis_half = major_axis / 2
    axis_vector = _angle_vector(orientation)
    axis_start = (
        center_x - axis_vector[0] * axis_half,
        center_y - axis_vector[1] * axis_half,
    )
    axis_end = (
        center_x + axis_vector[0] * axis_half,
        center_y + axis_vector[1] * axis_half,
    )
    rect_points = cv2.boxPoints(rect)
    epsilon = (polygon_epsilon_percent / 100.0) * max(float(cv2.arcLength(contour, True)), 1.0)
    approx = cv2.approxPolyDP(contour, epsilon, True) if epsilon > 0 else contour
    contour_polygon = _normalized_cv_contour_points(approx, width, height, limit=240)
    hull = cv2.convexHull(contour)
    hull_approx = cv2.approxPolyDP(hull, epsilon, True) if epsilon > 0 else hull
    oriented_box_polygon = _normalized_sorted_cv_box_points(rect_points, width, height)
    hull_polygon = _normalized_cv_contour_points(hull_approx, width, height, limit=80)
    hull_area = float(cv2.contourArea(cv2.convexHull(contour)))
    rect_area = max(float(rect_width) * float(rect_height), 1.0)
    object_id = _stable_analysis_id(
        context,
        node,
        "color_object",
        profile_index,
        profile["label"],
        contour_index,
        (contour_metadata or {}).get("component_index", 0),
        (contour_metadata or {}).get("split_index", 0),
        round(area_px, 3),
        x,
        y,
        box_width,
        box_height,
    )
    box = DetectionBox(
        x_min=max(x / width, 0.0),
        y_min=max(y / height, 0.0),
        x_max=min((x + box_width) / width, 1.0),
        y_max=min((y + box_height) / height, 1.0),
    )
    display_polygon = _display_contour_polygon(
        contour_display_mode,
        contour_polygon=contour_polygon,
        hull_polygon=hull_polygon,
        oriented_box_polygon=oriented_box_polygon,
    )
    geometry = {
        "box": box.model_dump(mode="json"),
        "polygon": display_polygon,
        "contour_polygon": contour_polygon,
        "hull_polygon": hull_polygon,
        "oriented_box": oriented_box_polygon,
        "axis": {
            "start": {
                "x": round(axis_start[0] / width, 6),
                "y": round(axis_start[1] / height, 6),
            },
            "end": {"x": round(axis_end[0] / width, 6), "y": round(axis_end[1] / height, 6)},
            "coordinate_system": "normalized_xy",
        },
        "orientation_degrees": round(orientation, 6),
    }
    return Region(
        key=object_id,
        label=str(profile["label"]),
        score=1.0,
        box=box,
        source_image_context_id=image_context_id,
        source_object_id=object_id,
        source_node_key=node.node_key,
        metadata={
            "source": "classic_cv",
            "algorithm": "color_object_segmentation",
            "profile_index": profile_index,
            "contour_index": contour_index,
            "segmentation": contour_metadata or {"split_method": "external_contour"},
            "color_profile": {
                key: value
                for key, value in profile.items()
                if key
                in {
                    "label",
                    "color_space",
                    "hue_min",
                    "hue_max",
                    "saturation_min",
                    "saturation_max",
                    "value_min",
                    "value_max",
                    "color_tolerance_percent",
                    "display_color",
                }
            },
            "geometry": geometry,
            "points": display_polygon,
            "orientation_degrees": round(orientation, 6),
            "area_px": round(area_px, 3),
            "area_percent": round(area_percent, 6),
            "major_axis_px": round(major_axis, 3),
            "minor_axis_px": round(minor_axis, 3),
            "major_axis_percent": round((major_axis / diagonal) * 100.0, 6),
            "minor_axis_percent": round((minor_axis / diagonal) * 100.0, 6),
            "aspect_ratio": round(aspect_ratio, 6),
            "solidity": round(area_px / hull_area, 6) if hull_area > 0 else 0.0,
            "fill_ratio": round(area_px / rect_area, 6),
            "image_width": width,
            "image_height": height,
            "object_ref": {
                "object_id": object_id,
                "object_type": "region",
                "source_image_context_id": image_context_id,
                "coordinate_system": "normalized_xy",
                "label": str(profile["label"]),
                "score": 1.0,
                "producer_node_key": node.node_key,
                "geometry": geometry,
            },
        },
    )


def _color_object_overlay_primitives(
    regions: list[Region],
    zones: list[dict[str, Any]],
    *,
    mask_frame: FramePayload | None = None,
    source_image_context_id: str | None = None,
    overlay_mode: str = "objects",
) -> list[OverlayPrimitive]:
    primitives: list[OverlayPrimitive] = []
    include_objects = overlay_mode in {"objects", "both"}
    include_mask = overlay_mode in {"mask", "both"}
    if include_mask and mask_frame is not None:
        mask_context_id = source_image_context_id or _frame_context_id(mask_frame)
        primitives.append(
            OverlayPrimitive(
                primitive_type="mask",
                layer="color_object_mask",
                label="Color object mask",
                geometry={
                    "data": mask_frame.image_base64_value(),
                    "width": mask_frame.width,
                    "height": mask_frame.height,
                },
                image_context_id=mask_context_id,
                style={"opacity": 0.36},
                metadata={
                    "source_image_context_id": source_image_context_id,
                    "mask_image_context_id": _frame_context_id(mask_frame),
                },
            )
        )
    if include_objects:
        for zone in zones:
            primitives.append(_spatial_zone_primitive(zone, 0))
        for region in regions:
            color = _region_display_color(region)
            polygon = _region_polygon_points(region)
            if polygon:
                primitives.append(
                    OverlayPrimitive(
                        primitive_type="polygon",
                        layer="color_object_contour",
                        label=region.label,
                        confidence=region.score,
                        geometry={"points": polygon, "coordinate_system": "normalized_xy"},
                        image_context_id=region.source_image_context_id,
                        object_id=region.source_object_id,
                        style={"stroke": color, "fill": "rgba(244,183,64,0.16)"},
                        metadata={"object": _region_summary(region)},
                    )
                )
            primitives.append(
                OverlayPrimitive(
                    primitive_type="box",
                    layer="color_object_box",
                    label=region.label,
                    confidence=region.score,
                    geometry=region.box.model_dump(mode="json"),
                    image_context_id=region.source_image_context_id,
                    object_id=region.source_object_id,
                    style={"stroke": color},
                    metadata={"object": _region_summary(region)},
                )
            )
            axis = _region_axis_geometry(region)
            primitives.append(
                OverlayPrimitive(
                    primitive_type="line",
                    layer="color_object_axis",
                    label=f"{axis['orientation_degrees']:.1f} deg",
                    geometry={
                        "start": axis["start"],
                        "end": axis["end"],
                        "coordinate_system": "normalized_xy",
                    },
                    image_context_id=region.source_image_context_id,
                    object_id=region.source_object_id,
                    style={"stroke": color, "stroke_width": 0.45},
                    metadata={"object": _region_summary(region), "axis": axis},
                )
            )
    return primitives


def _region_display_color(region: Region) -> str:
    profile = region.metadata.get("color_profile")
    if isinstance(profile, dict) and isinstance(profile.get("display_color"), str):
        return str(profile["display_color"])
    return "#f4b740"


def _region_summary(region: Region) -> dict[str, Any]:
    center = _box_center(region.box)
    return {
        "key": region.key,
        "label": region.label,
        "score": region.score,
        "center": {"x": round(center[0], 6), "y": round(center[1], 6)},
        "box": region.box.model_dump(mode="json"),
        "source_image_context_id": region.source_image_context_id,
        "source_object_id": region.source_object_id,
        "orientation_degrees": region.metadata.get("orientation_degrees"),
        "aspect_ratio": region.metadata.get("aspect_ratio"),
    }


def _region_result_object(region: Region) -> dict[str, Any]:
    object_ref = region.metadata.get("object_ref")
    return {
        "object_id": region.source_object_id or region.key,
        "region_key": region.key,
        "label": region.label,
        "score": region.score,
        "box": region.box.model_dump(mode="json"),
        "source_image_context_id": region.source_image_context_id,
        "source_node_key": region.source_node_key,
        "geometry": region.metadata.get("geometry"),
        "area_px": region.metadata.get("area_px"),
        "area_percent": region.metadata.get("area_percent"),
        "orientation_degrees": region.metadata.get("orientation_degrees"),
        "aspect_ratio": region.metadata.get("aspect_ratio"),
        "object_ref": object_ref if isinstance(object_ref, dict) else None,
        "metadata": region.metadata,
    }


def _region_counts_by_label(regions: list[Region]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for region in regions:
        label = region.label or "unknown"
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items()))


def _count_measurements_from_regions(
    regions: list[Region],
    *,
    image_context_id: str | None,
) -> list[MeasurementPayload]:
    measurements = [
        MeasurementPayload(
            key="count_total",
            label="Total objects",
            value=len(regions),
            unit="objects",
            source_image_context_id=image_context_id,
            metadata={"group_by": "total"},
        )
    ]
    used_keys = {"count_total"}
    for label, count in _region_counts_by_label(regions).items():
        key_base = f"count_{_result_key_from_label(label)}"
        key = key_base
        suffix = 2
        while key in used_keys:
            key = f"{key_base}_{suffix}"
            suffix += 1
        used_keys.add(key)
        measurements.append(
            MeasurementPayload(
                key=key,
                label=label,
                value=count,
                unit="objects",
                source_image_context_id=image_context_id,
                metadata={"group_by": "label", "label": label},
            )
        )
    return measurements


def _result_key_from_label(label: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", label.strip().lower()).strip("_")
    return normalized or "unknown"


def _region_polygon_points(region: Region) -> list[dict[str, float]]:
    geometry = region.metadata.get("geometry")
    if isinstance(geometry, dict) and isinstance(geometry.get("polygon"), list):
        return [
            {"x": float(point["x"]), "y": float(point["y"])}
            for point in geometry["polygon"]
            if isinstance(point, dict) and "x" in point and "y" in point
        ]
    points = region.metadata.get("points")
    if isinstance(points, list):
        return [
            {"x": float(point["x"]), "y": float(point["y"])}
            for point in points
            if isinstance(point, dict) and "x" in point and "y" in point
        ]
    return []


def _region_axis_geometry(region: Region) -> dict[str, Any]:
    geometry = region.metadata.get("geometry")
    axis = geometry.get("axis") if isinstance(geometry, dict) else None
    if isinstance(axis, dict):
        start = _normalized_point_mapping(axis.get("start"))
        end = _normalized_point_mapping(axis.get("end"))
        if start is not None and end is not None and math.dist(start, end) > 1e-9:
            orientation = _angle_from_points(start, end)
            box_width = max(region.box.x_max - region.box.x_min, 0.0)
            box_height = max(region.box.y_max - region.box.y_min, 0.0)
            centered_axis_length = max(box_width, box_height)
            centered_axis = _centered_axis_on_box(
                region.box,
                orientation,
                centered_axis_length,
            )
            major_percent = _optional_float(region.metadata.get("major_axis_percent"))
            minor_percent = _optional_float(region.metadata.get("minor_axis_percent"))
            aspect_ratio = _optional_float(region.metadata.get("aspect_ratio"))
            if major_percent is None:
                major_percent = _normalized_distance_percent(centered_axis_length)
            if minor_percent is None:
                minor_percent = 0.0
            if aspect_ratio is None:
                aspect_ratio = 1.0
            return {
                "start": {"x": centered_axis[0][0], "y": centered_axis[0][1]},
                "end": {"x": centered_axis[1][0], "y": centered_axis[1][1]},
                "orientation_degrees": orientation,
                "major_axis_percent": major_percent,
                "minor_axis_percent": minor_percent,
                "aspect_ratio": aspect_ratio,
            }
    box_width = max(region.box.x_max - region.box.x_min, 0.0)
    box_height = max(region.box.y_max - region.box.y_min, 0.0)
    center = _box_center(region.box)
    if box_width >= box_height:
        start = (region.box.x_min, center[1])
        end = (region.box.x_max, center[1])
        major = box_width
        minor = box_height
    else:
        start = (center[0], region.box.y_min)
        end = (center[0], region.box.y_max)
        major = box_height
        minor = box_width
    return {
        "start": {"x": start[0], "y": start[1]},
        "end": {"x": end[0], "y": end[1]},
        "orientation_degrees": _angle_from_points(start, end),
        "major_axis_percent": _normalized_distance_percent(major),
        "minor_axis_percent": _normalized_distance_percent(minor),
        "aspect_ratio": major / max(minor, 1e-9),
    }


def _centered_axis_on_box(
    box: DetectionBox,
    orientation_degrees: float,
    length: float,
) -> tuple[tuple[float, float], tuple[float, float]]:
    center = _box_center(box)
    vector = _angle_vector(orientation_degrees)
    half_length = max(length, 0.0) / 2.0
    return (
        (
            _clamp_float(center[0] - vector[0] * half_length, 0.0, 1.0, 0.0),
            _clamp_float(center[1] - vector[1] * half_length, 0.0, 1.0, 0.0),
        ),
        (
            _clamp_float(center[0] + vector[0] * half_length, 0.0, 1.0, 1.0),
            _clamp_float(center[1] + vector[1] * half_length, 0.0, 1.0, 1.0),
        ),
    )


def _orientation_label_filters(config: dict[str, Any]) -> list[str]:
    raw = config.get("labels")
    if raw in (None, "", []):
        return []
    return [label.casefold() for label in _field_list(raw)]


def _orientation_label_matches(label: str | None, filters: list[str]) -> bool:
    return not filters or (label or "").casefold() in filters


def _orientation_reference_angle_degrees(
    config: dict[str, Any],
    coordinate_frame: CoordinateFramePayload | None,
) -> float:
    source = str(config.get("orientation_source") or "drawn_direction")
    if coordinate_frame is not None and source in {
        "coordinate_frame_longitudinal",
        "coordinate_frame_transverse",
    }:
        axis = (
            coordinate_frame.frame.transverse_axis
            if source == "coordinate_frame_transverse"
            else coordinate_frame.frame.longitudinal_axis
        )
        return _vector_angle_degrees(axis.x, axis.y)
    if source == "drawn_direction":
        line = _direction_reference_from_config(config.get("reference_direction"))
        if line is not None:
            return _angle_from_points(line[0], line[1])
    return (_optional_float(config.get("reference_angle_degrees")) or 0.0) % 180.0


def _direction_reference_from_config(
    value: object,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    if not isinstance(value, dict):
        return None
    start = _normalized_point_mapping(value.get("start"))
    end = _normalized_point_mapping(value.get("end"))
    if start is None or end is None or math.dist(start, end) <= 1e-9:
        return None
    return start, end


def _normalized_point_mapping(value: object) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    x = _optional_float(value.get("x"))
    y = _optional_float(value.get("y"))
    if x is None or y is None:
        return None
    return _clamp_float(x, 0.0, 1.0, 0.0), _clamp_float(y, 0.0, 1.0, 0.0)


def _orientation_zone_primitives(zones: list[dict[str, Any]]) -> list[OverlayPrimitive]:
    return [_spatial_zone_primitive(zone, 0) for zone in zones]


def _orientation_object_primitives(
    region: Region,
    *,
    geometry: dict[str, Any],
    valid: bool,
    label: str,
) -> list[OverlayPrimitive]:
    color = "#2f9e44" if valid else "#e03131"
    status_label = f"{label}: {'ok' if valid else 'violation'}"
    return [
        OverlayPrimitive(
            primitive_type="box",
            layer="orientation_object_valid" if valid else "orientation_object_violation",
            label=region.label,
            confidence=region.score,
            geometry=region.box.model_dump(mode="json"),
            image_context_id=region.source_image_context_id,
            object_id=region.source_object_id,
            style={"stroke": color},
            metadata={"object": _region_summary(region), "valid": valid},
        ),
        OverlayPrimitive(
            primitive_type="line",
            layer="orientation_axis_valid" if valid else "orientation_axis_violation",
            label=f"{status_label}: {geometry['orientation_degrees']:.1f} deg",
            geometry={
                "start": geometry["start"],
                "end": geometry["end"],
                "coordinate_system": "normalized_xy",
            },
            image_context_id=region.source_image_context_id,
            object_id=region.source_object_id,
            style={"stroke": color, "stroke_width": 0.55},
            metadata={"object": _region_summary(region), "geometry": geometry, "valid": valid},
        ),
    ]


def _orientation_reference_primitive(
    config: dict[str, Any],
    reference_angle: float,
    regions: list[Region],
) -> OverlayPrimitive:
    line = _direction_reference_from_config(config.get("reference_direction"))
    if line is None:
        if regions:
            centers = [_box_center(region.box) for region in regions]
            center = (
                sum(point[0] for point in centers) / len(centers),
                sum(point[1] for point in centers) / len(centers),
            )
        else:
            center = (0.5, 0.5)
        vector = _angle_vector(reference_angle)
        half_length = 0.22
        line = (
            (
                _clamp_float(center[0] - vector[0] * half_length, 0.0, 1.0, 0.0),
                _clamp_float(center[1] - vector[1] * half_length, 0.0, 1.0, 0.0),
            ),
            (
                _clamp_float(center[0] + vector[0] * half_length, 0.0, 1.0, 1.0),
                _clamp_float(center[1] + vector[1] * half_length, 0.0, 1.0, 1.0),
            ),
        )
    return OverlayPrimitive(
        primitive_type="arrow",
        layer="orientation_reference",
        label=f"reference {reference_angle:.1f} deg",
        geometry={
            "start": {"x": line[0][0], "y": line[0][1]},
            "end": {"x": line[1][0], "y": line[1][1]},
            "coordinate_system": "normalized_xy",
        },
        style={"stroke": "#1c7ed6", "stroke_width": 0.65},
        metadata={"reference_angle_degrees": reference_angle},
    )


def _region_in_zone(region: Region, zone: DetectionBox, *, mode: str) -> bool:
    if mode == "fully_inside":
        return (
            region.box.x_min >= zone.x_min
            and region.box.y_min >= zone.y_min
            and region.box.x_max <= zone.x_max
            and region.box.y_max <= zone.y_max
        )
    if mode == "box_overlaps":
        return _box_intersection_area(region.box, zone) > 0
    center = _box_center(region.box)
    return zone.x_min <= center[0] <= zone.x_max and zone.y_min <= center[1] <= zone.y_max


def _angle_vector(angle_degrees: float) -> tuple[float, float]:
    radians = math.radians(angle_degrees)
    return math.cos(radians), math.sin(radians)


def _angle_from_points(start: tuple[float, float], end: tuple[float, float]) -> float:
    return _vector_angle_degrees(end[0] - start[0], end[1] - start[1])


def _vector_angle_degrees(x: float, y: float) -> float:
    angle = math.degrees(math.atan2(y, x)) % 180.0
    return angle + 180.0 if angle < 0 else angle


def _angle_deviation_degrees(first: float, second: float) -> float:
    delta = abs((first % 180.0) - (second % 180.0))
    return min(delta, 180.0 - delta)


def _hough_line_candidates(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    mask: Any,
    *,
    threshold: int,
    min_line_length: float,
    max_line_gap: float,
) -> list[CvLineCandidate]:
    cv2 = _require_cv2(node)
    raw_lines = cv2.HoughLinesP(
        mask,
        rho=1,
        theta=math.pi / 180,
        threshold=threshold,
        minLineLength=max(float(min_line_length), 1.0),
        maxLineGap=max(float(max_line_gap), 0.0),
    )
    if raw_lines is None:
        return []
    diagonal = math.hypot(mask.shape[1], mask.shape[0]) or 1.0
    candidates: list[CvLineCandidate] = []
    for index, item in enumerate(raw_lines[:, 0, :], start=1):
        x1, y1, x2, y2 = (float(value) for value in item)
        line = LineGeometry(
            start=InspectionPoint(x=x1, y=y1),
            end=InspectionPoint(x=x2, y=y2),
        )
        length = _line_length(line)
        if length < min_line_length:
            continue
        angle = _line_orientation_degrees(line)
        candidates.append(
            CvLineCandidate(
                line_id=_stable_analysis_id(context, node, "line", index, x1, y1, x2, y2),
                line=line,
                length_px=round(length, 6),
                angle_degrees=round(angle, 6),
                score=round(min(length / diagonal, 1.0), 6),
                metadata={
                    "raw_hough_index": index,
                    "midpoint": {
                        "x": (x1 + x2) / 2,
                        "y": (y1 + y2) / 2,
                    },
                },
            )
        )
    return sorted(candidates, key=lambda candidate: candidate.length_px, reverse=True)


def _line_orientation_degrees(line: LineGeometry) -> float:
    angle = math.degrees(math.atan2(line.end.y - line.start.y, line.end.x - line.start.x))
    angle = angle % 180
    if angle < 0:
        angle += 180
    return angle


def _cv_line_selection_rules(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw = config.get("line_selection_rules") or []
    if isinstance(raw, dict):
        raw = [{"edge_role": key, **value} for key, value in raw.items() if isinstance(value, dict)]
    roles = {"start_edge", "side_edge", "left_edge", "right_edge", "custom"}
    rules: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("edge_role") or item.get("role") or "").strip()
        if role not in roles:
            continue
        rules.append({**item, "edge_role": role})
    return rules


def _select_cv_line_candidate(
    candidates: list[CvLineCandidate],
    rule: dict[str, Any],
    *,
    image_width: int,
    image_height: int,
    selected_candidate_ids: set[str],
) -> CvLineCandidate | None:
    allow_reuse = bool(rule.get("allow_reuse", False))
    min_length = _optional_float(rule.get("minimum_line_length_px")) or 0.0
    scored: list[tuple[float, CvLineCandidate]] = []
    for candidate in candidates:
        if not allow_reuse and candidate.line_id in selected_candidate_ids:
            continue
        if candidate.length_px < min_length:
            continue
        if not _candidate_matches_angle(candidate, rule):
            continue
        score = _cv_line_ranking_score(
            candidate, rule, image_width=image_width, image_height=image_height
        )
        scored.append((score, candidate))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1].length_px), reverse=True)
    return scored[0][1]


def _candidate_matches_angle(candidate: CvLineCandidate, rule: dict[str, Any]) -> bool:
    angle_min = _optional_float(rule.get("angle_min_degrees"))
    angle_max = _optional_float(rule.get("angle_max_degrees"))
    if angle_min is not None and angle_max is not None:
        return _angle_in_range(candidate.angle_degrees, angle_min, angle_max)
    target = _optional_float(rule.get("target_angle_degrees"))
    tolerance = _optional_float(rule.get("angle_tolerance_degrees"))
    if target is None:
        return True
    return _angle_distance_degrees(candidate.angle_degrees, target) <= (
        tolerance if tolerance is not None else 15.0
    )


def _angle_in_range(angle: float, angle_min: float, angle_max: float) -> bool:
    normalized = angle % 180
    lower = angle_min % 180
    upper = angle_max % 180
    if lower <= upper:
        return lower <= normalized <= upper
    return normalized >= lower or normalized <= upper


def _angle_distance_degrees(first: float, second: float) -> float:
    delta = abs((first % 180) - (second % 180))
    return min(delta, 180 - delta)


def _cv_line_ranking_score(
    candidate: CvLineCandidate,
    rule: dict[str, Any],
    *,
    image_width: int,
    image_height: int,
) -> float:
    ranking = str(rule.get("ranking") or "strongest_longest")
    side = str(rule.get("nearest_to_image_side") or "none")
    region = str(rule.get("region_preference") or "any")
    length_score = candidate.score
    side_score = _nearest_side_score(
        candidate, side, image_width=image_width, image_height=image_height
    )
    region_score = _region_preference_score(
        candidate, region, image_width=image_width, image_height=image_height
    )
    if ranking == "nearest_to_side":
        return side_score * 2.0 + length_score + region_score * 0.5
    if ranking == "longest":
        return length_score * 2.0 + region_score * 0.4 + side_score * 0.2
    return length_score + side_score + region_score * 0.5


def _line_midpoint(line: LineGeometry) -> tuple[float, float]:
    return (line.start.x + line.end.x) / 2, (line.start.y + line.end.y) / 2


def _nearest_side_score(
    candidate: CvLineCandidate,
    side: str,
    *,
    image_width: int,
    image_height: int,
) -> float:
    if side in {"", "none", "any"}:
        return 0.0
    mid_x, mid_y = _line_midpoint(candidate.line)
    distances = {
        "left": mid_x,
        "right": image_width - mid_x,
        "top": mid_y,
        "bottom": image_height - mid_y,
    }
    distance = max(float(distances.get(side, max(image_width, image_height))), 0.0)
    scale = max(float(image_width if side in {"left", "right"} else image_height), 1.0)
    return max(0.0, 1.0 - min(distance / scale, 1.0))


def _region_preference_score(
    candidate: CvLineCandidate,
    region: str,
    *,
    image_width: int,
    image_height: int,
) -> float:
    if region in {"", "any"}:
        return 0.0
    mid_x, mid_y = _line_midpoint(candidate.line)
    if region == "left":
        return 1.0 if mid_x <= image_width / 2 else 0.0
    if region == "right":
        return 1.0 if mid_x >= image_width / 2 else 0.0
    if region == "top":
        return 1.0 if mid_y <= image_height / 2 else 0.0
    if region == "bottom":
        return 1.0 if mid_y >= image_height / 2 else 0.0
    if region == "center":
        return 1.0 - min(abs(mid_x - image_width / 2) / max(image_width / 2, 1), 1.0)
    return 0.0


def _edge_role_mappings(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mappings: dict[str, dict[str, Any]] = {}
    raw = config.get("class_role_mappings") or []
    if isinstance(raw, dict):
        raw = [{"feature_class": key, "edge_role": value} for key, value in raw.items()]
    legacy = config.get("class_to_edge_role")
    if isinstance(legacy, dict):
        raw = [
            *raw,
            *({"feature_class": key, "edge_role": value} for key, value in legacy.items()),
        ]
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        feature_class = str(item.get("feature_class") or item.get("class_name") or "").strip()
        role = str(item.get("edge_role") or item.get("role") or "").strip()
        if not feature_class or role not in {
            "start_edge",
            "side_edge",
            "left_edge",
            "right_edge",
            "custom",
        }:
            continue
        mappings[feature_class] = {**item, "edge_role": role}
    return mappings


def _line_from_region(
    region: Region,
    image_width: int,
    image_height: int,
    *,
    mode: str,
    bbox_side: str,
) -> LineGeometry | None:
    if mode in {"from_polygon_line", "from_mask_contour"}:
        points = _pixel_points_from_region_metadata(region, image_width, image_height)
        if len(points) >= 2:
            start, end = _longest_point_segment(points)
            return LineGeometry(
                start=InspectionPoint(x=start[0], y=start[1]),
                end=InspectionPoint(x=end[0], y=end[1]),
            )
        return None
    bbox = _box_to_pixel_geometry(region.box, image_width, image_height)
    side = bbox_side
    if side == "longest_side":
        side = "left" if bbox["height"] >= bbox["width"] else "top"
    if side == "left":
        start = (bbox["x_min"], bbox["y_min"])
        end = (bbox["x_min"], bbox["y_max"])
    elif side == "right":
        start = (bbox["x_max"], bbox["y_min"])
        end = (bbox["x_max"], bbox["y_max"])
    elif side == "bottom":
        start = (bbox["x_min"], bbox["y_max"])
        end = (bbox["x_max"], bbox["y_max"])
    else:
        start = (bbox["x_min"], bbox["y_min"])
        end = (bbox["x_max"], bbox["y_min"])
    return LineGeometry(
        start=InspectionPoint(x=start[0], y=start[1]),
        end=InspectionPoint(x=end[0], y=end[1]),
    )


def _box_to_pixel_geometry(
    box: DetectionBox, image_width: int, image_height: int
) -> dict[str, Any]:
    x_min = float(box.x_min) * image_width
    y_min = float(box.y_min) * image_height
    x_max = float(box.x_max) * image_width
    y_max = float(box.y_max) * image_height
    corners = [
        (x_min, y_min),
        (x_max, y_min),
        (x_max, y_max),
        (x_min, y_max),
    ]
    return {
        "x_min": x_min,
        "y_min": y_min,
        "x_max": x_max,
        "y_max": y_max,
        "width": max(x_max - x_min, 0.0),
        "height": max(y_max - y_min, 0.0),
        "area": max(x_max - x_min, 0.0) * max(y_max - y_min, 0.0),
        "corners": corners,
        "coordinate_system": "pixel_xy",
        "image_width": image_width,
        "image_height": image_height,
    }


def _pixel_points_from_region_metadata(
    region: Region,
    image_width: int,
    image_height: int,
) -> list[tuple[float, float]]:
    object_ref = region.metadata.get("object_ref")
    geometry = object_ref.get("geometry") if isinstance(object_ref, dict) else None
    if not isinstance(geometry, dict):
        geometry = (
            region.metadata.get("geometry")
            if isinstance(region.metadata.get("geometry"), dict)
            else {}
        )
    raw_points = (
        geometry.get("points")
        or geometry.get("polygon")
        or geometry.get("contour")
        or region.metadata.get("points")
        or []
    )
    points: list[tuple[float, float]] = []
    for point in raw_points if isinstance(raw_points, list) else []:
        if isinstance(point, dict):
            x = _optional_float(point.get("x"))
            y = _optional_float(point.get("y"))
        elif isinstance(point, list | tuple) and len(point) >= 2:
            x = _optional_float(point[0])
            y = _optional_float(point[1])
        else:
            x = y = None
        if x is None or y is None:
            continue
        if 0 <= x <= 1 and 0 <= y <= 1:
            points.append((x * image_width, y * image_height))
        else:
            points.append((x, y))
    return points


def _longest_point_segment(
    points: list[tuple[float, float]],
) -> tuple[tuple[float, float], tuple[float, float]]:
    best = (points[0], points[1])
    best_length = -1.0
    for index, start in enumerate(points):
        end = points[(index + 1) % len(points)]
        length = math.dist(start, end)
        if length > best_length:
            best = (start, end)
            best_length = length
    return best


def _line_length(line: LineGeometry) -> float:
    return math.hypot(line.end.x - line.start.x, line.end.y - line.start.y)


def _line_vector(line: LineGeometry) -> tuple[float, float]:
    return line.end.x - line.start.x, line.end.y - line.start.y


def _vector_length(vector: tuple[float, float]) -> float:
    return math.hypot(vector[0], vector[1])


def _normalize_vector(vector: tuple[float, float]) -> tuple[float, float]:
    length = _vector_length(vector)
    if length <= 1e-12:
        return 0.0, 0.0
    return vector[0] / length, vector[1] / length


def _dot(first: tuple[float, float], second: tuple[float, float]) -> float:
    return first[0] * second[0] + first[1] * second[1]


def _edge_by_role(edges: list[ReferenceEdge], role: str) -> ReferenceEdge | None:
    return next((edge for edge in edges if edge.role == role), None)


def _coordinate_origin(
    start_edge: ReferenceEdge,
    direction_edge: ReferenceEdge,
    origin_rule: str,
    config: dict[str, Any],
) -> tuple[float, float] | None:
    if origin_rule == "center_of_start_edge":
        return (
            (start_edge.line.start.x + start_edge.line.end.x) / 2,
            (start_edge.line.start.y + start_edge.line.end.y) / 2,
        )
    if origin_rule == "configured_endpoint":
        endpoint = str(config.get("configured_endpoint") or "start")
        point = start_edge.line.end if endpoint == "end" else start_edge.line.start
        return point.x, point.y
    if origin_rule == "intersection_with_side_edge":
        return _line_intersection(start_edge.line, direction_edge.line)
    return None


def _line_intersection(first: LineGeometry, second: LineGeometry) -> tuple[float, float] | None:
    x1, y1 = first.start.x, first.start.y
    x2, y2 = first.end.x, first.end.y
    x3, y3 = second.start.x, second.start.y
    x4, y4 = second.end.x, second.end.y
    denominator = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denominator) <= 1e-9:
        return None
    px = ((x1 * y2 - y1 * x2) * (x3 - x4) - (x1 - x2) * (x3 * y4 - y3 * x4)) / denominator
    py = ((x1 * y2 - y1 * x2) * (y3 - y4) - (y1 - y2) * (x3 * y4 - y3 * x4)) / denominator
    return px, py


def _first_number(*values: object) -> float | None:
    for value in values:
        parsed = _optional_float(value)
        if parsed is not None:
            return parsed
    return None


def _measurement_rule_map(config: dict[str, Any]) -> dict[str, list[str]]:
    allowed = {
        "bbox_width",
        "bbox_height",
        "approximate_diameter",
        "polygon_length",
        "mask_area",
        "axis_projected_width",
        "axis_projected_length",
    }
    raw = config.get("measurement_rules") or []
    if isinstance(raw, dict):
        raw = [{"feature_type": key, "bases": value} for key, value in raw.items()]
    result: dict[str, list[str]] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        feature_type = str(item.get("feature_type") or item.get("class_name") or "").strip()
        bases_raw = item.get("bases") or item.get("measurement_bases") or []
        bases = _field_list(bases_raw)
        bases = [basis for basis in bases if basis in allowed]
        if feature_type and bases:
            result[feature_type] = bases
    return result


def _measurement_value_for_basis(
    basis: str,
    *,
    bbox: dict[str, Any],
    region: Region,
    coordinate_frame: InspectionCoordinateFrame | None,
    axis_projection: dict[str, Any],
) -> float | None:
    if basis == "bbox_width":
        return float(bbox["width"])
    if basis == "bbox_height":
        return float(bbox["height"])
    if basis == "approximate_diameter":
        return math.sqrt(float(bbox["area"]))
    if basis == "axis_projected_width":
        if coordinate_frame is None:
            return None
        return abs(
            _required_number(axis_projection.get("transverse_end_px"))
            - _required_number(axis_projection.get("transverse_start_px"))
        )
    if basis == "axis_projected_length":
        if coordinate_frame is None:
            return None
        return abs(
            _required_number(axis_projection.get("longitudinal_end_px"))
            - _required_number(axis_projection.get("longitudinal_start_px"))
        )
    if basis == "polygon_length":
        image_width = _optional_float(bbox.get("image_width"))
        image_height = _optional_float(bbox.get("image_height"))
        if image_width is None or image_height is None:
            return None
        points = _pixel_points_from_region_metadata(region, int(image_width), int(image_height))
        if len(points) < 2:
            return None
        return sum(
            math.dist(points[index], points[(index + 1) % len(points)])
            for index in range(len(points))
        )
    if basis == "mask_area":
        mask_area = _optional_float(
            region.metadata.get("mask_area_px") or region.metadata.get("mask_area")
        )
        return mask_area
    return None


def _axis_projection_metadata(
    points: list[tuple[float, float]],
    frame: InspectionCoordinateFrame | None,
) -> dict[str, float]:
    if frame is None:
        return {}
    origin = (frame.origin.x, frame.origin.y)
    longitudinal = (frame.longitudinal_axis.x, frame.longitudinal_axis.y)
    transverse = (frame.transverse_axis.x, frame.transverse_axis.y)
    longitudinal_values = [
        _dot((point[0] - origin[0], point[1] - origin[1]), longitudinal) for point in points
    ]
    transverse_values = [
        _dot((point[0] - origin[0], point[1] - origin[1]), transverse) for point in points
    ]
    return {
        "longitudinal_start_px": min(longitudinal_values),
        "longitudinal_end_px": max(longitudinal_values),
        "transverse_start_px": min(transverse_values),
        "transverse_end_px": max(transverse_values),
    }


def _positive_optional_float(value: object) -> float | None:
    parsed = _optional_float(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _clearance_rules(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw = config.get("clearance_rules") or []
    return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _required_number(value: object) -> float:
    parsed = _optional_float(value)
    if parsed is None:
        raise ValueError("Expected numeric geometry metadata.")
    return parsed


def _merge_clearance_zones(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    zones: list[ClearanceZone],
) -> list[ClearanceZone]:
    if not zones:
        return []
    ordered = sorted(zones, key=lambda zone: zone.interval_start_px)
    merged: list[list[ClearanceZone]] = []
    current: list[ClearanceZone] = [ordered[0]]
    current_end = ordered[0].interval_end_px
    for zone in ordered[1:]:
        if zone.interval_start_px <= current_end:
            current.append(zone)
            current_end = max(current_end, zone.interval_end_px)
        else:
            merged.append(current)
            current = [zone]
            current_end = zone.interval_end_px
    merged.append(current)
    result: list[ClearanceZone] = []
    for group in merged:
        if len(group) == 1:
            result.append(group[0])
            continue
        start = min(zone.interval_start_px for zone in group)
        end = max(zone.interval_end_px for zone in group)
        feature_types = sorted({zone.feature_type for zone in group})
        zone_ids = [zone.zone_id for zone in group]
        result.append(
            ClearanceZone(
                zone_id=_stable_analysis_id(context, node, "zone", "merged", *zone_ids),
                feature_id="merged:" + ",".join(zone.feature_id for zone in group),
                feature_type=feature_types[0] if len(feature_types) == 1 else "multiple",
                interval_start_px=round(start, 6),
                interval_end_px=round(end, 6),
                clearance_px=max(zone.clearance_px for zone in group),
                rule_metadata={
                    "merged": True,
                    "merged_zone_ids": zone_ids,
                    "merged_feature_types": feature_types,
                    "source_zones": [zone.model_dump(mode="json") for zone in group],
                },
                severity="warning"
                if any(zone.severity == "warning" for zone in group)
                else group[0].severity,
                category="merged_clearance",
            )
        )
    return result


def _distance_config_px(
    config: dict[str, Any],
    prefix: str,
    px_key: str,
    mm_key: str,
    mm_per_pixel: float | None,
    node: RuntimeNodeSpec,
) -> float:
    value_mm = _optional_float(config.get(mm_key))
    if value_mm is not None:
        if mm_per_pixel is None:
            raise _node_error(
                node,
                "metric_calibration_missing",
                f"{mm_key} requires mm_per_pixel calibration.",
            )
        return value_mm / mm_per_pixel
    value_px = _optional_float(config.get(px_key) if px_key in config else config.get(prefix))
    return max(value_px or 0.0, 0.0)


def _optional_distance_config_px(
    config: dict[str, Any],
    px_key: str,
    mm_key: str,
    mm_per_pixel: float | None,
    node: RuntimeNodeSpec,
) -> float | None:
    if config.get(px_key) in (None, "") and config.get(mm_key) in (None, ""):
        return None
    value = _distance_config_px(config, px_key, px_key, mm_key, mm_per_pixel, node)
    return value


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    ordered = sorted((min(start, end), max(start, end)) for start, end in intervals)
    if not ordered:
        return []
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def _find_first_free_interval(
    intervals: list[tuple[float, float]],
    *,
    start_offset: float,
    minimum_length: float,
    end_limit: float | None,
    require_end_limit: bool,
) -> FreeRegion:
    cursor = start_offset
    for start, end in intervals:
        if end <= cursor:
            continue
        if start > cursor and (start - cursor) >= minimum_length:
            return FreeRegion(
                found=True,
                start_position_px=round(cursor, 6),
                end_position_px=round(start, 6),
                distance_from_origin_px=round(cursor, 6),
                metadata={"blocked_intervals_px": intervals},
            )
        cursor = max(cursor, end)
        if end_limit is not None and cursor > end_limit:
            break
    if end_limit is not None:
        if (end_limit - cursor) >= minimum_length:
            return FreeRegion(
                found=True,
                start_position_px=round(cursor, 6),
                end_position_px=round(end_limit, 6),
                distance_from_origin_px=round(cursor, 6),
                metadata={"blocked_intervals_px": intervals},
            )
        return FreeRegion(
            found=False,
            distance_from_origin_px=None,
            reason="No free interval satisfies the minimum length before the configured board end.",
            metadata={"blocked_intervals_px": intervals, "end_limit_px": end_limit},
        )
    if require_end_limit:
        return FreeRegion(
            found=False,
            reason="No board end limit is configured and outside the known board region is invalid.",
            metadata={"blocked_intervals_px": intervals},
        )
    return FreeRegion(
        found=True,
        start_position_px=round(cursor, 6),
        end_position_px=round(cursor + minimum_length, 6),
        distance_from_origin_px=round(cursor, 6),
        metadata={"blocked_intervals_px": intervals, "open_ended": True},
    )


def _metric_measurements(
    payload: FeatureMeasurementsPayload, mm_per_pixel: float
) -> FeatureMeasurementsPayload:
    return payload.model_copy(
        update={
            "measurements": [
                measurement.model_copy(
                    update={"value_mm": round(measurement.value_px * mm_per_pixel, 6)}
                )
                for measurement in payload.measurements
            ],
            "metadata": {**payload.metadata, "mm_per_pixel": mm_per_pixel},
        }
    )


def _metric_clearance_zones(
    payload: ClearanceZonesPayload, mm_per_pixel: float
) -> ClearanceZonesPayload:
    return payload.model_copy(
        update={
            "zones": [
                zone.model_copy(
                    update={
                        "interval_start_mm": round(zone.interval_start_px * mm_per_pixel, 6),
                        "interval_end_mm": round(zone.interval_end_px * mm_per_pixel, 6),
                        "clearance_mm": round(zone.clearance_px * mm_per_pixel, 6),
                    }
                )
                for zone in payload.zones
            ],
            "metadata": {**payload.metadata, "mm_per_pixel": mm_per_pixel},
        }
    )


def _metric_free_region(free: FreeRegion, mm_per_pixel: float) -> FreeRegion:
    updates: dict[str, float] = {}
    if free.start_position_px is not None:
        updates["start_position_mm"] = round(free.start_position_px * mm_per_pixel, 6)
    if free.end_position_px is not None:
        updates["end_position_mm"] = round(free.end_position_px * mm_per_pixel, 6)
    if free.distance_from_origin_px is not None:
        updates["distance_from_origin_mm"] = round(free.distance_from_origin_px * mm_per_pixel, 6)
    return free.model_copy(update=updates)


def _inspection_result_payload(
    *,
    mm_per_pixel: float,
    measurements: FeatureMeasurementsPayload | None,
    zones: ClearanceZonesPayload | None,
    free: FreeRegionPayload | None,
    frame: CoordinateFramePayload | None,
    edges: ReferenceEdgesPayload | None,
    decision: QualityDecisionPayload | None,
) -> ResultPayload:
    result: dict[str, Any] = {"mm_per_pixel": mm_per_pixel}
    if edges is not None:
        result["reference_edges"] = [edge.model_dump(mode="json") for edge in edges.edges]
    if frame is not None:
        result["coordinate_frame"] = frame.frame.model_dump(mode="json")
    if measurements is not None:
        result["feature_measurements"] = [
            measurement.model_dump(mode="json") for measurement in measurements.measurements
        ]
        result["features"] = measurements.features
    if zones is not None:
        result["clearance_zones"] = [zone.model_dump(mode="json") for zone in zones.zones]
    if free is not None:
        free_region = free.free_region.model_dump(mode="json")
        result["free_region"] = free_region
        result["free_region_found"] = free.free_region.found
        result["distance_px"] = free.free_region.distance_from_origin_px
        result["distance_mm"] = free.free_region.distance_from_origin_mm
        result["distance_from_origin_px"] = free.free_region.distance_from_origin_px
        result["distance_from_origin_mm"] = free.free_region.distance_from_origin_mm
        result["start_position_px"] = free.free_region.start_position_px
        result["start_position_mm"] = free.free_region.start_position_mm
    if decision is not None:
        decision_payload = decision.decision.model_dump(mode="json")
        result["decision"] = decision_payload
        result["status"] = decision.decision.status
        result["result_label"] = decision.decision.result_label
    decision_bool = bool(result.get("free_region_found", True))
    if decision is not None:
        decision_bool = decision_bool and decision.decision.status == "pass"
    result.setdefault("result", decision_bool)
    return ResultPayload(result=result, decision=decision_bool)


def _quality_rules(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw = config.get("rules") or []
    return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _evaluate_quality_rule(
    rule: dict[str, Any],
    measurements: FeatureMeasurementsPayload | None,
    free: FreeRegionPayload | None,
) -> dict[str, Any]:
    rule_type = str(rule.get("rule_type") or "")
    feature_type = str(rule.get("feature_type") or "")
    operator = str(rule.get("operator") or "<=")
    threshold = _optional_float(rule.get("threshold")) or 0.0
    value: float = 0.0
    if rule_type == "feature_count_by_type":
        feature_ids = {
            measurement.feature_id
            for measurement in (measurements.measurements if measurements else [])
            if measurement.feature_type == feature_type
        }
        value = float(len(feature_ids))
    elif rule_type == "feature_size_by_type":
        basis = str(rule.get("measurement_basis") or "bbox_width")
        use_mm = bool(rule.get("use_mm", True))
        values = [
            measurement.value_mm
            if use_mm and measurement.value_mm is not None
            else measurement.value_px
            for measurement in (measurements.measurements if measurements else [])
            if measurement.feature_type == feature_type and measurement.basis == basis
        ]
        value = max([float(item) for item in values], default=0.0)
    elif rule_type == "total_feature_length_area_by_type":
        basis = str(rule.get("measurement_basis") or "axis_projected_length")
        use_mm = bool(rule.get("use_mm", True))
        values = [
            measurement.value_mm
            if use_mm and measurement.value_mm is not None
            else measurement.value_px
            for measurement in (measurements.measurements if measurements else [])
            if measurement.feature_type == feature_type and measurement.basis == basis
        ]
        value = sum(float(item) for item in values)
    elif rule_type == "free_region_distance":
        if free is not None:
            value = float(
                free.free_region.distance_from_origin_mm
                if bool(rule.get("use_mm", True))
                and free.free_region.distance_from_origin_mm is not None
                else free.free_region.distance_from_origin_px or 0.0
            )
    passed = _compare_numbers(value, operator, threshold)
    return {
        "rule": rule,
        "value": value,
        "threshold": threshold,
        "operator": operator,
        "passed": passed,
        "message": (
            f"{rule_type} passed with {value:g} {operator} {threshold:g}"
            if passed
            else f"{rule_type} failed with {value:g} not {operator} {threshold:g}"
        ),
    }


def _compare_numbers(value: float, operator: str, threshold: float) -> bool:
    if operator == "<":
        return value < threshold
    if operator == "<=":
        return value <= threshold
    if operator == ">":
        return value > threshold
    if operator == ">=":
        return value >= threshold
    if operator == "==":
        return value == threshold
    if operator == "!=":
        return value != threshold
    return value <= threshold


def _reference_edge_overlay_primitives(payload: ReferenceEdgesPayload) -> list[OverlayPrimitive]:
    primitives: list[OverlayPrimitive] = []
    candidate_lines = payload.metadata.get("candidate_lines")
    selected = set(_field_list(payload.metadata.get("selected_line_ids")))
    if isinstance(candidate_lines, list):
        for item in candidate_lines[:50]:
            if not isinstance(item, dict):
                continue
            line = item.get("line")
            if not isinstance(line, dict):
                continue
            line_id = str(item.get("line_id") or "")
            primitives.append(
                OverlayPrimitive(
                    primitive_type="line",
                    layer="cv_candidate_lines",
                    label="candidate" if line_id not in selected else "selected candidate",
                    confidence=_optional_float(item.get("score")),
                    geometry={
                        "start": line.get("start"),
                        "end": line.get("end"),
                        "coordinate_system": "pixel_xy",
                    },
                    image_context_id=payload.image_context_id,
                    style={
                        "stroke": "#9aa5ad" if line_id not in selected else "#ffffff",
                        "stroke_width": 1.2 if line_id not in selected else 1.8,
                    },
                    metadata=item,
                )
            )
    primitives.extend(
        [
            OverlayPrimitive(
                primitive_type="line",
                layer="reference_edges",
                label=edge.role,
                confidence=edge.confidence,
                geometry={
                    "start": edge.line.start.model_dump(mode="json"),
                    "end": edge.line.end.model_dump(mode="json"),
                    "coordinate_system": "pixel_xy",
                },
                image_context_id=edge.image_context_id,
                style={"stroke": "#5db7ff", "stroke_width": 2.5},
                metadata=edge.model_dump(mode="json"),
            )
            for edge in payload.edges
        ]
    )
    return primitives


def _cv_edge_overlay_primitives(payload: CvEdgesPayload) -> list[OverlayPrimitive]:
    primitives: list[OverlayPrimitive] = []
    if payload.edge_mask_frame is not None:
        primitives.append(
            OverlayPrimitive(
                primitive_type="mask",
                layer="cv_edge_mask",
                label="Canny edge mask",
                confidence=1.0 if payload.edge_pixel_count else 0.0,
                geometry={
                    "format": "png",
                    "data": payload.edge_mask_frame.image_base64_value(),
                    "width": payload.width,
                    "height": payload.height,
                    "edge_pixel_count": payload.edge_pixel_count,
                },
                image_context_id=payload.source_image_context_id,
                style={"opacity": 0.42, "color": "#ffffff"},
                metadata=payload.metadata,
            )
        )
    for contour in payload.contours[:40]:
        if len(contour.points) < 2:
            continue
        primitives.append(
            OverlayPrimitive(
                primitive_type="polygon",
                layer="cv_edge_contours",
                label="edge contour",
                confidence=contour.score,
                geometry={
                    "points": [point.model_dump(mode="json") for point in contour.points],
                    "coordinate_system": "pixel_xy",
                },
                image_context_id=payload.source_image_context_id,
                style={"stroke": "#5db7ff", "fill": "rgba(93,183,255,0.05)"},
                metadata=contour.model_dump(mode="json"),
            )
        )
    for candidate in payload.line_candidates[:50]:
        primitives.append(
            OverlayPrimitive(
                primitive_type="line",
                layer="cv_candidate_lines",
                label="candidate",
                confidence=candidate.score,
                geometry={
                    "start": candidate.line.start.model_dump(mode="json"),
                    "end": candidate.line.end.model_dump(mode="json"),
                    "coordinate_system": "pixel_xy",
                },
                image_context_id=payload.source_image_context_id,
                style={"stroke": "#9aa5ad", "stroke_width": 1.2},
                metadata=candidate.model_dump(mode="json"),
            )
        )
    return primitives


def _coordinate_frame_overlay_primitives(
    frame: InspectionCoordinateFrame,
) -> list[OverlayPrimitive]:
    length = float(
        frame.metadata.get("axis_display_length_px")
        or max(
            _optional_float(frame.metadata.get("image_width")) or 0,
            _optional_float(frame.metadata.get("image_height")) or 0,
            80,
        )
        * 0.25
    )
    origin = (frame.origin.x, frame.origin.y)
    long_end = (
        origin[0] + frame.longitudinal_axis.x * length,
        origin[1] + frame.longitudinal_axis.y * length,
    )
    trans_end = (
        origin[0] + frame.transverse_axis.x * (length * 0.5),
        origin[1] + frame.transverse_axis.y * (length * 0.5),
    )
    return [
        OverlayPrimitive(
            primitive_type="axis",
            layer="coordinate_frame",
            label="longitudinal",
            confidence=frame.confidence,
            geometry={
                "start": {"x": origin[0], "y": origin[1]},
                "end": {"x": long_end[0], "y": long_end[1]},
                "coordinate_system": "pixel_xy",
            },
            image_context_id=frame.image_context_id,
            style={"stroke": "#49c47a", "stroke_width": 2.5},
            metadata=frame.model_dump(mode="json"),
        ),
        OverlayPrimitive(
            primitive_type="axis",
            layer="coordinate_frame",
            label="transverse",
            confidence=frame.confidence,
            geometry={
                "start": {"x": origin[0], "y": origin[1]},
                "end": {"x": trans_end[0], "y": trans_end[1]},
                "coordinate_system": "pixel_xy",
            },
            image_context_id=frame.image_context_id,
            style={"stroke": "#f4b740", "stroke_width": 2},
            metadata=frame.model_dump(mode="json"),
        ),
    ]


def _feature_measurement_overlay_primitives(
    payload: FeatureMeasurementsPayload,
) -> list[OverlayPrimitive]:
    primitives: list[OverlayPrimitive] = []
    emitted_boxes: set[str] = set()
    for measurement in payload.measurements:
        bbox = measurement.method_metadata.get("bbox_px")
        if isinstance(bbox, dict) and measurement.feature_id not in emitted_boxes:
            primitives.append(
                OverlayPrimitive(
                    primitive_type="box",
                    layer="features",
                    label=measurement.feature_type,
                    confidence=measurement.confidence,
                    geometry={
                        "x_min": bbox.get("x_min"),
                        "y_min": bbox.get("y_min"),
                        "x_max": bbox.get("x_max"),
                        "y_max": bbox.get("y_max"),
                        "coordinate_system": "pixel_xy",
                    },
                    image_context_id=measurement.source_image_context_id,
                    object_id=measurement.source_object_id,
                    metadata={"feature_id": measurement.feature_id},
                )
            )
            emitted_boxes.add(measurement.feature_id)
        if measurement.basis in {"bbox_width", "axis_projected_width", "axis_projected_length"}:
            value = (
                measurement.value_mm if measurement.value_mm is not None else measurement.value_px
            )
            unit = "mm" if measurement.value_mm is not None else "px"
            primitives.append(
                OverlayPrimitive(
                    primitive_type="measurement",
                    layer="measurements",
                    label=f"{measurement.feature_type} {measurement.basis}: {value:g} {unit}",
                    geometry={},
                    image_context_id=measurement.source_image_context_id,
                    object_id=measurement.source_object_id,
                    metadata=measurement.model_dump(mode="json"),
                )
            )
    return primitives


def _clearance_zone_overlay_primitives(
    payload: ClearanceZonesPayload,
    frame: InspectionCoordinateFrame | None,
) -> list[OverlayPrimitive]:
    if frame is None:
        return [
            OverlayPrimitive(
                primitive_type="measurement",
                layer="clearance_zones",
                label=f"{zone.feature_type} clearance {zone.interval_start_px:g}-{zone.interval_end_px:g}px",
                geometry={},
                metadata=zone.model_dump(mode="json"),
            )
            for zone in payload.zones
        ]
    return [
        OverlayPrimitive(
            primitive_type="polygon",
            layer="clearance_zones",
            label=f"{zone.feature_type} clearance",
            geometry={
                "points": _interval_polygon(frame, zone.interval_start_px, zone.interval_end_px),
                "coordinate_system": "pixel_xy",
            },
            image_context_id=frame.image_context_id,
            style={"fill": "rgba(244,183,64,0.18)", "stroke": "#f4b740"},
            metadata=zone.model_dump(mode="json"),
        )
        for zone in payload.zones
    ]


def _free_region_overlay_primitives(
    payload: FreeRegionPayload,
    frame: InspectionCoordinateFrame | None,
) -> list[OverlayPrimitive]:
    free = payload.free_region
    if not free.found or free.start_position_px is None:
        return [
            OverlayPrimitive(
                primitive_type="label",
                layer="free_region",
                label=free.reason or "No free region found",
                geometry={},
                metadata=free.model_dump(mode="json"),
            )
        ]
    label_value = (
        free.distance_from_origin_mm
        if free.distance_from_origin_mm is not None
        else free.distance_from_origin_px
    )
    label_unit = "mm" if free.distance_from_origin_mm is not None else "px"
    if frame is None:
        return [
            OverlayPrimitive(
                primitive_type="measurement",
                layer="free_region",
                label=f"Free region distance {label_value:g} {label_unit}",
                geometry={},
                metadata=free.model_dump(mode="json"),
            )
        ]
    end_position = (
        free.end_position_px if free.end_position_px is not None else free.start_position_px
    )
    origin = (frame.origin.x, frame.origin.y)
    start_point = _point_on_frame_axis(frame, free.start_position_px)
    return [
        OverlayPrimitive(
            primitive_type="polygon",
            layer="free_region",
            label="first free region",
            geometry={
                "points": _interval_polygon(frame, free.start_position_px, end_position),
                "coordinate_system": "pixel_xy",
            },
            image_context_id=frame.image_context_id,
            style={"fill": "rgba(73,196,122,0.16)", "stroke": "#49c47a"},
            metadata=free.model_dump(mode="json"),
        ),
        OverlayPrimitive(
            primitive_type="arrow",
            layer="free_region_distance",
            label=f"{label_value:g} {label_unit}",
            geometry={
                "start": {"x": origin[0], "y": origin[1]},
                "end": {"x": start_point[0], "y": start_point[1]},
                "coordinate_system": "pixel_xy",
            },
            image_context_id=frame.image_context_id,
            style={"stroke": "#ffffff", "stroke_width": 2.5},
            metadata=free.model_dump(mode="json"),
        ),
    ]


def _inspection_overlay_from_result(result: dict[str, Any]) -> list[OverlayPrimitive]:
    primitives: list[OverlayPrimitive] = []
    frame = None
    frame_payload = result.get("coordinate_frame")
    if isinstance(frame_payload, dict):
        try:
            frame = InspectionCoordinateFrame.model_validate(frame_payload)
        except Exception:
            frame = None
    edges = result.get("reference_edges")
    if isinstance(edges, list):
        try:
            primitives.extend(
                _reference_edge_overlay_primitives(
                    ReferenceEdgesPayload(
                        edges=[
                            ReferenceEdge.model_validate(edge)
                            for edge in edges
                            if isinstance(edge, dict)
                        ]
                    )
                )
            )
        except Exception:
            pass
    if frame is not None:
        primitives.extend(_coordinate_frame_overlay_primitives(frame))
    measurements = result.get("feature_measurements")
    if isinstance(measurements, list):
        try:
            primitives.extend(
                _feature_measurement_overlay_primitives(
                    FeatureMeasurementsPayload(
                        measurements=[
                            FeatureMeasurement.model_validate(item)
                            for item in measurements
                            if isinstance(item, dict)
                        ],
                        features=result.get("features")
                        if isinstance(result.get("features"), list)
                        else [],
                    )
                )
            )
        except Exception:
            pass
    zones = result.get("clearance_zones")
    if isinstance(zones, list):
        try:
            primitives.extend(
                _clearance_zone_overlay_primitives(
                    ClearanceZonesPayload(
                        zones=[
                            ClearanceZone.model_validate(item)
                            for item in zones
                            if isinstance(item, dict)
                        ]
                    ),
                    frame,
                )
            )
        except Exception:
            pass
    free = result.get("free_region")
    if isinstance(free, dict):
        try:
            primitives.extend(
                _free_region_overlay_primitives(
                    FreeRegionPayload(free_region=FreeRegion.model_validate(free)),
                    frame,
                )
            )
        except Exception:
            pass
    decision = result.get("decision")
    if isinstance(decision, dict):
        primitives.append(
            OverlayPrimitive(
                primitive_type="label",
                layer="decision",
                label=f"{decision.get('result_label', 'Decision')}: {decision.get('status', 'unknown')}",
                geometry={},
                metadata=decision,
            )
        )
    return primitives


def _point_on_frame_axis(
    frame: InspectionCoordinateFrame, distance_px: float
) -> tuple[float, float]:
    return (
        frame.origin.x + frame.longitudinal_axis.x * distance_px,
        frame.origin.y + frame.longitudinal_axis.y * distance_px,
    )


def _interval_polygon(
    frame: InspectionCoordinateFrame,
    start_px: float,
    end_px: float,
) -> list[dict[str, float]]:
    image_width = _optional_float(frame.metadata.get("image_width")) or 200.0
    image_height = _optional_float(frame.metadata.get("image_height")) or 200.0
    half_extent = max(image_width, image_height) * 0.55
    start_center = _point_on_frame_axis(frame, start_px)
    end_center = _point_on_frame_axis(frame, end_px)
    transverse = (frame.transverse_axis.x, frame.transverse_axis.y)
    points = [
        (
            start_center[0] - transverse[0] * half_extent,
            start_center[1] - transverse[1] * half_extent,
        ),
        (
            end_center[0] - transverse[0] * half_extent,
            end_center[1] - transverse[1] * half_extent,
        ),
        (
            end_center[0] + transverse[0] * half_extent,
            end_center[1] + transverse[1] * half_extent,
        ),
        (
            start_center[0] + transverse[0] * half_extent,
            start_center[1] + transverse[1] * half_extent,
        ),
    ]
    return [{"x": x, "y": y} for x, y in points]


def _ensure_frame_context(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    frame: FramePayload,
) -> FramePayload:
    if frame.image_context is not None:
        return frame
    context_id = _stable_context_id(context, node.node_key, "original", frame.timestamp or "")
    image_context = RuntimeImageContext(
        context_id=context_id,
        kind="original",
        width=frame.width,
        height=frame.height,
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
                relationship="produced_original_context",
                node_key=node.node_key,
            )
        ],
        metadata={"correlation_id": context.correlation_id},
    )
    return frame.model_copy(update={"image_context": image_context})


def _derived_image_context(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    *,
    source: FramePayload,
    kind: str,
    transform_type: str,
    parameters: dict[str, Any],
    coordinate_projection: str,
    source_object_id: str | None = None,
) -> RuntimeImageContext:
    parent_context_id = _frame_context_id(source)
    context_id = _stable_context_id(
        context,
        node.node_key,
        kind,
        parent_context_id or "source",
        source_object_id or "",
        str(parameters),
    )
    return RuntimeImageContext(
        context_id=context_id,
        kind=kind,  # type: ignore[arg-type]
        width=parameters.get("width") if isinstance(parameters.get("width"), int) else None,
        height=parameters.get("height") if isinstance(parameters.get("height"), int) else None,
        source_uri=source.source_uri,
        source_node_key=node.node_key,
        source_image_context_id=parent_context_id,
        parent_image_context_id=parent_context_id,
        source_object_id=source_object_id,
        transform=RuntimeImageTransform(
            transform_type=transform_type,  # type: ignore[arg-type]
            parameters=parameters,
            coordinate_projection=coordinate_projection,  # type: ignore[arg-type]
        ),
        provenance=[
            ProvenanceLink(
                source_kind="image_context",
                source_id=parent_context_id or "unknown",
                relationship=f"derived_by_{transform_type}",
                node_key=node.node_key,
            )
        ]
        + (
            [
                ProvenanceLink(
                    source_kind="object",
                    source_id=source_object_id,
                    relationship="roi_source_object",
                    node_key=node.node_key,
                )
            ]
            if source_object_id
            else []
        ),
        metadata={"correlation_id": context.correlation_id},
    )


def _stable_context_id(context: RuntimeExecutionContext, *parts: object) -> str:
    raw = "|".join(str(part) for part in (context.correlation_id, *parts))
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"ctx_{digest}"


def _stable_object_id(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    image_context_id: str | None,
    index: int,
) -> str:
    raw = "|".join([context.correlation_id, node.node_key, image_context_id or "ctx", str(index)])
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"obj_{digest}"


def _frame_context_id(frame: FramePayload) -> str | None:
    return frame.image_context.context_id if frame.image_context is not None else None


def _source_object_id(frame: FramePayload) -> str | None:
    if frame.image_context is None:
        return None
    return frame.image_context.source_object_id


def _object_refs_from_detections(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    frame: FramePayload,
    detections: list[DetectionPrediction],
) -> list[RuntimeObjectReference]:
    image_context_id = _frame_context_id(frame)
    if image_context_id is None:
        return []
    return [
        RuntimeObjectReference(
            object_id=_stable_object_id(context, node, image_context_id, index),
            object_type="detection",
            source_image_context_id=image_context_id,
            geometry=detection.box.model_dump(mode="json"),
            label=detection.label,
            score=detection.score,
            producer_node_key=node.node_key,
            provenance=[
                ProvenanceLink(
                    source_kind="image_context",
                    source_id=image_context_id,
                    relationship="detected_on",
                    node_key=node.node_key,
                )
            ],
        )
        for index, detection in enumerate(detections, start=1)
    ]


def _object_refs_from_segments(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    frame: FramePayload,
    segments: list[SegmentationPrediction],
) -> list[RuntimeObjectReference]:
    image_context_id = _frame_context_id(frame)
    if image_context_id is None:
        return []
    objects: list[RuntimeObjectReference] = []
    for index, segment in enumerate(segments, start=1):
        geometry: dict[str, Any] = {
            "points": [point.model_dump(mode="json") for point in segment.polygon]
        }
        if segment.polygon:
            xs = [point.x for point in segment.polygon]
            ys = [point.y for point in segment.polygon]
            geometry["box"] = {
                "x_min": min(xs),
                "y_min": min(ys),
                "x_max": max(xs),
                "y_max": max(ys),
            }
        objects.append(
            RuntimeObjectReference(
                object_id=_stable_object_id(context, node, image_context_id, index),
                object_type="segment",
                source_image_context_id=image_context_id,
                geometry=geometry,
                coordinate_system="normalized_polygon",
                label=segment.label,
                score=segment.score,
                producer_node_key=node.node_key,
                provenance=[
                    ProvenanceLink(
                        source_kind="image_context",
                        source_id=image_context_id,
                        relationship="segmented_on",
                        node_key=node.node_key,
                    )
                ],
            )
        )
    return objects


def _classification_enrichments(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    frame: FramePayload,
    labels: list[ClassificationLabelScore],
) -> list[RuntimeObjectEnrichment]:
    target_object_id = _source_object_id(frame)
    if not target_object_id:
        return []
    image_context_id = _frame_context_id(frame)
    source_context_id = (
        frame.image_context.source_image_context_id
        if frame.image_context is not None
        else image_context_id
    )
    top_label = labels[0] if labels else None
    return [
        RuntimeObjectEnrichment(
            target_object_id=target_object_id,
            source_object_id=target_object_id,
            source_image_context_id=source_context_id,
            derived_image_context_id=image_context_id,
            enrichment_type="classification",
            attributes=[
                RuntimeObjectAttribute(
                    key="classification",
                    value=[label.model_dump(mode="json") for label in labels],
                    score=top_label.score if top_label else None,
                    label=top_label.label if top_label else None,
                    source_node_key=node.node_key,
                    source_image_context_id=image_context_id,
                )
            ],
            producer_node_key=node.node_key,
            provenance=[
                ProvenanceLink(
                    source_kind="object",
                    source_id=target_object_id,
                    relationship="enriched_by_roi_classification",
                    node_key=node.node_key,
                )
            ],
            metadata={"correlation_id": context.correlation_id},
        )
    ]


def _roi_provenance(
    frame: FramePayload,
    source_object_id: str | None,
    node_key: str,
) -> list[ProvenanceLink]:
    links: list[ProvenanceLink] = []
    if _frame_context_id(frame):
        links.append(
            ProvenanceLink(
                source_kind="image_context",
                source_id=str(_frame_context_id(frame)),
                relationship="cropped_from",
                node_key=node_key,
            )
        )
    if source_object_id:
        links.append(
            ProvenanceLink(
                source_kind="object",
                source_id=source_object_id,
                relationship="roi_of_object",
                node_key=node_key,
            )
        )
    return links


def _merged_config(context: RuntimeExecutionContext, node: RuntimeNodeSpec) -> dict[str, Any]:
    return {**(context.source_config or {}), **(node.config or {})}


def _prepare_inference_image(
    frame: FramePayload,
    binding: Any,
    *,
    task_type: TaskType,
    lossless: bool = False,
) -> PreparedInferenceImage:
    try:
        import cv2
        import numpy as np
    except ImportError:
        image_bytes = frame.image_bytes()
        source_width = max(int(_frame_width(frame) or 1), 1)
        source_height = max(int(_frame_height(frame) or 1), 1)
        return PreparedInferenceImage(
            image_bytes=image_bytes,
            mime_type=frame.mime_type,
            source_width=source_width,
            source_height=source_height,
            input_width=source_width,
            input_height=source_height,
        )

    raw_frame = frame.cached_cv2_frame()
    if raw_frame is None:
        original_bytes = frame.image_bytes()
        decoded = cv2.imdecode(np.frombuffer(original_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if decoded is None:
            raise RuntimeError("OpenCV could not decode the runtime inference image.")
        source = decoded
    else:
        original_bytes = None
        source = np.asarray(raw_frame)
        if source.ndim == 2:
            source = cv2.cvtColor(source, cv2.COLOR_GRAY2BGR)
        elif source.ndim == 3 and source.shape[2] == 4:
            source = cv2.cvtColor(source, cv2.COLOR_BGRA2BGR)
        if source.ndim != 3 or source.shape[2] != 3:
            raise RuntimeError("Runtime inference requires a BGR frame with three channels.")

    source_height, source_width = (int(source.shape[0]), int(source.shape[1]))
    preprocessing = (
        dict(binding.capability.get("preprocessing") or {})
        if isinstance(binding.capability, dict)
        else {}
    )
    target = _preprocessing_target_size(preprocessing)
    target_width, target_height = target or (source_width, source_height)
    letterboxed = bool(
        target
        and (target_width, target_height) != (source_width, source_height)
        and _uses_letterbox_preprocessing(binding, preprocessing, task_type=task_type)
    )
    scale = 1.0
    pad_left = 0
    pad_top = 0
    prepared_frame = source
    if target and (target_width, target_height) != (source_width, source_height):
        if letterboxed:
            scale = min(target_width / source_width, target_height / source_height)
            resized_width = min(max(int(round(source_width * scale)), 1), target_width)
            resized_height = min(max(int(round(source_height * scale)), 1), target_height)
            interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
            resized = cv2.resize(
                source,
                (resized_width, resized_height),
                interpolation=interpolation,
            )
            padding = preprocessing.get("letterbox")
            padding_value = 114
            if isinstance(padding, dict):
                try:
                    padding_value = min(max(int(padding.get("padding_value", 114)), 0), 255)
                except (TypeError, ValueError):
                    padding_value = 114
            prepared_frame = np.full(
                (target_height, target_width, 3),
                padding_value,
                dtype=np.uint8,
            )
            pad_left = (target_width - resized_width) // 2
            pad_top = (target_height - resized_height) // 2
            prepared_frame[
                pad_top : pad_top + resized_height,
                pad_left : pad_left + resized_width,
            ] = resized
        else:
            interpolation = (
                cv2.INTER_AREA
                if source_width > target_width or source_height > target_height
                else cv2.INTER_LINEAR
            )
            prepared_frame = cv2.resize(
                source,
                (target_width, target_height),
                interpolation=interpolation,
            )
    elif original_bytes is not None and frame.mime_type == "image/jpeg" and not lossless:
        return PreparedInferenceImage(
            image_bytes=original_bytes,
            mime_type=frame.mime_type,
            source_width=source_width,
            source_height=source_height,
            input_width=target_width,
            input_height=target_height,
        )

    success, encoded = cv2.imencode(
        ".png" if lossless else ".jpg",
        np.ascontiguousarray(prepared_frame),
        [int(cv2.IMWRITE_JPEG_QUALITY), frame.jpeg_quality()],
    )
    if not success:
        raise RuntimeError("OpenCV could not encode the prepared runtime inference image.")
    return PreparedInferenceImage(
        image_bytes=encoded.tobytes(),
        mime_type="image/png" if lossless else "image/jpeg",
        source_width=source_width,
        source_height=source_height,
        input_width=target_width,
        input_height=target_height,
        scale=scale,
        pad_left=pad_left,
        pad_top=pad_top,
        letterboxed=letterboxed,
    )


def _validate_inference_image_view(
    frame: FramePayload,
    binding: Any,
    *,
    node: RuntimeNodeSpec,
) -> dict[str, Any]:
    """Describe training-view differences without blocking generic segmentation reuse."""
    preprocessing = (
        dict(binding.capability.get("preprocessing") or {})
        if isinstance(binding.capability, dict)
        else {}
    )
    raw_requirement = preprocessing.get("image_view")
    requirement = raw_requirement if isinstance(raw_requirement, dict) else None
    report_requested = bool(
        (node.config or {}).get("require_image_view_compatibility", False)
    )
    frame_profile_id = frame.metadata.get("view_profile_id")
    frame_checksum = frame.metadata.get("view_profile_checksum")
    if requirement is None:
        return {
            "status": "TRAINING_VIEW_NOT_DECLARED",
            "blocking": False,
            "report_requested": report_requested,
            "training_view": None,
            "runtime_view": {
                "view_profile_id": frame_profile_id,
                "profile_checksum": frame_checksum,
                "input_width": frame.width,
                "input_height": frame.height,
                "color_space": frame.metadata.get("color_space"),
            },
            "mismatches": {},
        }
    comparisons = {
        "view_profile_id": (requirement.get("view_profile_id"), frame_profile_id),
        "profile_checksum": (requirement.get("profile_checksum"), frame_checksum),
        "input_width": (requirement.get("input_width"), frame.width),
        "input_height": (requirement.get("input_height"), frame.height),
        "color_space": (requirement.get("color_space"), frame.metadata.get("color_space")),
        "undistort_preprocessing_version": (
            requirement.get("undistort_preprocessing_version"),
            frame.metadata.get("undistort_preprocessing_version"),
        ),
        "image_view_preprocessing_version": (
            requirement.get("image_view_preprocessing_version"),
            frame.metadata.get("image_view_preprocessing_version"),
        ),
    }
    mismatches = {
        key: {"expected": expected, "actual": actual}
        for key, (expected, actual) in comparisons.items()
        if expected is not None and expected != actual
    }
    return {
        "status": "MATCH" if not mismatches else "DIFFERENT_RUNTIME_VIEW",
        "blocking": False,
        "report_requested": report_requested,
        "training_view": requirement,
        "runtime_view": {key: actual for key, (_expected, actual) in comparisons.items()},
        "mismatches": mismatches,
    }


def _preprocessing_target_size(preprocessing: dict[str, Any]) -> tuple[int, int] | None:
    resize = preprocessing.get("resize")
    try:
        if isinstance(resize, dict):
            width = int(resize.get("width") or resize.get("size") or 0)
            height = int(resize.get("height") or resize.get("size") or width)
            return (width, height) if width > 0 and height > 0 else None
        if resize is not None:
            size = int(resize)
            return (size, size) if size > 0 else None
    except (TypeError, ValueError):
        return None
    return None


def _uses_letterbox_preprocessing(
    binding: Any,
    preprocessing: dict[str, Any],
    *,
    task_type: TaskType,
) -> bool:
    mode = str(preprocessing.get("resize_mode") or "").strip().lower()
    if mode in {"letterbox", "keep_aspect_ratio"}:
        return True
    if mode in {"stretch", "resize"}:
        return False
    letterbox = preprocessing.get("letterbox")
    if isinstance(letterbox, dict) and "enabled" in letterbox:
        return bool(letterbox.get("enabled"))
    if isinstance(letterbox, bool):
        return letterbox
    if task_type not in {
        TaskType.DETECTION,
        TaskType.ORIENTED_DETECTION,
        TaskType.SEGMENTATION,
    }:
        return False
    capability = binding.capability if isinstance(binding.capability, dict) else {}
    architecture = capability.get("architecture") or {}
    family = architecture.get("family") if isinstance(architecture, dict) else architecture
    trainer_backend = str(capability.get("trainer_backend") or "").lower()
    return trainer_backend in {"ultralytics-yolo", "openmmlab-mmyolo"} or str(
        family or ""
    ).lower() == "yolo"


def _restore_detection_geometry(
    detections: list[DetectionPrediction],
    prepared: PreparedInferenceImage,
) -> list[DetectionPrediction]:
    if not prepared.letterboxed:
        return detections
    restored: list[DetectionPrediction] = []
    for detection in detections:
        box = detection.box
        x_min, y_min = _restore_inference_point(box.x_min, box.y_min, prepared)
        x_max, y_max = _restore_inference_point(box.x_max, box.y_max, prepared)
        oriented_box = detection.oriented_box
        if oriented_box is not None:
            oriented_box = oriented_box.model_copy(
                update={
                    "points": [
                        DetectionPoint(x=x, y=y)
                        for x, y in (
                            _restore_inference_point(point.x, point.y, prepared)
                            for point in oriented_box.points
                        )
                    ]
                }
            )
        restored.append(
            detection.model_copy(
                update={
                    "box": DetectionBox(
                        x_min=min(x_min, x_max),
                        y_min=min(y_min, y_max),
                        x_max=max(x_min, x_max),
                        y_max=max(y_min, y_max),
                    ),
                    "oriented_box": oriented_box,
                }
            )
        )
    return restored


def _restore_segmentation_geometry(
    segments: list[SegmentationPrediction],
    prepared: PreparedInferenceImage,
) -> list[SegmentationPrediction]:
    from cv_runtime.segmentation_masks import restore_mask

    return [
        segment.model_copy(
            update={
                "mask": restore_mask(
                    segment.mask,
                    source_size=(prepared.source_width, prepared.source_height),
                    input_size=(prepared.input_width, prepared.input_height),
                    padding=(prepared.pad_left, prepared.pad_top),
                    scale=prepared.scale,
                    letterboxed=prepared.letterboxed,
                )
                if segment.mask is not None
                else None,
                "polygon": [
                    SegmentationPoint(x=x, y=y)
                    for x, y in (
                        _restore_inference_point(point.x, point.y, prepared)
                        for point in segment.polygon
                    )
                ]
                if prepared.letterboxed
                else segment.polygon,
            }
        )
        for segment in segments
    ]


def _restore_inference_point(
    x: float,
    y: float,
    prepared: PreparedInferenceImage,
) -> tuple[float, float]:
    source_x = (
        (float(x) * prepared.input_width - prepared.pad_left)
        / max(prepared.scale, 1e-12)
        / prepared.source_width
    )
    source_y = (
        (float(y) * prepared.input_height - prepared.pad_top)
        / max(prepared.scale, 1e-12)
        / prepared.source_height
    )
    return min(max(source_x, 0.0), 1.0), min(max(source_y, 0.0), 1.0)


def _resolved_model_metadata(binding) -> dict[str, Any]:
    return {
        "deployment_release_id": binding.deployment_release_id,
        "deployment_target": binding.deployment_target,
        "target_path": binding.target_path.value,
        "model_family_slug": binding.model_family_slug,
        "model_version_id": binding.model_version_id,
        "task_type": binding.task_type.value,
        "contract_major": binding.contract_major,
        "display_name": binding.display_name,
        "capability": binding.capability,
    }


def _elapsed_ms(started_perf: float) -> float:
    return round(max((time.perf_counter() - started_perf) * 1000, 0.0), 3)


def _runtime_inference_timings(
    *,
    model_resolve_ms: float,
    request_image_prepare_ms: float | None = None,
    client_roundtrip_ms: float,
    response: Any,
    binary_transport: bool | None = None,
) -> dict[str, float]:
    timings = {
        "model_resolve_ms": model_resolve_ms,
        "client_roundtrip_ms": client_roundtrip_ms,
    }
    if request_image_prepare_ms is not None:
        timings["request_image_prepare_ms"] = request_image_prepare_ms
    if binary_transport is not None:
        timings["binary_transport"] = 1.0 if binary_transport else 0.0
    gateway_timing = getattr(response, "timing", None)
    if gateway_timing is None:
        return timings
    if hasattr(gateway_timing, "model_dump"):
        timing_values = gateway_timing.model_dump(mode="json")
    elif isinstance(gateway_timing, dict):
        timing_values = gateway_timing
    else:
        return timings
    for key, value in timing_values.items():
        if not isinstance(value, int | float):
            continue
        timing_key = f"gateway_{key}" if not str(key).startswith("gateway_") else str(key)
        timings[timing_key] = round(float(value), 3)
    return timings


def _trigger_payload(context: RuntimeExecutionContext, body: dict[str, Any]) -> Any:
    from cv_runtime.payloads import TriggerPayload

    return TriggerPayload(trigger_id=context.correlation_id, body=body)


def _node_error(
    node: RuntimeNodeSpec,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    metadata: dict[str, Any] | None = None,
) -> RuntimeNodeExecutionError:
    return RuntimeNodeExecutionError(
        code=code,
        message=message,
        node_key=node.node_key,
        node_type=node.node_type,
        retryable=retryable,
        metadata=metadata or {},
    )


def _frame_from_path(path: Path, *, timestamp: str | None = None) -> FramePayload:
    if not path.exists() or not path.is_file():
        raise RuntimeNodeExecutionError(
            code="source_file_missing",
            message=f"Source image file does not exist: {path}",
            metadata={"path": str(path)},
        )
    data = path.read_bytes()
    image = Image.open(io.BytesIO(data))
    mime_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return FramePayload.from_bytes(
        data,
        mime_type=mime_type,
        width=image.width,
        height=image.height,
        source_uri=str(path),
        timestamp=timestamp,
    )


def _open_frame(frame: FramePayload, *, copy: bool = True) -> Image.Image:
    cached = frame.cached_image()
    if cached is not None:
        return cached.copy() if copy else cached
    image = Image.open(io.BytesIO(frame.image_bytes())).convert("RGB")
    frame.set_cached_image(image)
    return image.copy() if copy else image


def _frame_from_image(
    image: Image.Image,
    *,
    source: FramePayload | None = None,
    metadata: dict[str, Any] | None = None,
    image_context: RuntimeImageContext | None = None,
    image_format: str = "JPEG",
    mime_type: str = "image/jpeg",
) -> FramePayload:
    if image_context is not None and (image_context.width is None or image_context.height is None):
        image_context = image_context.model_copy(
            update={"width": image.width, "height": image.height}
        )
    return FramePayload.from_image(
        image,
        image_format=image_format,
        mime_type=mime_type,
        jpeg_quality=int((metadata or {}).get("jpeg_quality") or 80),
        source_uri=source.source_uri if source else None,
        timestamp=source.timestamp if source else None,
        image_context=image_context,
        metadata={**(source.metadata if source else {}), **(metadata or {})},
    )


def _required_frame(node: RuntimeNodeSpec, inputs: NodeInputs, key: str) -> FramePayload:
    payload = inputs.get(key)
    if not isinstance(payload, FramePayload):
        raise _node_error(
            node, "frame_input_missing", f"{node.node_type} requires frame input {key}."
        )
    return payload


def _required_model_ref(node: RuntimeNodeSpec):
    if node.logical_model_ref is None:
        raise _node_error(
            node, "model_reference_missing", "Inference node requires a logical model reference."
        )
    return node.logical_model_ref


def _inference_timeout_ms(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    *,
    fallback_ms: int = 3000,
) -> int:
    config = node.config or {}
    raw = config.get("timeout_ms")
    if raw is None or raw == "":
        raw = context.metadata.get("default_inference_timeout_ms")
    if raw is None or raw == "":
        raw = fallback_ms
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        parsed = fallback_ms
    return min(max(parsed, 50), 30_000)


def _crop_normalized(image: Image.Image, box: DetectionBox) -> Image.Image:
    width, height = image.size
    left = int(max(0, min(box.x_min, 1)) * width)
    top = int(max(0, min(box.y_min, 1)) * height)
    right = int(max(0, min(box.x_max, 1)) * width)
    bottom = int(max(0, min(box.y_max, 1)) * height)
    if right <= left:
        right = min(left + 1, width)
    if bottom <= top:
        bottom = min(top + 1, height)
    return image.crop((left, top, right, bottom))


def _pad_box(box: DetectionBox, padding_percent: float) -> DetectionBox:
    pad_x = (box.x_max - box.x_min) * (padding_percent / 100)
    pad_y = (box.y_max - box.y_min) * (padding_percent / 100)
    return DetectionBox(
        x_min=max(box.x_min - pad_x, 0),
        y_min=max(box.y_min - pad_y, 0),
        x_max=min(box.x_max + pad_x, 1),
        y_max=min(box.y_max + pad_y, 1),
    )


def _spatial_objects_from_payload(payload: object) -> list[SpatialObject]:
    if isinstance(payload, DetectionPayload):
        objects: list[SpatialObject] = []
        for index, detection in enumerate(payload.detections, start=1):
            object_ref = payload.objects[index - 1] if index <= len(payload.objects) else None
            objects.append(
                SpatialObject(
                    key=object_ref.object_id if object_ref is not None else f"detection_{index}",
                    label=detection.label,
                    score=detection.score,
                    box=detection.box,
                    polygon=[],
                    source_image_context_id=payload.image_context_id,
                    source_object_id=object_ref.object_id if object_ref is not None else None,
                    source_node_key=object_ref.producer_node_key
                    if object_ref is not None
                    else None,
                    metadata={
                        "payload_type": payload.payload_type,
                        "object_ref": object_ref.model_dump(mode="json")
                        if object_ref is not None
                        else None,
                        "image_width": payload.image_width,
                        "image_height": payload.image_height,
                    },
                )
            )
        return objects
    if isinstance(payload, SegmentPayload):
        objects = []
        for index, segment in enumerate(payload.segments, start=1):
            if not segment.polygon:
                continue
            polygon = [(point.x, point.y) for point in segment.polygon]
            xs = [point[0] for point in polygon]
            ys = [point[1] for point in polygon]
            object_ref = payload.objects[index - 1] if index <= len(payload.objects) else None
            objects.append(
                SpatialObject(
                    key=object_ref.object_id if object_ref is not None else f"segment_{index}",
                    label=segment.label,
                    score=segment.score,
                    box=DetectionBox(
                        x_min=min(xs),
                        y_min=min(ys),
                        x_max=max(xs),
                        y_max=max(ys),
                    ),
                    polygon=polygon,
                    source_image_context_id=payload.image_context_id,
                    source_object_id=object_ref.object_id if object_ref is not None else None,
                    source_node_key=object_ref.producer_node_key
                    if object_ref is not None
                    else None,
                    metadata={
                        "payload_type": payload.payload_type,
                        "object_ref": object_ref.model_dump(mode="json")
                        if object_ref is not None
                        else None,
                        "image_width": payload.image_width,
                        "image_height": payload.image_height,
                    },
                )
            )
        return objects
    if isinstance(payload, RegionSetPayload):
        return [
            SpatialObject(
                key=region.source_object_id or region.key or f"region_{index}",
                label=region.label,
                score=region.score,
                box=region.box,
                polygon=_spatial_polygon_from_region(region),
                source_image_context_id=region.source_image_context_id,
                source_object_id=region.source_object_id,
                source_node_key=region.source_node_key,
                metadata=region.metadata,
            )
            for index, region in enumerate(payload.regions, start=1)
        ]
    return []


def _spatial_polygon_from_region(region: Region) -> list[tuple[float, float]]:
    object_ref = region.metadata.get("object_ref")
    if not isinstance(object_ref, dict):
        return []
    geometry = object_ref.get("geometry")
    if not isinstance(geometry, dict):
        return []
    points = geometry.get("points")
    if not isinstance(points, list):
        return []
    polygon: list[tuple[float, float]] = []
    for point in points:
        if not isinstance(point, dict):
            continue
        x = _optional_float(point.get("x"))
        y = _optional_float(point.get("y"))
        if x is not None and y is not None:
            polygon.append((x, y))
    return polygon


def _spatial_rules_from_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw_rules = config.get("rules")
    if isinstance(raw_rules, list) and raw_rules:
        return [dict(rule) for rule in raw_rules if isinstance(rule, dict)]
    relation = str(config.get("relation") or "distance")
    return [
        {
            "rule_key": config.get("rule_key") or relation,
            "relation": relation,
            "enabled": True,
            "primary_labels": config.get("primary_labels") or config.get("labels") or [],
            "secondary_labels": config.get("secondary_labels") or [],
            "label_pairing": config.get("label_pairing") or "any",
            "max_distance_percent": config.get("max_distance_percent", 10),
            "min_distance_percent": config.get("min_distance_percent"),
            "min_overlap_percent": config.get("min_overlap_percent", 1),
            "min_closing_percent": config.get("min_closing_percent", 1),
            "zone_key": config.get("zone_key") or "",
            "zone_match": config.get("zone_match") or "center_inside",
            "min_count": config.get("min_count", 1),
            "max_count": config.get("max_count"),
            "signal_label": config.get("signal_label") or relation,
        }
    ]


def _zone_detection_rules_from_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    rules = []
    for rule in _spatial_rules_from_config(config):
        normalized = dict(rule)
        normalized["relation"] = "zone_occupancy"
        normalized.setdefault("zone_match", config.get("zone_match") or "center_inside")
        normalized.setdefault("min_count", 1)
        rules.append(normalized)
    return rules


def _spatial_rule_key(rule: dict[str, Any], index: int) -> str:
    raw = str(rule.get("rule_key") or rule.get("key") or rule.get("signal_label") or "").strip()
    if raw:
        return re.sub(r"[^a-zA-Z0-9_.-]+", "_", raw).strip("_") or f"rule_{index}"
    return f"rule_{index}"


def _spatial_rule_labels(rule: dict[str, Any], key: str) -> list[str]:
    raw = rule.get(key)
    if raw in (None, "", []):
        return []
    return [label.casefold() for label in _field_list(raw)]


def _spatial_label_matches(label: str | None, filters: list[str]) -> bool:
    return not filters or (label or "").casefold() in filters


def _spatial_candidate_pairs(
    primary_objects: list[SpatialObject],
    secondary_objects: list[SpatialObject],
    *,
    same_object_set: bool,
    rule: dict[str, Any],
) -> list[tuple[SpatialObject, SpatialObject]]:
    primary_labels = _spatial_rule_labels(rule, "primary_labels")
    secondary_labels = _spatial_rule_labels(rule, "secondary_labels")
    label_pairing = str(rule.get("label_pairing") or "any")
    pairs: list[tuple[SpatialObject, SpatialObject]] = []
    for primary_index, primary in enumerate(primary_objects):
        if not _spatial_label_matches(primary.label, primary_labels):
            continue
        for secondary_index, secondary in enumerate(secondary_objects):
            if same_object_set and secondary_index <= primary_index:
                continue
            if _spatial_same_object(primary, secondary):
                continue
            if not _spatial_label_matches(secondary.label, secondary_labels):
                continue
            same_label = (primary.label or "").casefold() == (secondary.label or "").casefold()
            if label_pairing == "same_label" and not same_label:
                continue
            if label_pairing == "different_label" and same_label:
                continue
            pairs.append((primary, secondary))
    return pairs


def _spatial_same_object(primary: SpatialObject, secondary: SpatialObject) -> bool:
    if primary.key and primary.key == secondary.key:
        return True
    return bool(
        primary.source_object_id
        and secondary.source_object_id
        and primary.source_object_id == secondary.source_object_id
    )


def _evaluate_spatial_distance(
    rule: dict[str, Any],
    *,
    rule_key: str,
    pair_index: int,
    primary: SpatialObject,
    secondary: SpatialObject,
    distance_percent: float,
) -> dict[str, Any] | None:
    max_distance = _optional_float(rule.get("max_distance_percent"))
    min_distance = _optional_float(rule.get("min_distance_percent"))
    if max_distance is not None and distance_percent > max_distance:
        return None
    if min_distance is not None and distance_percent < min_distance:
        return None
    return _spatial_pair_match(
        rule,
        rule_key=rule_key,
        pair_index=pair_index,
        relation="distance",
        primary=primary,
        secondary=secondary,
        values={"distance_percent": round(distance_percent, 4)},
    )


def _evaluate_spatial_overlap(
    rule: dict[str, Any],
    *,
    rule_key: str,
    pair_index: int,
    primary: SpatialObject,
    secondary: SpatialObject,
) -> dict[str, Any] | None:
    overlap_percent = _spatial_overlap_percent(primary, secondary)
    minimum = _optional_float(rule.get("min_overlap_percent"))
    if overlap_percent < (minimum if minimum is not None else 1.0):
        return None
    return _spatial_pair_match(
        rule,
        rule_key=rule_key,
        pair_index=pair_index,
        relation="overlap",
        primary=primary,
        secondary=secondary,
        values={"overlap_percent": round(overlap_percent, 4)},
    )


def _evaluate_spatial_approach(
    rule: dict[str, Any],
    *,
    rule_key: str,
    pair_index: int,
    primary: SpatialObject,
    secondary: SpatialObject,
    distance_percent: float,
    previous_pairs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    previous = _find_previous_spatial_pair(rule, rule_key, primary, secondary, previous_pairs)
    if previous is None:
        return None
    previous_distance = _optional_float(previous.get("distance_percent"))
    if previous_distance is None:
        return None
    closing_percent = previous_distance - distance_percent
    minimum_closing = _optional_float(rule.get("min_closing_percent"))
    if closing_percent < (minimum_closing if minimum_closing is not None else 1.0):
        return None
    max_distance = _optional_float(rule.get("max_distance_percent"))
    if max_distance is not None and distance_percent > max_distance:
        return None
    return _spatial_pair_match(
        rule,
        rule_key=rule_key,
        pair_index=pair_index,
        relation="approaching",
        primary=primary,
        secondary=secondary,
        values={
            "distance_percent": round(distance_percent, 4),
            "previous_distance_percent": round(previous_distance, 4),
            "closing_percent": round(closing_percent, 4),
            "frames_seen": int(previous.get("frames_seen") or 1) + 1,
        },
    )


def _spatial_pair_match(
    rule: dict[str, Any],
    *,
    rule_key: str,
    pair_index: int,
    relation: str,
    primary: SpatialObject,
    secondary: SpatialObject,
    values: dict[str, Any],
) -> dict[str, Any]:
    return {
        "match_key": f"{rule_key}_{pair_index}",
        "rule_key": rule_key,
        "relation": relation,
        "signal_label": str(rule.get("signal_label") or rule_key),
        "primary": _spatial_object_summary(primary),
        "secondary": _spatial_object_summary(secondary),
        **values,
    }


def _evaluate_spatial_zone_rule(
    rule: dict[str, Any],
    *,
    rule_key: str,
    objects: list[SpatialObject],
    zones: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[OverlayPrimitive]]:
    selected_zones = _spatial_selected_zones(rule, zones)
    if not selected_zones:
        return [], []
    labels = _spatial_rule_labels(rule, "primary_labels") or _spatial_rule_labels(rule, "labels")
    zone_match = str(rule.get("zone_match") or "center_inside")
    min_count = max(int(rule.get("min_count") or 1), 1)
    max_count = _optional_int(rule.get("max_count"))
    matches: list[dict[str, Any]] = []
    primitives: list[OverlayPrimitive] = []
    for zone in selected_zones:
        zone_box = zone["box"]
        zone_objects = [
            item
            for item in objects
            if _spatial_label_matches(item.label, labels)
            and _spatial_object_in_zone(
                item,
                zone_box,
                mode=zone_match,
                min_overlap_percent=_optional_float(rule.get("min_overlap_percent")),
            )
        ]
        primitives.append(_spatial_zone_primitive(zone, len(zone_objects)))
        if len(zone_objects) < min_count:
            continue
        if max_count is not None and len(zone_objects) > max_count:
            continue
        matches.append(
            {
                "match_key": f"{rule_key}_{zone['key']}",
                "rule_key": rule_key,
                "relation": "zone_occupancy",
                "signal_label": str(rule.get("signal_label") or rule_key),
                "zone": {
                    "key": zone["key"],
                    "label": zone["label"],
                    "box": zone_box.model_dump(mode="json"),
                },
                "zone_match": zone_match,
                "min_overlap_percent": _optional_float(rule.get("min_overlap_percent")),
                "count": len(zone_objects),
                "min_count": min_count,
                "max_count": max_count,
                "objects": [_spatial_object_summary(item) for item in zone_objects],
            }
        )
        primitives.extend(
            _spatial_object_box_primitive(item, layer="spatial_zone_object")
            for item in zone_objects
        )
    return matches, primitives


def _spatial_selected_zones(
    rule: dict[str, Any],
    zones: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    zone_key = str(rule.get("zone_key") or rule.get("zone") or "").strip()
    if not zone_key:
        return zones
    return [
        zone
        for zone in zones
        if zone_key.casefold() in {str(zone["key"]).casefold(), str(zone["label"]).casefold()}
    ]


def _spatial_object_in_zone(
    item: SpatialObject,
    zone: DetectionBox,
    *,
    mode: str,
    min_overlap_percent: float | None = None,
) -> bool:
    if mode == "fully_inside":
        return (
            item.box.x_min >= zone.x_min
            and item.box.y_min >= zone.y_min
            and item.box.x_max <= zone.x_max
            and item.box.y_max <= zone.y_max
        )
    if mode == "box_overlaps":
        intersection = _box_intersection_area(item.box, zone)
        if intersection <= 0:
            return False
        minimum = min_overlap_percent if min_overlap_percent is not None else 0.0
        if minimum <= 0:
            return True
        object_area = _box_area(item.box)
        if object_area <= 0:
            return False
        return (intersection / object_area) * 100.0 >= minimum
    x, y = _box_center(item.box)
    return zone.x_min <= x <= zone.x_max and zone.y_min <= y <= zone.y_max


def _spatial_pair_snapshot(
    rule_key: str,
    primary: SpatialObject,
    secondary: SpatialObject,
    distance_percent: float,
) -> dict[str, Any]:
    primary_center = _box_center(primary.box)
    secondary_center = _box_center(secondary.box)
    return {
        "rule_key": rule_key,
        "primary_key": primary.key,
        "secondary_key": secondary.key,
        "primary_label": primary.label,
        "secondary_label": secondary.label,
        "primary_center": {"x": primary_center[0], "y": primary_center[1]},
        "secondary_center": {"x": secondary_center[0], "y": secondary_center[1]},
        "distance_percent": distance_percent,
        "frames_seen": 1,
    }


def _find_previous_spatial_pair(
    rule: dict[str, Any],
    rule_key: str,
    primary: SpatialObject,
    secondary: SpatialObject,
    previous_pairs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    threshold = _optional_float(rule.get("max_pair_shift_percent"))
    max_shift = threshold if threshold is not None else 15.0
    best: tuple[float, dict[str, Any]] | None = None
    for previous in previous_pairs:
        if previous.get("rule_key") != rule_key:
            continue
        if (previous.get("primary_label") or None) != primary.label:
            continue
        if (previous.get("secondary_label") or None) != secondary.label:
            continue
        shift = _spatial_pair_shift_percent(previous, primary, secondary)
        if shift > max_shift:
            continue
        if best is None or shift < best[0]:
            best = (shift, previous)
    return best[1] if best is not None else None


def _spatial_pair_shift_percent(
    previous: dict[str, Any],
    primary: SpatialObject,
    secondary: SpatialObject,
) -> float:
    previous_primary = previous.get("primary_center")
    previous_secondary = previous.get("secondary_center")
    if not isinstance(previous_primary, dict) or not isinstance(previous_secondary, dict):
        return 100.0
    primary_center = _box_center(primary.box)
    secondary_center = _box_center(secondary.box)
    primary_shift = _center_distance_percent(previous_primary, primary_center)
    secondary_shift = _center_distance_percent(previous_secondary, secondary_center)
    return max(primary_shift, secondary_shift)


def _center_distance_percent(previous: dict[str, Any], current: tuple[float, float]) -> float:
    x = _optional_float(previous.get("x"))
    y = _optional_float(previous.get("y"))
    if x is None or y is None:
        return 100.0
    return _normalized_distance_percent(math.hypot(current[0] - x, current[1] - y))


def _spatial_object_summary(item: SpatialObject) -> dict[str, Any]:
    x, y = _box_center(item.box)
    summary = {
        "key": item.key,
        "label": item.label,
        "score": item.score,
        "center": {"x": round(x, 6), "y": round(y, 6)},
        "box": item.box.model_dump(mode="json"),
        "source_image_context_id": item.source_image_context_id,
        "source_object_id": item.source_object_id,
        "source_node_key": item.source_node_key,
    }
    if isinstance(item.metadata, dict):
        if "zones" in item.metadata:
            summary["zones"] = item.metadata.get("zones")
        if "zone_status" in item.metadata:
            summary["zone_status"] = item.metadata.get("zone_status")
    return summary


def _spatial_pair_overlay_primitives(
    match: dict[str, Any],
    primary: SpatialObject,
    secondary: SpatialObject,
) -> list[OverlayPrimitive]:
    primary_center = _box_center(primary.box)
    secondary_center = _box_center(secondary.box)
    relation = str(match.get("relation") or "spatial")
    label = str(match.get("signal_label") or relation)
    line_label = label
    if match.get("distance_percent") is not None:
        line_label = f"{label}: {float(match['distance_percent']):g}%"
    return [
        _spatial_object_box_primitive(primary, layer=f"spatial_{relation}"),
        _spatial_object_box_primitive(secondary, layer=f"spatial_{relation}"),
        OverlayPrimitive(
            primitive_type="line",
            layer="spatial_distance",
            label=line_label,
            geometry={
                "start": {"x": primary_center[0], "y": primary_center[1]},
                "end": {"x": secondary_center[0], "y": secondary_center[1]},
                "coordinate_system": "normalized_xy",
            },
            image_context_id=primary.source_image_context_id or secondary.source_image_context_id,
            object_id=primary.source_object_id,
            source_object_id=secondary.source_object_id,
            style={"stroke": "#f4b740", "stroke_width": 2},
            metadata=match,
        ),
    ]


def _spatial_object_box_primitive(item: SpatialObject, *, layer: str) -> OverlayPrimitive:
    return OverlayPrimitive(
        primitive_type="box",
        layer=layer,
        label=item.label,
        confidence=item.score,
        geometry=item.box.model_dump(mode="json"),
        image_context_id=item.source_image_context_id,
        object_id=item.source_object_id,
        metadata={"object": _spatial_object_summary(item)},
    )


def _spatial_zone_primitive(zone: dict[str, Any], count: int) -> OverlayPrimitive:
    box = zone["box"]
    geometry = box.model_dump(mode="json")
    angle_degrees = float(zone.get("angle_degrees") or 0.0)
    if abs(angle_degrees) > 1e-9:
        geometry["angle_degrees"] = angle_degrees
    return OverlayPrimitive(
        primitive_type="zone",
        layer="spatial_field",
        label=f"{zone['label']}: {count}",
        geometry=geometry,
        style={"fill": "rgba(244,183,64,0.14)", "stroke": "#f4b740"},
        metadata={
            "zone": {
                "key": zone["key"],
                "label": zone["label"],
                "angle_degrees": angle_degrees,
            },
            "count": count,
        },
    )


def _spatial_distance_percent(primary: SpatialObject, secondary: SpatialObject) -> float:
    distance = _polygon_distance(primary.polygon, secondary.polygon)
    if distance is None:
        distance = _box_edge_distance(primary.box, secondary.box)
    return round(_normalized_distance_percent(distance), 6)


def _normalized_distance_percent(distance: float) -> float:
    return (max(distance, 0.0) / math.sqrt(2.0)) * 100.0


def _spatial_overlap_percent(primary: SpatialObject, secondary: SpatialObject) -> float:
    if len(primary.polygon) >= 3 and len(secondary.polygon) >= 3:
        polygon_overlap = _polygon_overlap_percent(primary.polygon, secondary.polygon)
        if polygon_overlap is not None:
            return polygon_overlap
    intersection = _box_intersection_area(primary.box, secondary.box)
    smaller = min(_box_area(primary.box), _box_area(secondary.box))
    if smaller <= 0:
        return 0.0
    return (intersection / smaller) * 100.0


def _box_center(box: DetectionBox) -> tuple[float, float]:
    return ((box.x_min + box.x_max) / 2, (box.y_min + box.y_max) / 2)


def _box_area(box: DetectionBox) -> float:
    return max(box.x_max - box.x_min, 0.0) * max(box.y_max - box.y_min, 0.0)


def _box_intersection_area(first: DetectionBox, second: DetectionBox) -> float:
    width = max(min(first.x_max, second.x_max) - max(first.x_min, second.x_min), 0.0)
    height = max(min(first.y_max, second.y_max) - max(first.y_min, second.y_min), 0.0)
    return width * height


def _box_edge_distance(first: DetectionBox, second: DetectionBox) -> float:
    dx = max(first.x_min - second.x_max, second.x_min - first.x_max, 0.0)
    dy = max(first.y_min - second.y_max, second.y_min - first.y_max, 0.0)
    return math.hypot(dx, dy)


def _polygon_overlap_percent(
    first: list[tuple[float, float]],
    second: list[tuple[float, float]],
) -> float | None:
    x_min = min([point[0] for point in first] + [point[0] for point in second])
    y_min = min([point[1] for point in first] + [point[1] for point in second])
    x_max = max([point[0] for point in first] + [point[0] for point in second])
    y_max = max([point[1] for point in first] + [point[1] for point in second])
    width = x_max - x_min
    height = y_max - y_min
    if width <= 0 or height <= 0:
        return None
    grid = 32
    both = 0
    first_count = 0
    second_count = 0
    for x_index in range(grid):
        x = x_min + ((x_index + 0.5) / grid) * width
        for y_index in range(grid):
            y = y_min + ((y_index + 0.5) / grid) * height
            in_first = _point_in_polygon((x, y), first)
            in_second = _point_in_polygon((x, y), second)
            if in_first:
                first_count += 1
            if in_second:
                second_count += 1
            if in_first and in_second:
                both += 1
    smaller = min(first_count, second_count)
    if smaller <= 0:
        return None
    return (both / smaller) * 100.0


def _polygon_distance(
    first: list[tuple[float, float]],
    second: list[tuple[float, float]],
) -> float | None:
    if len(first) < 3 or len(second) < 3:
        return None
    if any(_point_in_polygon(point, second) for point in first) or any(
        _point_in_polygon(point, first) for point in second
    ):
        return 0.0
    distances = []
    first_edges = _polygon_edges(first)
    second_edges = _polygon_edges(second)
    for edge_a in first_edges:
        for edge_b in second_edges:
            distances.append(_segment_distance(edge_a[0], edge_a[1], edge_b[0], edge_b[1]))
    return min(distances) if distances else None


def _polygon_edges(
    polygon: list[tuple[float, float]],
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    return list(zip(polygon, polygon[1:] + polygon[:1], strict=False))


def _segment_distance(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> float:
    if _segments_intersect(a1, a2, b1, b2):
        return 0.0
    return min(
        _point_to_segment_distance(a1, b1, b2),
        _point_to_segment_distance(a2, b1, b2),
        _point_to_segment_distance(b1, a1, a2),
        _point_to_segment_distance(b2, a1, a2),
    )


def _point_to_segment_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    px, py = point
    sx, sy = start
    ex, ey = end
    dx = ex - sx
    dy = ey - sy
    denom = dx * dx + dy * dy
    if denom <= 0:
        return math.hypot(px - sx, py - sy)
    t = max(0.0, min(((px - sx) * dx + (py - sy) * dy) / denom, 1.0))
    projected_x = sx + t * dx
    projected_y = sy + t * dy
    return math.hypot(px - projected_x, py - projected_y)


def _segments_intersect(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> bool:
    def orientation(
        p: tuple[float, float],
        q: tuple[float, float],
        r: tuple[float, float],
    ) -> float:
        return (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])

    def on_segment(
        p: tuple[float, float],
        q: tuple[float, float],
        r: tuple[float, float],
    ) -> bool:
        return min(p[0], r[0]) <= q[0] <= max(p[0], r[0]) and min(p[1], r[1]) <= q[1] <= max(
            p[1], r[1]
        )

    o1 = orientation(a1, a2, b1)
    o2 = orientation(a1, a2, b2)
    o3 = orientation(b1, b2, a1)
    o4 = orientation(b1, b2, a2)
    if abs(o1) <= 1e-12 and on_segment(a1, b1, a2):
        return True
    if abs(o2) <= 1e-12 and on_segment(a1, b2, a2):
        return True
    if abs(o3) <= 1e-12 and on_segment(b1, a1, b2):
        return True
    if abs(o4) <= 1e-12 and on_segment(b1, a2, b2):
        return True
    return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0)


def _point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    j = len(polygon) - 1
    for i, current in enumerate(polygon):
        previous = polygon[j]
        if ((current[1] > y) != (previous[1] > y)) and (
            x
            < (previous[0] - current[0]) * (y - current[1]) / ((previous[1] - current[1]) or 1e-12)
            + current[0]
        ):
            inside = not inside
        j = i
    return inside


def _detections_from_payload(payload: object) -> list[DetectionPrediction]:
    if isinstance(payload, DetectionPayload):
        return payload.detections
    if isinstance(payload, RegionSetPayload):
        return [
            DetectionPrediction(
                label=region.label or "object", score=region.score or 1.0, box=region.box
            )
            for region in payload.regions
        ]
    return []


def _regions_from_payload(payload: object) -> list[Region]:
    if isinstance(payload, RegionSetPayload):
        return payload.regions
    if isinstance(payload, DetectionPayload):
        regions: list[Region] = []
        for index, detection in enumerate(payload.detections, start=1):
            object_ref = payload.objects[index - 1] if index <= len(payload.objects) else None
            regions.append(
                Region(
                    key=f"detection_{index}",
                    label=detection.label,
                    score=detection.score,
                    box=detection.box,
                    source_image_context_id=payload.image_context_id,
                    source_object_id=object_ref.object_id if object_ref is not None else None,
                    source_node_key=object_ref.producer_node_key
                    if object_ref is not None
                    else None,
                    metadata={
                        "object_ref": object_ref.model_dump(mode="json")
                        if object_ref is not None
                        else None,
                        "image_width": payload.image_width,
                        "image_height": payload.image_height,
                    },
                )
            )
        return regions
    if isinstance(payload, SegmentPayload):
        regions = []
        for index, segment in enumerate(payload.segments, start=1):
            if not segment.polygon:
                continue
            xs = [point.x for point in segment.polygon]
            ys = [point.y for point in segment.polygon]
            object_ref = payload.objects[index - 1] if index <= len(payload.objects) else None
            regions.append(
                Region(
                    key=f"segment_{index}",
                    label=segment.label,
                    score=segment.score,
                    box=DetectionBox(
                        x_min=min(xs),
                        y_min=min(ys),
                        x_max=max(xs),
                        y_max=max(ys),
                    ),
                    source_image_context_id=payload.image_context_id,
                    source_object_id=object_ref.object_id if object_ref is not None else None,
                    source_node_key=object_ref.producer_node_key
                    if object_ref is not None
                    else None,
                    metadata={
                        "object_ref": object_ref.model_dump(mode="json")
                        if object_ref is not None
                        else None,
                        "image_width": payload.image_width,
                        "image_height": payload.image_height,
                    },
                )
            )
        return regions
    return []


def _configured_regions(config: dict[str, Any]) -> list[Region]:
    regions = []
    for index, item in enumerate(config.get("regions") or config.get("rois") or [], start=1):
        if not isinstance(item, dict):
            continue
        box = _box_from_mapping(item)
        if box is not None:
            regions.append(
                Region(
                    key=str(item.get("key") or f"configured_{index}"),
                    label=item.get("label"),
                    box=box,
                )
            )
    return regions


def _box_from_mapping(item: dict[str, Any]) -> DetectionBox | None:
    try:
        if {"x_min", "y_min", "x_max", "y_max"}.issubset(item):
            return DetectionBox(
                x_min=float(item["x_min"]),
                y_min=float(item["y_min"]),
                x_max=float(item["x_max"]),
                y_max=float(item["y_max"]),
            )
        if {"x", "y", "width", "height"}.issubset(item):
            x = float(item["x"])
            y = float(item["y"])
            return DetectionBox(
                x_min=x,
                y_min=y,
                x_max=min(x + float(item["width"]), 1),
                y_max=min(y + float(item["height"]), 1),
            )
    except (TypeError, ValueError):
        return None
    return None


def _zones_from_config(config: dict[str, Any]) -> list[DetectionBox]:
    return [item["box"] for item in _named_zones_from_config(config)]


def _named_zones_from_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    zones = []
    for index, item in enumerate(config.get("zones") or [], start=1):
        if not isinstance(item, dict):
            continue
        box = _box_from_mapping(item)
        if box is None:
            continue
        zones.append(
            {
                "key": str(item.get("key") or f"zone_{index}"),
                "label": str(item.get("label") or item.get("key") or f"zone_{index}"),
                "box": box,
                "angle_degrees": _box_angle_degrees(item),
            }
        )
    return zones


def _box_angle_degrees(item: dict[str, Any]) -> float:
    for key in ("angle_degrees", "rotation_degrees", "angle"):
        parsed = _optional_float(item.get(key))
        if parsed is not None:
            return _normalize_angle_degrees(parsed)
    nested_box = item.get("box")
    if isinstance(nested_box, dict):
        for key in ("angle_degrees", "rotation_degrees", "angle"):
            parsed = _optional_float(nested_box.get(key))
            if parsed is not None:
                return _normalize_angle_degrees(parsed)
    return 0.0


def _normalize_angle_degrees(value: float) -> float:
    normalized = ((value + 180.0) % 360.0) - 180.0
    return round(normalized, 6)


def _box_in_any_zone(box: DetectionBox, zones: list[DetectionBox]) -> bool:
    x = (box.x_min + box.x_max) / 2
    y = (box.y_min + box.y_max) / 2
    return _point_in_any_zone(x, y, zones)


def _match_zone_label(box: DetectionBox, zones: list[dict[str, Any]]) -> str | None:
    x = (box.x_min + box.x_max) / 2
    y = (box.y_min + box.y_max) / 2
    for zone in zones:
        zone_box = zone["box"]
        if zone_box.x_min <= x <= zone_box.x_max and zone_box.y_min <= y <= zone_box.y_max:
            return str(zone["label"])
    return None


def _point_in_any_zone(x: float, y: float, zones: list[DetectionBox]) -> bool:
    return any(zone.x_min <= x <= zone.x_max and zone.y_min <= y <= zone.y_max for zone in zones)


def _positions_from_payload(payload: object) -> list[PositionPayload]:
    if isinstance(payload, PositionsPayload):
        return payload.positions
    return [
        PositionPayload(
            key=region.key or f"position_{index}",
            label=region.label,
            x=(region.box.x_min + region.box.x_max) / 2,
            y=(region.box.y_min + region.box.y_max) / 2,
            box=region.box,
            source_image_context_id=region.source_image_context_id,
            source_object_id=region.source_object_id,
        )
        for index, region in enumerate(_regions_from_payload(payload), start=1)
    ]


def _measurements_from_payload(payload: object) -> list[MeasurementPayload]:
    return payload.measurements if isinstance(payload, MeasurementsPayload) else []


def _rois_from_payload(payload: object) -> list[RoiPayload]:
    return payload.rois if isinstance(payload, RoiSetPayload) else []


def _direction_from_delta(dx: float, dy: float) -> str:
    if abs(dx) < 0.001 and abs(dy) < 0.001:
        return "stationary"
    if abs(dx) > abs(dy):
        return "right" if dx > 0 else "left"
    return "down" if dy > 0 else "up"


def _result_from_payload(payload: object) -> ResultPayload:
    if isinstance(payload, ResultPayload):
        return payload
    if isinstance(payload, DetectionPayload):
        return ResultPayload(
            result={
                "count": len(payload.detections),
                "detections": [item.model_dump(mode="json") for item in payload.detections],
                "objects": [item.model_dump(mode="json") for item in payload.objects],
                "image_context_id": payload.image_context_id,
            },
            decision=bool(payload.detections),
        )
    if isinstance(payload, ClassificationPayload):
        top = payload.labels[0].model_dump(mode="json") if payload.labels else None
        top_label = top.get("label") if isinstance(top, dict) else None
        top_score = top.get("score") if isinstance(top, dict) else None
        return ResultPayload(
            result={
                "count": len(payload.labels),
                "labels": [item.model_dump(mode="json") for item in payload.labels],
                "top_label": top,
                "class": top_label,
                "classification": top_label,
                "score": top_score,
                "image_context_id": payload.image_context_id,
                "source_object_id": payload.source_object_id,
                "enrichments": [item.model_dump(mode="json") for item in payload.enrichments],
            },
            decision=bool(top),
        )
    if isinstance(payload, SegmentPayload):
        return ResultPayload(
            result={
                "count": len(payload.segments),
                "segments": [item.model_dump(mode="json") for item in payload.segments],
                "objects": [item.model_dump(mode="json") for item in payload.objects],
                "image_context_id": payload.image_context_id,
            },
            decision=bool(payload.segments),
        )
    if isinstance(payload, StemHorizontalCurvaturePayload):
        curvature = payload.model_dump(mode="json")
        return ResultPayload(
            result={
                "stem_horizontal_curvature": curvature,
                "valid": payload.valid,
                "observation_state": payload.observation_state,
                "track_id": payload.track_id,
                "signed_bow_mm": payload.signed_bow_mm,
                "curvature_rms_mm": payload.curvature_rms_mm,
                "curvature_maxabs_mm": payload.curvature_maxabs_mm,
                "curvature_peak_to_peak_mm": payload.curvature_peak_to_peak_mm,
                "geometry_confidence": payload.geometry_confidence,
                "bow_shape_hint": payload.bow_shape_hint,
                "common_segment_comparable": (
                    payload.common_segment_descriptor.comparable
                ),
            },
            decision=payload.valid,
        )
    if isinstance(payload, StemSurfacePhasePayload):
        phase = payload.model_dump(mode="json")
        return ResultPayload(
            result={
                "stem_surface_phase": phase,
                "raw_phase_valid": payload.raw_phase_valid,
                "calibrated_phase_valid": payload.calibrated_phase_valid,
                "phase_locked": payload.phase_locked,
                "track_id": payload.track_id,
                "delta_raw_phase": payload.delta_raw_phase,
                "cumulative_raw_phase": payload.cumulative_raw_phase,
                "delta_phase_deg": payload.delta_phase_deg,
                "cumulative_phase_deg": payload.cumulative_phase_deg,
                "tracking_mode": payload.tracking_mode,
                "relock_status": payload.relock_status,
                "confidence": payload.confidence,
                "reason_codes": payload.reason_codes,
            },
            decision=payload.calibrated_phase_valid and payload.phase_locked,
        )
    if isinstance(payload, StemCurvaturePhaseSolutionPayload):
        if payload.orientation_result is not None:
            return ResultPayload(
                result=payload.orientation_result.model_dump(mode="json"),
                decision=True,
            )
        solution = payload.model_dump(mode="json")
        return ResultPayload(
            result={
                "stem_curvature_phase_solution": solution,
                "session_id": payload.session_id,
                "track_id": payload.track_id,
                "state": payload.state,
                "observation_count": payload.observation_count,
                "observed_phase_span_deg": payload.observed_phase_span_deg,
                "curvature_frozen": payload.curvature_frozen,
                "decision_gate_reached": payload.decision_gate_reached,
                "target_phase_deg": payload.target_phase_deg,
                "maximum_phase_deg": payload.maximum_phase_deg,
                "fitted_min_curvature_mm": payload.fitted_min_curvature_mm,
                "fitted_max_curvature_mm": payload.fitted_max_curvature_mm,
                "curvature_span_mm": payload.curvature_span_mm,
                "minimum_unique": payload.minimum_unique,
                "bow_relevant": payload.bow_relevant,
                "confidence": payload.confidence,
                "reason_codes": payload.reason_codes,
            },
            decision=(
                payload.state in {"SOLVED", "FROZEN"}
                and payload.minimum_unique
                and payload.bow_relevant is True
            ),
        )
    if isinstance(payload, RegionSetPayload):
        image_context_id = payload.metadata.get("image_context_id") or next(
            (
                region.source_image_context_id
                for region in payload.regions
                if region.source_image_context_id
            ),
            None,
        )
        counts_by_label = _region_counts_by_label(payload.regions)
        objects = [_region_result_object(region) for region in payload.regions]
        result: dict[str, Any] = {
            "count": len(payload.regions),
            "count_total": len(payload.regions),
            "counts_by_label": counts_by_label,
            "objects": objects,
            "regions": objects,
            "image_context_id": image_context_id,
            "source": payload.metadata.get("source"),
            "algorithm": payload.metadata.get("algorithm"),
        }
        for measurement in _count_measurements_from_regions(
            payload.regions,
            image_context_id=str(image_context_id) if image_context_id else None,
        ):
            result[measurement.key] = measurement.value
        return ResultPayload(result=result, decision=bool(payload.regions))
    if isinstance(payload, AnomalyPayload):
        return ResultPayload(
            result={
                "anomaly_score": payload.anomaly_score,
                "predicted_label": payload.predicted_label,
                "is_anomalous": payload.is_anomalous,
                "threshold": payload.threshold,
                "image_context_id": payload.image_context_id,
                "image_width": payload.image_width,
                "image_height": payload.image_height,
                "heatmap": payload.heatmap.model_dump(mode="json"),
                "mask": payload.mask.model_dump(mode="json") if payload.mask is not None else None,
                "route": payload.route,
                "model": payload.metadata,
            },
            decision=payload.is_anomalous,
        )
    if isinstance(payload, MeasurementsPayload):
        result: dict[str, Any] = {}
        for item in payload.measurements:
            result[item.key] = item.value
        if "count_total" not in result and payload.measurements:
            numeric = [
                item.value for item in payload.measurements if isinstance(item.value, int | float)
            ]
            if numeric:
                result["count"] = numeric[0]
        return ResultPayload(result=result, decision=bool(result))
    if isinstance(payload, ReferenceEdgesPayload):
        return ResultPayload(
            result={
                "reference_edges": [edge.model_dump(mode="json") for edge in payload.edges],
                "edge_count": len(payload.edges),
                "image_context_id": payload.image_context_id,
            },
            decision=bool(payload.edges),
        )
    if isinstance(payload, CvEdgesPayload):
        return ResultPayload(
            result={
                "cv_edges": {
                    "image_context_id": payload.image_context_id,
                    "source_image_context_id": payload.source_image_context_id,
                    "edge_pixel_count": payload.edge_pixel_count,
                    "contour_count": len(payload.contours),
                    "line_candidate_count": len(payload.line_candidates),
                    "width": payload.width,
                    "height": payload.height,
                    "algorithm": payload.algorithm,
                    "metadata": payload.metadata,
                },
                "edge_pixel_count": payload.edge_pixel_count,
                "contour_count": len(payload.contours),
                "line_candidate_count": len(payload.line_candidates),
                "image_context_id": payload.source_image_context_id or payload.image_context_id,
            },
            decision=payload.edge_pixel_count > 0,
        )
    if isinstance(payload, CoordinateFramePayload):
        return ResultPayload(
            result={"coordinate_frame": payload.frame.model_dump(mode="json")},
            decision=True,
        )
    if isinstance(payload, FeatureMeasurementsPayload):
        result = {
            "feature_measurements": [
                measurement.model_dump(mode="json") for measurement in payload.measurements
            ],
            "features": payload.features,
            "feature_count": len({measurement.feature_id for measurement in payload.measurements}),
        }
        for measurement in payload.measurements:
            key = f"{measurement.feature_type}_{measurement.basis}_px"
            result.setdefault(key, measurement.value_px)
            if measurement.value_mm is not None:
                result.setdefault(
                    f"{measurement.feature_type}_{measurement.basis}_mm", measurement.value_mm
                )
        return ResultPayload(result=result, decision=bool(payload.measurements))
    if isinstance(payload, ClearanceZonesPayload):
        return ResultPayload(
            result={
                "clearance_zones": [zone.model_dump(mode="json") for zone in payload.zones],
                "clearance_zone_count": len(payload.zones),
            },
            decision=True,
        )
    if isinstance(payload, FreeRegionPayload):
        free = payload.free_region
        return ResultPayload(
            result={
                "free_region": free.model_dump(mode="json"),
                "free_region_found": free.found,
                "distance_px": free.distance_from_origin_px,
                "distance_mm": free.distance_from_origin_mm,
                "distance_from_origin_px": free.distance_from_origin_px,
                "distance_from_origin_mm": free.distance_from_origin_mm,
                "start_position_px": free.start_position_px,
                "start_position_mm": free.start_position_mm,
            },
            decision=free.found,
        )
    if isinstance(payload, QualityDecisionPayload):
        decision = payload.decision
        return ResultPayload(
            result={
                "status": decision.status,
                "result_label": decision.result_label,
                "result": decision.status == "pass",
                "evaluated_rules": decision.evaluated_rules,
                "messages": decision.messages,
                **decision.selected_output_values,
                "decision": decision.model_dump(mode="json"),
            },
            decision=decision.status == "pass",
        )
    if isinstance(payload, EventsPayload):
        return ResultPayload(
            result={"events": [item.model_dump(mode="json") for item in payload.events]},
            decision=bool(payload.events),
        )
    if isinstance(payload, FramePayload):
        return ResultPayload(
            result={
                "frame": {
                    "width": payload.width,
                    "height": payload.height,
                    "source_uri": payload.source_uri,
                }
            },
            decision=True,
        )
    return ResultPayload(result={}, decision=None)


def _mapped_result_output(result: ResultPayload, config: dict[str, Any]) -> dict[str, Any]:
    mappings = config.get("result_fields") or config.get("response_fields")
    output: dict[str, Any] = {}
    if (
        result.result.get("schema_version") == "stem.orientation.v3"
        and not mappings
        and "fields" not in config
    ):
        output = dict(result.result)
    if isinstance(mappings, list):
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            source_field = str(mapping.get("source_field") or "").strip()
            output_key = str(
                mapping.get("output_key") or mapping.get("response_key") or source_field
            ).strip()
            if not source_field or not output_key:
                continue
            value = _lookup_result_output_value(result, source_field)
            if value is None:
                if mapping.get("default_value") is not None:
                    value = mapping.get("default_value")
                elif bool(mapping.get("required", False)):
                    output[output_key] = None
                else:
                    continue
            output[output_key] = value
    if not output:
        fields = _field_list(config.get("fields"))
        output = {
            field: value
            for field in fields
            if (value := _lookup_result_output_value(result, field)) is not None
        }
    if not output:
        output = dict(result.result)
    if bool(config.get("include_decision", True)) and result.decision is not None:
        output.setdefault("result", result.decision)
        output.setdefault("decision", result.decision)
    output_key = str(config.get("wrap_key") or "").strip()
    if output_key:
        return {output_key: output}
    return output


def _lookup_result_output_value(result: ResultPayload, key: str) -> Any:
    if key == "decision":
        return result.decision
    return _lookup_result_value(result.result, key)


def _evaluate_condition(expression: str, result: dict[str, Any]) -> bool:
    normalized = expression.strip().lower()
    if normalized in {"pass", "true", "always"}:
        return True
    if normalized in {"false", "never"}:
        return False
    if normalized in {"result == true", "result is true"}:
        return bool(result.get("result"))
    if normalized in {"result == false", "result is false"}:
        return not bool(result.get("result"))
    match = re.fullmatch(
        r"([a-zA-Z0-9_.-]+)\s*(==|!=|>=|<=|>|<)\s*([a-zA-Z0-9_.-]+)", expression.strip()
    )
    if not match:
        return bool(result.get("result", result))
    left_key, operator, right_raw = match.groups()
    left = _lookup_result_value(result, left_key)
    right: Any = right_raw
    if right_raw.lower() in {"true", "false"}:
        right = right_raw.lower() == "true"
    else:
        try:
            right = float(right_raw)
        except ValueError:
            right = right_raw
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    try:
        left_number = float(left)
        right_number = float(right)
    except (TypeError, ValueError):
        return False
    if operator == ">":
        return left_number > right_number
    if operator == "<":
        return left_number < right_number
    if operator == ">=":
        return left_number >= right_number
    if operator == "<=":
        return left_number <= right_number
    return False


def _lookup_result_value(result: dict[str, Any], key: str) -> Any:
    if key == "count" and "count" not in result:
        detections = result.get("detections")
        segments = result.get("segments")
        if isinstance(detections, list):
            return len(detections)
        if isinstance(segments, list):
            return len(segments)
    cursor: Any = result
    for part in key.split("."):
        if isinstance(cursor, dict):
            cursor = cursor.get(part)
        else:
            return None
    return cursor


def _project_ids(node: RuntimeNodeSpec) -> list[str]:
    raw = (node.config or {}).get("target_project_ids") or []
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return [str(item) for item in raw]


def _field_list(raw: object) -> list[str]:
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return ["result", "count", "direction", "segment_area"]


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _config_bool(value: object, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _clamp_float(
    value: float | None,
    minimum: float,
    maximum: float,
    default: float,
) -> float:
    parsed = default if value is None else value
    return min(max(parsed, minimum), maximum)


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _overlay_primitives(payload: object) -> list[OverlayPrimitive]:
    primitives: list[OverlayPrimitive] = []
    if isinstance(payload, OverlayPayload):
        return list(payload.primitives)
    if isinstance(payload, DetectionPayload):
        for index, item in enumerate(payload.detections, start=1):
            object_ref = payload.objects[index - 1] if index <= len(payload.objects) else None
            primitives.append(
                OverlayPrimitive(
                    primitive_type="box",
                    layer="detections",
                    label=item.label,
                    confidence=item.score,
                    geometry={
                        **item.box.model_dump(mode="json"),
                        "coordinate_system": "normalized_xy",
                    },
                    image_context_id=payload.image_context_id,
                    object_id=object_ref.object_id if object_ref is not None else None,
                    metadata={
                        "layer": "detections",
                        "object_ref": object_ref.model_dump(mode="json")
                        if object_ref is not None
                        else None,
                    },
                )
            )
    elif isinstance(payload, RegionSetPayload):
        primitives.extend(_color_object_overlay_primitives(payload.regions, []))
    elif isinstance(payload, ClassificationPayload):
        if payload.enrichments:
            for enrichment in payload.enrichments:
                label_attribute = next(
                    (
                        attribute
                        for attribute in enrichment.attributes
                        if attribute.key == "classification"
                    ),
                    None,
                )
                primitives.append(
                    OverlayPrimitive(
                        primitive_type="label",
                        label=label_attribute.label if label_attribute else None,
                        confidence=label_attribute.score if label_attribute else None,
                        geometry={},
                        image_context_id=enrichment.source_image_context_id,
                        object_id=enrichment.target_object_id,
                        source_object_id=enrichment.source_object_id,
                        metadata={
                            "layer": "enriched_labels",
                            "enrichment": enrichment.model_dump(mode="json"),
                        },
                    )
                )
        else:
            top = payload.labels[0] if payload.labels else None
            primitives.append(
                OverlayPrimitive(
                    primitive_type="label",
                    label=top.label if top else None,
                    confidence=top.score if top else None,
                    geometry={},
                    image_context_id=payload.image_context_id,
                    source_object_id=payload.source_object_id,
                    metadata={"layer": "labels"},
                )
            )
    elif isinstance(payload, SegmentPayload):
        for index, item in enumerate(payload.segments, start=1):
            object_ref = payload.objects[index - 1] if index <= len(payload.objects) else None
            primitives.append(
                OverlayPrimitive(
                    primitive_type="polygon",
                    layer="segmentation",
                    label=item.label,
                    confidence=item.score,
                    geometry={
                        "points": [point.model_dump(mode="json") for point in item.polygon],
                        "coordinate_system": "normalized_xy",
                    },
                    image_context_id=payload.image_context_id,
                    object_id=object_ref.object_id if object_ref is not None else None,
                    metadata={
                        "layer": "segmentation",
                        "object_ref": object_ref.model_dump(mode="json")
                        if object_ref is not None
                        else None,
                    },
                )
            )
    elif isinstance(payload, AnomalyPayload):
        opacity = float(payload.metadata.get("heatmap_opacity") or 0.55)
        primitives.append(
            OverlayPrimitive(
                primitive_type="heatmap",
                layer="anomaly_heatmap",
                label=payload.predicted_label,
                confidence=min(float(payload.anomaly_score), 1.0),
                geometry={
                    "format": payload.heatmap.format,
                    "data": payload.heatmap.data,
                    "width": payload.heatmap.width,
                    "height": payload.heatmap.height,
                    "min_value": payload.heatmap.min_value,
                    "max_value": payload.heatmap.max_value,
                    "colormap": payload.heatmap.colormap,
                    "threshold": payload.threshold,
                },
                image_context_id=payload.image_context_id,
                style={"opacity": max(0.0, min(opacity, 1.0)), "blend_mode": "screen"},
                metadata={
                    "layer": "anomaly_heatmap",
                    "anomaly_score": payload.anomaly_score,
                    "is_anomalous": payload.is_anomalous,
                    "route": payload.route,
                },
            )
        )
        if payload.mask is not None:
            primitives.append(
                OverlayPrimitive(
                    primitive_type="mask",
                    layer="anomaly_mask",
                    label="Anomaly mask",
                    confidence=min(float(payload.anomaly_score), 1.0),
                    geometry={
                        "format": payload.mask.format,
                        "data": payload.mask.data,
                        "width": payload.mask.width,
                        "height": payload.mask.height,
                        "threshold": payload.mask.threshold,
                    },
                    image_context_id=payload.image_context_id,
                    style={"opacity": 0.35, "color": "#ff3158"},
                    metadata={"layer": "anomaly_mask"},
                )
            )
    elif isinstance(payload, PositionsPayload):
        primitives.extend(
            OverlayPrimitive(
                primitive_type="point",
                label=item.label,
                geometry={"x": item.x, "y": item.y},
                image_context_id=item.source_image_context_id,
                object_id=item.source_object_id,
            )
            for item in payload.positions
        )
    elif isinstance(payload, MeasurementsPayload):
        primitives.extend(
            OverlayPrimitive(
                primitive_type="measurement",
                label=item.label or item.key,
                geometry={},
                image_context_id=item.source_image_context_id,
                object_id=item.source_object_id,
                metadata=item.model_dump(mode="json"),
            )
            for item in payload.measurements
        )
    elif isinstance(payload, ReferenceEdgesPayload):
        primitives.extend(_reference_edge_overlay_primitives(payload))
    elif isinstance(payload, CvEdgesPayload):
        primitives.extend(_cv_edge_overlay_primitives(payload))
    elif isinstance(payload, CoordinateFramePayload):
        primitives.extend(_coordinate_frame_overlay_primitives(payload.frame))
    elif isinstance(payload, FeatureMeasurementsPayload):
        primitives.extend(_feature_measurement_overlay_primitives(payload))
    elif isinstance(payload, ClearanceZonesPayload):
        primitives.extend(_clearance_zone_overlay_primitives(payload, None))
    elif isinstance(payload, FreeRegionPayload):
        primitives.extend(_free_region_overlay_primitives(payload, None))
    elif isinstance(payload, QualityDecisionPayload):
        primitives.append(
            OverlayPrimitive(
                primitive_type="label",
                layer="decision",
                label=f"{payload.decision.result_label}: {payload.decision.status}",
                geometry={},
                metadata=payload.decision.model_dump(mode="json"),
            )
        )
    elif isinstance(payload, BoardCutDecisionPayload):
        decision = payload.decision
        value = decision.longitudinal_shift_mm
        label = (
            f"Board cut: {value:.2f} mm"
            if decision.valid and value is not None
            else f"Board cut invalid: {decision.reason_code or decision.status}"
        )
        primitives.append(
            OverlayPrimitive(
                primitive_type="label",
                layer="board_cut_decision",
                label=label,
                geometry={},
                metadata=decision.model_dump(mode="json"),
            )
        )
    elif isinstance(payload, ResultPayload):
        primitives.extend(_inspection_overlay_from_result(payload.result))
    else:
        result = _result_from_payload(payload)
        primitives.append(
            OverlayPrimitive(
                primitive_type="label",
                label=str(result.result.get("result", "result")),
                geometry={},
                metadata=result.result,
            )
        )
    return primitives


def _overlay_layer_filter(config: dict[str, Any]) -> set[str]:
    raw = config.get("layers")
    if raw in (None, "", []):
        return set()
    return set(_field_list(raw))


def _filter_overlay_primitives(
    primitives: list[OverlayPrimitive],
    layer_filter: set[str],
) -> list[OverlayPrimitive]:
    if not layer_filter:
        return primitives
    return [
        primitive
        for primitive in primitives
        if _overlay_primitive_matches_layer_filter(primitive, layer_filter)
    ]


def _overlay_primitive_matches_layer_filter(
    primitive: OverlayPrimitive,
    layer_filter: set[str],
) -> bool:
    if primitive.layer in layer_filter:
        return True
    if "boxes" in layer_filter and primitive.primitive_type == "box":
        return True
    if "labels" in layer_filter and primitive.primitive_type == "label":
        return True
    if "zones" in layer_filter and primitive.primitive_type == "zone":
        return True
    if "lines" in layer_filter and primitive.primitive_type in {"line", "axis", "arrow"}:
        return True
    if "polygons" in layer_filter and primitive.primitive_type == "polygon":
        return True
    if "measurements" in layer_filter and primitive.primitive_type == "measurement":
        return True
    if "masks" in layer_filter and primitive.primitive_type == "mask":
        return True
    if "heatmaps" in layer_filter and primitive.primitive_type == "heatmap":
        return True
    return False


source_channel_frame = ChannelFrameSourceHandler("source.channel_frame")
source_test_photo = TestPhotoSourceHandler("source.test_photo")
source_channel_snapshot = ChannelSnapshotSourceHandler("source.channel_snapshot")
source_rtsp = RtspSourceHandler("source.rtsp")
source_trigger_snapshot = TriggerSnapshotSourceHandler("source.trigger_snapshot")
source_file_test = FileTestSourceHandler("source.file_test")
trigger_http_put = HttpPutTriggerHandler("trigger.http_put")
trigger_interval = IntervalTriggerHandler("trigger.interval")
preprocessing_resize = ResizeHandler("preprocessing.resize")
preprocessing_crop = CropHandler("preprocessing.crop")
preprocessing_roi_extract = RoiExtractHandler("preprocessing.roi_extract")
preprocessing_frame_sampler = FrameSamplerHandler("preprocessing.frame_sampler")
preprocessing_zone_filter = ZoneFilterHandler("preprocessing.zone_filter")
inference_detection = DetectionInferenceHandler("inference.detection")
inference_classification = ClassificationInferenceHandler("inference.classification")
inference_segmentation = SegmentationInferenceHandler("inference.segmentation")
inference_anomaly = AnomalyInferenceHandler("inference.anomaly")
computer_vision_canny_edge_detection = CannyEdgeDetectionHandler(
    "computer_vision.canny_edge_detection"
)
computer_vision_color_object_segmentation = ColorObjectSegmentationHandler(
    "computer_vision.color_object_segmentation"
)
computer_vision_reference_edge_from_cv_edges = ReferenceEdgeFromCvEdgesHandler(
    "computer_vision.reference_edge_from_cv_edges"
)
analysis_position_determination = PositionDeterminationHandler("analysis.position_determination")
analysis_zone_entry_exit = ZoneEntryExitHandler("analysis.zone_entry_exit")
analysis_optical_flow_motion = OpticalFlowMotionHandler("analysis.optical_flow_motion")
analysis_detection_size = DetectionSizeHandler("analysis.detection_size")
analysis_zone_detection = ZoneDetectionHandler("analysis.zone_detection")
analysis_close_detection = CloseDetectionHandler("analysis.close_detection")
analysis_object_spatial_signal = ObjectSpatialSignalHandler("analysis.object_spatial_signal")
analysis_reference_edge_from_features = ReferenceEdgeFromFeaturesHandler(
    "analysis.reference_edge_from_features"
)
analysis_coordinate_frame_from_edges = CoordinateFrameFromEdgesHandler(
    "analysis.coordinate_frame_from_edges"
)
analysis_feature_measurement = FeatureMeasurementHandler("analysis.feature_measurement")
analysis_clearance_zone_generator = ClearanceZoneGeneratorHandler(
    "analysis.clearance_zone_generator"
)
analysis_first_free_region_finder = FirstFreeRegionFinderHandler(
    "analysis.first_free_region_finder"
)
analysis_pixel_to_metric = PixelToMetricHandler("analysis.pixel_to_metric")
analysis_quality_rule_evaluation = QualityRuleEvaluationHandler("analysis.quality_rule_evaluation")
analysis_object_counting = ObjectCountingHandler("analysis.object_counting")
analysis_object_orientation_signal = ObjectOrientationSignalHandler(
    "analysis.object_orientation_signal"
)
analysis_movement_direction = MovementDirectionHandler("analysis.movement_direction")
analysis_rule_evaluation = RuleEvaluationHandler("analysis.rule_evaluation")
capture_full_frame_labelstudio = FullFrameCaptureHandler("capture.full_frame_labelstudio")
capture_roi_labelstudio = RoiCaptureHandler("capture.roi_labelstudio")
capture_conditional = ConditionalCaptureHandler("capture.conditional")
capture_multi_project = MultiProjectCaptureHandler("capture.multi_project")
output_overlay = OverlayOutputHandler("output.overlay")
output_display_output = DisplayOutputHandler("output.display_output")
output_http_trigger_response = HttpTriggerResponseOutputHandler("output.http_trigger_response")
output_result = ResultOutputHandler("output.result")
output_persisted_result = PersistedResultOutputHandler("output.persisted_result")
output_internal_event = InternalEventOutputHandler("output.internal_event")

BUILTIN_HANDLERS = {
    handler.node_type: handler
    for handler in (
        trigger_http_put,
        trigger_interval,
        source_channel_snapshot,
        source_rtsp,
        source_channel_frame,
        source_test_photo,
        source_trigger_snapshot,
        source_file_test,
        preprocessing_resize,
        preprocessing_crop,
        preprocessing_roi_extract,
        preprocessing_frame_sampler,
        preprocessing_zone_filter,
        inference_detection,
        inference_classification,
        inference_segmentation,
        inference_anomaly,
        computer_vision_canny_edge_detection,
        computer_vision_color_object_segmentation,
        computer_vision_reference_edge_from_cv_edges,
        analysis_position_determination,
        analysis_zone_entry_exit,
        analysis_optical_flow_motion,
        analysis_detection_size,
        analysis_zone_detection,
        analysis_close_detection,
        analysis_object_spatial_signal,
        analysis_reference_edge_from_features,
        analysis_coordinate_frame_from_edges,
        analysis_feature_measurement,
        analysis_clearance_zone_generator,
        analysis_first_free_region_finder,
        analysis_pixel_to_metric,
        analysis_quality_rule_evaluation,
        analysis_object_counting,
        analysis_object_orientation_signal,
        analysis_movement_direction,
        analysis_rule_evaluation,
        capture_full_frame_labelstudio,
        capture_roi_labelstudio,
        capture_conditional,
        capture_multi_project,
        output_overlay,
        output_display_output,
        output_http_trigger_response,
        output_result,
        output_persisted_result,
        output_internal_event,
    )
}
