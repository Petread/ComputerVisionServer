from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from cv_contracts import (
    EdgeDeploymentManifest,
    RuntimeGraphEdge,
    RuntimeNodeSpec,
    RuntimePipelineGraph,
)
from cv_runtime.context import RuntimeExecutionContext
from cv_runtime.distribution import edge_node_contracts, runtime_code_digest
from cv_runtime.engine import ExecutablePipelineGraph
from cv_runtime.payloads import FramePayload, TriggerPayload
from cv_runtime.registry import builtin_node_types
from cv_status import RuntimeMode
from edge_agent.runtime import SourceFrame
from edge_agent.shared_graph import SharedEdgeGraphExecutor


def node(key: str, kind: str, config: dict | None = None) -> RuntimeNodeSpec:
    summary = next(n for n in builtin_node_types() if n.node_type == kind)
    return RuntimeNodeSpec(
        node_key=key,
        node_type=kind,
        node_category=summary.node_category,
        config=config or summary.default_config,
        input_ports=summary.input_ports,
        output_ports=summary.output_ports,
    )


def manifest() -> EdgeDeploymentManifest:
    return EdgeDeploymentManifest(
        deployment_id="dep_test",
        workload_release_id="release_test",
        channel_id="channel_test",
        pipeline_version_id="pipeline_test",
        idempotency_key="test-key",
        created_at="2026-09-06T00:00:00+00:00",
        runtime_requirements={"executor": "cv_runtime_v1"},
    )


def test_edge_and_server_execute_identical_geometry_and_result_projection() -> None:
    graph = RuntimePipelineGraph(
        nodes=[
            node("frame", "source.channel_frame"),
            node("resize", "preprocessing.resize", {"width": 16, "height": 12}),
            node("result", "output.result"),
        ],
        edges=[
            RuntimeGraphEdge(
                from_node_key="frame", from_port="frame", to_node_key="resize", to_port="frame"
            ),
            RuntimeGraphEdge(
                from_node_key="resize", from_port="frame", to_node_key="result", to_port="result"
            ),
        ],
    )
    frame = SourceFrame(
        4, 32, 24, "2026-09-06T00:00:00+00:00", np.zeros((24, 32, 3), dtype=np.uint8)
    )
    edge = SharedEdgeGraphExecutor(
        manifest=manifest(), graph=graph, inference_runtime=None, mode="realtime"
    )
    actual = edge.execute(frame, correlation_id="server-test")
    server = ExecutablePipelineGraph.build(
        pipeline_version_id="pipeline_test",
        graph=graph,
        handler_refs={n.node_type: n.runtime_handler_ref for n in builtin_node_types()},
    )
    expected = server.execute(
        RuntimeExecutionContext(
            correlation_id="server-test",
            pipeline_version_id="pipeline_test",
            mode=RuntimeMode.REALTIME,
            channel_id="channel_test",
            input_frame=FramePayload.from_cv2_frame(
                frame.image,
                timestamp=frame.captured_at,
                metadata={"sequence": 4, "frame_sequence": 4},
            ),
            trigger=TriggerPayload(),
            metadata={
                "frame_sequence": 4,
                "live_frame_sequence": 4,
                "materialize_frame_node_outputs": False,
            },
        )
    )
    assert actual["status"] == expected.status
    assert actual["final_response"] == expected.model_dump(mode="json")["final_response"]
    assert actual["runtime_metadata"]["executor"] == "cv_runtime"
    assert actual["status"] != "failed"


def test_stem_temporal_and_board_analysis_are_discovered_from_shared_registry() -> None:
    contracts = edge_node_contracts()
    for name in (
        "analysis.stem_surface_phase",
        "analysis.stem_curvature_phase_solver",
        "analysis.active_stem_tracker",
        "inference.temporal_sequence",
        "analysis.single_view_board_cut_decision",
    ):
        assert name in contracts
    assert len(runtime_code_digest()) == 64


def test_unimplemented_camera_adapter_is_not_advertised() -> None:
    assert "source.synchronized_camera_frames" not in edge_node_contracts()
    with pytest.raises(ValueError, match="adapters unavailable"):
        SharedEdgeGraphExecutor(
            manifest=manifest(),
            graph=RuntimePipelineGraph(
                nodes=[node("cameras", "source.synchronized_camera_frames")], edges=[]
            ),
            inference_runtime=None,
            mode="trigger",
        )


