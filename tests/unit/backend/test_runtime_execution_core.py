from __future__ import annotations

import asyncio
import base64
import io
import math
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from admin_api.main import app as admin_app
from cv_contracts import (
    AnomalyHeatmap,
    AnomalyInferenceResponse,
    ClassificationInferenceResponse,
    ClassificationLabelScore,
    DetectionBox,
    DetectionInferenceResponse,
    DetectionPrediction,
    InferenceTiming,
    ModelArchitectureSummary,
    ModelRouteRef,
    SegmentationPoint,
    SegmentationPrediction,
)
from cv_contracts.runtime import (
    RuntimeChannelCreateRequest,
    RuntimeGraphEdge,
    RuntimeLogicalModelRef,
    RuntimeNodePort,
    RuntimeNodeSpec,
    RuntimePipelineDefinitionCreateRequest,
    RuntimePipelineGraph,
    RuntimePipelineValidationRequest,
    RuntimePipelineVersionCreateRequest,
)
from cv_core import PlatformSettings, generate_prefixed_id
from cv_db import (
    ResultRecord,
    RuntimeModelCapabilityCache,
    RuntimeNodeTypeRegistry,
    TriggerEndpoint,
    build_runtime_model_resolver,
    create_pipeline_definition,
    create_pipeline_version,
    create_runtime_channel,
    ensure_builtin_node_types,
    get_runtime_live_view_state,
    list_runtime_model_capability_options,
    publish_pipeline_version,
    session_scope,
    validate_pipeline_request,
)
from cv_db.models import ModelPromotion, RuntimeChannel
from cv_runtime import (
    BUILTIN_NODE_TYPES,
    ExecutablePipelineGraph,
    RuntimeExecutionContext,
    RuntimeHandlerLoadError,
    validate_runtime_handler_ref,
)
from cv_runtime.nodes.builtin import (
    ColorObjectSegmentationHandler,
    HttpTriggerResponseOutputHandler,
    ObjectSpatialSignalHandler,
)
from cv_runtime.payloads import (
    FramePayload,
    MeasurementsPayload,
    RegionSetPayload,
    ResultPayload,
    RuntimeImageContext,
    RuntimeImageTransform,
    SegmentPayload,
)
from cv_status import (
    DeploymentReleaseStatus,
    PromotionStatus,
    RuntimeMode,
    RuntimeNodeCategory,
    RuntimeSourceType,
    TargetPath,
    TaskType,
)
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from starlette.requests import Request
from realtime_executor.live import _display_selection_from_result
from realtime_executor.main import app as realtime_app
from result_service.main import app as result_app
from runtime_admin_api.main import app as runtime_admin_app
from tests.conftest import build_test_settings, configure_sqlite_factories
from tests.support.runtime_model_seed import seed_deployed_runtime_model
from trigger_executor.api.routes import _read_trigger_execution_payload
from trigger_executor.main import app as trigger_app


def _trigger_request_with_body(body: bytes, *, query_string: bytes = b"") -> Request:
    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/api/v1/runtime/triggers/test",
            "headers": [],
            "query_string": query_string,
        },
        receive,
    )


def test_trigger_execution_payload_accepts_empty_body_without_content_type() -> None:
    payload = asyncio.run(
        _read_trigger_execution_payload(
            _trigger_request_with_body(
                b"",
                query_string=b"trigger_id=sps-cycle-1&timeout_ms=1200",
            )
        )
    )

    assert payload.trigger_id == "sps-cycle-1"
    assert payload.timeout_ms == 1200
    assert payload.payload == {}


def test_trigger_execution_payload_accepts_json_without_content_type() -> None:
    payload = asyncio.run(
        _read_trigger_execution_payload(
            _trigger_request_with_body(
                b'{"trigger_id":"sps-cycle-2","payload":{"station":"line-1"}}'
            )
        )
    )

    assert payload.trigger_id == "sps-cycle-2"
    assert payload.payload == {"station": "line-1"}


class _FakeInferenceClient:
    def anomaly(self, payload) -> AnomalyInferenceResponse:
        return AnomalyInferenceResponse(
            request_id="req_anomaly",
            correlation_id="corr_anomaly",
            deployment_target=payload.deployment_target,
            route=ModelRouteRef(
                deployment_release_id=payload.deployment_release_id or "rel_anomaly",
                target_path=TargetPath.REALTIME,
                release_status=DeploymentReleaseStatus.ACTIVATED,
                model_family_slug="surface-anomaly-ad",
                model_version_id="mvr_anomaly",
                task_type=TaskType.ANOMALY,
                triton_model_name="surface_anomaly",
                triton_model_version=1,
                contract_major=1,
            ),
            architecture=ModelArchitectureSummary(
                task_type=TaskType.ANOMALY,
                family="patchcore",
                version="anomalib",
                size="wide_resnet50_2",
                display_name="PatchCore WideResNet-50-2",
                internal_key="anomaly:patchcore:anomalib:wide_resnet50_2",
            ),
            warm_model=True,
            timing=InferenceTiming(
                received_at="2026-05-01T12:00:00Z",
                validation_ms=1,
                routing_ms=1,
                preprocessing_ms=2,
                inference_ms=12,
                postprocessing_ms=2,
                total_ms=18,
            ),
            anomaly_score=0.73,
            predicted_label="anomalous",
            is_anomalous=True,
            threshold=0.5,
            heatmap=AnomalyHeatmap(
                data="iVBORw0KGgo=",
                width=1,
                height=1,
                min_value=0.0,
                max_value=1.0,
                colormap="turbo",
            ),
            image_width=1,
            image_height=1,
            model_metadata={"method": "patchcore"},
        )

    def classify(self, payload):  # pragma: no cover - not used in these tests
        raise AssertionError("Classification should not be called in this test.")

    def detect(self, payload) -> DetectionInferenceResponse:
        return DetectionInferenceResponse(
            request_id="req_detection",
            correlation_id="corr_detection",
            deployment_target=payload.deployment_target,
            route=ModelRouteRef(
                deployment_release_id=payload.deployment_release_id or "rel_detection",
                target_path=TargetPath.REALTIME,
                release_status=DeploymentReleaseStatus.ACTIVATED,
                model_family_slug="defect-detector",
                model_version_id="mvr_detection",
                task_type=TaskType.DETECTION,
                triton_model_name="defect_detector",
                triton_model_version=1,
                contract_major=1,
            ),
            architecture=ModelArchitectureSummary(
                task_type=TaskType.DETECTION,
                family="yolo",
                version="v11",
                size="n",
                display_name="YOLOv11 Nano",
                internal_key="yolo-v11-n",
            ),
            warm_model=True,
            timing=InferenceTiming(
                received_at="2026-05-01T12:00:00Z",
                validation_ms=1,
                routing_ms=1,
                preprocessing_ms=2,
                inference_ms=10,
                postprocessing_ms=1,
                total_ms=15,
            ),
            detections=[
                DetectionPrediction(
                    label="defect",
                    score=0.92,
                    box=DetectionBox(x_min=0.1, y_min=0.15, x_max=0.4, y_max=0.55),
                )
            ],
        )

    def segment(self, payload):  # pragma: no cover - not used in these tests
        raise AssertionError("Segmentation should not be called in this test.")


class _BoardFeatureInferenceClient(_FakeInferenceClient):
    def detect(self, payload) -> DetectionInferenceResponse:
        response = super().detect(payload)
        return response.model_copy(
            update={
                "request_id": "req_board_features",
                "detections": [
                    DetectionPrediction(
                        label="board_start_edge",
                        score=0.99,
                        box=DetectionBox(x_min=0.05, y_min=0.10, x_max=0.08, y_max=0.90),
                    ),
                    DetectionPrediction(
                        label="board_side_edge",
                        score=0.98,
                        box=DetectionBox(x_min=0.05, y_min=0.10, x_max=0.95, y_max=0.13),
                    ),
                    DetectionPrediction(
                        label="knot",
                        score=0.91,
                        box=DetectionBox(x_min=0.35, y_min=0.40, x_max=0.45, y_max=0.60),
                    ),
                ],
            }
        )


class _PairMovementInferenceClient(_FakeInferenceClient):
    def __init__(self) -> None:
        self.detections = [
            DetectionPrediction(
                label="person",
                score=0.96,
                box=DetectionBox(x_min=0.10, y_min=0.40, x_max=0.20, y_max=0.60),
            ),
            DetectionPrediction(
                label="forklift",
                score=0.94,
                box=DetectionBox(x_min=0.70, y_min=0.40, x_max=0.80, y_max=0.60),
            ),
        ]

    def detect(self, payload) -> DetectionInferenceResponse:
        response = super().detect(payload)
        return response.model_copy(
            update={
                "request_id": "req_pair_movement",
                "detections": self.detections,
            }
        )


class _OverlappingVehicleInferenceClient(_FakeInferenceClient):
    def detect(self, payload) -> DetectionInferenceResponse:
        response = super().detect(payload)
        return response.model_copy(
            update={
                "request_id": "req_vehicle_overlap",
                "detections": [
                    DetectionPrediction(
                        label="LKW",
                        score=0.94,
                        box=DetectionBox(x_min=0.20, y_min=0.20, x_max=0.62, y_max=0.82),
                    ),
                    DetectionPrediction(
                        label="Lader",
                        score=0.88,
                        box=DetectionBox(x_min=0.45, y_min=0.28, x_max=0.78, y_max=0.62),
                    ),
                    DetectionPrediction(
                        label="LKW",
                        score=0.91,
                        box=DetectionBox(x_min=0.72, y_min=0.12, x_max=0.88, y_max=0.35),
                    ),
                ],
            }
        )


class _SlowInferenceClient(_FakeInferenceClient):
    def detect(self, payload) -> DetectionInferenceResponse:
        time.sleep(0.2)
        return super().detect(payload)


class _FakeClassificationClient(_FakeInferenceClient):
    def classify(self, payload) -> ClassificationInferenceResponse:
        return ClassificationInferenceResponse(
            request_id="req_classification",
            correlation_id="corr_classification",
            deployment_target=payload.deployment_target,
            route=ModelRouteRef(
                deployment_release_id=payload.deployment_release_id or "rel_classifier",
                target_path=TargetPath.REALTIME,
                release_status=DeploymentReleaseStatus.ACTIVATED,
                model_family_slug="roi-classifier",
                model_version_id="mvr_classifier",
                task_type=TaskType.CLASSIFICATION,
                triton_model_name="roi_classifier",
                triton_model_version=1,
                contract_major=1,
            ),
            architecture=ModelArchitectureSummary(
                task_type=TaskType.CLASSIFICATION,
                family="resnet",
                version="v1",
                size="18",
                display_name="ResNet 18",
                internal_key="resnet-18",
            ),
            warm_model=True,
            timing=InferenceTiming(
                received_at="2026-05-01T12:00:00Z",
                validation_ms=1,
                routing_ms=1,
                preprocessing_ms=2,
                inference_ms=8,
                postprocessing_ms=1,
                total_ms=13,
            ),
            labels=[ClassificationLabelScore(label="scratch", score=0.88, rank=1)],
        )


class _FakeBinaryClassificationClient(_FakeClassificationClient):
    def __init__(self) -> None:
        self.used_binary_transport = False
        self.last_image_bytes = b""

    def classify_bytes(
        self,
        *,
        deployment_target: str,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        deployment_release_id: str | None = None,
        timeout_ms: int | None = None,
        top_k: int = 1,
    ) -> ClassificationInferenceResponse:
        self.used_binary_transport = True
        self.last_image_bytes = image_bytes
        return self.classify(
            SimpleNamespace(
                deployment_target=deployment_target,
                deployment_release_id=deployment_release_id,
            )
        )


def test_handler_resolution_rejects_missing_modules() -> None:
    with pytest.raises(RuntimeHandlerLoadError):
        validate_runtime_handler_ref(
            "missing.runtime.module:handler",
            expected_node_type="source.file_test",
        )


def test_graph_build_uses_deterministic_topological_order() -> None:
    graph = RuntimePipelineGraph(
        nodes=[
            _node("count_objects", "analysis.object_counting"),
            _node("overlay", "output.overlay"),
            _inference_node("detect_objects", "inference.detection"),
            _node("file_source", "source.file_test", config={"path": "/tmp/sample.png"}),
            _node("persist", "output.persisted_result"),
        ],
        edges=[
            _edge("file_source", "frame", "detect_objects", "frame"),
            _edge("detect_objects", "detections", "count_objects", "objects"),
            _edge("detect_objects", "detections", "overlay", "result"),
            _edge("count_objects", "counts", "persist", "result"),
        ],
    )

    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_order",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    assert [node.spec.node_key for node in executable.nodes] == [
        "file_source",
        "detect_objects",
        "count_objects",
        "overlay",
        "persist",
    ]


def test_uploaded_test_photo_source_materializes_embedded_image(
    sample_png_bytes: bytes,
) -> None:
    graph = RuntimePipelineGraph(
        nodes=[
            _node(
                "test_photo",
                "source.test_photo",
                config={
                    "photo": {
                        "data_url": "data:image/png;base64,"
                        + base64.b64encode(sample_png_bytes).decode("ascii"),
                        "name": "sample.png",
                        "mime_type": "image/png",
                        "size": len(sample_png_bytes),
                    }
                },
            )
        ],
        edges=[],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_test_photo",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_test_photo",
            pipeline_version_id="plv_test_photo",
            channel_id="chn_test_photo",
            mode=RuntimeMode.REALTIME,
        )
    )

    assert result.status == "completed", result.errors
    frame = result.node_outputs["test_photo"]["frame"]
    assert frame["mime_type"] == "image/png"
    assert frame["source_uri"] == "pipeline-test-photo://sample.png"


