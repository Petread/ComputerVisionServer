from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cv_contracts import EdgeResultAck, RuntimePipelineGraph, RuntimeResultHistoryPolicy
from cv_db.edge_result_store import _edge_result_history_is_due
from edge_agent import persistence, result_storage
from edge_agent.state import EdgeAgentState, EdgeStateError
from runtime_admin_api import edge_workload_builder
from tests.conftest import build_test_settings
from tests.unit.backend.test_edge_agent_m2_runtime import _config, _manifest
from tests.unit.backend.test_edge_trigger_response import plant as trigger_plant

plant = trigger_plant


@pytest.mark.parametrize("enabled", [False, True])
def test_workload_contains_history_policy_in_content_addressed_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    policy = RuntimeResultHistoryPolicy(enabled=enabled, sample_interval_seconds=17)
    channel = SimpleNamespace(
        id="channel-built",
        execution_target="edge_device",
        edge_device_id="edge-built",
        result_history_policy=policy.model_dump(mode="json"),
    )
    loaded = SimpleNamespace(
        channel=channel,
        source=None,
        graph=RuntimePipelineGraph(nodes=[], edges=[]),
        version=SimpleNamespace(id="pipeline-built", config_digest="a" * 64, mode="realtime"),
    )
    state = EdgeAgentState(_config(tmp_path / "edge"))
    try:
        device = SimpleNamespace(
            edge_device_id=channel.edge_device_id,
            enabled=True,
            connectivity_state="online",
            deployment_revalidation_required=False,
            latest_capability_snapshot=SimpleNamespace(
                capabilities=state.capabilities(),
                digest="b" * 64,
            ),
        )
        monkeypatch.setattr(
            edge_workload_builder,
            "validate_runtime_channel_start_requirements",
            lambda *_args, **_kwargs: loaded,
        )
        monkeypatch.setattr(edge_workload_builder, "get_edge_device_detail", lambda *_args: device)
        monkeypatch.setattr(edge_workload_builder, "build_runtime_model_resolver", lambda _: None)
        monkeypatch.setattr(edge_workload_builder, "_edge_source", lambda **_: {"type": "passive"})
        monkeypatch.setattr(
            edge_workload_builder,
            "save_edge_workload_release",
            lambda _session, **kwargs: SimpleNamespace(**kwargs),
        )
        settings = build_test_settings("runtime-admin-api", tmp_path / "central")
        release = edge_workload_builder.build_edge_workload_release(
            None,
            settings=settings,
            channel_id=channel.id,
            created_by="operator",
        )
        requirements = release.manifest.runtime_requirements
        assert requirements["result_history_policy"] == policy.model_dump(mode="json")
        assert requirements["emit_source_results"] is False
        path = edge_workload_builder._cas_path(settings, requirements["configuration_sha256"])
        assert json.loads(path.read_text())["result_history_policy"] == policy.model_dump(
            mode="json"
        )
        previous_digest = release.release_digest
        channel.result_history_policy = {**channel.result_history_policy, "enabled": not enabled}
        changed = edge_workload_builder.build_edge_workload_release(
            None,
            settings=settings,
            channel_id=channel.id,
            created_by="operator",
        )
        assert changed.release_digest != previous_digest
    finally:
        state.close()


@pytest.mark.parametrize("enabled", [False, True])
def test_central_history_accepts_sampled_projection_without_resampling(enabled: bool) -> None:
    session = SimpleNamespace(scalar=lambda _: pytest.fail("Already sampled on the Edge"))
    assert (
        _edge_result_history_is_due(
            session,
            channel=SimpleNamespace(result_history_policy={"enabled": enabled}),
            occurred_at=datetime.now(UTC),
            sampled_on_edge=True,
        )
        is enabled
    )


