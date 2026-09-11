from __future__ import annotations

# ruff: noqa: ANN401 - graph values intentionally carry heterogeneous node payloads
import copy
import json
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from cv_contracts import EdgeDeploymentManifest, RuntimeNodeSpec, RuntimePipelineGraph
from cv_runtime.context import RuntimeExecutionContext
from cv_runtime.payloads import ResultPayload
from cv_runtime.siemens_sps import (
    SiemensSpsWriteHandler,
    close_siemens_sps_connection_cache,
)
from cv_status import RuntimeMode
from PIL import Image

from edge_agent.inference import EdgeInferenceRuntime

PRODUCTIVE_EDGE_NODE_TYPES = (
    "source.channel_frame",
    "source.rtsp",
    "source.file_test",
    "trigger.http_put",
    "preprocessing.resize",
    "preprocessing.crop",
    "preprocessing.frame_sampler",
    "inference.classification",
    "inference.detection",
    "inference.oriented_detection",
    "inference.segmentation",
    "inference.anomaly",
    "output.overlay",
    "output.display_output",
    "output.result",
    "output.persisted_result",
    "output.http_trigger_response",
    "output.siemens_sps_write",
)


class EdgeGraphError(RuntimeError):
    """An invalid or failed edge-local pipeline graph."""


@dataclass(frozen=True)
class EdgeFrameValue:
    image: Any
    frame_index: int
    width: int
    height: int
    captured_at: str
    source_color_format: str = "bgr"
    source_offset_x: float = 0.0
    source_offset_y: float = 0.0
    source_scale_x: float = 1.0
    source_scale_y: float = 1.0


@dataclass
class EdgeExecutionContext:
    correlation_id: str
    channel_id: str
    pipeline_version_id: str
    mode: RuntimeMode
    trigger_payload: dict[str, Any]
    frame: EdgeFrameValue
    runtime_state: dict[str, Any]
    normalized_outputs: list[dict[str, Any]] = field(default_factory=list)
    raw_outputs: list[dict[str, Any]] = field(default_factory=list)
    derived_outputs: list[dict[str, Any]] = field(default_factory=list)
    persisted_results: list[dict[str, Any]] = field(default_factory=list)
    overlays: list[dict[str, Any]] = field(default_factory=list)
    model_routes: list[dict[str, Any]] = field(default_factory=list)
    trigger_response: Any = None
    final_response: Any = None


Handler = Callable[[RuntimeNodeSpec, dict[str, Any], EdgeExecutionContext], dict[str, Any]]


class EdgeHandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, node_type: str, handler: Handler) -> None:
        if node_type in self._handlers:
            raise ValueError(f"Edge handler already registered for {node_type}.")
        self._handlers[node_type] = handler

    def handler(self, node_type: str) -> Handler:
        try:
            return self._handlers[node_type]
        except KeyError as exc:
            raise EdgeGraphError(f"No edge handler is registered for {node_type}.") from exc

    @property
    def supported_node_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))


