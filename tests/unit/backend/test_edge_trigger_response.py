from __future__ import annotations

import copy
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from cv_contracts import EdgeDeploymentManifest, EdgeDeviceAddress, RuntimePipelineGraph
from cv_contracts.runtime import RuntimeNodeSpec
from cv_runtime.projections import edge_http_response_policy
from edge_agent.plant import create_plant_app
from edge_agent.runtime import SourceFrame, _RuntimeWorker
from edge_agent.state import EdgeAgentConfig, EdgeAgentState
from fastapi.testclient import TestClient


class _Graph:
    def __init__(self) -> None:
        self.graph = RuntimePipelineGraph(
            nodes=[
                RuntimeNodeSpec(
                    node_key="http_trigger", node_type="trigger.http_put", node_category="trigger"
                ),
                RuntimeNodeSpec(
                    node_key="result_output", node_type="output.result", node_category="output"
                ),
            ],
            edges=[],
        )
        self.calls = 0
        self.result = {"result": "LKW wartend"}
        self.status = "completed"
        self.errors: list[dict] = []

    def execute(self, _frame: SourceFrame, **_kwargs: object) -> dict[str, object]:
        self.calls += 1
        return {
            "schema_version": "runtime.execution.result.v1",
            "status": self.status,
            "pipeline_version_id": "pipeline-trigger-response",
            "final_response": copy.deepcopy(self.result),
            "node_outputs": {"diagnostic": "not part of compact response"},
            "raw_inference_outputs": [{"model": "internal-model-details"}],
            "overlay_primitives": [{"shape": "box"}],
            "timing": {"total_ms": 237.5, "node_timings_ms": []},
            "errors": self.errors,
        }


@pytest.fixture()
def plant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    state = EdgeAgentState(
        EdgeAgentConfig(
            address=EdgeDeviceAddress(management_ip="192.168.50.21"),
            data_root=tmp_path,
            minimum_disk_free_bytes=0,
            engine_builder_mode="simulated",
            plant_rate_limit_per_second=100,
        )
    )
    graph = _Graph()
    manifest = EdgeDeploymentManifest(
        deployment_id="deployment-trigger-response",
        workload_release_id="release-trigger-response",
        channel_id="channel-trigger-response",
        pipeline_version_id="pipeline-trigger-response",
        idempotency_key="prepare-trigger-response",
        created_at="2026-09-22T19:20:00Z",
    )
    frame = SourceFrame(1, 640, 480, "2026-09-22T19:20:00Z", image=object())
    worker = _RuntimeWorker(executor=graph, latest_frame=frame)
    handle = SimpleNamespace(manifest=manifest, runtime=worker)
    monkeypatch.setattr(state.supervisor, "_handle_for_channel", lambda _channel: handle)
    monkeypatch.setattr(state.supervisor, "_preview_sink", None)
    with TestClient(create_plant_app(state=state)) as client:
        try:
            yield SimpleNamespace(
                client=client,
                state=state,
                graph=graph,
                manifest=manifest,
                output=graph.graph.nodes[1].config,
                trigger=graph.graph.nodes[0].config,
                path=f"/edge-plant/v1/channels/{manifest.channel_id}/trigger",
            )
        finally:
            state.close()


def test_legacy_pipeline_still_receives_full_response(plant: SimpleNamespace) -> None:
    response = plant.client.put(plant.path)
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "edge.plant.trigger.response.v1"
    assert body["execution_result"]["node_outputs"]
    assert body["execution_result"]["final_response"] == {"result": "LKW wartend"}


@pytest.mark.parametrize(
    "fields",
    [
        {"result": "LKW wartend"},
        {"result": "LKW wartend", "score": 0.39, "measurements": {"count": 2}},
        {},
        {"data": "configured data", "meta": "configured meta", "duplicate": "own field"},
    ],
)
def test_result_only_is_exact_http_body_and_keeps_diagnostics_only_in_memory(
    plant: SimpleNamespace, fields: dict[str, object]
) -> None:
    plant.output["edge_response_mode"] = "result_only"
    plant.graph.result = fields
    response = plant.client.put(plant.path)
    assert response.status_code == 200
    assert response.json() == fields
    assert b"node_outputs" not in response.content
    last = plant.client.get(plant.path.replace("/trigger", "/last-result")).json()
    assert last["result"]["execution_result"]["node_outputs"]
    assert last["result"]["execution_result"]["final_response"] == fields
    assert last["result"]["result_sequence"] is None
    assert plant.state.store.results_after(plant.state.boot_id, 0) == []
    assert plant.state.store.latest_plant_trigger_response(plant.manifest.channel_id) is None