def _history_execution(value: int = 1) -> dict:
    return {
        "schema_version": "runtime.execution.result.v1",
        "status": "completed",
        "persisted_results": [{"payload": {"result": {"count": value}}}],
        "final_response": {"response_only": "do not store"},
        "raw_inference_outputs": [{"large": "model internals"}],
        "node_outputs": {"frame": {"image_base64": "not a recording"}},
        "timing": {"total_ms": 82},
    }


def test_disabled_history_never_enqueues_even_when_output_declares_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = EdgeAgentState(_config(tmp_path))
    manifest = _manifest("deployment-lean")
    frame = SimpleNamespace(index=1)
    try:
        monkeypatch.setattr(
            state,
            "enqueue_result",
            lambda **_kwargs: pytest.fail("History is disabled; no durable write is allowed"),
        )
        for _ in range(300):
            assert state._runtime_result_sink(manifest, frame, _history_execution()) is None
        assert state.store.outbox_usage() == (0, 0)
    finally:
        state.close()


def test_history_opt_in_sends_only_selected_fields_at_the_configured_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = EdgeAgentState(_config(tmp_path))
    manifest = _manifest(
        "deployment-history",
        requirements={
            "result_history_policy": {"enabled": True, "sample_interval_seconds": 3600},
        },
    )
    now = [0.0]
    monkeypatch.setattr(result_storage.time, "monotonic", lambda: now[0])
    frame = SimpleNamespace(index=1)
    execution = _history_execution()
    execution["persisted_results"][0]["payload"]["result"]["image_base64"] = "exclude me"
    try:
        first = state._runtime_result_sink(manifest, frame, execution)
        assert first is not None
        assert first.payload["final_response"] == {"count": 1}
        assert "node_outputs" not in first.payload
        assert "raw_inference_outputs" not in first.payload
        assert "timing" not in first.payload
        assert "image_base64" not in first.model_dump_json()
        now[0] = 3599
        assert state._runtime_result_sink(manifest, frame, execution) is None
        now[0] = 3600
        second = state._runtime_result_sink(manifest, frame, execution)
        assert second is not None and second.sequence == 2
        execution["persisted_results"] = []
        now[0] = 7200
        assert state._runtime_result_sink(manifest, frame, execution) is None
        assert state.store.outbox_usage()[0] == 2
    finally:
        state.close()


def test_acknowledged_results_are_deleted_and_numbering_continues_with_bounded_acks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(persistence, "ACK_CACHE_MAX_ITEMS", 3)
    state = EdgeAgentState(_config(tmp_path))
    try:
        for index in range(1, 31):
            envelope = state.enqueue_result(
                channel_id="channel-history",
                workload_release_id="release-history",
                payload={"value": index},
            )
            assert envelope.sequence == index
            ack = EdgeResultAck(
                management_ip=state.config.address.management_ip,
                boot_id=state.boot_id,
                acknowledged_through_sequence=index,
                idempotency_key=f"ack-{index}",
            )
            state.acknowledge(ack)
            assert state.acknowledge(ack) == ack
            assert state.store.outbox_usage() == (0, 0)
        count = state.store._fetchone("SELECT COUNT(*) FROM result_ack_requests")[0]
        assert count == 3
    finally:
        state.close()


def test_old_reused_sequences_are_recovered_without_dropping_results(tmp_path: Path) -> None:
    state = EdgeAgentState(_config(tmp_path))
    try:
        envelope = state.enqueue_result(
            channel_id="channel-recover",
            workload_release_id="release-recover",
            payload={"value": "first"},
        )
        state.acknowledge(
            EdgeResultAck(
                management_ip=state.config.address.management_ip,
                boot_id=state.boot_id,
                acknowledged_through_sequence=1,
                idempotency_key="ack-first",
            )
        )
        orphan = envelope.model_copy(update={"payload": {"value": "undelivered"}})
        state.store.insert_result(orphan, size_bytes=len(orphan.model_dump_json()))
        boot_id = state.boot_id
    finally:
        state.close()
    restored = EdgeAgentState(_config(tmp_path))
    try:
        batch = restored.results(boot_id=boot_id, after_sequence=1, limit=10)
        assert [(item.sequence, item.payload) for item in batch.items] == [
            (2, {"value": "undelivered"}),
        ]
        assert restored.store.next_sequence(boot_id) == 3
    finally:
        restored.close()