def test_local_classification_uses_the_shared_typed_response() -> None:
    import io

    from edge_agent.inference import EdgeInferenceRuntime
    from edge_agent.shared_graph import LocalInferenceClient
    from PIL import Image
    from tests.unit.backend.test_edge_e1_execution import _FakeSession, _model_spec

    runtime = EdgeInferenceRuntime(
        specs={"classify": _model_spec()}, sessions={"classify": _FakeSession()}
    )
    client = LocalInferenceClient(
        runtime,
        [
            {
                "node_key": "classify",
                "deployment_release_id": "release-test",
                "model_family_slug": "test-model",
                "model_version_id": "version-test",
                "task_type": "classification",
                "architecture": {
                    "task_type": "classification",
                    "family": "test",
                    "version": "1",
                    "size": "s",
                    "display_name": "Test model",
                    "internal_key": "test-model",
                },
            }
        ],
    )
    image = io.BytesIO()
    Image.new("RGB", (8, 8)).save(image, format="PNG")
    response = client.classify_bytes(
        deployment_target="edge:classify", image_bytes=image.getvalue(), top_k=1
    )
    assert response.labels[0].label == "good"
    assert response.labels[0].rank == 1
    assert 0 < response.labels[0].score <= 1


def test_edge_temporal_inference_uses_training_scaler_and_calibrated_angle(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from cv_runtime.temporal_execution import _prepare_temporal_model_tensor
    from edge_agent.shared_graph import LocalInferenceClient
    from tests.unit.backend.test_temporal_runtime_inference import _request, _temporal_route

    route = _temporal_route(tmp_path)
    request = _request().model_copy(update={"deployment_target": "edge:temporal"})
    expected, _ = _prepare_temporal_model_tensor(route=route, payload=request)

    class Session:
        input_names = ("features",)

        def infer(self, inputs: dict) -> dict:
            np.testing.assert_allclose(inputs["features"], expected)
            return {
                name: np.asarray([value])
                for name, value in {
                    "output__bow_relevance_probability": 0.9,
                    "output__target_observed_probability": 0.9,
                    "output__angle_sin_2e": 1.0,
                    "output__angle_cos_2e": 0.0,
                    "output__angle_confidence_raw": 0.7,
                }.items()
            }

    paths = {
        descriptor["sha256"]: tmp_path / descriptor["path"]
        for descriptor in route.manifest["source_manifests"].values()
    }
    model = {
        "node_key": "temporal",
        "deployment_release_id": "release-test",
        "model_version_id": "version-test",
        "model_family_slug": "test-temporal",
        "task_type": "temporal_sequence",
        "architecture": route.architecture.model_dump(mode="json"),
        "temporal": route.manifest["temporal"],
        "temporal_artifacts": {
            name: descriptor["sha256"]
            for name, descriptor in route.manifest["source_manifests"].items()
        },
    }
    client = LocalInferenceClient(
        SimpleNamespace(sessions={"temporal": Session()}), [model], paths.__getitem__
    )
    result = client.temporal_sequence(request)
    assert result.signed_correction_deg == pytest.approx(45)
    assert result.confidence_calibration_valid
    assert result.rotation_viewpoint_convention == request.rotation_viewpoint_convention


def test_software_requests_are_idempotent_and_require_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cv_contracts.edge import EdgeSoftwareCommand
    from edge_agent.software import queue_software_command

    command = EdgeSoftwareCommand(request_id="update-test", package_sha256="a" * 64)
    monkeypatch.delenv("YBB_EDGE_SOFTWARE_LAUNCHER", raising=False)
    with pytest.raises(ValueError, match="one-time"):
        queue_software_command(tmp_path, command)
    monkeypatch.setenv("YBB_EDGE_SOFTWARE_LAUNCHER", "1")
    assert queue_software_command(tmp_path, command).phase == "queued"
    assert queue_software_command(tmp_path, command).phase == "queued"
    with pytest.raises(ValueError, match="different request"):
        queue_software_command(tmp_path, command.model_copy(update={"package_sha256": "b" * 64}))
