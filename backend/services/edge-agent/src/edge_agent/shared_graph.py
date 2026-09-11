"""Edge adapters for the platform's executable graph; no second node implementation."""

# ruff: noqa: ANN401 - TensorRT arrays and the inference client's keyword protocol are heterogeneous.
from __future__ import annotations

import base64
import io
import threading
import time
import uuid
from typing import Any

import numpy as np
from cv_contracts import (
    AnomalyInferenceResponse,
    ClassificationInferenceResponse,
    DetectionInferenceResponse,
    SegmentationInferenceResponse,
    TemporalSequenceInferenceResponse,
)
from cv_runtime.context import RuntimeExecutionContext, RuntimeModelBinding
from cv_runtime.distribution import edge_node_contracts
from cv_runtime.engine import ExecutablePipelineGraph
from cv_runtime.payloads import FramePayload, TriggerPayload
from cv_runtime.registry import builtin_node_types
from cv_runtime.siemens_sps import close_siemens_sps_connection_cache
from cv_runtime.temporal_execution import (
    TemporalModelContext,
    TemporalOutput,
    _prepare_temporal_model_tensor,
    _temporal_prediction_from_outputs,
)
from cv_status import RuntimeMode, TargetPath, TaskType
from cv_utils import utc_now_iso
from PIL import Image


