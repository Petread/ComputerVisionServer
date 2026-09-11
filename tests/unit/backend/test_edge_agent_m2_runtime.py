from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cv_contracts import EdgeDeploymentManifest, EdgeDeviceAddress, EdgeResultAck
from edge_agent.state import EdgeAgentConfig, EdgeAgentState, EdgeStateError


def _config(root: Path, **changes: object) -> EdgeAgentConfig:
    values = {
        "address": EdgeDeviceAddress(management_ip="192.168.50.21"),
        "data_root": root,
        "minimum_disk_free_bytes": 0,
    }
    values.update(changes)
    return EdgeAgentConfig(**values)  # type: ignore[arg-type]


def _manifest(deployment_id: str, *, requirements: dict | None = None) -> EdgeDeploymentManifest:
    return EdgeDeploymentManifest(
        deployment_id=deployment_id,
        workload_release_id=f"release-{deployment_id}",
        channel_id=f"channel-{deployment_id}",
        pipeline_version_id=f"pipeline-{deployment_id}",
        idempotency_key=f"prepare-{deployment_id}",
        created_at=datetime.now(UTC).isoformat(),
        runtime_requirements=requirements or {"source": {"type": "passive", "interval_ms": 10}},
    )


def test_sqlite_wal_deployment_and_outbox_survive_agent_restart(tmp_path: Path) -> None:
    first = EdgeAgentState(_config(tmp_path))
    manifest = _manifest("deployment-one")
    prepared = first.prepare(manifest)
    running = first.commit(manifest.deployment_id, "commit-one")
    result = first.enqueue_result(
        channel_id=manifest.channel_id,
        workload_release_id=manifest.workload_release_id,
        payload={"decision": "ok"},
    )
    first_boot = first.boot_id
    assert prepared.runtime_slot == "A"
    assert running.status == "running"
    assert first.store.journal_mode == "wal"
    first.close()

    second = EdgeAgentState(_config(tmp_path))
    try:
        restored = second.deployment(manifest.deployment_id)
        replay = second.results(boot_id=first_boot, after_sequence=0, limit=10)
        runtime = second.runtime_status()
        assert restored.status == "running"
        assert restored.recovery_count == 1
        assert runtime.active_slot == "A"
        assert manifest.deployment_id in runtime.recovered_deployment_ids
        assert [item.sequence for item in replay.items] == [result.sequence]

        second.acknowledge(
            EdgeResultAck(
                management_ip="192.168.50.21",
                boot_id=first_boot,
                acknowledged_through_sequence=result.sequence,
                idempotency_key="ack-after-restart",
            )
        )
    finally:
        second.close()

    third = EdgeAgentState(_config(tmp_path))
    try:
        assert third.results(boot_id=first_boot, after_sequence=0, limit=10).items == []
    finally:
        third.close()


def test_ab_slots_commit_and_restore_previous_deployment(tmp_path: Path) -> None:
    state = EdgeAgentState(_config(tmp_path))
    first = _manifest("deployment-a")
    second = _manifest("deployment-b")
    try:
        assert state.prepare(first).runtime_slot == "A"
        state.commit(first.deployment_id, "commit-a")
        assert state.prepare(second).runtime_slot == "B"
        state.commit(second.deployment_id, "commit-b")

        status = state.runtime_status()
        assert status.active_slot == "B"
        assert status.previous_slot == "A"
        assert state.deployment(first.deployment_id).status == "standby"

        state.rollback(second.deployment_id, "rollback-b")
        restored = state.runtime_status()
        assert restored.active_slot == "A"
        assert restored.previous_slot is None
        assert state.deployment(first.deployment_id).status == "running"
        assert state.deployment(second.deployment_id).status == "rolled_back"
    finally:
        state.close()


def test_artifact_and_outbox_quotas_reject_only_new_writes(tmp_path: Path) -> None:
    artifact_state = EdgeAgentState(_config(tmp_path / "artifacts", artifact_quota_bytes=3))
    payload = b"four"
    try:
        with pytest.raises(EdgeStateError) as artifact_error:
            artifact_state.append_blob(
                hashlib.sha256(payload).hexdigest(),
                payload,
                offset=0,
                total_size=len(payload),
                idempotency_key="quota-upload",
            )
        assert artifact_error.value.code == "artifact_quota_exceeded"
        assert artifact_state.health().state == "degraded"
    finally:
        artifact_state.close()

    outbox_state = EdgeAgentState(
        _config(tmp_path / "outbox", result_outbox_max_items=1)
    )
    try:
        outbox_state.enqueue_result(
            channel_id="channel-one", workload_release_id="release-one", payload={"value": 1}
        )
        with pytest.raises(EdgeStateError) as outbox_error:
            outbox_state.enqueue_result(
                channel_id="channel-one",
                workload_release_id="release-one",
                payload={"value": 2},
            )
        assert outbox_error.value.code == "result_outbox_full"
        assert outbox_state.health().state == "degraded"
    finally:
        outbox_state.close()


def test_supervisor_persists_backoff_and_crash_loop(tmp_path: Path) -> None:
    state = EdgeAgentState(
        _config(
            tmp_path,
            supervisor_max_restarts=1,
            supervisor_base_backoff_seconds=0.01,
        )
    )
    manifest = _manifest(
        "deployment-crash",
        requirements={
            "source": {"type": "synthetic", "interval_ms": 1},
            "simulate_runtime_failure_after_frames": 1,
        },
    )
    try:
        state.prepare(manifest)
        state.commit(manifest.deployment_id, "commit-crash")
        deadline = time.monotonic() + 2
        process_state = None
        while time.monotonic() < deadline:
            processes = state.runtime_status().processes
            process_state = processes[0].state if processes else None
            if process_state == "crash_loop":
                break
            time.sleep(0.01)
        assert process_state == "crash_loop"
        assert state.health().state == "degraded"
    finally:
        state.close()