def test_graph_execution_collects_structured_result_envelopes(
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(sample_png_bytes)
    graph = RuntimePipelineGraph(
        nodes=[
            _node("file_source", "source.file_test", config={"path": str(image_path)}),
            _inference_node("detect_objects", "inference.detection"),
            _node("count_objects", "analysis.object_counting"),
            _node("evaluate", "analysis.rule_evaluation", config={"expression": "count >= 1"}),
            _node("persist", "output.persisted_result"),
            _node("overlay", "output.overlay"),
        ],
        edges=[
            _edge("file_source", "frame", "detect_objects", "frame"),
            _edge("detect_objects", "detections", "count_objects", "objects"),
            _edge("count_objects", "counts", "evaluate", "result"),
            _edge("evaluate", "result", "persist", "result"),
            _edge("detect_objects", "detections", "overlay", "result"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_exec",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_exec",
            pipeline_version_id="plv_exec",
            channel_id="chn_exec",
            mode=RuntimeMode.REALTIME,
            inference_client=_FakeInferenceClient(),
        )
    )

    assert result.status == "completed", result.errors
    assert result.raw_inference_outputs[0]["node_type"] == "inference.detection"
    assert (
        result.raw_inference_outputs[0]["gateway_response"]["route"]["deployment_release_id"]
        == "rel_detection"
    )
    assert (
        result.normalized_inference_outputs[0]["payload"]["metadata"]["model_family_slug"]
        == "defect-detector"
    )
    assert result.overlay_outputs[0]["node_type"] == "output.overlay"
    assert (
        result.overlay_outputs[0]["payload"]["metadata"]["source_payload_type"]
        == "detections"
    )
    assert result.persisted_results[0]["payload"]["result"]["pipeline_version_id"] == "plv_exec"
    assert result.final_response["count"] == 1
    assert result.final_response["result"] is True
    assert result.image_contexts
    assert result.object_sets[0]["objects"][0]["source_image_context_id"]
    assert result.overlay_primitives[0]["image_context_id"]
    assert result.overlay_primitives[0]["object_id"]


def test_result_output_publishes_counting_fields_for_realtime(
    sample_png_bytes: bytes,
) -> None:
    graph = RuntimePipelineGraph(
        nodes=[
            _node("channel_input", "source.channel_frame"),
            _inference_node("detect_objects", "inference.detection"),
            _node("count_objects", "analysis.object_counting"),
            _node("result_output", "output.result"),
        ],
        edges=[
            _edge("channel_input", "frame", "detect_objects", "frame"),
            _edge("detect_objects", "detections", "count_objects", "objects"),
            _edge("count_objects", "counts", "result_output", "result"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_result_count",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_result_count",
            pipeline_version_id="plv_result_count",
            channel_id="chn_result_count",
            mode=RuntimeMode.REALTIME,
            input_frame=FramePayload.from_bytes(sample_png_bytes, mime_type="image/png"),
            inference_client=_FakeInferenceClient(),
        )
    )

    assert result.status == "completed", result.errors
    assert result.final_response["count_defect"] == 1
    assert result.final_response["result"] is True
    assert result.persisted_results[0]["node_type"] == "output.result"


def test_result_output_maps_classification_label(
    sample_png_bytes: bytes,
) -> None:
    graph = RuntimePipelineGraph(
        nodes=[
            _node("channel_input", "source.channel_frame"),
            _classification_node("classify_frame"),
            _node(
                "result_output",
                "output.result",
                config={
                    "result_fields": [
                        {
                            "source_field": "top_label.label",
                            "response_key": "class",
                            "required": True,
                        }
                    ],
                    "include_runtime_metadata": False,
                },
            ),
        ],
        edges=[
            _edge("channel_input", "frame", "classify_frame", "frame"),
            _edge("classify_frame", "classifications", "result_output", "result"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_result_classification",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_result_classification",
            pipeline_version_id="plv_result_classification",
            channel_id="chn_result_classification",
            mode=RuntimeMode.TRIGGER,
            input_frame=FramePayload.from_bytes(sample_png_bytes, mime_type="image/png"),
            inference_client=_FakeClassificationClient(),
        )
    )

    assert result.status == "completed", result.errors
    assert result.final_response["class"] == "scratch"
    assert result.final_response["result"] is True
    assert "channel_id" not in result.final_response


def test_zone_and_close_detection_result_output_includes_zone_matches(
    sample_png_bytes: bytes,
) -> None:
    graph = RuntimePipelineGraph(
        nodes=[
            _node("channel_input", "source.channel_frame"),
            _inference_node("detect_objects", "inference.detection"),
            _node(
                "zone_detection",
                "analysis.zone_detection",
                config={
                    "zones": [
                        {
                            "label": "Zone A",
                            "x": 0.15,
                            "y": 0.15,
                            "width": 0.70,
                            "height": 0.70,
                        }
                    ],
                    "zone_match": "box_overlaps",
                    "match_policy": "all",
                    "rules": [
                        {
                            "rule_key": "zone_a_lkw",
                            "relation": "zone_occupancy",
                            "enabled": True,
                            "primary_labels": ["LKW"],
                            "zone_key": "Zone A",
                            "zone_match": "box_overlaps",
                            "min_count": 1,
                            "signal_label": "LKW in Zone A",
                        },
                        {
                            "rule_key": "zone_a_lader",
                            "relation": "zone_occupancy",
                            "enabled": True,
                            "primary_labels": ["Lader"],
                            "zone_key": "Zone A",
                            "zone_match": "box_overlaps",
                            "min_count": 1,
                            "signal_label": "Lader in Zone A",
                        },
                    ],
                },
            ),
            _node(
                "close_detection",
                "analysis.close_detection",
                config={
                    "rules": [
                        {
                            "rule_key": "lkw_lader_overlap",
                            "relation": "overlap",
                            "enabled": True,
                            "primary_labels": ["LKW"],
                            "secondary_labels": ["Lader"],
                            "label_pairing": "different_label",
                            "min_overlap_percent": 1,
                            "signal_label": "LKW overlaps Lader",
                        }
                    ]
                },
            ),
            _node("result_output", "output.result", config={"include_runtime_metadata": False}),
        ],
        edges=[
            _edge("channel_input", "frame", "detect_objects", "frame"),
            _edge("detect_objects", "detections", "zone_detection", "objects"),
            _edge("zone_detection", "objects", "close_detection", "objects"),
            _edge("close_detection", "result", "result_output", "result"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_zone_close",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_zone_close",
            pipeline_version_id="plv_zone_close",
            channel_id="chn_zone_close",
            mode=RuntimeMode.REALTIME,
            input_frame=FramePayload.from_bytes(sample_png_bytes, mime_type="image/png"),
            inference_client=_OverlappingVehicleInferenceClient(),
        )
    )

    assert result.status == "completed", result.errors
    assert result.final_response["signal"] is True
    assert result.final_response["result"] is True
    assert result.final_response["match_count"] == 1
    match = result.final_response["matches"][0]
    assert match["relation"] == "overlap"
    assert match["primary"]["label"] == "LKW"
    assert match["secondary"]["label"] == "Lader"
    assert match["primary"]["zones"][0]["label"] == "Zone A"
    assert match["secondary"]["zones"][0]["label"] == "Zone A"
    assert result.persisted_results[0]["node_type"] == "output.result"


def test_result_output_projections_control_response_history_and_decision(
    sample_png_bytes: bytes,
) -> None:
    graph = RuntimePipelineGraph(
        nodes=[
            _node("channel_input", "source.channel_frame"),
            _inference_node("detect_objects", "inference.detection"),
            _node(
                "zone_detection",
                "analysis.zone_detection",
                config={
                    "zones": [
                        {
                            "label": "Zone A",
                            "x": 0.15,
                            "y": 0.15,
                            "width": 0.70,
                            "height": 0.70,
                        }
                    ],
                    "rules": [
                        {
                            "rule_key": "zone_a_entry",
                            "enabled": True,
                            "class_filter": ["LKW", "Lader"],
                            "zone_key": "Zone A",
                            "zone_match": "box_overlaps",
                            "min_count": 1,
                        },
                    ],
                },
            ),
            _node(
                "close_detection",
                "analysis.close_detection",
                config={
                    "rules": [
                        {
                            "rule_key": "lkw_lader_overlap",
                            "relation": "overlap",
                            "enabled": True,
                            "primary_labels": ["LKW"],
                            "secondary_labels": ["Lader"],
                            "label_pairing": "different_label",
                            "min_overlap_percent": 1,
                        }
                    ]
                },
            ),
            _node(
                "result_output",
                "output.result",
                config={
                    "include_runtime_metadata": False,
                    "projections": [
                        {
                            "key": "skewed_count",
                            "source": {"node_key": "close_detection", "path": "matches"},
                            "aggregate": {"op": "count"},
                            "destinations": ["response", "history"],
                        },
                        {
                            "key": "skewed_objects",
                            "source": {"node_key": "close_detection", "path": "matches"},
                            "aggregate": {"op": "list"},
                            "fields": [
                                {"source_path": "primary.label", "output_key": "label"},
                                {
                                    "source_path": "primary.geometry",
                                    "output_key": "position",
                                },
                            ],
                            "destinations": ["response"],
                        },
                        {
                            "key": "quality",
                            "source": {"node_key": "close_detection", "path": "matches"},
                            "aggregate": {"op": "count"},
                            "decision": {
                                "op": "gt",
                                "value": 0,
                                "when_true": "FAIL",
                                "when_false": "PASS",
                            },
                            "destinations": ["response"],
                        },
                    ],
                },
            ),
        ],
        edges=[
            _edge("channel_input", "frame", "detect_objects", "frame"),
            _edge("detect_objects", "detections", "zone_detection", "objects"),
            _edge("zone_detection", "objects", "close_detection", "objects"),
            _edge("close_detection", "result", "result_output", "result"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_projection_result",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_projection_result",
            pipeline_version_id="plv_projection_result",
            channel_id="chn_projection_result",
            mode=RuntimeMode.REALTIME,
            input_frame=FramePayload.from_bytes(sample_png_bytes, mime_type="image/png"),
            inference_client=_OverlappingVehicleInferenceClient(),
        )
    )

    assert result.status == "completed", result.errors
    assert result.final_response == {
        "skewed_count": 1,
        "skewed_objects": [
            {
                "label": "LKW",
                "position": result.final_response["skewed_objects"][0]["position"],
            }
        ],
        "quality": "FAIL",
    }
    assert result.final_response["skewed_objects"][0]["position"]
    assert result.persisted_results[0]["payload"]["result"] == {"skewed_count": 1}
    assert {item["projection_key"] for item in result.projected_results} == {
        "skewed_count",
        "skewed_objects",
        "quality",
    }
    assert "signal" not in result.final_response
    assert "result" not in result.final_response


def test_zone_detection_respects_overlap_percent_threshold(
    sample_png_bytes: bytes,
) -> None:
    graph = RuntimePipelineGraph(
        nodes=[
            _node("channel_input", "source.channel_frame"),
            _inference_node("detect_objects", "inference.detection"),
            _node(
                "zone_detection",
                "analysis.zone_detection",
                config={
                    "zones": [
                        {
                            "label": "Zone A",
                            "x": 0.15,
                            "y": 0.15,
                            "width": 0.20,
                            "height": 0.20,
                        }
                    ],
                    "match_policy": "any",
                    "rules": [
                        {
                            "rule_key": "lkw_mostly_in_zone",
                            "relation": "zone_occupancy",
                            "enabled": True,
                            "primary_labels": ["LKW"],
                            "zone_key": "Zone A",
                            "zone_match": "box_overlaps",
                            "min_overlap_percent": 50,
                            "min_count": 1,
                        }
                    ],
                },
            ),
            _node("result_output", "output.result", config={"include_runtime_metadata": False}),
        ],
        edges=[
            _edge("channel_input", "frame", "detect_objects", "frame"),
            _edge("detect_objects", "detections", "zone_detection", "objects"),
            _edge("zone_detection", "result", "result_output", "result"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_zone_overlap_threshold",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_zone_overlap_threshold",
            pipeline_version_id="plv_zone_overlap_threshold",
            channel_id="chn_zone_overlap_threshold",
            mode=RuntimeMode.REALTIME,
            input_frame=FramePayload.from_bytes(sample_png_bytes, mime_type="image/png"),
            inference_client=_OverlappingVehicleInferenceClient(),
        )
    )

    assert result.status == "completed", result.errors
    assert result.final_response["signal"] is False
    assert result.final_response["match_count"] == 0
    assert result.final_response["matches"] == []


def test_object_spatial_signal_detects_approaching_objects(
    sample_png_bytes: bytes,
) -> None:
    graph = RuntimePipelineGraph(
        nodes=[
            _node("channel_input", "source.channel_frame"),
            _inference_node("detect_objects", "inference.detection"),
            _node(
                "spatial_signal",
                "analysis.object_spatial_signal",
                config={
                    "rules": [
                        {
                            "rule_key": "person_forklift_approach",
                            "relation": "approaching",
                            "primary_labels": ["person"],
                            "secondary_labels": ["forklift"],
                            "label_pairing": "different_label",
                            "max_distance_percent": 50,
                            "min_closing_percent": 3,
                            "signal_label": "Person approaching forklift",
                        }
                    ]
                },
            ),
            _node("persist", "output.persisted_result"),
        ],
        edges=[
            _edge("channel_input", "frame", "detect_objects", "frame"),
            _edge("detect_objects", "detections", "spatial_signal", "primary_objects"),
            _edge("spatial_signal", "signal", "persist", "result"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_spatial_approach",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )
    client = _PairMovementInferenceClient()
    state: dict[str, object] = {}

    first = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_spatial_1",
            pipeline_version_id="plv_spatial_approach",
            channel_id="chn_spatial",
            mode=RuntimeMode.REALTIME,
            input_frame=FramePayload.from_bytes(sample_png_bytes, mime_type="image/png"),
            inference_client=client,
            state=state,
        )
    )
    assert first.final_response["signal"] is False

    client.detections = [
        DetectionPrediction(
            label="person",
            score=0.96,
            box=DetectionBox(x_min=0.30, y_min=0.40, x_max=0.40, y_max=0.60),
        ),
        DetectionPrediction(
            label="forklift",
            score=0.94,
            box=DetectionBox(x_min=0.50, y_min=0.40, x_max=0.60, y_max=0.60),
        ),
    ]
    second = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_spatial_2",
            pipeline_version_id="plv_spatial_approach",
            channel_id="chn_spatial",
            mode=RuntimeMode.REALTIME,
            input_frame=FramePayload.from_bytes(sample_png_bytes, mime_type="image/png"),
            inference_client=client,
            state=state,
        )
    )

    assert second.final_response["signal"] is True
    match = second.final_response["matches"][0]
    assert match["relation"] == "approaching"
    assert match["closing_percent"] > 3
    assert second.persisted_results[0]["payload"]["decision"] is True
    assert any(primitive["layer"] == "spatial_distance" for primitive in second.overlay_primitives)


def test_object_spatial_signal_uses_segmentation_polygons_for_overlap() -> None:
    handler = ObjectSpatialSignalHandler("analysis.object_spatial_signal")
    node = _node(
        "spatial_signal",
        "analysis.object_spatial_signal",
        config={
            "rules": [
                {
                    "rule_key": "segment_overlap",
                    "relation": "overlap",
                    "primary_labels": ["part"],
                    "secondary_labels": ["seal"],
                    "min_overlap_percent": 20,
                    "signal_label": "Part overlaps seal",
                }
            ]
        },
    )
    segments = SegmentPayload(
        image_context_id="ctx_segments",
        segments=[
            SegmentationPrediction(
                label="part",
                score=0.91,
                polygon=[
                    SegmentationPoint(x=0.10, y=0.10),
                    SegmentationPoint(x=0.50, y=0.10),
                    SegmentationPoint(x=0.50, y=0.50),
                    SegmentationPoint(x=0.10, y=0.50),
                ],
            ),
            SegmentationPrediction(
                label="seal",
                score=0.88,
                polygon=[
                    SegmentationPoint(x=0.30, y=0.30),
                    SegmentationPoint(x=0.70, y=0.30),
                    SegmentationPoint(x=0.70, y=0.70),
                    SegmentationPoint(x=0.30, y=0.70),
                ],
            ),
        ],
    )

    result = handler.execute(
        RuntimeExecutionContext(
            correlation_id="corr_segment_overlap",
            pipeline_version_id="plv_segment_overlap",
            channel_id="chn_segment_overlap",
            mode=RuntimeMode.REALTIME,
        ),
        node,
        {"primary_objects": segments},
    )

    signal = result.outputs["signal"]
    assert isinstance(signal, ResultPayload)
    assert signal.decision is True
    assert signal.result["matches"][0]["overlap_percent"] >= 20


def test_color_object_segmentation_feeds_orientation_signal() -> None:
    image = Image.new("RGB", (220, 140), (36, 48, 45))
    draw = ImageDraw.Draw(image)
    draw.polygon([(20, 32), (200, 32), (200, 43), (20, 43)], fill=(238, 218, 176))
    draw.polygon([(40, 84), (188, 112), (186, 123), (38, 95)], fill=(238, 218, 176))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    graph = RuntimePipelineGraph(
        nodes=[
            _node("channel_input", "source.channel_frame"),
            _node(
                "color_objects",
                "computer_vision.color_object_segmentation",
                config={
                    "profiles": [
                        {
                            "label": "board",
                            "enabled": True,
                            "color_space": "hsv",
                            "hue_min": 0,
                            "hue_max": 179,
                            "saturation_min": 0,
                            "saturation_max": 120,
                            "value_min": 150,
                            "value_max": 255,
                            "min_area_percent": 0.1,
                            "min_aspect_ratio": 1,
                            "display_color": "#f3d7a5",
                        }
                    ],
                    "blur_kernel_size": 0,
                    "morphology_kernel_size": 0,
                    "morphology_iterations": 0,
                    "polygon_epsilon_percent": 1,
                },
            ),
            _node(
                "orientation_signal",
                "analysis.object_orientation_signal",
                config={
                    "labels": ["board"],
                    "orientation_source": "drawn_direction",
                    "reference_direction": {
                        "label": "parallel",
                        "start": {"x": 0.1, "y": 0.25},
                        "end": {"x": 0.9, "y": 0.25},
                    },
                    "angle_tolerance_degrees": 8,
                    "min_aspect_ratio": 3,
                    "minimum_object_count": 2,
                    "emit_signal_on": "violation",
                },
            ),
            _node("persist", "output.persisted_result"),
        ],
        edges=[
            _edge("channel_input", "frame", "color_objects", "frame"),
            _edge("color_objects", "objects", "orientation_signal", "objects"),
            _edge("orientation_signal", "signal", "persist", "result"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_color_orientation",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_color_orientation",
            pipeline_version_id="plv_color_orientation",
            channel_id="chn_color_orientation",
            mode=RuntimeMode.REALTIME,
            input_frame=FramePayload.from_bytes(buffer.getvalue(), mime_type="image/png"),
        )
    )

    assert result.status == "completed", result.errors
    assert result.final_response["signal"] is True
    assert result.final_response["status"] == "fail"
    assert result.final_response["inspected_count"] == 2
    assert result.final_response["violation_count"] == 1
    assert result.object_sets[0]["objects"][0]["label"] == "board"
    assert any(
        primitive["layer"] == "orientation_axis_violation"
        for primitive in result.overlay_primitives
    )


def test_color_object_segmentation_splits_touching_same_color_objects_and_counts() -> None:
    image = Image.new("RGB", (180, 110), (24, 24, 24))
    draw = ImageDraw.Draw(image)
    draw.ellipse((42, 30, 92, 80), fill=(235, 24, 24))
    draw.ellipse((80, 30, 130, 80), fill=(235, 24, 24))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    handler = ColorObjectSegmentationHandler("computer_vision.color_object_segmentation")
    node = _node(
        "color_objects",
        "computer_vision.color_object_segmentation",
        config={
            "profiles": [
                {
                    "label": "red_part",
                    "enabled": True,
                    "color_space": "hsv",
                    "hue_min": 170,
                    "hue_max": 10,
                    "saturation_min": 140,
                    "saturation_max": 255,
                    "value_min": 120,
                    "value_max": 255,
                    "min_area_percent": 0.1,
                    "min_aspect_ratio": 1,
                    "display_color": "#ff0000",
                }
            ],
            "blur_kernel_size": 0,
            "morphology_kernel_size": 0,
            "morphology_iterations": 0,
            "split_touching_objects": True,
            "split_distance_threshold": 0.45,
            "exclusive_profile_matching": True,
        },
    )

    result = handler.execute(
        RuntimeExecutionContext(
            correlation_id="corr_color_split",
            pipeline_version_id="plv_color_split",
            channel_id="chn_color_split",
            mode=RuntimeMode.REALTIME,
        ),
        node,
        {"frame": FramePayload.from_bytes(buffer.getvalue(), mime_type="image/png")},
    )

    objects = result.outputs["objects"]
    counts = result.outputs["counts"]
    assert isinstance(objects, RegionSetPayload)
    assert isinstance(counts, MeasurementsPayload)
    assert len(objects.regions) == 2
    assert objects.metadata["counts_by_label"] == {"red_part": 2}
    assert {item.key: item.value for item in counts.measurements} == {
        "count_total": 2,
        "count_red_part": 2,
    }
    assert all(region.source_object_id for region in objects.regions)
    assert any(
        region.metadata["segmentation"]["split_method"] == "distance_watershed"
        for region in objects.regions
    )


def test_color_object_segmentation_result_output_counts_multiple_profiles() -> None:
    image = Image.new("RGB", (220, 130), (22, 28, 34))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 30, 60, 80), fill=(235, 24, 24))
    draw.rectangle((92, 30, 132, 80), fill=(24, 220, 70))
    draw.rectangle((155, 34, 196, 84), fill=(24, 220, 70))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    graph = RuntimePipelineGraph(
        nodes=[
            _node("channel_input", "source.channel_frame"),
            _node(
                "color_objects",
                "computer_vision.color_object_segmentation",
                config={
                    "profiles": [
                        {
                            "label": "red_part",
                            "enabled": True,
                            "color_space": "hsv",
                            "hue_min": 170,
                            "hue_max": 10,
                            "saturation_min": 140,
                            "saturation_max": 255,
                            "value_min": 120,
                            "value_max": 255,
                            "min_area_percent": 0.1,
                            "min_aspect_ratio": 1,
                            "display_color": "#ff0000",
                        },
                        {
                            "label": "green_part",
                            "enabled": True,
                            "color_space": "hsv",
                            "hue_min": 45,
                            "hue_max": 75,
                            "saturation_min": 120,
                            "saturation_max": 255,
                            "value_min": 100,
                            "value_max": 255,
                            "min_area_percent": 0.1,
                            "min_aspect_ratio": 1,
                            "display_color": "#00cc44",
                        },
                    ],
                    "blur_kernel_size": 0,
                    "morphology_kernel_size": 0,
                    "morphology_iterations": 0,
                    "exclusive_profile_matching": True,
                    "split_touching_objects": True,
                },
            ),
            _node("result_output", "output.result"),
        ],
        edges=[
            _edge("channel_input", "frame", "color_objects", "frame"),
            _edge("color_objects", "objects", "result_output", "result"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_color_counts",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_color_counts",
            pipeline_version_id="plv_color_counts",
            channel_id="chn_color_counts",
            mode=RuntimeMode.REALTIME,
            input_frame=FramePayload.from_bytes(buffer.getvalue(), mime_type="image/png"),
        )
    )

    assert result.status == "completed", result.errors
    assert result.final_response["count_total"] == 3
    assert result.final_response["count_red_part"] == 1
    assert result.final_response["count_green_part"] == 2
    assert result.final_response["counts_by_label"] == {"green_part": 2, "red_part": 1}
    assert len(result.final_response["objects"]) == 3
    assert len({item["object_id"] for item in result.final_response["objects"]}) == 3


def test_color_object_segmentation_overlay_output_passes_object_layers() -> None:
    image = Image.new("RGB", (180, 110), (24, 24, 24))
    draw = ImageDraw.Draw(image)
    draw.rectangle((30, 40, 140, 56), fill=(235, 210, 160))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    graph = RuntimePipelineGraph(
        nodes=[
            _node("channel_input", "source.channel_frame"),
            _node(
                "color_objects",
                "computer_vision.color_object_segmentation",
                config={
                    "profiles": [
                        {
                            "label": "board",
                            "enabled": True,
                            "color_space": "hsv",
                            "hue_min": 0,
                            "hue_max": 179,
                            "saturation_min": 0,
                            "saturation_max": 160,
                            "value_min": 120,
                            "value_max": 255,
                            "min_area_percent": 0.1,
                            "min_aspect_ratio": 1,
                            "display_color": "#ebd2a0",
                        }
                    ],
                    "blur_kernel_size": 0,
                    "morphology_kernel_size": 0,
                    "morphology_iterations": 0,
                    "split_touching_objects": False,
                },
            ),
            _node("overlay_output", "output.overlay"),
        ],
        edges=[
            _edge("channel_input", "frame", "color_objects", "frame"),
            _edge("color_objects", "overlay", "overlay_output", "result"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_color_overlay",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_color_overlay",
            pipeline_version_id="plv_color_overlay",
            channel_id="chn_color_overlay",
            mode=RuntimeMode.REALTIME,
            input_frame=FramePayload.from_bytes(buffer.getvalue(), mime_type="image/png"),
            metadata={"materialize_live_display_frame": True},
        )
    )

    assert result.status == "completed", result.errors
    overlay_payload = next(
        entry["payload"] for entry in result.overlay_outputs if entry["node_key"] == "overlay_output"
    )
    layers = {primitive["layer"] for primitive in overlay_payload["primitives"]}
    assert {"color_object_box", "color_object_contour", "color_object_axis"}.issubset(layers)
    assert all(primitive["object_id"] for primitive in overlay_payload["primitives"])
    selection = _display_selection_from_result(result)
    assert selection["layer_policy"]["selection"] == "default_primary_visual_context"
    assert selection["display_context"]["kind"] == "original"
    assert selection["frame"]["image_context"]["kind"] == "original"
    assert "mask" not in result.node_outputs["color_objects"]


def test_color_object_segmentation_overlay_output_can_emit_mask_only() -> None:
    image = Image.new("RGB", (120, 80), (24, 24, 24))
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 20, 96, 56), fill=(235, 40, 40))
    draw.rectangle((5, 5, 8, 8), fill=(235, 40, 40))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    graph = RuntimePipelineGraph(
        nodes=[
            _node("channel_input", "source.channel_frame"),
            _node(
                "color_objects",
                "computer_vision.color_object_segmentation",
                config={
                    "profiles": [
                        {
                            "label": "red_part",
                            "enabled": True,
                            "color_space": "hsv",
                            "hue_min": 0,
                            "hue_max": 10,
                            "saturation_min": 80,
                            "saturation_max": 255,
                            "value_min": 80,
                            "value_max": 255,
                            "min_area_percent": 1.0,
                            "display_color": "#eb2828",
                        }
                    ],
                    "blur_kernel_size": 0,
                    "morphology_kernel_size": 0,
                    "morphology_iterations": 0,
                    "split_touching_objects": False,
                    "overlay_output_mode": "mask",
                },
            ),
            _node("overlay_output", "output.overlay"),
        ],
        edges=[
            _edge("channel_input", "frame", "color_objects", "frame"),
            _edge("color_objects", "overlay", "overlay_output", "result"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_color_mask_overlay",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_color_mask_overlay",
            pipeline_version_id="plv_color_mask_overlay",
            channel_id="chn_color_mask_overlay",
            mode=RuntimeMode.REALTIME,
            input_frame=FramePayload.from_bytes(buffer.getvalue(), mime_type="image/png"),
            metadata={"materialize_live_display_frame": True},
        )
    )

    assert result.status == "completed", result.errors
    overlay_payload = next(
        entry["payload"] for entry in result.overlay_outputs if entry["node_key"] == "overlay_output"
    )
    primitives = overlay_payload["primitives"]
    layers = {primitive["layer"] for primitive in primitives}
    assert layers == {"color_object_mask"}
    assert primitives[0]["primitive_type"] == "mask"
    assert primitives[0]["geometry"]["data"]
    assert result.node_outputs["color_objects"]["mask"]["image_context"]["kind"] == "processed"
    assert (
        primitives[0]["image_context_id"]
        == result.node_outputs["channel_input"]["frame"]["image_context"]["context_id"]
    )
    mask_payload = FramePayload.model_validate(result.node_outputs["color_objects"]["mask"])
    mask_image = Image.open(io.BytesIO(mask_payload.image_bytes())).convert("L")
    assert mask_image.getpixel((48, 38)) > 0
    assert mask_image.getpixel((6, 6)) == 0


def test_color_object_segmentation_applies_rotated_zone_mask() -> None:
    image = Image.new("RGB", (200, 140), (24, 24, 24))
    draw = ImageDraw.Draw(image)
    draw.polygon(
        _rotated_rect_points(center=(100, 70), width=82, height=12, angle_degrees=30),
        fill=(235, 24, 24),
    )
    draw.rectangle((52, 51, 64, 63), fill=(235, 24, 24))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    handler = ColorObjectSegmentationHandler("computer_vision.color_object_segmentation")
    node = _node(
        "color_objects",
        "computer_vision.color_object_segmentation",
        config={
            "profiles": [
                {
                    "label": "red_board",
                    "enabled": True,
                    "color_space": "hsv",
                    "hue_min": 170,
                    "hue_max": 10,
                    "saturation_min": 140,
                    "saturation_max": 255,
                    "value_min": 120,
                    "value_max": 255,
                    "min_area_percent": 0.02,
                    "min_aspect_ratio": 1,
                    "display_color": "#ff0000",
                }
            ],
            "zones": [
                {
                    "label": "diagonal_lane",
                    "x": 0.25,
                    "y": 0.35,
                    "width": 0.5,
                    "height": 0.25,
                    "angle_degrees": 30,
                }
            ],
            "blur_kernel_size": 0,
            "morphology_kernel_size": 0,
            "morphology_iterations": 0,
            "split_touching_objects": False,
        },
    )

    result = handler.execute(
        RuntimeExecutionContext(
            correlation_id="corr_color_rotated_zone",
            pipeline_version_id="plv_color_rotated_zone",
            channel_id="chn_color_rotated_zone",
            mode=RuntimeMode.REALTIME,
        ),
        node,
        {"frame": FramePayload.from_bytes(buffer.getvalue(), mime_type="image/png")},
    )

    objects = result.outputs["objects"]
    assert isinstance(objects, RegionSetPayload)
    assert len(objects.regions) == 1
    assert objects.metadata["count_total"] == 1
    assert objects.metadata["contour_display_mode"] == "oriented_box"
    geometry = objects.regions[0].metadata["geometry"]
    assert geometry["polygon"] == geometry["oriented_box"]
    assert len(geometry["polygon"]) == 4
    assert len(geometry["oriented_box"]) == 4
    assert len(geometry["contour_polygon"]) >= 4
    assert len(geometry["hull_polygon"]) >= 3
    axis_center_x = (geometry["axis"]["start"]["x"] + geometry["axis"]["end"]["x"]) / 2
    axis_center_y = (geometry["axis"]["start"]["y"] + geometry["axis"]["end"]["y"]) / 2
    assert axis_center_x == pytest.approx(0.5, abs=0.03)
    assert axis_center_y == pytest.approx(0.5, abs=0.03)
    overlay = result.outputs["overlay"]
    assert overlay.primitives[0].geometry["angle_degrees"] == 30


def test_http_response_output_can_map_result_decision() -> None:
    handler = HttpTriggerResponseOutputHandler("output.http_trigger_response")
    node = _node(
        "http_response",
        "output.http_trigger_response",
        config={"response_fields": [{"source_field": "decision", "response_key": "accepted"}]},
    )

    result = handler.execute(
        RuntimeExecutionContext(
            correlation_id="corr_response_decision",
            pipeline_version_id="plv_response_decision",
            channel_id="chn_response_decision",
            mode=RuntimeMode.TRIGGER,
        ),
        node,
        {"result": ResultPayload(result={"count": 1}, decision=True)},
    )

    response = result.outputs["response"]
    assert isinstance(response, ResultPayload)
    assert response.result == {"accepted": True}
    assert response.decision is True


def test_board_measurement_pipeline_computes_first_free_region_response_and_overlay(
    sample_png_bytes: bytes,
) -> None:
    graph = RuntimePipelineGraph(
        nodes=[
            _node("channel_input", "source.channel_frame"),
            _inference_node("detect_features", "inference.detection"),
            _node("reference_edges", "analysis.reference_edge_from_features"),
            _node("coordinate_frame", "analysis.coordinate_frame_from_edges"),
            _node(
                "measure_features",
                "analysis.feature_measurement",
                config={
                    "measurement_rules": [
                        {
                            "feature_type": "knot",
                            "bases": [
                                "bbox_width",
                                "bbox_height",
                                "approximate_diameter",
                                "axis_projected_width",
                                "axis_projected_length",
                            ],
                        }
                    ]
                },
            ),
            _node(
                "clearance_zones",
                "analysis.clearance_zone_generator",
                config={
                    "clearance_rules": [
                        {
                            "feature_type": "knot",
                            "enabled": True,
                            "measurement_basis": "bbox_width",
                            "multiplier": 1.0,
                            "minimum_clearance_px": 0,
                            "apply_before": True,
                            "apply_after": True,
                            "merge_overlapping_zones": True,
                            "severity": "warning",
                            "category": "defect_clearance",
                        }
                    ]
                },
            ),
            _node(
                "free_region",
                "analysis.first_free_region_finder",
                config={"start_offset_px": 0, "minimum_free_length_px": 50},
            ),
            _node("metric", "analysis.pixel_to_metric", config={"mm_per_pixel": 2.0}),
            _node(
                "quality",
                "analysis.quality_rule_evaluation",
                config={
                    "rules": [
                        {
                            "rule_type": "free_region_distance",
                            "operator": ">=",
                            "threshold": 150,
                            "status_on_fail": "fail",
                        }
                    ],
                    "result_label": "Board quality",
                },
            ),
            _node("overlay", "output.overlay"),
            _node("persist", "output.persisted_result"),
            _node(
                "http_response",
                "output.http_trigger_response",
                config={
                    "response_fields": [
                        {
                            "source_field": "distance_mm",
                            "response_key": "distance_mm",
                            "required": True,
                        }
                    ]
                },
            ),
        ],
        edges=[
            _edge("channel_input", "frame", "detect_features", "frame"),
            _edge("detect_features", "detections", "reference_edges", "features"),
            _edge("reference_edges", "reference_edges", "coordinate_frame", "reference_edges"),
            _edge("detect_features", "detections", "measure_features", "features"),
            _edge("coordinate_frame", "coordinate_frame", "measure_features", "coordinate_frame"),
            _edge("measure_features", "feature_measurements", "clearance_zones", "measurements"),
            _edge("coordinate_frame", "coordinate_frame", "clearance_zones", "coordinate_frame"),
            _edge("coordinate_frame", "coordinate_frame", "free_region", "coordinate_frame"),
            _edge("clearance_zones", "clearance_zones", "free_region", "clearance_zones"),
            _edge("reference_edges", "reference_edges", "metric", "reference_edges"),
            _edge("coordinate_frame", "coordinate_frame", "metric", "coordinate_frame"),
            _edge("measure_features", "feature_measurements", "metric", "measurements"),
            _edge("clearance_zones", "clearance_zones", "metric", "clearance_zones"),
            _edge("free_region", "free_region", "metric", "free_region"),
            _edge("metric", "feature_measurements", "quality", "measurements"),
            _edge("metric", "free_region", "quality", "free_region"),
            _edge("metric", "result", "overlay", "result"),
            _edge("metric", "result", "persist", "result"),
            _edge("metric", "result", "http_response", "result"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_board_measurement",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_board",
            pipeline_version_id="plv_board_measurement",
            channel_id="chn_board",
            mode=RuntimeMode.TRIGGER,
            input_frame=FramePayload.from_bytes(
                sample_png_bytes,
                mime_type="image/png",
                width=200,
                height=100,
            ),
            inference_client=_BoardFeatureInferenceClient(),
        )
    )

    assert result.status == "completed"
    assert result.errors == []
    reference_edges = result.reference_edges[0]["payload"]["edges"]
    assert {edge["role"] for edge in reference_edges} == {"start_edge", "side_edge"}
    assert reference_edges[0]["source"]["line_extraction_mode"] == "from_bbox_edge"
    frame = result.coordinate_frames[0]["payload"]["frame"]
    assert frame["origin"] == {"x": 10.0, "y": 10.0}
    measurements = result.feature_measurements[0]["payload"]["measurements"]
    knot_width = next(
        item
        for item in measurements
        if item["feature_type"] == "knot" and item["basis"] == "bbox_width"
    )
    assert knot_width["value_px"] == 20
    assert knot_width["value_mm"] is None
    axis_length = next(
        item
        for item in measurements
        if item["feature_type"] == "knot" and item["basis"] == "axis_projected_length"
    )
    assert axis_length["value_px"] == 20
    zone = result.clearance_zones[0]["payload"]["zones"][0]
    assert zone["interval_start_px"] == 40
    assert zone["interval_end_px"] == 100
    free_region = result.free_regions[0]["payload"]["free_region"]
    assert free_region["found"] is True
    assert free_region["distance_from_origin_px"] == 100
    metric_measurements = result.node_outputs["metric"]["feature_measurements"]["measurements"]
    metric_knot_width = next(
        item
        for item in metric_measurements
        if item["feature_type"] == "knot" and item["basis"] == "bbox_width"
    )
    assert metric_knot_width["value_mm"] == 40
    metric_zone = result.node_outputs["metric"]["clearance_zones"]["zones"][0]
    assert metric_zone["clearance_mm"] == 40
    assert metric_zone["interval_start_mm"] == 80
    assert metric_zone["interval_end_mm"] == 200
    metric_result = result.node_outputs["metric"]["result"]["result"]
    assert metric_result["distance_mm"] == 200
    decision = result.quality_decisions[0]["payload"]["decision"]
    assert decision["status"] == "pass"
    assert decision["selected_output_values"]["free_region_distance_mm"] == 200
    assert result.final_response == {"distance_mm": 200}
    assert result.persisted_results[0]["payload"]["result"]["distance_mm"] == 200
    primitive_types = {primitive["primitive_type"] for primitive in result.overlay_primitives}
    assert {"line", "axis", "box", "polygon", "arrow", "measurement"}.issubset(primitive_types)


def test_canny_edge_detection_detects_synthetic_board_edges() -> None:
    graph = RuntimePipelineGraph(
        nodes=[
            _node("channel_input", "source.channel_frame"),
            _node("canny_edges", "computer_vision.canny_edge_detection"),
            _node("overlay", "output.overlay"),
        ],
        edges=[
            _edge("channel_input", "frame", "canny_edges", "frame"),
            _edge("canny_edges", "edges", "overlay", "result"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_canny",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_canny",
            pipeline_version_id="plv_canny",
            channel_id="chn_canny",
            mode=RuntimeMode.REALTIME,
            input_frame=FramePayload.from_bytes(
                _synthetic_board_png_bytes(),
                mime_type="image/png",
                width=200,
                height=100,
            ),
        )
    )

    assert result.status == "completed"
    cv_edges = result.cv_edges[0]["payload"]
    assert cv_edges["payload_type"] == "cv_edges"
    assert cv_edges["edge_pixel_count"] > 400
    assert cv_edges["edge_mask_frame"]["image_context"]["kind"] == "processed"
    assert result.overlay_primitives[0]["primitive_type"] == "mask"
    assert result.overlay_primitives[0]["layer"] == "cv_edge_mask"


def test_reference_edge_from_cv_edges_selects_expected_lines() -> None:
    graph = RuntimePipelineGraph(
        nodes=[
            _node("channel_input", "source.channel_frame"),
            _node("canny_edges", "computer_vision.canny_edge_detection"),
            _node("reference_edges", "computer_vision.reference_edge_from_cv_edges"),
            _node("coordinate_frame", "analysis.coordinate_frame_from_edges"),
            _node("overlay", "output.overlay"),
        ],
        edges=[
            _edge("channel_input", "frame", "canny_edges", "frame"),
            _edge("canny_edges", "edges", "reference_edges", "edges"),
            _edge("reference_edges", "reference_edges", "coordinate_frame", "reference_edges"),
            _edge("reference_edges", "reference_edges", "overlay", "result"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_cv_edges",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_cv_edges",
            pipeline_version_id="plv_cv_edges",
            channel_id="chn_cv_edges",
            mode=RuntimeMode.REALTIME,
            input_frame=FramePayload.from_bytes(
                _synthetic_board_png_bytes(),
                mime_type="image/png",
                width=200,
                height=100,
            ),
        )
    )

    assert result.status == "completed"
    reference_edges = result.reference_edges[0]["payload"]["edges"]
    assert {edge["role"] for edge in reference_edges} == {"start_edge", "side_edge"}
    assert result.reference_edges[0]["payload"]["metadata"]["candidate_count"] >= 2
    frame = result.coordinate_frames[0]["payload"]["frame"]
    assert frame["origin"]["x"] == pytest.approx(10, abs=5)
    assert frame["origin"]["y"] == pytest.approx(10, abs=5)
    assert {"cv_candidate_lines", "reference_edges"}.issubset(
        {primitive["layer"] for primitive in result.overlay_primitives}
    )


def test_multi_branch_cv_edges_and_feature_detection_measure_board_workflow() -> None:
    graph = RuntimePipelineGraph(
        nodes=[
            _node("channel_input", "source.channel_frame"),
            _node("canny_edges", "computer_vision.canny_edge_detection"),
            _node("cv_reference_edges", "computer_vision.reference_edge_from_cv_edges"),
            _node("coordinate_frame", "analysis.coordinate_frame_from_edges"),
            _inference_node("detect_features", "inference.detection"),
            _node(
                "measure_features",
                "analysis.feature_measurement",
                config={
                    "measurement_rules": [
                        {
                            "feature_type": "knot",
                            "bases": ["bbox_width", "bbox_height", "axis_projected_length"],
                        }
                    ]
                },
            ),
            _node(
                "clearance_zones",
                "analysis.clearance_zone_generator",
                config={
                    "clearance_rules": [
                        {
                            "feature_type": "knot",
                            "enabled": True,
                            "measurement_basis": "bbox_width",
                            "multiplier": 1.0,
                            "minimum_clearance_px": 0,
                            "apply_before": True,
                            "apply_after": True,
                            "merge_overlapping_zones": True,
                        }
                    ]
                },
            ),
            _node(
                "free_region",
                "analysis.first_free_region_finder",
                config={"minimum_free_length_px": 50},
            ),
            _node("metric", "analysis.pixel_to_metric", config={"mm_per_pixel": 2.0}),
            _node("overlay", "output.overlay"),
            _node("persist", "output.persisted_result"),
            _node(
                "http_response",
                "output.http_trigger_response",
                config={
                    "response_fields": [
                        {
                            "source_field": "distance_mm",
                            "response_key": "distance_mm",
                            "required": True,
                        }
                    ]
                },
            ),
        ],
        edges=[
            _edge("channel_input", "frame", "canny_edges", "frame"),
            _edge("canny_edges", "edges", "cv_reference_edges", "edges"),
            _edge("cv_reference_edges", "reference_edges", "coordinate_frame", "reference_edges"),
            _edge("channel_input", "frame", "detect_features", "frame"),
            _edge("detect_features", "detections", "measure_features", "features"),
            _edge("coordinate_frame", "coordinate_frame", "measure_features", "coordinate_frame"),
            _edge("measure_features", "feature_measurements", "clearance_zones", "measurements"),
            _edge("coordinate_frame", "coordinate_frame", "clearance_zones", "coordinate_frame"),
            _edge("coordinate_frame", "coordinate_frame", "free_region", "coordinate_frame"),
            _edge("clearance_zones", "clearance_zones", "free_region", "clearance_zones"),
            _edge("cv_reference_edges", "reference_edges", "metric", "reference_edges"),
            _edge("coordinate_frame", "coordinate_frame", "metric", "coordinate_frame"),
            _edge("measure_features", "feature_measurements", "metric", "measurements"),
            _edge("clearance_zones", "clearance_zones", "metric", "clearance_zones"),
            _edge("free_region", "free_region", "metric", "free_region"),
            _edge("metric", "result", "overlay", "result"),
            _edge("metric", "result", "persist", "result"),
            _edge("metric", "result", "http_response", "result"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_multi_branch_cv",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_multi_branch_cv",
            pipeline_version_id="plv_multi_branch_cv",
            channel_id="chn_multi_branch_cv",
            mode=RuntimeMode.TRIGGER,
            input_frame=FramePayload.from_bytes(
                _synthetic_board_png_bytes(),
                mime_type="image/png",
                width=200,
                height=100,
            ),
            inference_client=_BoardFeatureInferenceClient(),
        )
    )

    assert result.status == "completed"
    assert result.errors == []
    assert result.cv_edges[0]["node_key"] == "canny_edges"
    assert result.reference_edges[0]["node_key"] == "cv_reference_edges"
    assert result.coordinate_frames[0]["node_key"] == "coordinate_frame"
    assert result.feature_measurements[0]["node_key"] == "measure_features"
    assert result.free_regions[0]["payload"]["free_region"]["found"] is True
    assert result.final_response["distance_mm"] is not None
    assert (
        result.persisted_results[0]["payload"]["result"]["reference_edges"][0]["source"]["source"]
        == "classic_cv"
    )


def test_anomaly_inference_node_emits_score_result_and_heatmap_overlay(
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(sample_png_bytes)
    graph = RuntimePipelineGraph(
        nodes=[
            _node("file_source", "source.file_test", config={"path": str(image_path)}),
            _anomaly_node("inspect_surface"),
            _node(
                "evaluate",
                "analysis.rule_evaluation",
                config={"expression": "is_anomalous == true"},
            ),
            _node("persist", "output.persisted_result"),
            _node("overlay", "output.overlay"),
        ],
        edges=[
            _edge("file_source", "frame", "inspect_surface", "frame"),
            _edge("inspect_surface", "anomaly", "evaluate", "result"),
            _edge("evaluate", "result", "persist", "result"),
            _edge("inspect_surface", "anomaly", "overlay", "result"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_anomaly",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_anomaly",
            pipeline_version_id="plv_anomaly",
            channel_id="chn_anomaly",
            mode=RuntimeMode.REALTIME,
            inference_client=_FakeInferenceClient(),
        )
    )

    heatmap = result.overlay_primitives[0]
    assert result.status == "completed"
    assert result.raw_inference_outputs[0]["node_type"] == "inference.anomaly"
    assert result.normalized_inference_outputs[0]["payload"]["payload_type"] == "anomaly"
    assert result.final_response["is_anomalous"] is True
    assert result.final_response["anomaly_score"] == 0.73
    assert result.persisted_results[0]["payload"]["result"]["result"] is True
    assert heatmap["primitive_type"] == "heatmap"
    assert heatmap["layer"] == "anomaly_heatmap"
    assert heatmap["geometry"]["data"] == "iVBORw0KGgo="
    assert heatmap["image_context_id"]


def test_display_output_selects_explicit_live_view_context(
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(sample_png_bytes)
    graph = RuntimePipelineGraph(
        nodes=[
            _node("file_source", "source.file_test", config={"path": str(image_path)}),
            _node(
                "crop_frame", "preprocessing.crop", config={"x": 0, "y": 0, "width": 1, "height": 1}
            ),
            _node("display", "output.display_output"),
        ],
        edges=[
            _edge("file_source", "frame", "crop_frame", "frame"),
            _edge("crop_frame", "frame", "display", "frame"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_display",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_display",
            pipeline_version_id="plv_display",
            channel_id="chn_display",
            mode=RuntimeMode.REALTIME,
        )
    )

    display_payload = result.display_outputs[0]["payload"]
    assert display_payload["payload_type"] == "display_output"
    assert display_payload["display_context"]["kind"] == "crop"
    assert (
        display_payload["display_context_id"]
        == display_payload["frame"]["image_context"]["context_id"]
    )


def test_default_live_view_selection_uses_latest_single_visual_context(
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(sample_png_bytes)
    graph = RuntimePipelineGraph(
        nodes=[
            _node("file_source", "source.file_test", config={"path": str(image_path)}),
            _node("resize_frame", "preprocessing.resize", config={"width": 1, "height": 1}),
            _node("persist", "output.persisted_result"),
        ],
        edges=[
            _edge("file_source", "frame", "resize_frame", "frame"),
            _edge("resize_frame", "frame", "persist", "result"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_default_display",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_default_display",
            pipeline_version_id="plv_default_display",
            channel_id="chn_default_display",
            mode=RuntimeMode.REALTIME,
        )
    )

    selection = _display_selection_from_result(result)
    assert selection["selection"] == "default_primary_visual_context"
    assert selection["display_context"]["kind"] == "transformed"


def test_live_deferred_node_outputs_keep_cropped_default_display_frame() -> None:
    image = Image.new("RGB", (4, 3), "blue")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    graph = RuntimePipelineGraph(
        nodes=[
            _node("channel_input", "source.channel_frame"),
            _node("crop", "preprocessing.crop", config={"x": 1, "y": 1, "width": 2, "height": 1}),
        ],
        edges=[
            _edge("channel_input", "frame", "crop", "frame"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_live_deferred_display_crop",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_live_deferred_display_crop",
            pipeline_version_id="plv_live_deferred_display_crop",
            channel_id="chn_live_deferred_display_crop",
            mode=RuntimeMode.REALTIME,
            input_frame=FramePayload.from_bytes(
                buffer.getvalue(),
                mime_type="image/png",
                width=4,
                height=3,
            ),
            metadata={
                "materialize_frame_node_outputs": False,
                "materialize_live_display_frame": True,
            },
        )
    )

    assert "image_base64" not in result.node_outputs["crop"]["frame"]
    assert result.node_outputs["crop"]["frame"]["metadata"]["image_base64_deferred"] is True
    display_payload = result.display_outputs[0]["payload"]
    display_frame = display_payload["frame"]
    selection = _display_selection_from_result(result)
    assert display_payload["display_context"]["kind"] == "crop"
    assert selection["display_context"]["kind"] == "crop"
    assert display_frame["image_context"]["kind"] == "crop"
    assert display_frame["width"] == 2
    assert display_frame["height"] == 1
    assert display_frame["image_base64"]


def test_roi_source_classification_emits_object_enrichment_and_label_overlay(
    sample_png_base64: str,
) -> None:
    roi_frame = FramePayload(
        image_base64=sample_png_base64,
        mime_type="image/png",
        image_context=RuntimeImageContext(
            context_id="ctx_roi_1",
            kind="roi",
            source_image_context_id="ctx_original_1",
            parent_image_context_id="ctx_original_1",
            source_object_id="obj_detection_1",
            transform=RuntimeImageTransform(
                transform_type="roi_crop",
                coordinate_projection="crop_offset",
            ),
        ),
    )
    graph = RuntimePipelineGraph(
        nodes=[
            _node("channel_input", "source.channel_frame"),
            _classification_node("classify_roi"),
            _node("overlay", "output.overlay"),
        ],
        edges=[
            _edge("channel_input", "frame", "classify_roi", "frame"),
            _edge("classify_roi", "classifications", "overlay", "result"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_roi_classify",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_roi_classify",
            pipeline_version_id="plv_roi_classify",
            channel_id="chn_roi_classify",
            mode=RuntimeMode.REALTIME,
            input_frame=roi_frame,
            inference_client=_FakeClassificationClient(),
        )
    )

    enrichment = result.enriched_object_annotations[0]["annotations"][0]
    primitive = result.overlay_primitives[0]
    assert enrichment["target_object_id"] == "obj_detection_1"
    assert enrichment["source_image_context_id"] == "ctx_original_1"
    assert primitive["label"] == "scratch"
    assert primitive["object_id"] == "obj_detection_1"


def test_realtime_crop_classification_uses_lazy_frame_and_binary_transport(
    sample_png_bytes: bytes,
) -> None:
    graph = RuntimePipelineGraph(
        nodes=[
            _node("channel_input", "source.channel_frame"),
            _node("crop", "preprocessing.crop", config={"x": 0, "y": 0, "width": 1, "height": 1}),
            _classification_node("classify_crop"),
        ],
        edges=[
            _edge("channel_input", "frame", "crop", "frame"),
            _edge("crop", "frame", "classify_crop", "frame"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_crop_classify_binary",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )
    client = _FakeBinaryClassificationClient()

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_crop_classify_binary",
            pipeline_version_id="plv_crop_classify_binary",
            channel_id="chn_crop_classify_binary",
            mode=RuntimeMode.REALTIME,
            input_frame=FramePayload.from_bytes(
                sample_png_bytes,
                mime_type="image/png",
                width=1,
                height=1,
            ),
            inference_client=client,
            metadata={"materialize_frame_node_outputs": False},
        )
    )

    assert result.status == "completed"
    assert client.used_binary_transport is True
    assert client.last_image_bytes
    assert "image_base64" not in result.node_outputs["crop"]["frame"]
    inference_timing = result.timing["inference_timings_ms"][0]
    assert inference_timing["node_key"] == "classify_crop"
    assert inference_timing["binary_transport"] == 1.0
    assert inference_timing["request_image_prepare_ms"] >= 0


def test_roi_capture_node_materializes_detection_derived_roi_intent(
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(sample_png_bytes)
    graph = RuntimePipelineGraph(
        nodes=[
            _node("file_source", "source.file_test", config={"path": str(image_path)}),
            _inference_node("detect_objects", "inference.detection"),
            _node(
                "roi_capture",
                "capture.roi_labelstudio",
                config={"target_project_ids": ["lpr_roi"]},
            ),
        ],
        edges=[
            _edge("file_source", "frame", "detect_objects", "frame"),
            _edge("file_source", "frame", "roi_capture", "frame"),
            _edge("detect_objects", "detections", "roi_capture", "detections"),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="plv_roi_intent",
        graph=graph,
        handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
    )

    result = executable.execute(
        RuntimeExecutionContext(
            correlation_id="corr_roi_intent",
            pipeline_version_id="plv_roi_intent",
            channel_id="chn_roi_intent",
            mode=RuntimeMode.REALTIME,
            inference_client=_FakeInferenceClient(),
        )
    )

    assert result.status == "completed"
    assert len(result.capture_intents) == 1
    intent = result.capture_intents[0]["intent"]
    assert intent["capture_mode"] == "roi"
    assert intent["target_project_ids"] == ["lpr_roi"]
    assert intent["asset"]["roi_key"] == "detection_1"
    assert intent["asset"]["frame"]["payload_type"] == "frame"
    assert intent["asset"]["source_object_id"].startswith("obj_")
    assert intent["asset"]["image_context"]["kind"] == "roi"


def test_pipeline_validation_rejects_bad_handlers_and_duplicate_inputs(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)
    with session_scope(settings) as session:
        _seed_runtime_capability(session)
        ensure_builtin_node_types(session)
        session.add(
            RuntimeNodeTypeRegistry(
                id=generate_prefixed_id("nty"),
                node_type="source.invalid_plugin",
                node_category=RuntimeNodeCategory.SOURCE.value,
                contract_version=1,
                display_name="Invalid source plugin",
                description="Broken test plugin",
                supported_modes=[RuntimeMode.REALTIME.value],
                input_ports=[],
                output_ports=[
                    RuntimeNodePort(key="frame", data_type="frame", required=True).model_dump(
                        mode="json"
                    )
                ],
                config_schema_ref="runtime.node.source.invalid_plugin.config.v1",
                property_schema=[],
                default_config={"path": "C:/tmp/sample.png"},
                runtime_handler_ref="missing.runtime.module:handler",
                compatibility_constraints={},
                active=True,
            )
        )
        session.flush()

        report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                graph=RuntimePipelineGraph(
                    nodes=[
                        RuntimeNodeSpec(
                            node_key="file_source",
                            node_type="source.invalid_plugin",
                            node_category=RuntimeNodeCategory.SOURCE,
                            output_ports=[
                                RuntimeNodePort(key="frame", data_type="frame", required=True)
                            ],
                            config={"path": "C:/tmp/sample.png"},
                        ),
                        _node("resize_frame", "preprocessing.resize"),
                        _inference_node("detect_objects", "inference.detection"),
                        _node("persist", "output.persisted_result"),
                    ],
                    edges=[
                        _edge("file_source", "frame", "resize_frame", "frame"),
                        _edge("resize_frame", "frame", "detect_objects", "frame"),
                        _edge("file_source", "frame", "detect_objects", "frame"),
                        _edge("detect_objects", "detections", "persist", "result"),
                    ],
                ),
            ),
        )
        trigger_report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.TRIGGER,
                graph=RuntimePipelineGraph(
                    nodes=[
                        _node("http_trigger", "trigger.http_put"),
                        _node(
                            "file_source", "source.file_test", config={"path": "C:/tmp/sample.png"}
                        ),
                        _inference_node("detect_objects", "inference.detection"),
                        _node("http_response", "output.http_trigger_response"),
                    ],
                    edges=[
                        _edge("http_trigger", "trigger", "file_source", "trigger"),
                        _edge("file_source", "frame", "detect_objects", "frame"),
                        _edge("detect_objects", "detections", "http_response", "result"),
                    ],
                ),
            ),
        )

    codes = {issue.code for issue in report.issues}
    assert report.status == "invalid"
    assert "handler_unavailable" in codes
    assert "input_port_multiple_sources" in codes
    assert trigger_report.status == "invalid"
    assert {issue.code for issue in trigger_report.issues} >= {"channel_frame_source_required"}


def test_trigger_executor_runs_published_pipeline_and_persists_results(
    sqlite_engine,
    tmp_path: Path,
    sample_png_base64: str,
    monkeypatch,
) -> None:
    configure_sqlite_factories(
        sqlite_engine,
        "admin-api",
        "trigger-executor",
        "runtime-orchestrator",
        "result-service",
    )
    settings = build_test_settings(
        "trigger-executor",
        tmp_path,
        api_keys=[
            (
                "trigger-client",
                "trigger-secret",
                ["runtime.trigger.invoke"],
                "inference_client",
                "plc-smoke",
            )
        ],
    )
    admin_settings = build_test_settings("admin-api", tmp_path)
    result_settings = build_test_settings("result-service", tmp_path)
    admin_app.state.settings = admin_settings
    trigger_app.state.settings = settings
    result_app.state.settings = result_settings

    with session_scope(settings) as session:
        _seed_runtime_capability(session)
        channel = _create_snapshot_channel(session)
        version = _create_published_pipeline(
            session,
            name="Trigger defect response",
            mode=RuntimeMode.TRIGGER,
            graph=RuntimePipelineGraph(
                nodes=[
                    _node(
                        "http_trigger",
                        "trigger.http_put",
                        config={"auth_required": True, "timeout_ms": 2500},
                    ),
                    _node("channel_input", "source.channel_frame"),
                    _inference_node("detect_objects", "inference.detection"),
                    _node(
                        "evaluate", "analysis.rule_evaluation", config={"expression": "count >= 1"}
                    ),
                    _node(
                        "capture_frame",
                        "capture.full_frame_labelstudio",
                        config={"target_project_ids": ["lpr_123"], "rate_limit_per_minute": 30},
                    ),
                    _node(
                        "http_response",
                        "output.http_trigger_response",
                        config={
                            "response_fields": [
                                {
                                    "source_field": "count",
                                    "response_key": "piece_count",
                                    "required": True,
                                }
                            ]
                        },
                    ),
                    _node("overlay", "output.overlay"),
                    _node("display", "output.display_output"),
                ],
                edges=[
                    _edge("http_trigger", "trigger", "channel_input", "trigger"),
                    _edge("channel_input", "frame", "detect_objects", "frame"),
                    _edge("detect_objects", "detections", "evaluate", "result"),
                    _edge("evaluate", "result", "http_response", "result"),
                    _edge("channel_input", "frame", "capture_frame", "frame"),
                    _edge("detect_objects", "detections", "overlay", "result"),
                    _edge("channel_input", "frame", "display", "frame"),
                    _edge("detect_objects", "detections", "display", "objects"),
                ],
            ),
            channel_id=channel.channel_id,
        )
        endpoint_row = session.query(TriggerEndpoint).one()
        endpoint_slug = endpoint_row.slug

    monkeypatch.setattr(
        "trigger_executor.api.routes.HttpInferenceGatewayClient",
        lambda **_kwargs: _FakeInferenceClient(),
    )

    with TestClient(trigger_app) as client:
        response = client.put(
            f"/api/v1/runtime/triggers/{endpoint_slug}",
            json={
                "trigger_id": "trigger_001",
                "image_base64": sample_png_base64,
                "mime_type": "image/png",
            },
            headers={"Authorization": "Bearer trigger-client.trigger-secret"},
        )
        unsafe_response = client.put(
            f"/api/v1/runtime/triggers/{endpoint_slug}",
            json={
                "trigger_id": "trigger_unsafe",
                "image_base64": sample_png_base64,
                "mime_type": "image/png",
                "payload": {"snapshot": {"path": "C:/unsafe/frame.png"}},
            },
            headers={"Authorization": "Bearer trigger-client.trigger-secret"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["final_response"] == {"piece_count": 1}
    assert payload["capture_intents"][0]["intent"]["capture_mode"] == "full_frame"
    assert unsafe_response.status_code == 422
    assert unsafe_response.json()["code"] == "runtime_trigger_payload_unsafe"

    with session_scope(settings) as session:
        record = session.query(ResultRecord).one()
        endpoint = session.query(TriggerEndpoint).one()
        live_state = get_runtime_live_view_state(session, settings, channel_id=channel.channel_id)

    assert record.status == "completed"
    assert record.trigger_endpoint_id == endpoint.id
    assert record.mode == "trigger"
    assert record.pipeline_version_id == version
    assert record.history_retained is False
    assert record.raw_inference_outputs == []
    assert record.normalized_inference_outputs == []
    assert record.derived_outputs == []
    assert record.trigger_response == {"piece_count": 1}
    assert record.model_routes[0]["deployment_release_id"] == "rel_detection"
    assert endpoint.last_status == "completed"
    assert endpoint.invocation_count == 1
    assert live_state.available is True
    assert live_state.frame and live_state.frame.image_base64
    assert live_state.overlay

    with TestClient(admin_app) as admin_client, TestClient(result_app) as result_client:
        _login(admin_client)
        _copy_auth_cookies(admin_client, result_client, admin_settings)
        list_response = result_client.get(
            "/api/v1/runtime/results",
            params={
                "channel_id": channel.channel_id,
                "mode": "trigger",
                "pipeline_version_id": version,
            },
        )
        detail_response = result_client.get(f"/api/v1/runtime/results/{record.id}")

    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["total"] == 0
    assert list_payload["items"] == []
    assert detail_response.status_code == 404


def test_trigger_executor_acquires_managed_channel_test_file(
    sqlite_engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
    monkeypatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "trigger-executor")
    settings = build_test_settings(
        "trigger-executor",
        tmp_path,
        api_keys=[
            (
                "trigger-client",
                "trigger-secret",
                ["runtime.trigger.invoke"],
                "inference_client",
                "plc-smoke",
            )
        ],
    )
    trigger_app.state.settings = settings
    test_file_root = settings.storage.runtime_test_files_root()
    test_file_root.mkdir(parents=True, exist_ok=True)
    test_file_path = test_file_root / "press-sample.png"
    test_file_path.write_bytes(sample_png_bytes)

    with session_scope(settings) as session:
        _seed_runtime_capability(session)
        channel = create_runtime_channel(
            session,
            RuntimeChannelCreateRequest(
                name="Test file camera",
                technical_place="pipeline-lab",
                source_type=RuntimeSourceType.FILE_TEST,
                source_config={
                    "path": str(test_file_path),
                    "file_name": "press-sample.png",
                    "kind": "image",
                    "managed_test_file": True,
                    "managed_upload_id": "tfl_test",
                },
                mode=RuntimeMode.REALTIME,
            ),
            created_by="tester",
        )
        _create_published_pipeline(
            session,
            name="Test file trigger",
            mode=RuntimeMode.TRIGGER,
            graph=_trigger_pipeline_graph(channel.channel_id),
            channel_id=channel.channel_id,
        )
        endpoint_slug = session.query(TriggerEndpoint).one().slug

    monkeypatch.setattr(
        "trigger_executor.api.routes.HttpInferenceGatewayClient",
        lambda **_kwargs: _FakeInferenceClient(),
    )

    with TestClient(trigger_app) as client:
        response = client.put(
            f"/api/v1/runtime/triggers/{endpoint_slug}",
            json={"trigger_id": "trigger_test_file"},
            headers=_trigger_auth_headers(),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["final_response"]
    with session_scope(settings) as session:
        live_state = get_runtime_live_view_state(session, settings, channel_id=channel.channel_id)
    assert live_state.available is True
    assert live_state.frame is not None
    assert live_state.frame.metadata["source_uri"].endswith("press-sample.png")


def test_trigger_live_view_uses_source_frame_when_color_segmentation_emits_mask_without_overlay_output(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "trigger-executor")
    settings = build_test_settings("trigger-executor", tmp_path)
    trigger_app.state.settings = settings

    image = Image.new("RGB", (120, 80), (24, 24, 24))
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 20, 96, 56), fill=(235, 40, 40))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    source_image_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")

    graph = RuntimePipelineGraph(
        nodes=[
            _node("http_trigger", "trigger.http_put", config={"timeout_ms": 10000}),
            _node("channel_input", "source.channel_frame"),
            _node(
                "color_objects",
                "computer_vision.color_object_segmentation",
                config={
                    "profiles": [
                        {
                            "label": "red_part",
                            "enabled": True,
                            "color_space": "hsv",
                            "hue_min": 0,
                            "hue_max": 10,
                            "saturation_min": 80,
                            "saturation_max": 255,
                            "value_min": 80,
                            "value_max": 255,
                            "min_area_percent": 0.1,
                            "display_color": "#eb2828",
                        }
                    ],
                    "blur_kernel_size": 0,
                    "morphology_kernel_size": 0,
                    "morphology_iterations": 0,
                    "split_touching_objects": False,
                    "overlay_output_mode": "mask",
                },
            ),
            _node("http_response", "output.http_trigger_response"),
        ],
        edges=[
            _edge("http_trigger", "trigger", "channel_input", "trigger"),
            _edge("channel_input", "frame", "color_objects", "frame"),
            _edge("color_objects", "counts", "http_response", "result"),
        ],
    )

    with session_scope(settings) as session:
        _seed_runtime_capability(session)
        channel = _create_snapshot_channel(session)
        _create_published_pipeline(
            session,
            name="Color trigger response without overlay output",
            mode=RuntimeMode.TRIGGER,
            graph=graph,
            channel_id=channel.channel_id,
        )
        endpoint_slug = session.query(TriggerEndpoint).one().slug

    with TestClient(trigger_app) as client:
        response = client.put(
            f"/api/v1/runtime/triggers/{endpoint_slug}",
            json={
                "trigger_id": "trigger_color_mask_no_overlay_output",
                "image_base64": source_image_base64,
                "mime_type": "image/png",
            },
        )

    assert response.status_code == 200
    with session_scope(settings) as session:
        live_state = get_runtime_live_view_state(session, settings, channel_id=channel.channel_id)

    assert live_state.available is True
    assert live_state.frame is not None
    assert live_state.frame.image_base64 == source_image_base64
    assert live_state.frame.image_context
    image_context = dict(live_state.frame.image_context)
    assert image_context["kind"] == "original"
    assert image_context["transform"]["transform_type"] == "identity"


def test_trigger_executor_allows_unauthenticated_default_trigger(
    sqlite_engine,
    tmp_path: Path,
    sample_png_base64: str,
    monkeypatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "trigger-executor")
    settings = build_test_settings("trigger-executor", tmp_path)
    trigger_app.state.settings = settings

    with session_scope(settings) as session:
        _seed_runtime_capability(session)
        channel = _create_snapshot_channel(session)
        _create_published_pipeline(
            session,
            name="Open trigger response",
            mode=RuntimeMode.TRIGGER,
            graph=_trigger_pipeline_graph(channel.channel_id),
            channel_id=channel.channel_id,
        )
        endpoint_row = session.query(TriggerEndpoint).one()
        endpoint_slug = endpoint_row.slug
        assert endpoint_row.auth_required is False

    monkeypatch.setattr(
        "trigger_executor.api.routes.HttpInferenceGatewayClient",
        lambda **_kwargs: _FakeInferenceClient(),
    )

    with TestClient(trigger_app) as client:
        response = client.put(
            f"/api/v1/runtime/triggers/{endpoint_slug}",
            json={
                "trigger_id": "trigger_open",
                "image_base64": sample_png_base64,
                "mime_type": "image/png",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"


def test_trigger_executor_returns_structured_timeout(
    sqlite_engine,
    tmp_path: Path,
    sample_png_base64: str,
    monkeypatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "trigger-executor")
    settings = build_test_settings(
        "trigger-executor",
        tmp_path,
        api_keys=[
            (
                "trigger-client",
                "trigger-secret",
                ["runtime.trigger.invoke"],
                "inference_client",
                "plc-smoke",
            )
        ],
    )
    trigger_app.state.settings = settings

    with session_scope(settings) as session:
        _seed_runtime_capability(session)
        channel = _create_snapshot_channel(session)
        version = _create_published_pipeline(
            session,
            name="Slow trigger response",
            mode=RuntimeMode.TRIGGER,
            graph=_trigger_pipeline_graph(channel.channel_id, timeout_ms=50),
            channel_id=channel.channel_id,
        )
        endpoint_row = session.query(TriggerEndpoint).one()
        endpoint_slug = endpoint_row.slug

    monkeypatch.setattr(
        "trigger_executor.api.routes.HttpInferenceGatewayClient",
        lambda **_kwargs: _SlowInferenceClient(),
    )

    with TestClient(trigger_app) as client:
        response = client.put(
            f"/api/v1/runtime/triggers/{endpoint_slug}",
            json={
                "trigger_id": "trigger_timeout",
                "image_base64": sample_png_base64,
                "mime_type": "image/png",
            },
            headers=_trigger_auth_headers(),
        )

    assert response.status_code == 504
    payload = response.json()
    assert payload["status"] == "timeout"
    assert payload["error_code"] == "runtime_trigger_timeout"
    assert payload["retryable"] is True
    assert payload["correlation_id"]

    with session_scope(settings) as session:
        record = session.query(ResultRecord).one()
        endpoint = session.query(TriggerEndpoint).one()
    assert record.status == "timeout"
    assert endpoint.last_status == "timeout"


def test_trigger_executor_captures_live_channel_source_on_demand(
    sqlite_engine,
    tmp_path: Path,
    monkeypatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "trigger-executor")
    settings = build_test_settings(
        "trigger-executor",
        tmp_path,
        api_keys=[
            (
                "trigger-client",
                "trigger-secret",
                ["runtime.trigger.invoke"],
                "inference_client",
                "plc-smoke",
            )
        ],
    )
    trigger_app.state.settings = settings
    built_sources: list[dict[str, object]] = []

    with session_scope(settings) as session:
        _seed_runtime_capability(session)
        channel = _create_rtsp_channel(session)
        _create_published_pipeline(
            session,
            name="Live source trigger response",
            mode=RuntimeMode.TRIGGER,
            graph=_trigger_pipeline_graph(channel.channel_id),
            channel_id=channel.channel_id,
        )
        endpoint = session.query(TriggerEndpoint).one()

    class _FakeLiveFramePacket:
        image_bytes = _synthetic_board_png_bytes()
        source_uri = "rtsp://camera.local/reject"
        source_timestamp = "2026-06-05T08:00:00Z"
        sequence = 7
        metadata = {"source_type": "rtsp"}

        def ensure_encoded(self):
            return self

    class _FakeLiveFrameSource:
        health = SimpleNamespace(
            connected=True,
            status_text="RTSP source connected",
            last_error=None,
        )

        def read(self, _stop_event):
            return _FakeLiveFramePacket()

        def close(self):
            return None

    def _fake_build_frame_source(*, source_type, config, secret_refs):
        built_sources.append(
            {"source_type": source_type, "config": dict(config), "secret_refs": dict(secret_refs)}
        )
        return _FakeLiveFrameSource()

    monkeypatch.setattr(
        "trigger_executor.api.routes.HttpInferenceGatewayClient",
        lambda **_kwargs: _FakeInferenceClient(),
    )
    monkeypatch.setattr(
        "trigger_executor.api.routes.build_frame_source",
        _fake_build_frame_source,
    )

    with TestClient(trigger_app) as client:
        response = client.put(
            f"/api/v1/runtime/triggers/{endpoint.slug}",
            json={"trigger_id": "trigger_live_source"},
            headers=_trigger_auth_headers(),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert built_sources == [
        {
            "source_type": "rtsp",
            "config": {
                "url": "rtsp://camera.local/reject",
                "read_timeout_seconds": 2.5,
                "fetch_timeout_seconds": 2.5,
                "reconnect_seconds": 0.05,
            },
            "secret_refs": {},
        }
    ]
    with session_scope(settings) as session:
        live_state = get_runtime_live_view_state(session, settings, channel_id=channel.channel_id)
    assert live_state.available is True
    assert live_state.frame is not None
    assert live_state.frame.metadata["source_uri"] == "rtsp://camera.local/reject"


def test_trigger_executor_captures_basler_channel_source_on_demand(
    sqlite_engine,
    tmp_path: Path,
    monkeypatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "trigger-executor")
    settings = build_test_settings(
        "trigger-executor",
        tmp_path,
        api_keys=[
            (
                "trigger-client",
                "trigger-secret",
                ["runtime.trigger.invoke"],
                "inference_client",
                "plc-smoke",
            )
        ],
    )
    trigger_app.state.settings = settings
    built_sources: list[dict[str, object]] = []

    with session_scope(settings) as session:
        _seed_runtime_capability(session)
        channel = _create_basler_channel(session)
        _create_published_pipeline(
            session,
            name="Basler source trigger response",
            mode=RuntimeMode.TRIGGER,
            graph=_trigger_pipeline_graph(channel.channel_id),
            channel_id=channel.channel_id,
        )
        endpoint = session.query(TriggerEndpoint).one()

    class _FakeBaslerFramePacket:
        image_bytes = _synthetic_board_png_bytes()
        source_uri = "basler-pylon://Press Basler"
        source_timestamp = "2026-06-17T08:00:00Z"
        sequence = 3
        metadata = {
            "source_type": "basler_pylon",
            "transport": "pylon",
            "producer": "basler_pylon",
        }

        def ensure_encoded(self):
            return self

    class _FakeBaslerFrameSource:
        health = SimpleNamespace(
            connected=True,
            status_text="Basler Pylon source connected",
            last_error=None,
        )

        def read(self, _stop_event):
            return _FakeBaslerFramePacket()

        def close(self):
            return None

    def _fake_build_frame_source(*, source_type, config, secret_refs):
        built_sources.append(
            {"source_type": source_type, "config": dict(config), "secret_refs": dict(secret_refs)}
        )
        return _FakeBaslerFrameSource()

    monkeypatch.setattr(
        "trigger_executor.api.routes.HttpInferenceGatewayClient",
        lambda **_kwargs: _FakeInferenceClient(),
    )
    monkeypatch.setattr(
        "trigger_executor.api.routes.build_frame_source",
        _fake_build_frame_source,
    )

    with TestClient(trigger_app) as client:
        response = client.put(
            f"/api/v1/runtime/triggers/{endpoint.slug}",
            json={"trigger_id": "trigger_basler_source"},
            headers=_trigger_auth_headers(),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert len(built_sources) == 1
    built = built_sources[0]
    assert built["source_type"] == "basler_pylon"
    assert built["secret_refs"] == {}
    config = built["config"]
    assert config["serial_number"] == "40123456"
    assert config["trigger_mode"] == "On"
    assert config["trigger_source"] == "Software"
    assert config["grab_strategy"] == "OneByOne"
    assert config["fetch_timeout_seconds"] == 2.5
    assert config["reconnect_seconds"] == 0.05
    with session_scope(settings) as session:
        live_state = get_runtime_live_view_state(session, settings, channel_id=channel.channel_id)
    assert live_state.available is True
    assert live_state.frame is not None
    assert live_state.frame.metadata["source_uri"] == "basler-pylon://Press Basler"
    assert live_state.frame.metadata["source_type"] == "basler_pylon"


def test_trigger_executor_rejects_invalid_pipeline_endpoint_config(
    sqlite_engine,
    tmp_path: Path,
    sample_png_base64: str,
) -> None:
    configure_sqlite_factories(sqlite_engine, "trigger-executor")
    settings = build_test_settings(
        "trigger-executor",
        tmp_path,
        api_keys=[
            (
                "trigger-client",
                "trigger-secret",
                ["runtime.trigger.invoke"],
                "inference_client",
                "plc-smoke",
            )
        ],
    )
    trigger_app.state.settings = settings

    with session_scope(settings) as session:
        _seed_runtime_capability(session)
        channel = _create_snapshot_channel(session)
        trigger_version = _create_published_pipeline(
            session,
            name="Trigger response",
            mode=RuntimeMode.TRIGGER,
            graph=_trigger_pipeline_graph(channel.channel_id),
            channel_id=channel.channel_id,
        )
        invalid_version = _create_published_pipeline(
            session,
            name="Realtime response",
            mode=RuntimeMode.REALTIME,
            graph=RuntimePipelineGraph(
                nodes=[
                    _node("channel_input", "source.channel_frame"),
                    _inference_node("detect_objects", "inference.detection"),
                    _node("persist", "output.persisted_result"),
                ],
                edges=[
                    _edge("channel_input", "frame", "detect_objects", "frame"),
                    _edge("detect_objects", "detections", "persist", "result"),
                ],
            ),
        )
        endpoint_row = session.query(TriggerEndpoint).one()
        endpoint_row.pipeline_version_id = invalid_version
        endpoint_slug = endpoint_row.slug

    with TestClient(trigger_app) as client:
        response = client.put(
            f"/api/v1/runtime/triggers/{endpoint_slug}",
            json={
                "trigger_id": "trigger_invalid_config",
                "image_base64": sample_png_base64,
                "mime_type": "image/png",
            },
            headers=_trigger_auth_headers(),
        )

    assert response.status_code == 503
    assert response.json()["code"] == "runtime_trigger_runtime_unavailable"
    assert "triggerable" in response.json()["detail"]
    assert response.json()["retryable"] is True


def test_executor_apis_reject_server_local_image_paths(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "trigger-executor", "realtime-executor")
    trigger_settings = build_test_settings(
        "trigger-executor",
        tmp_path,
        api_keys=[
            (
                "trigger-client",
                "trigger-secret",
                ["runtime.trigger.invoke"],
                "inference_client",
                "plc-smoke",
            )
        ],
    )
    realtime_settings = build_test_settings(
        "realtime-executor",
        tmp_path,
        api_keys=[
            (
                "runtime-service",
                "runtime-secret",
                ["runtime.executor.invoke"],
                "service",
                "runtime-orchestrator",
            )
        ],
    )
    trigger_app.state.settings = trigger_settings
    realtime_app.state.settings = realtime_settings

    with TestClient(trigger_app) as client:
        trigger_response = client.put(
            "/api/v1/runtime/triggers/chn_test",
            json={"trigger_id": "trigger_001", "image_path": "C:/unsafe/frame.png"},
            headers={"Authorization": "Bearer trigger-client.trigger-secret"},
        )
    with TestClient(realtime_app) as client:
        realtime_response = client.post(
            "/api/v1/runtime/channels/chn_test:execute-frame",
            json={"trigger_id": "frame_001", "image_path": "C:/unsafe/frame.png"},
            headers={"Authorization": "Bearer runtime-service.runtime-secret"},
        )

    assert trigger_response.status_code == 422
    assert trigger_response.json()["code"] == "invalid_request"
    assert realtime_response.status_code == 422
    assert realtime_response.json()["code"] == "invalid_request"


def test_runtime_model_capabilities_endpoint_returns_picker_metadata(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings

    with session_scope(runtime_settings) as session:
        seed_deployed_runtime_model(session)

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)
        response = runtime_client.get(
            "/api/v1/runtime/model-capabilities",
            params={"task_type": "detection", "target_path": "realtime"},
            headers={"X-CSRF-Token": csrf_token},
        )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["display_name"] == "Defect Detector"
    assert item["task_type"] == "detection"
    assert item["architecture_family"] == "yolo"
    assert item["architecture_version"] == "v11"
    assert item["architecture_size"] == "m"
    assert item["deployment_status"] == "production"


def test_runtime_model_capabilities_are_projected_from_deployed_releases(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(
            session,
            release_id="rel_defect_detector_rt",
            model_family_id="mf_defect_detector",
            model_version_id="mver_defect_detector_1",
            serving_manifest={
                "class_labels": ["ok", "defect"],
                "confidence_threshold_default": 0.4,
            },
        )

        options = list_runtime_model_capability_options(
            session,
            task_type=TaskType.DETECTION,
            target_path=TargetPath.REALTIME,
        )
        binding = build_runtime_model_resolver(session)(
            RuntimeLogicalModelRef(
                model_family_slug="defect-detector",
                target_path=TargetPath.REALTIME,
                contract_major=1,
                selection_policy="active_production",
                required_task_type=TaskType.DETECTION,
            )
        )

    assert len(options) == 1
    assert options[0].deployment_release_id == "rel_defect_detector_rt"
    assert options[0].deployment_status == "production"
    assert options[0].architecture_family == "yolo"
    assert options[0].capability["source_of_truth"] == "deployment_releases"
    assert binding.deployment_release_id == "rel_defect_detector_rt"
    assert binding.capability["class_labels"] == ["ok", "defect"]


def test_runtime_model_resolver_prefers_latest_active_release_over_previous_production(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(
            session,
            release_id="rel_defect_detector_v2",
            model_family_id="mf_defect_detector",
            model_family_slug="defect-detector",
            model_version_id="mver_defect_detector_2",
            version_no=2,
            active_release=True,
        )
        session.add(
            ModelPromotion(
                id="mpr_rel_defect_detector_v2",
                model_version_id="mver_defect_detector_2",
                target_path=TargetPath.REALTIME.value,
                status=PromotionStatus.PRODUCTION.value,
                deployment_release_id="rel_defect_detector_v2",
            )
        )
        seed_deployed_runtime_model(
            session,
            release_id="rel_defect_detector_v3",
            model_family_id="mf_defect_detector",
            model_family_slug="defect-detector",
            model_version_id="mver_defect_detector_3",
            version_no=3,
            active_release=True,
        )

        binding = build_runtime_model_resolver(session)(
            RuntimeLogicalModelRef(
                model_family_slug="defect-detector",
                target_path=TargetPath.REALTIME,
                contract_major=1,
                selection_policy="active_production",
                required_task_type=TaskType.DETECTION,
            )
        )

    assert binding.deployment_release_id == "rel_defect_detector_v3"
    assert binding.capability["triton_model_version"] == 3


def test_runtime_model_resolver_reports_staged_policy_mismatch(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(
            session,
            release_id="rel_staged_detector",
            model_family_id="mf_defect_detector",
            model_family_slug="defect-detector",
            model_version_id="mver_defect_detector_1",
            active_release=False,
            staged=True,
        )

        with pytest.raises(ValueError) as exc_info:
            build_runtime_model_resolver(session)(
                RuntimeLogicalModelRef(
                    model_family_slug="defect-detector",
                    target_path=TargetPath.REALTIME,
                    contract_major=1,
                    selection_policy="active_production",
                    required_task_type=TaskType.DETECTION,
                )
            )

    detail = str(exc_info.value)
    assert "Candidates on requested target" in detail
    assert "deployment_status=staged" in detail
    assert "release_status=activated" in detail


def test_runtime_model_resolver_reports_target_path_mismatch(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(
            session,
            release_id="rel_batch_detector",
            model_family_id="mf_defect_detector",
            model_family_slug="defect-detector",
            model_version_id="mver_defect_detector_1",
            target_path=TargetPath.BATCH,
        )

        with pytest.raises(ValueError) as exc_info:
            build_runtime_model_resolver(session)(
                RuntimeLogicalModelRef(
                    model_family_slug="defect-detector",
                    target_path=TargetPath.REALTIME,
                    contract_major=1,
                    selection_policy="active_production",
                    required_task_type=TaskType.DETECTION,
                )
            )

    detail = str(exc_info.value)
    assert "Candidates on other targets" in detail
    assert "target=batch" in detail
    assert "rel_batch_detector" in detail


def test_runtime_model_capabilities_keep_previous_production_release_bindable(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(
            session,
            release_id="rel_previous_production",
            model_family_id="mf_defect_detector",
            model_family_slug="defect-detector",
            model_version_id="mver_defect_detector_1",
            active_release=False,
            staged=True,
        )
        previous_promotion = session.get(ModelPromotion, "mpr_rel_previous_production")
        assert previous_promotion is not None
        previous_promotion.rollback_candidate = True

        seed_deployed_runtime_model(
            session,
            release_id="rel_other_active",
            model_family_id="mf_other_detector",
            model_family_slug="other-detector",
            model_family_display_name="Other Detector",
            model_version_id="mver_other_detector_1",
            active_release=True,
        )

        options = list_runtime_model_capability_options(
            session,
            task_type=TaskType.DETECTION,
            target_path=TargetPath.REALTIME,
        )
        binding = build_runtime_model_resolver(session)(
            RuntimeLogicalModelRef(
                model_family_slug="defect-detector",
                target_path=TargetPath.REALTIME,
                contract_major=1,
                selection_policy="active_production",
                required_task_type=TaskType.DETECTION,
            )
        )

    previous_option = next(
        item for item in options if item.deployment_release_id == "rel_previous_production"
    )
    assert previous_option.deployment_status == "production"
    assert previous_option.active is True
    assert binding.deployment_release_id == "rel_previous_production"


def test_runtime_model_capabilities_ignore_legacy_cache_without_deployment_truth(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        session.add(
            RuntimeModelCapabilityCache(
                id=generate_prefixed_id("rmc"),
                model_family_slug="defect-detector",
                target_path=TargetPath.REALTIME.value,
                contract_major=1,
                deployment_release_id="rel_legacy_hidden_source",
                model_version_id="mvr_legacy_hidden_source",
                task_type=TaskType.DETECTION.value,
                capability={
                    "display_name": "Legacy Hidden Source",
                    "deployment_status": "production",
                    "active": True,
                },
                refreshed_at=datetime.now(tz=UTC),
                expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
            )
        )
        options = list_runtime_model_capability_options(
            session,
            task_type=TaskType.DETECTION,
            target_path=TargetPath.REALTIME,
        )

    assert options == []


def _node(
    node_key: str,
    node_type: str,
    *,
    config: dict[str, object] | None = None,
) -> RuntimeNodeSpec:
    spec = {item.node_type: item for item in BUILTIN_NODE_TYPES}[node_type]
    return RuntimeNodeSpec(
        node_key=node_key,
        node_type=node_type,
        node_category=spec.node_category,
        input_ports=spec.input_ports,
        output_ports=spec.output_ports,
        config={**spec.default_config, **(config or {})},
    )


def _trigger_auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer trigger-client.trigger-secret"}


def _inference_node(node_key: str, node_type: str) -> RuntimeNodeSpec:
    spec = {item.node_type: item for item in BUILTIN_NODE_TYPES}[node_type]
    return RuntimeNodeSpec(
        node_key=node_key,
        node_type=node_type,
        node_category=spec.node_category,
        input_ports=spec.input_ports,
        output_ports=spec.output_ports,
        config=dict(spec.default_config),
        logical_model_ref=RuntimeLogicalModelRef(
            model_family_slug="defect-detector",
            target_path=TargetPath.REALTIME,
            contract_major=1,
            selection_policy="explicit_release",
            required_task_type=TaskType.DETECTION,
            deployment_release_id="rel_detection",
        ),
    )


def _classification_node(node_key: str) -> RuntimeNodeSpec:
    spec = {item.node_type: item for item in BUILTIN_NODE_TYPES}["inference.classification"]
    return RuntimeNodeSpec(
        node_key=node_key,
        node_type="inference.classification",
        node_category=spec.node_category,
        input_ports=spec.input_ports,
        output_ports=spec.output_ports,
        config=dict(spec.default_config),
        logical_model_ref=RuntimeLogicalModelRef(
            model_family_slug="roi-classifier",
            target_path=TargetPath.REALTIME,
            contract_major=1,
            selection_policy="explicit_release",
            required_task_type=TaskType.CLASSIFICATION,
            deployment_release_id="rel_classifier",
        ),
    )


def _anomaly_node(node_key: str) -> RuntimeNodeSpec:
    spec = {item.node_type: item for item in BUILTIN_NODE_TYPES}["inference.anomaly"]
    return RuntimeNodeSpec(
        node_key=node_key,
        node_type="inference.anomaly",
        node_category=spec.node_category,
        input_ports=spec.input_ports,
        output_ports=spec.output_ports,
        config=dict(spec.default_config),
        logical_model_ref=RuntimeLogicalModelRef(
            model_family_slug="surface-anomaly-ad",
            target_path=TargetPath.REALTIME,
            contract_major=1,
            selection_policy="explicit_release",
            required_task_type=TaskType.ANOMALY,
            deployment_release_id="rel_anomaly",
        ),
    )


def _edge(from_node_key: str, from_port: str, to_node_key: str, to_port: str) -> RuntimeGraphEdge:
    return RuntimeGraphEdge(
        from_node_key=from_node_key,
        from_port=from_port,
        to_node_key=to_node_key,
        to_port=to_port,
    )


def _rotated_rect_points(
    *,
    center: tuple[float, float],
    width: float,
    height: float,
    angle_degrees: float,
) -> list[tuple[float, float]]:
    radians = math.radians(angle_degrees)
    cos_angle = math.cos(radians)
    sin_angle = math.sin(radians)
    half_width = width / 2
    half_height = height / 2
    points = []
    for offset_x, offset_y in (
        (-half_width, -half_height),
        (half_width, -half_height),
        (half_width, half_height),
        (-half_width, half_height),
    ):
        points.append(
            (
                center[0] + offset_x * cos_angle - offset_y * sin_angle,
                center[1] + offset_x * sin_angle + offset_y * cos_angle,
            )
        )
    return points


def _synthetic_board_png_bytes() -> bytes:
    image = Image.new("RGB", (200, 100), "black")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 190, 90), outline="white", width=3)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _trigger_pipeline_graph(channel_id: str, *, timeout_ms: int = 2500) -> RuntimePipelineGraph:
    _ = channel_id
    return RuntimePipelineGraph(
        nodes=[
            _node("http_trigger", "trigger.http_put", config={"timeout_ms": timeout_ms}),
            _node("channel_input", "source.channel_frame"),
            _inference_node("detect_objects", "inference.detection"),
            _node("http_response", "output.http_trigger_response"),
        ],
        edges=[
            _edge("http_trigger", "trigger", "channel_input", "trigger"),
            _edge("channel_input", "frame", "detect_objects", "frame"),
            _edge("detect_objects", "detections", "http_response", "result"),
        ],
    )


def _create_snapshot_channel(session):
    return create_runtime_channel(
        session,
        RuntimeChannelCreateRequest(
            name="Reject camera",
            technical_place="packing-station",
            source_type=RuntimeSourceType.SNAPSHOT,
            source_config={"url": "https://camera.local/reject-snapshot.jpg"},
            mode=RuntimeMode.REALTIME,
        ),
        created_by="tester",
    )


def _create_rtsp_channel(session):
    return create_runtime_channel(
        session,
        RuntimeChannelCreateRequest(
            name="Reject RTSP camera",
            technical_place="packing-station",
            source_type=RuntimeSourceType.RTSP,
            source_config={"url": "rtsp://camera.local/reject"},
            mode=RuntimeMode.REALTIME,
        ),
        created_by="tester",
    )


def _create_basler_channel(session):
    return create_runtime_channel(
        session,
        RuntimeChannelCreateRequest(
            name="Press Basler",
            technical_place="packing-station",
            source_type=RuntimeSourceType.BASLER_PYLON,
            source_config={
                "serial_number": "40123456",
                "transport_mode": "pylon_direct",
                "pixel_format": "BayerRG8",
                "acquisition_mode": "Continuous",
                "grab_strategy": "OneByOne",
                "trigger_mode": "On",
                "trigger_source": "Software",
                "exposure_time_us": 8000,
                "buffer_count": 24,
                "grab_failure_reconnect_threshold": 3,
            },
            mode=RuntimeMode.REALTIME,
        ),
        created_by="tester",
    )


def _seed_runtime_capability(
    session,
    *,
    capability: dict[str, object] | None = None,
) -> None:
    serving_manifest = {
        "class_labels": ["ok", "defect"],
        "confidence_threshold_default": 0.35,
        "compatible_targets": [TargetPath.REALTIME.value],
    }
    if capability:
        serving_manifest.update(
            {key: value for key, value in capability.items() if key != "architecture"}
        )
    seed_deployed_runtime_model(
        session,
        serving_manifest=serving_manifest,
    )


def _create_published_pipeline(
    session,
    *,
    name: str,
    mode: RuntimeMode,
    graph: RuntimePipelineGraph,
    channel_id: str | None = None,
) -> str:
    definition = create_pipeline_definition(
        session,
        RuntimePipelineDefinitionCreateRequest(name=name),
        created_by="tester",
    )
    version = create_pipeline_version(
        session,
        definition.pipeline_definition_id,
        RuntimePipelineVersionCreateRequest(mode=mode, graph=graph),
        created_by="tester",
    )
    if channel_id is not None:
        channel = session.get(RuntimeChannel, channel_id)
        assert channel is not None
        channel.pipeline_version_id = version.pipeline_version_id
        channel.assigned_pipeline = name
    published = publish_pipeline_version(
        session,
        version.pipeline_version_id,
        published_by="tester",
    )
    return published.pipeline_version_id


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "adminadmin"},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def _copy_auth_cookies(
    source_client: TestClient,
    target_client: TestClient,
    settings: PlatformSettings,
) -> None:
    target_client.cookies.set(
        settings.session.cookie_name,
        source_client.cookies.get(settings.session.cookie_name),
    )
    target_client.cookies.set(
        settings.session.csrf_cookie_name,
        source_client.cookies.get(settings.session.csrf_cookie_name),
    )