@pytest.mark.parametrize("required", [False, True])
def test_node_controls_header_requirement_before_execution(
    plant: SimpleNamespace, required: bool
) -> None:
    plant.output["edge_response_mode"] = "result_only"
    plant.trigger["require_idempotency_key"] = required
    response = plant.client.put(plant.path)
    assert response.status_code == (422 if required else 200)
    assert plant.graph.calls == (0 if required else 1)
    if required:
        assert response.json()["code"] == "trigger_idempotency_key_required"
        assert plant.state.store.outbox_usage()[0] == 0
    accepted = plant.client.put(plant.path, headers={"X-Idempotency-Key": "cycle-001"})
    assert accepted.status_code == 200
    replay = plant.client.put(plant.path, headers={"X-Idempotency-Key": "cycle-001"})
    assert replay.json() == {"result": "LKW wartend"}
    assert plant.graph.calls == (1 if required else 2)


def test_each_headerless_put_executes_and_explicit_keys_still_detect_conflicts(
    plant: SimpleNamespace,
) -> None:
    plant.output["edge_response_mode"] = "result_only"
    assert plant.client.put(plant.path).status_code == 200
    assert plant.client.put(plant.path, json={}).status_code == 200
    assert plant.graph.calls == 2
    headers = {"X-Idempotency-Key": "cycle-conflict"}
    assert plant.client.put(plant.path, headers=headers, json={}).status_code == 200
    conflict = plant.client.put(plant.path, headers=headers, json={"payload": {"part": 42}})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "trigger_idempotency_conflict"
    assert plant.graph.calls == 3


def test_metadata_selection_has_only_selected_fields_and_tracks_duplicate(
    plant: SimpleNamespace,
) -> None:
    plant.output.update(
        edge_response_mode="result_with_metadata",
        edge_response_metadata_fields=["correlation_id", "duration_ms", "duplicate", "status"],
    )
    headers = {"X-Idempotency-Key": "cycle-metadata"}
    request = {"correlation_id": "part-4711"}
    first = plant.client.put(plant.path, headers=headers, json=request)
    assert first.json() == {
        "data": {"result": "LKW wartend"},
        "meta": {
            "correlation_id": "part-4711",
            "duration_ms": 237.5,
            "duplicate": False,
            "status": "completed",
        },
    }
    plant.output["edge_response_mode"] = "full"
    replay = plant.client.put(plant.path, headers=headers, json=request)
    assert replay.json()["meta"]["duplicate"] is True
    assert replay.json()["data"] == first.json()["data"]
    assert plant.graph.calls == 1


def test_compact_replay_survives_edge_restart(plant: SimpleNamespace) -> None:
    plant.output["edge_response_mode"] = "result_only"
    headers = {"X-Idempotency-Key": "cycle-restart"}
    assert plant.client.put(plant.path, headers=headers).json() == {"result": "LKW wartend"}
    restored = EdgeAgentState(plant.state.config)
    try:
        with TestClient(create_plant_app(state=restored)) as client:
            assert client.put(plant.path, headers=headers).json() == {"result": "LKW wartend"}
    finally:
        restored.close()
    assert plant.graph.calls == 1


@pytest.mark.parametrize("mode", ["result_only", "result_with_metadata"])
def test_failed_execution_is_not_reported_as_successful_compact_result(
    plant: SimpleNamespace, mode: str
) -> None:
    plant.output["edge_response_mode"] = mode
    plant.graph.status = "failed"
    plant.graph.errors = [{"node_key": "classification", "message": "inference failed"}]
    headers = {"X-Idempotency-Key": "cycle-failed"}
    for _ in range(2):
        response = plant.client.put(plant.path, headers=headers)
        assert response.status_code == 503
        assert response.json()["code"] == "trigger_execution_failed"
        assert "result" not in response.json()
    assert plant.graph.calls == 1


def test_invalid_response_policy_is_rejected_before_execution(plant: SimpleNamespace) -> None:
    plant.output.update(
        edge_response_mode="result_with_metadata", edge_response_metadata_fields=["secret"]
    )
    response = plant.client.put(plant.path)
    assert response.status_code == 422
    assert response.json()["code"] == "trigger_configuration_invalid"
    assert plant.graph.calls == 0


def test_conflicting_result_node_policies_are_rejected(plant: SimpleNamespace) -> None:
    plant.output["edge_response_mode"] = "result_only"
    plant.graph.graph.nodes.append(
        plant.graph.graph.nodes[1].model_copy(update={"node_key": "other_result", "config": {}})
    )
    with pytest.raises(ValueError, match="same Edge HTTP response settings"):
        edge_http_response_policy(plant.graph.graph.nodes)


def test_openapi_describes_optional_body_header_and_configurable_response(
    plant: SimpleNamespace,
) -> None:
    operation = plant.client.get("/openapi.json").json()["paths"][
        "/edge-plant/v1/channels/{channel_id}/trigger"
    ]["put"]
    header = next(item for item in operation["parameters"] if item["in"] == "header")
    assert header["required"] is False
    assert operation["requestBody"].get("required", False) is False
    schemas = operation["responses"]["200"]["content"]["application/json"]["schema"]["anyOf"]
    assert {"type": "object", "additionalProperties": True} in schemas
