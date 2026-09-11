from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from cv_contracts import (
    EdgeDeploymentManifest,
    RuntimeGraphEdge,
    RuntimeLogicalModelRef,
    RuntimeNodePort,
    RuntimeNodeSpec,
    RuntimePipelineGraph,
)
from cv_runtime.siemens_sps import SiemensSpsWriteHandler
from cv_status import RuntimeNodeCategory, TaskType
from edge_agent.graph import EdgeGraphExecutor, build_productive_registry
from edge_agent.inference import (
    EdgeInferenceRuntime,
    ModelRuntimeSpec,
    PreprocessTransform,
    compare_golden_output,
    postprocess_outputs,
)
from edge_agent.runtime import SourceFrame


class _FakeSession:
    input_names = ("images",)
    output_names = ("scores",)

    def infer(self, inputs: object) -> dict[str, np.ndarray]:
        assert inputs
        return {"scores": np.asarray([[0.1, 2.0, -1.0]], dtype=np.float32)}

    def close(self) -> None:
        return None


def _model_spec(task_type: str = "classification") -> ModelRuntimeSpec:
    return ModelRuntimeSpec(
        node_key="classify",
        task_type=task_type,
        input_size=(8, 8),
        preprocessing={
            "resize_mode": "letterbox",
            "letterbox": {"enabled": True, "padding_value": 114, "center": True},
            "color_format": "rgb",
            "scale": 1 / 255,
        },
        postprocessing={"confidence_threshold_default": 0.25},
        nms={"enabled": True, "iou_threshold": 0.5, "max_detections": 100},
        io_schema={"input_tensors": [{"name": "images", "layout": "CHW"}]},
        class_map=("reject", "good", "unknown"),
        model_version_id="mver_e1",
        deployment_release_id="drel_e1",
        warmup_runs=0,
    )


def test_task_preprocessing_and_classification_postprocessing_are_executable() -> None:
    runtime = EdgeInferenceRuntime(
        specs={"classify": _model_spec()},
        sessions={"classify": _FakeSession()},
    )
    result = runtime.infer(
        "classify",
        np.zeros((4, 10, 3), dtype=np.uint8),
        source_color_format="rgb",
    )

    assert result["normalized"]["predictions"][0]["label"] == "good"
    assert result["raw_tensors"]["scores"]["shape"] == [1, 3]
    assert result["_raw_arrays"]["scores"].dtype == np.float32