class LocalInferenceClient:
    def __init__(
        self, runtime: Any, models: list[dict[str, Any]], artifact_resolver: Any = None
    ) -> None:
        self.runtime = runtime
        self.models = {item["node_key"]: item for item in models}
        self.artifact_resolver = artifact_resolver

    def temporal_sequence(self, payload: Any) -> TemporalSequenceInferenceResponse:
        key = payload.deployment_target.removeprefix("edge:")
        model = self.models[key]
        sources = {}
        for name, digest in model["temporal_artifacts"].items():
            path = self.artifact_resolver(digest).resolve()
            sources[name] = {"path": str(path), "sha256": digest}
        # The blob root contains only immutable, checksum-addressed local artifacts.
        root = (
            self.artifact_resolver(next(iter(model["temporal_artifacts"].values())))
            .resolve()
            .parents[1]
        )
        route = TemporalModelContext(
            manifest={
                "package_root": str(root),
                "source_manifests": sources,
                "temporal": model["temporal"],
            }
        )
        started = time.perf_counter()
        tensor, contracts = _prepare_temporal_model_tensor(route=route, payload=payload)
        session = self.runtime.sessions[key]
        if len(session.input_names) != 1:
            raise ValueError("Temporal model must expose exactly one feature tensor input.")
        arrays = session.infer({session.input_names[0]: tensor})
        parsed = _temporal_prediction_from_outputs(
            {
                name: TemporalOutput(np.asarray(value).reshape(-1).tolist())
                for name, value in arrays.items()
            },
            contracts=contracts,
        )
        contract, angle = contracts["temporal_contract"], contracts["angle_contract"]
        return TemporalSequenceInferenceResponse(
            **self._base(key, (time.perf_counter() - started) * 1000),
            sequence_id=payload.sequence_id,
            track_id=payload.track_id,
            sequence_length=len(payload.frames),
            last_frame_index=payload.frames[-1].frame_index,
            decision_frame_index=payload.decision_frame_index,
            feature_set_id=contract["feature_set_id"],
            feature_schema_digest=contract["schema_digest"],
            scaler_sha256=contract["scaler_sha256"],
            snapshot_id=contract["snapshot_id"],
            feature_schema_version=payload.feature_schema_version,
            rotation_schema_version=payload.rotation_schema_version,
            calibration_schema_version=payload.calibration_schema_version,
            rotation_calibration_id=payload.rotation_calibration_id,
            rotation_calibration_hash=payload.rotation_calibration_hash,
            rotation_viewpoint_convention=angle["rotation_viewpoint_convention"],
            positive_rotation_direction=angle["positive_rotation_direction"],
            confidence_calibration_valid=bool(contracts["confidence_calibration"].get("valid")),
            deadband_deg=float(angle["deadband_deg"]),
            estimate_method="gru" if model["architecture"]["family"].lower() == "gru" else "tcn",
            **parsed,
        )

    def _base(self, key: str, elapsed: float) -> dict[str, Any]:
        model = self.models[key]
        return {
            "request_id": str(uuid.uuid4()),
            "deployment_target": f"edge:{key}",
            "warm_model": True,
            "route": {
                "deployment_release_id": model["deployment_release_id"],
                "target_path": "realtime",
                "release_status": "activated",
                "model_family_slug": model["model_family_slug"],
                "model_version_id": model["model_version_id"],
                "task_type": model["task_type"],
                "triton_model_name": "edge-local",
                "triton_model_version": 1,
                "serving_artifact_format": "tensorrt_plan",
                "contract_major": 1,
            },
            "architecture": model["architecture"],
            "timing": {
                "received_at": utc_now_iso(),
                "validation_ms": 0,
                "routing_ms": 0,
                "preprocessing_ms": 0,
                "inference_ms": elapsed,
                "postprocessing_ms": 0,
                "total_ms": elapsed,
            },
        }

    def _image(self, operation: str, **kwargs: Any) -> Any:
        key = kwargs["deployment_target"].removeprefix("edge:")
        with Image.open(io.BytesIO(kwargs["image_bytes"])) as source:
            image = np.asarray(source.convert("RGB"))
        config = dict(kwargs)
        config["confidence_threshold"] = kwargs.get("score_threshold", 0.35)
        started = time.perf_counter()
        result = self.runtime.infer(key, image, node_config=config, source_color_format="rgb")
        base = self._base(key, (time.perf_counter() - started) * 1000)
        normalized = result["normalized"]
        height, width = image.shape[:2]
        if operation == "classify":
            return ClassificationInferenceResponse(
                **base,
                labels=[
                    {"label": p["label"], "score": p["score"], "rank": i + 1}
                    for i, p in enumerate(normalized["predictions"][: kwargs.get("top_k", 3)])
                ],
            )
        if operation == "detect":
            predictions = []
            for item in normalized["detections"]:
                box = item["box"]
                prediction = {
                    "label": item["label"],
                    "score": item["confidence"],
                    "box": {
                        "x_min": box["x1"] / width,
                        "y_min": box["y1"] / height,
                        "x_max": box["x2"] / width,
                        "y_max": box["y2"] / height,
                    },
                }
                if item.get("oriented_box"):
                    oriented = item["oriented_box"]
                    prediction["oriented_box"] = {
                        "rotation_degrees": oriented.get("rotation_degrees"),
                        "points": [
                            {"x": p["x"] / width, "y": p["y"] / height} for p in oriented["points"]
                        ],
                    }
                predictions.append(prediction)
            return DetectionInferenceResponse(**base, detections=predictions)
        if operation == "segment":
            return SegmentationInferenceResponse(
                **base,
                image_width=width,
                image_height=height,
                segments=[
                    {
                        "label": p["label"],
                        "score": p["confidence"],
                        "polygon": [
                            {"x": point["x"] / width, "y": point["y"] / height}
                            for point in p["polygon"]
                        ],
                    }
                    for p in normalized["segments"][: kwargs.get("max_segments", 50)]
                ],
            )
        arrays = result.get("_raw_arrays", {})
        maps = [
            np.asarray(value).squeeze()
            for value in arrays.values()
            if np.asarray(value).squeeze().ndim == 2
        ]
        if not maps:
            raise ValueError("Anomaly model must supply a spatial heatmap for the shared contract.")
        heatmap = maps[0]
        buffer = io.BytesIO()
        Image.fromarray(np.uint8(np.clip(heatmap, 0, 1) * 255)).save(buffer, format="PNG")
        threshold = float(kwargs.get("threshold") or normalized["threshold"])
        score = float(normalized["score"])
        return AnomalyInferenceResponse(
            **base,
            anomaly_score=score,
            threshold=threshold,
            predicted_label="anomalous" if score >= threshold else "normal",
            is_anomalous=score >= threshold,
            image_width=width,
            image_height=height,
            heatmap={
                "data": base64.b64encode(buffer.getvalue()).decode(),
                "width": heatmap.shape[1],
                "height": heatmap.shape[0],
            },
        )

    def classify_bytes(self, **kwargs: Any) -> ClassificationInferenceResponse:
        return self._image("classify", **kwargs)

    def classify(self, payload: Any) -> ClassificationInferenceResponse:
        return self._image_request("classify", payload)

    def detect_bytes(self, **kwargs: Any) -> DetectionInferenceResponse:
        return self._image("detect", **kwargs)

    def detect(self, payload: Any) -> DetectionInferenceResponse:
        return self._image_request("detect", payload)

    def segment_bytes(self, **kwargs: Any) -> SegmentationInferenceResponse:
        return self._image("segment", **kwargs)

    def segment(self, payload: Any) -> SegmentationInferenceResponse:
        return self._image_request("segment", payload)

    def anomaly_bytes(self, **kwargs: Any) -> AnomalyInferenceResponse:
        return self._image("anomaly", **kwargs)

    def anomaly(self, payload: Any) -> AnomalyInferenceResponse:
        return self._image_request("anomaly", payload)

    def _image_request(self, operation: str, payload: Any) -> Any:
        values = payload.model_dump(exclude_none=True)
        values["image_bytes"] = base64.b64decode(values.pop("image_base64"), validate=True)
        return self._image(operation, **values)