class EdgeGraphExecutor:
    def __init__(
        self,
        *,
        manifest: EdgeDeploymentManifest,
        graph: RuntimePipelineGraph,
        inference_runtime: EdgeInferenceRuntime | None,
        mode: str,
        registry: EdgeHandlerRegistry | None = None,
    ) -> None:
        self.manifest = manifest
        self.graph = graph
        self.inference_runtime = inference_runtime
        self.mode = mode
        self._runtime_state: dict[str, Any] = {}
        self.registry = registry or build_productive_registry(inference_runtime)
        unsupported = sorted(
            {node.node_type for node in graph.nodes} - set(self.registry.supported_node_types)
        )
        if unsupported:
            raise EdgeGraphError(f"Pipeline contains unsupported edge nodes: {unsupported}")
        self._node_by_key = {node.node_key: node for node in graph.nodes}
        self._incoming: dict[str, list[Any]] = defaultdict(list)
        for edge in graph.edges:
            self._incoming[edge.to_node_key].append(edge)
        self._validate_ports()
        self._order = self._topological_order()

    @classmethod
    def from_manifest(
        cls,
        manifest: EdgeDeploymentManifest,
        *,
        artifact_resolver: Callable[[str], Path],
        inference_runtime: EdgeInferenceRuntime | None,
    ) -> EdgeGraphExecutor | None:
        digest = str(manifest.runtime_requirements.get("pipeline_graph_sha256") or "")
        if not digest:
            return None
        try:
            envelope = json.loads(artifact_resolver(digest).read_text(encoding="utf-8"))
            graph = RuntimePipelineGraph.model_validate(envelope["graph"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise EdgeGraphError("Pipeline graph artifact is unavailable or invalid.") from exc
        if manifest.runtime_requirements.get("executor") == "cv_runtime_v1":
            from edge_agent.shared_graph import SharedEdgeGraphExecutor

            try:
                return SharedEdgeGraphExecutor(
                    manifest=manifest,
                    graph=graph,
                    inference_runtime=inference_runtime,
                    mode=str(envelope.get("mode") or "realtime"),
                    artifact_resolver=artifact_resolver,
                )
            except ValueError as exc:
                raise EdgeGraphError(str(exc)) from exc
        return cls(
            manifest=manifest,
            graph=graph,
            inference_runtime=inference_runtime,
            mode=str(envelope.get("mode") or "realtime"),
        )

    def execute(
        self,
        frame: Any,
        *,
        trigger_payload: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        if frame.image is None:
            raise EdgeGraphError("The productive graph requires an image frame.")
        edge_frame = EdgeFrameValue(
            image=frame.image,
            frame_index=int(frame.index),
            width=int(frame.width),
            height=int(frame.height),
            captured_at=str(frame.captured_at),
        )
        context = EdgeExecutionContext(
            correlation_id=correlation_id or f"edge-exec:{uuid.uuid4()}",
            channel_id=self.manifest.channel_id,
            pipeline_version_id=self.manifest.pipeline_version_id or "edge-pipeline",
            mode=_runtime_mode(self.mode),
            trigger_payload=dict(trigger_payload or {}),
            frame=edge_frame,
            runtime_state=self._runtime_state,
        )
        values: dict[str, dict[str, Any]] = {}
        node_timings: list[dict[str, Any]] = []
        started = time.perf_counter()
        for node_key in self._order:
            node = self._node_by_key[node_key]
            inputs: dict[str, Any] = {}
            for edge in self._incoming[node_key]:
                source_values = values[edge.from_node_key]
                inputs[edge.to_port] = source_values.get(
                    edge.from_port, source_values.get("default")
                )
            node_started = time.perf_counter()
            try:
                values[node_key] = self.registry.handler(node.node_type)(node, inputs, context)
            except Exception as exc:
                if isinstance(exc, EdgeGraphError):
                    raise
                raise EdgeGraphError(f"Node {node_key} ({node.node_type}) failed: {exc}") from exc
            node_timings.append(
                {
                    "node_key": node_key,
                    "node_type": node.node_type,
                    "duration_ms": round((time.perf_counter() - node_started) * 1000, 3),
                }
            )
        total_ms = round((time.perf_counter() - started) * 1000, 3)
        if context.final_response is None and context.normalized_outputs:
            context.final_response = context.normalized_outputs[-1]
        return {
            "schema_version": "runtime.execution.result.v1",
            "status": "success",
            "channel_id": self.manifest.channel_id,
            "pipeline_version_id": self.manifest.pipeline_version_id,
            "workload_release_id": self.manifest.workload_release_id,
            "correlation_id": context.correlation_id,
            "raw_inference_outputs": context.raw_outputs,
            "normalized_inference_outputs": context.normalized_outputs,
            "derived_outputs": context.derived_outputs,
            "persisted_results": context.persisted_results,
            "overlay_primitives": context.overlays,
            "trigger_response": _json_safe(context.trigger_response),
            "final_response": _json_safe(context.final_response),
            "runtime_metadata": {
                "executor": "edge_graph_v1",
                "execution_mode": self.mode,
                "source_frame_index": edge_frame.frame_index,
                "captured_at": edge_frame.captured_at,
                "width": edge_frame.width,
                "height": edge_frame.height,
            },
            "timing": {"total_ms": total_ms, "nodes": node_timings},
            "model_routes": context.model_routes,
        }

    def close(self) -> None:
        close_siemens_sps_connection_cache(self._runtime_state)
        if self.inference_runtime is not None:
            self.inference_runtime.close()

    def _topological_order(self) -> list[str]:
        indegree = {key: 0 for key in self._node_by_key}
        outgoing: dict[str, list[str]] = defaultdict(list)
        for edge in self.graph.edges:
            indegree[edge.to_node_key] += 1
            outgoing[edge.from_node_key].append(edge.to_node_key)
        pending = deque(sorted(key for key, degree in indegree.items() if degree == 0))
        result: list[str] = []
        while pending:
            key = pending.popleft()
            result.append(key)
            for target in outgoing[key]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    pending.append(target)
        if len(result) != len(indegree):
            raise EdgeGraphError("Edge pipeline graph contains a cycle.")
        return result

    def _validate_ports(self) -> None:
        for edge in self.graph.edges:
            source = self._node_by_key[edge.from_node_key]
            target = self._node_by_key[edge.to_node_key]
            source_ports = {port.key for port in source.output_ports}
            target_ports = {port.key for port in target.input_ports}
            if source_ports and edge.from_port not in source_ports:
                raise EdgeGraphError(
                    f"Edge {edge.from_node_key}.{edge.from_port} uses an undeclared output port."
                )
            if target_ports and edge.to_port not in target_ports:
                raise EdgeGraphError(
                    f"Edge {edge.to_node_key}.{edge.to_port} uses an undeclared input port."
                )
        for node in self.graph.nodes:
            connected = {edge.to_port for edge in self._incoming[node.node_key]}
            missing = sorted(
                port.key for port in node.input_ports if port.required and port.key not in connected
            )
            if missing:
                raise EdgeGraphError(
                    f"Node {node.node_key} has unconnected required ports: {missing}"
                )


def build_productive_registry(
    inference_runtime: EdgeInferenceRuntime | None,
    *,
    siemens_sps_handler: SiemensSpsWriteHandler | None = None,
) -> EdgeHandlerRegistry:
    registry = EdgeHandlerRegistry()
    for node_type in (
        "source.channel_frame",
        "source.rtsp",
        "source.file_test",
    ):
        registry.register(node_type, _source_handler)
    registry.register("trigger.http_put", _trigger_handler)
    registry.register("preprocessing.resize", _resize_handler)
    registry.register("preprocessing.crop", _crop_handler)
    registry.register("preprocessing.frame_sampler", _passthrough_handler)
    for node_type in (
        "inference.classification",
        "inference.detection",
        "inference.oriented_detection",
        "inference.segmentation",
        "inference.anomaly",
    ):
        registry.register(node_type, _inference_handler(inference_runtime))
    registry.register("output.overlay", _overlay_handler)
    registry.register("output.display_output", _overlay_handler)
    registry.register("output.result", _result_handler)
    registry.register("output.persisted_result", _persisted_result_handler)
    registry.register("output.http_trigger_response", _trigger_response_handler)
    registry.register(
        "output.siemens_sps_write",
        _siemens_sps_write_handler(siemens_sps_handler),
    )
    return registry


def _source_handler(
    node: RuntimeNodeSpec, inputs: dict[str, Any], context: EdgeExecutionContext
) -> dict[str, Any]:
    del node, inputs
    return _all_ports(context.frame)


def _trigger_handler(
    node: RuntimeNodeSpec, inputs: dict[str, Any], context: EdgeExecutionContext
) -> dict[str, Any]:
    del node, inputs
    return _all_ports(context.trigger_payload)


def _resize_handler(
    node: RuntimeNodeSpec, inputs: dict[str, Any], context: EdgeExecutionContext
) -> dict[str, Any]:
    frame = _first_frame(inputs, context)
    width = max(int(node.config.get("width") or node.config.get("target_width") or frame.width), 1)
    height = max(
        int(node.config.get("height") or node.config.get("target_height") or frame.height), 1
    )
    source = np.asarray(frame.image)
    # Pillow works for both RGB and BGR values; channel order is intentionally preserved.
    resized = np.asarray(Image.fromarray(source).resize((width, height), Image.Resampling.BILINEAR))
    result = EdgeFrameValue(
        image=resized,
        frame_index=frame.frame_index,
        width=width,
        height=height,
        captured_at=frame.captured_at,
        source_color_format=frame.source_color_format,
        source_offset_x=frame.source_offset_x,
        source_offset_y=frame.source_offset_y,
        source_scale_x=frame.source_scale_x * frame.width / width,
        source_scale_y=frame.source_scale_y * frame.height / height,
    )
    return _all_ports(result)


def _crop_handler(
    node: RuntimeNodeSpec, inputs: dict[str, Any], context: EdgeExecutionContext
) -> dict[str, Any]:
    frame = _first_frame(inputs, context)
    config = node.config
    x = max(int(config.get("x") or config.get("left") or 0), 0)
    y = max(int(config.get("y") or config.get("top") or 0), 0)
    width = int(config.get("width") or frame.width - x)
    height = int(config.get("height") or frame.height - y)
    x2, y2 = min(x + width, frame.width), min(y + height, frame.height)
    if x >= x2 or y >= y2:
        raise EdgeGraphError("Crop configuration produces an empty image.")
    cropped = np.ascontiguousarray(np.asarray(frame.image)[y:y2, x:x2])
    result = EdgeFrameValue(
        image=cropped,
        frame_index=frame.frame_index,
        width=x2 - x,
        height=y2 - y,
        captured_at=frame.captured_at,
        source_color_format=frame.source_color_format,
        source_offset_x=frame.source_offset_x + x * frame.source_scale_x,
        source_offset_y=frame.source_offset_y + y * frame.source_scale_y,
        source_scale_x=frame.source_scale_x,
        source_scale_y=frame.source_scale_y,
    )
    return _all_ports(result)


def _passthrough_handler(
    node: RuntimeNodeSpec, inputs: dict[str, Any], context: EdgeExecutionContext
) -> dict[str, Any]:
    del node
    value = next((item for item in inputs.values() if item is not None), context.frame)
    return _all_ports(value)


def _inference_handler(runtime: EdgeInferenceRuntime | None) -> Handler:
    def handle(
        node: RuntimeNodeSpec, inputs: dict[str, Any], context: EdgeExecutionContext
    ) -> dict[str, Any]:
        if runtime is None:
            raise EdgeGraphError(f"Inference node {node.node_key} has no loaded TensorRT runtime.")
        frame = _first_frame(inputs, context)
        result = runtime.infer(
            node.node_key,
            frame.image,
            node_config=node.config,
            source_color_format=frame.source_color_format,
        )
        normalized = _project_inference_to_source(
            {
                "node_key": node.node_key,
                "node_type": node.node_type,
                "task_type": result["task_type"],
                **result["normalized"],
            },
            frame,
        )
        context.normalized_outputs.append(normalized)
        context.raw_outputs.append({"node_key": node.node_key, "tensors": result["raw_tensors"]})
        context.model_routes.append(
            {
                "node_key": node.node_key,
                "backend": "tensorrt_local",
                "model_version_id": result.get("model_version_id"),
                "deployment_release_id": result.get("deployment_release_id"),
            }
        )
        return _all_ports({"frame": frame, "inference": normalized})

    return handle


def _overlay_handler(
    node: RuntimeNodeSpec, inputs: dict[str, Any], context: EdgeExecutionContext
) -> dict[str, Any]:
    del node
    inference = _first_inference(inputs, context)
    overlays = _overlays_from_inference(inference, context.frame)
    context.overlays.extend(overlays)
    return _all_ports({"inference": inference, "overlays": overlays})


def _result_handler(
    node: RuntimeNodeSpec, inputs: dict[str, Any], context: EdgeExecutionContext
) -> dict[str, Any]:
    del node
    value = _first_serializable(inputs, context)
    context.final_response = value
    context.derived_outputs.append({"kind": "result", "value": value})
    return _all_ports(value)


def _persisted_result_handler(
    node: RuntimeNodeSpec, inputs: dict[str, Any], context: EdgeExecutionContext
) -> dict[str, Any]:
    del node
    value = _first_serializable(inputs, context)
    context.persisted_results.append({"kind": "edge_result", "value": value})
    context.final_response = value
    return _all_ports(value)


def _trigger_response_handler(
    node: RuntimeNodeSpec, inputs: dict[str, Any], context: EdgeExecutionContext
) -> dict[str, Any]:
    del node
    value = _first_serializable(inputs, context)
    context.trigger_response = value
    context.final_response = value
    return _all_ports(value)


def _siemens_sps_write_handler(
    handler: SiemensSpsWriteHandler | None,
) -> Handler:
    productive_handler = handler or SiemensSpsWriteHandler("output.siemens_sps_write")

    def handle(
        node: RuntimeNodeSpec,
        inputs: dict[str, Any],
        context: EdgeExecutionContext,
    ) -> dict[str, Any]:
        inference = _first_inference(inputs, context)
        execution = productive_handler.execute(
            RuntimeExecutionContext(
                correlation_id=context.correlation_id,
                pipeline_version_id=context.pipeline_version_id,
                channel_id=context.channel_id,
                mode=context.mode,
                state=context.runtime_state,
                metadata={"executor": "edge_graph_v1"},
            ),
            node,
            {"result": _result_payload_from_inference(inference)},
        )
        status_payload = execution.outputs.get("status")
        if status_payload is None:
            raise EdgeGraphError("Siemens SPS handler returned no status output.")
        status = status_payload.model_dump(mode="json")
        context.derived_outputs.append({"kind": "siemens_sps_write", "value": status})
        return _all_ports(status)

    return handle


def _result_payload_from_inference(inference: dict[str, Any]) -> ResultPayload:
    result = copy.deepcopy(inference)
    decision: bool | None = None
    predictions = result.get("predictions")
    detections = result.get("detections")
    segments = result.get("segments")
    if isinstance(predictions, list):
        top = predictions[0] if predictions and isinstance(predictions[0], dict) else None
        result.setdefault("count", len(predictions))
        result.setdefault("labels", predictions)
        result.setdefault("top_label", top)
        if top is not None:
            result.setdefault("class", top.get("label"))
            result.setdefault("classification", top.get("label"))
            result.setdefault("score", top.get("score"))
        decision = bool(top)
    elif isinstance(detections, list):
        result.setdefault("count", len(detections))
        result.setdefault("count_total", len(detections))
        result.setdefault("objects", detections)
        decision = bool(detections)
    elif isinstance(segments, list):
        result.setdefault("count", len(segments))
        result.setdefault("objects", segments)
        decision = bool(segments)
    elif "is_anomaly" in result:
        decision = bool(result.get("is_anomaly"))
    return ResultPayload(result=_json_safe(result), decision=decision)


def _runtime_mode(value: str) -> RuntimeMode:
    try:
        return RuntimeMode(value)
    except ValueError as exc:
        raise EdgeGraphError(f"Unsupported edge execution mode: {value!r}.") from exc


def _overlays_from_inference(
    inference: dict[str, Any], frame: EdgeFrameValue
) -> list[dict[str, Any]]:
    overlays: list[dict[str, Any]] = []
    for detection in inference.get("detections") or []:
        oriented = detection.get("oriented_box") or {}
        points = oriented.get("points") or []
        overlays.append(
            {
                "primitive_type": "polygon" if points else "box",
                "layer": "inference",
                "label": detection.get("label"),
                "confidence": detection.get("confidence"),
                "geometry": (
                    {"points": [dict(point) for point in points]}
                    if points
                    else dict(detection.get("box") or {})
                ),
                "frame_id": str(frame.frame_index),
                "source_timestamp": frame.captured_at,
                "coordinate_system": "image_pixels",
                "resolution": {"width": frame.width, "height": frame.height},
                "style": {},
                "metadata": {
                    "class_id": detection.get("class_id"),
                    "rotation_degrees": oriented.get("rotation_degrees"),
                },
            }
        )
    predictions = inference.get("predictions") or []
    if predictions:
        best = predictions[0]
        overlays.append(
            {
                "primitive_type": "label",
                "layer": "inference",
                "label": best.get("label"),
                "confidence": best.get("score"),
                "geometry": {"x": 12, "y": 24},
                "frame_id": str(frame.frame_index),
                "source_timestamp": frame.captured_at,
                "coordinate_system": "image_pixels",
                "resolution": {"width": frame.width, "height": frame.height},
                "style": {},
                "metadata": {"class_id": best.get("class_id")},
            }
        )
    for segment in inference.get("segments") or []:
        overlays.append(
            {
                "primitive_type": "polygon",
                "layer": "inference",
                "label": segment.get("label"),
                "confidence": segment.get("confidence"),
                "geometry": {"points": segment.get("polygon") or []},
                "frame_id": str(frame.frame_index),
                "source_timestamp": frame.captured_at,
                "coordinate_system": "image_pixels",
                "resolution": {"width": frame.width, "height": frame.height},
                "style": {},
                "metadata": {"class_id": segment.get("class_id")},
            }
        )
    if "is_anomaly" in inference:
        overlays.append(
            {
                "primitive_type": "label",
                "layer": "inference",
                "label": "Anomaly" if inference["is_anomaly"] else "OK",
                "confidence": min(max(float(inference.get("score") or 0.0), 0.0), 1.0),
                "geometry": {"x": 12, "y": 24},
                "frame_id": str(frame.frame_index),
                "source_timestamp": frame.captured_at,
                "coordinate_system": "image_pixels",
                "resolution": {"width": frame.width, "height": frame.height},
                "style": {},
                "metadata": {},
            }
        )
    return overlays


def _first_frame(inputs: Mapping[str, Any], context: EdgeExecutionContext) -> EdgeFrameValue:
    for value in inputs.values():
        if isinstance(value, EdgeFrameValue):
            return value
        if isinstance(value, dict) and isinstance(value.get("frame"), EdgeFrameValue):
            return value["frame"]
    return context.frame


def _first_inference(inputs: Mapping[str, Any], context: EdgeExecutionContext) -> dict[str, Any]:
    for value in inputs.values():
        if isinstance(value, dict) and isinstance(value.get("inference"), dict):
            return value["inference"]
        if isinstance(value, dict) and (
            "detections" in value or "predictions" in value or "segments" in value
        ):
            return value
    if context.normalized_outputs:
        return context.normalized_outputs[-1]
    raise EdgeGraphError("Overlay/output node received no inference result.")


def _first_serializable(inputs: Mapping[str, Any], context: EdgeExecutionContext) -> Any:
    for value in inputs.values():
        if isinstance(value, EdgeFrameValue):
            return _frame_summary(value)
        if isinstance(value, dict) and isinstance(value.get("inference"), dict):
            return value["inference"]
        if value is not None:
            return _json_safe(value)
    if context.normalized_outputs:
        return context.normalized_outputs[-1]
    return _frame_summary(context.frame)


def _frame_summary(frame: EdgeFrameValue) -> dict[str, Any]:
    return {
        "source_frame_index": frame.frame_index,
        "width": frame.width,
        "height": frame.height,
        "captured_at": frame.captured_at,
    }


def _project_inference_to_source(
    inference: dict[str, Any], frame: EdgeFrameValue
) -> dict[str, Any]:
    if (
        frame.source_offset_x == 0
        and frame.source_offset_y == 0
        and frame.source_scale_x == 1
        and frame.source_scale_y == 1
    ):
        return inference
    projected = copy.deepcopy(inference)
    for detection in projected.get("detections") or []:
        box = detection.get("box") or {}
        for key in ("x1", "x2"):
            box[key] = frame.source_offset_x + float(box.get(key, 0)) * frame.source_scale_x
        for key in ("y1", "y2"):
            box[key] = frame.source_offset_y + float(box.get(key, 0)) * frame.source_scale_y
        for point in (detection.get("oriented_box") or {}).get("points") or []:
            point["x"] = frame.source_offset_x + float(point.get("x", 0)) * frame.source_scale_x
            point["y"] = frame.source_offset_y + float(point.get("y", 0)) * frame.source_scale_y
    for segment in projected.get("segments") or []:
        for point in segment.get("polygon") or []:
            point["x"] = frame.source_offset_x + float(point.get("x", 0)) * frame.source_scale_x
            point["y"] = frame.source_offset_y + float(point.get("y", 0)) * frame.source_scale_y
    return projected


def _all_ports(value: Any) -> dict[str, Any]:
    # Graph edges select by declared port name. The default makes handlers contract-neutral.
    return {"default": value, "frame": value, "result": value, "output": value, "response": value}


def _json_safe(value: Any) -> Any:
    if isinstance(value, EdgeFrameValue):
        return _frame_summary(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items() if not key.startswith("_")}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value