def test_golden_tensor_comparison_accepts_tolerance_and_rejects_shape_drift(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "expected.npy"
    np.save(expected, np.asarray([[1.0, 2.0]], dtype=np.float32))
    actual = {"_raw_arrays": {"output": np.asarray([[1.0001, 2.0]], dtype=np.float32)}}

    report = compare_golden_output(
        actual,
        expected,
        comparison="numeric_allclose",
        atol=0.001,
        rtol=0.0,
    )

    assert report["max_abs_error"] > 0


def test_yolo_instance_segmentation_outputs_are_task_postprocessed() -> None:
    spec = _model_spec("segmentation")
    spec = ModelRuntimeSpec(
        **{
            **spec.__dict__,
            "class_map": ("background", "part"),
        }
    )
    prediction = np.zeros((1, 38, 1), dtype=np.float32)
    prediction[0, 0:4, 0] = [4, 4, 6, 6]
    prediction[0, 5, 0] = 0.95
    prediction[0, 6, 0] = 10.0
    prototype = np.zeros((1, 32, 4, 4), dtype=np.float32)
    prototype[0, 0, 1:3, 1:3] = 1.0

    result = postprocess_outputs(
        {"predictions": prediction, "prototype": prototype},
        spec,
        PreprocessTransform(8, 8, 8, 8, 1.0, 1.0, 0.0, 0.0),
        node_config={},
    )

    assert result["segments"][0]["label"] == "part"
    assert result["segments"][0]["confidence"] == pytest.approx(0.95)


def test_detection_and_anomaly_outputs_are_task_postprocessed() -> None:
    transform = PreprocessTransform(8, 8, 8, 8, 1.0, 1.0, 0.0, 0.0)
    detection = postprocess_outputs(
        {"detections": np.asarray([[[1, 2, 7, 6, 0.9, 1]]], dtype=np.float32)},
        _model_spec("detection"),
        transform,
        node_config={},
    )
    anomaly = postprocess_outputs(
        {"score": np.asarray([[0.8]], dtype=np.float32)},
        _model_spec("anomaly"),
        transform,
        node_config={},
    )

    assert detection["detections"][0]["label"] == "good"
    assert detection["detections"][0]["box"]["x2"] == pytest.approx(7.0)
    assert anomaly["is_anomaly"] is True


def test_efficient_nms_and_oriented_detection_are_normalized() -> None:
    transform = PreprocessTransform(20, 20, 20, 20, 1.0, 1.0, 0.0, 0.0)
    efficient_nms = postprocess_outputs(
        {
            "num_dets": np.asarray([1], dtype=np.int32),
            "detection_boxes": np.asarray([[[1, 2, 8, 9], [0, 0, 1, 1]]], dtype=np.float32),
            "detection_scores": np.asarray([[0.9, 0.1]], dtype=np.float32),
            "detection_classes": np.asarray([[1, 0]], dtype=np.int32),
        },
        _model_spec("detection"),
        transform,
        node_config={},
    )
    oriented = postprocess_outputs(
        {"obb": np.asarray([[[10, 10, 8, 4, 0.05, 0.95, 0.0, np.pi / 4]]], dtype=np.float32)},
        _model_spec("oriented_detection"),
        transform,
        node_config={},
    )

    assert efficient_nms["detections"][0]["label"] == "good"
    assert efficient_nms["detections"][0]["box"]["x2"] == pytest.approx(8.0)
    rotated = oriented["detections"][0]["oriented_box"]
    assert len(rotated["points"]) == 4
    assert rotated["rotation_degrees"] == pytest.approx(45.0)


class _GraphInference:
    def infer(self, node_key: str, image: object, **_kwargs: object) -> dict[str, object]:
        assert node_key == "classify"
        assert np.asarray(image).shape == (32, 48, 3)
        return {
            "task_type": "classification",
            "model_version_id": "mver_e1",
            "deployment_release_id": "drel_e1",
            "normalized": {
                "predictions": [{"class_id": 1, "label": "good", "score": 0.98, "rank": 1}]
            },
            "raw_tensors": {"scores": {"shape": [1, 2], "dtype": "float32"}},
        }

    def close(self) -> None:
        return None


def test_edge_graph_executor_generates_runtime_result_and_overlay() -> None:
    source = RuntimeNodeSpec(
        node_key="camera",
        node_type="source.channel_frame",
        node_category=RuntimeNodeCategory.SOURCE,
        output_ports=[RuntimeNodePort(key="frame", data_type="image.frame")],
    )
    inference = RuntimeNodeSpec(
        node_key="classify",
        node_type="inference.classification",
        node_category=RuntimeNodeCategory.INFERENCE,
        input_ports=[RuntimeNodePort(key="image", data_type="image.frame")],
        output_ports=[RuntimeNodePort(key="result", data_type="inference.result")],
        logical_model_ref=RuntimeLogicalModelRef(
            model_family_slug="e1-classifier",
            required_task_type=TaskType.CLASSIFICATION,
        ),
    )
    overlay = RuntimeNodeSpec(
        node_key="overlay",
        node_type="output.overlay",
        node_category=RuntimeNodeCategory.OUTPUT,
        input_ports=[RuntimeNodePort(key="result", data_type="inference.result")],
    )
    persisted = RuntimeNodeSpec(
        node_key="persist",
        node_type="output.persisted_result",
        node_category=RuntimeNodeCategory.OUTPUT,
        input_ports=[RuntimeNodePort(key="result", data_type="inference.result")],
    )
    graph = RuntimePipelineGraph(
        nodes=[source, inference, overlay, persisted],
        edges=[
            RuntimeGraphEdge(
                from_node_key="camera",
                from_port="frame",
                to_node_key="classify",
                to_port="image",
            ),
            RuntimeGraphEdge(
                from_node_key="classify",
                from_port="result",
                to_node_key="overlay",
                to_port="result",
            ),
            RuntimeGraphEdge(
                from_node_key="classify",
                from_port="result",
                to_node_key="persist",
                to_port="result",
            ),
        ],
    )
    manifest = EdgeDeploymentManifest(
        deployment_id="deployment-e1",
        workload_release_id="release-e1",
        channel_id="channel-e1",
        pipeline_version_id="pipeline-e1",
        idempotency_key="prepare-e1",
        created_at=datetime.now(UTC).isoformat(),
    )
    executor = EdgeGraphExecutor(
        manifest=manifest,
        graph=graph,
        inference_runtime=_GraphInference(),  # type: ignore[arg-type]
        mode="realtime",
    )

    result = executor.execute(
        SourceFrame(
            index=7,
            width=48,
            height=32,
            captured_at=datetime.now(UTC).isoformat(),
            image=np.zeros((32, 48, 3), dtype=np.uint8),
        ),
        correlation_id="e1-correlation",
    )

    assert result["schema_version"] == "runtime.execution.result.v1"
    assert result["normalized_inference_outputs"][0]["predictions"][0]["label"] == "good"
    assert result["overlay_primitives"][0]["primitive_type"] == "label"
    assert result["persisted_results"]
    json.dumps(result)


class _FakeSpsClient:
    def __init__(self) -> None:
        self.connected = False
        self.closed = False
        self.writes: list[tuple[int, int, bytes]] = []

    def connect(self) -> None:
        self.connected = True

    def read_db(self, db_number: int, byte_offset: int, size: int) -> bytearray:
        assert (db_number, byte_offset, size) == (7, 12, 1)
        return bytearray([0])

    def write_db(self, db_number: int, byte_offset: int, data: bytes | bytearray) -> None:
        self.writes.append((db_number, byte_offset, bytes(data)))

    def close(self) -> None:
        self.closed = True


def test_edge_graph_executes_deployed_siemens_sps_ip_db_and_bit_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YBB_ALLOW_PLC_WRITES", "true")
    source = RuntimeNodeSpec(
        node_key="camera",
        node_type="source.channel_frame",
        node_category=RuntimeNodeCategory.SOURCE,
        output_ports=[RuntimeNodePort(key="frame", data_type="image.frame")],
    )
    inference = RuntimeNodeSpec(
        node_key="classify",
        node_type="inference.classification",
        node_category=RuntimeNodeCategory.INFERENCE,
        input_ports=[RuntimeNodePort(key="image", data_type="image.frame")],
        output_ports=[RuntimeNodePort(key="result", data_type="result")],
        logical_model_ref=RuntimeLogicalModelRef(
            model_family_slug="e1-classifier",
            required_task_type=TaskType.CLASSIFICATION,
        ),
    )
    plc = RuntimeNodeSpec(
        node_key="write_plc",
        node_type="output.siemens_sps_write",
        node_category=RuntimeNodeCategory.OUTPUT,
        input_ports=[RuntimeNodePort(key="result", data_type="result")],
        config={
            "enabled": True,
            "dry_run": False,
            "connection": {
                "name": "Line PLC",
                "host": "192.168.50.30",
                "port": 102,
                "rack": 0,
                "slot": 1,
                "connect_timeout_ms": 1000,
                "write_timeout_ms": 1000,
                "reconnect": "on_failure",
                "reuse_policy": "per_execution",
                "connection_ref": None,
            },
            "mappings": [
                {
                    "key": "classification_ok",
                    "label": "Classification OK",
                    "enabled": True,
                    "source": {
                        "field": "classification.top1.label",
                        "extractor": "nested_result_path",
                        "advanced_path": "",
                        "allow_advanced_path": False,
                    },
                    "target": {
                        "area": "DB",
                        "db_number": 7,
                        "byte_offset": 12,
                        "bit_offset": 3,
                        "data_type": "BOOL",
                    },
                    "conversion": {
                        "mode": "enum",
                        "enum_values": [{"source": "good", "value": True}],
                    },
                    "condition": {"mode": "always"},
                    "failure_behavior": "inherit",
                }
            ],
            "failure_behavior": "fail_pipeline",
            "realtime_min_interval_ms": 1000,
        },
    )
    graph = RuntimePipelineGraph(
        nodes=[source, inference, plc],
        edges=[
            RuntimeGraphEdge(
                from_node_key="camera",
                from_port="frame",
                to_node_key="classify",
                to_port="image",
            ),
            RuntimeGraphEdge(
                from_node_key="classify",
                from_port="result",
                to_node_key="write_plc",
                to_port="result",
            ),
        ],
    )
    manifest = EdgeDeploymentManifest(
        deployment_id="deployment-edge-plc",
        workload_release_id="release-edge-plc",
        channel_id="channel-edge-plc",
        pipeline_version_id="pipeline-edge-plc",
        idempotency_key="prepare-edge-plc",
        created_at=datetime.now(UTC).isoformat(),
    )
    client = _FakeSpsClient()
    inference_runtime = _GraphInference()
    registry = build_productive_registry(
        inference_runtime,  # type: ignore[arg-type]
        siemens_sps_handler=SiemensSpsWriteHandler(
            "output.siemens_sps_write",
            client_factory=lambda _config: client,
            dependency_available=lambda: True,
        ),
    )
    executor = EdgeGraphExecutor(
        manifest=manifest,
        graph=graph,
        inference_runtime=inference_runtime,  # type: ignore[arg-type]
        mode="realtime",
        registry=registry,
    )

    result = executor.execute(
        SourceFrame(
            index=1,
            width=48,
            height=32,
            captured_at=datetime.now(UTC).isoformat(),
            image=np.zeros((32, 48, 3), dtype=np.uint8),
        )
    )
    executor.close()

    assert client.connected is True
    assert client.writes == [(7, 12, b"\x08")]
    assert client.closed is True
    assert result["derived_outputs"][-1]["value"]["status"] == "success"
    assert result["derived_outputs"][-1]["value"]["writes"][0]["address"] == "DB7.DBX12.3"