class SharedEdgeGraphExecutor:
    def __init__(
        self,
        *,
        manifest: Any,
        graph: Any,
        inference_runtime: Any,
        mode: str,
        artifact_resolver: Any = None,
    ) -> None:
        self.manifest = manifest
        self.graph = graph
        self.inference_runtime = inference_runtime
        self.mode = RuntimeMode(mode)
        self.state: dict[str, Any] = {}
        # One graph's state is strictly causal even when continuous and trigger calls overlap.
        self.lock = threading.RLock()
        unsupported = {n.node_type for n in graph.nodes} - edge_node_contracts().keys()
        if unsupported:
            raise ValueError(f"Edge adapters unavailable for: {sorted(unsupported)}")
        self.executable = ExecutablePipelineGraph.build(
            pipeline_version_id=manifest.pipeline_version_id or "edge-pipeline",
            graph=graph,
            handler_refs={n.node_type: n.runtime_handler_ref for n in builtin_node_types()},
        )
        self.models = manifest.runtime_requirements.get("models", [])
        self.client = LocalInferenceClient(inference_runtime, self.models, artifact_resolver)

    def _resolve(self, reference: Any) -> RuntimeModelBinding:
        for node in self.graph.nodes:
            if node.logical_model_ref == reference:
                model = next(m for m in self.models if m["node_key"] == node.node_key)
                return RuntimeModelBinding(
                    deployment_release_id=model["deployment_release_id"],
                    deployment_target=f"edge:{node.node_key}",
                    target_path=TargetPath.REALTIME,
                    model_family_slug=model["model_family_slug"],
                    model_version_id=model["model_version_id"],
                    task_type=TaskType(model["task_type"]),
                    contract_major=reference.contract_major,
                    capability=model.get("capability", {}),
                )
        raise ValueError("No pinned local model matches the pipeline reference.")

    def execute(
        self, frame: Any, *, trigger_payload: Any = None, correlation_id: str | None = None
    ) -> dict[str, Any]:
        with self.lock:
            context = RuntimeExecutionContext(
                correlation_id=correlation_id or f"edge:{uuid.uuid4()}",
                pipeline_version_id=self.manifest.pipeline_version_id or "edge-pipeline",
                channel_id=self.manifest.channel_id,
                mode=self.mode,
                input_frame=FramePayload.from_cv2_frame(
                    frame.image,
                    timestamp=str(frame.captured_at),
                    metadata={"frame_sequence": int(frame.index), "sequence": int(frame.index)},
                ),
                trigger=TriggerPayload(body=trigger_payload or {}),
                state=self.state,
                model_resolver=self._resolve,
                inference_client=self.client,
                source_config=self.manifest.runtime_requirements.get("source", {}),
                metadata={
                    "executor": "cv_runtime",
                    "plc_writes_allowed": False,
                    "frame_sequence": int(frame.index),
                    "live_frame_sequence": int(frame.index),
                    "materialize_frame_node_outputs": False,
                },
            )
            result = self.executable.execute(context).model_dump(mode="json")
            result["workload_release_id"] = self.manifest.workload_release_id
            result.setdefault("runtime_metadata", {}).update(
                {
                    "executor": "cv_runtime",
                    "source_frame_index": int(frame.index),
                    "captured_at": str(frame.captured_at),
                    "width": frame.width,
                    "height": frame.height,
                }
            )
            return result

    def close(self) -> None:
        with self.lock:
            close_siemens_sps_connection_cache(self.state)
            if self.inference_runtime is not None:
                self.inference_runtime.close()