def test_disk_reserve_rejects_new_writes_without_removing_pending_results(tmp_path: Path) -> None:
    state = EdgeAgentState(_config(tmp_path))
    try:
        state.enqueue_result(
            channel_id="channel-reserve",
            workload_release_id="release-reserve",
            payload={},
        )
        state.faults.disk_full = True
        with pytest.raises(EdgeStateError, match="free-space reserve"):
            state.enqueue_result(
                channel_id="channel-reserve",
                workload_release_id="release-reserve",
                payload={},
            )
        assert state.store.outbox_usage()[0] == 1
    finally:
        state.close()


def test_keyless_triggers_only_keep_last_response_in_ram(plant: SimpleNamespace) -> None:
    for _ in range(25):
        assert plant.client.put(plant.path).status_code == 200
    assert plant.graph.calls == 25
    assert plant.state.store._fetchone("SELECT COUNT(*) FROM plant_trigger_requests")[0] == 0
    assert plant.state.store.outbox_usage() == (0, 0)
    restored = EdgeAgentState(plant.state.config)
    try:
        assert restored.last_trigger_result(plant.manifest.channel_id).available is False
    finally:
        restored.close()


def test_replay_cache_limit_rejects_before_execution_and_preserves_existing_key(
    plant: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(persistence, "TRIGGER_CACHE_MAX_ITEMS", 1)
    headers = {"X-Idempotency-Key": "first-key"}
    assert plant.client.put(plant.path, headers=headers).status_code == 200
    full = plant.client.put(plant.path, headers={"X-Idempotency-Key": "second-key"})
    assert full.status_code == 507
    assert plant.graph.calls == 1
    replay = plant.client.put(plant.path, headers=headers)
    assert replay.status_code == 200 and replay.json()["duplicate"] is True
    assert plant.graph.calls == 1
    # A full replay cache does not block headerless triggers.
    assert plant.client.put(plant.path).status_code == 200
    assert plant.graph.calls == 2


def test_replay_cache_byte_limit_is_checked_before_execution(
    plant: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(persistence, "TRIGGER_CACHE_MAX_BYTES", 1)
    response = plant.client.put(plant.path, headers={"X-Idempotency-Key": "too-large"})
    assert response.status_code == 507
    assert plant.graph.calls == 0


def test_expired_key_is_deleted_and_can_execute_a_new_request(plant: SimpleNamespace) -> None:
    headers = {"X-Idempotency-Key": "expiring-key"}
    assert plant.client.put(plant.path, headers=headers).status_code == 200
    old = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    with plant.state.store._transaction() as connection:
        connection.execute("UPDATE plant_trigger_requests SET created_at = ?", (old,))
    replay = plant.client.put(plant.path, headers=headers)
    assert replay.status_code == 200 and replay.json()["duplicate"] is False
    assert plant.graph.calls == 2
    assert plant.state.store._fetchone("SELECT COUNT(*) FROM plant_trigger_requests")[0] == 1


def test_transient_cache_bounds_bytes_items_and_age(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(result_storage.time, "monotonic", lambda: now[0])
    cache = result_storage.TransientCache[str](max_bytes=10, max_items=2)
    cache.put("one", "one", size_bytes=6)
    cache.put("two", "two", size_bytes=6)
    assert cache.get("one") is None and cache.get("two") == "two"
    cache.put("two", "new two", size_bytes=3)
    cache.put("three", "three", size_bytes=3)
    cache.put("four", "four", size_bytes=3)
    assert cache.get("two") is None
    assert cache._bytes == 6
    cache.put("large", "too large", size_bytes=11)
    assert cache.get("large") is None
    now[0] = 301
    assert cache.get("four") is None
    assert cache._bytes == 0
