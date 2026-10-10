from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from cv_contracts import EdgeDeploymentManifest, EdgeDeviceAddress
from edge_agent.runtime import EdgeRuntimeSupervisor, EdgeRuntimeTriggerError, SourceFrame
from edge_agent.state import EdgeAgentConfig, EdgeAgentState, EdgeStateError


def _manifest() -> EdgeDeploymentManifest:
    return EdgeDeploymentManifest(
        deployment_id="camera-stop",
        workload_release_id="camera-release",
        channel_id="camera-channel",
        pipeline_version_id="camera-pipeline",
        idempotency_key="prepare-camera",
        created_at=datetime.now(UTC).isoformat(),
        runtime_requirements={"source": {"type": "passive"}, "emit_source_results": True},
    )


def test_stop_during_model_load_never_opens_camera(monkeypatch: pytest.MonkeyPatch) -> None:
    loading, release = threading.Event(), threading.Event()
    graph = Mock()

    def load_graph(_manifest: EdgeDeploymentManifest) -> Mock:
        loading.set()
        assert release.wait(5)
        return graph

    supervisor = EdgeRuntimeSupervisor(store=Mock(), result_sink=Mock(), graph_factory=load_graph)
    create_source = Mock()
    monkeypatch.setattr(supervisor, "_create_source", create_source)
    manifest = _manifest()
    try:
        supervisor.start(manifest)
        assert loading.wait(2)
        assert supervisor.stop(manifest.deployment_id, timeout_seconds=0) is False
        # A second stop must still find the retiring worker.
        assert supervisor.stop(manifest.deployment_id, timeout_seconds=0) is False
        release.set()
        assert supervisor.stop(manifest.deployment_id) is True
        create_source.assert_not_called()
        graph.close.assert_called_once()
    finally:
        release.set()
        supervisor.close()


def test_stop_waits_for_camera_release_and_drops_late_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reading, release, closed = threading.Event(), threading.Event(), threading.Event()
    manifest = _manifest()
    result_sink = Mock()

    def read(_stop: threading.Event) -> SourceFrame:
        reading.set()
        assert release.wait(5)
        return SourceFrame(1, 32, 24, datetime.now(UTC).isoformat())

    camera = Mock(read=read, close=closed.set)
    supervisor = EdgeRuntimeSupervisor(store=Mock(), result_sink=result_sink)
    monkeypatch.setattr(supervisor, "_create_source", lambda _manifest: camera)
    try:
        supervisor.start(manifest)
        assert reading.wait(2)
        assert supervisor.stop(manifest.deployment_id, timeout_seconds=0) is False
        assert not closed.is_set()
        assert supervisor.active_channel_ids() == [manifest.channel_id]
        with pytest.raises(EdgeRuntimeTriggerError, match="No active edge workload"):
            supervisor.trigger(manifest.channel_id, payload={}, correlation_id="after-stop")
        release.set()
        assert supervisor.stop(manifest.deployment_id) is True
        assert closed.is_set()
        assert supervisor.active_channel_ids() == []
        result_sink.assert_not_called()
    finally:
        release.set()
        supervisor.close()


@pytest.mark.parametrize("retry_before_restart", [False, True])
def test_pending_stop_is_not_successful_or_recovered_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_before_restart: bool,
) -> None:
    config = EdgeAgentConfig(
        address=EdgeDeviceAddress(management_ip="192.168.50.21"),
        data_root=tmp_path,
        minimum_disk_free_bytes=0,
    )
    manifest = _manifest()
    state = EdgeAgentState(config)
    try:
        state.prepare(manifest)
        state.commit(manifest.deployment_id, "commit-camera")
        real_stop = state.supervisor.stop

        def timed_out_stop(deployment_id: str) -> bool:
            real_stop(deployment_id)
            return False

        with monkeypatch.context() as patch:
            patch.setattr(state.supervisor, "stop", timed_out_stop)
            with pytest.raises(EdgeStateError) as error:
                state.stop(manifest.deployment_id, "stop-camera")
            assert error.value.code == "runtime_stop_pending"
            assert error.value.retryable
            assert state.deployment(manifest.deployment_id).status == "stopping"
        if retry_before_restart:
            assert state.stop(manifest.deployment_id, "stop-camera").status == "stopped"
            assert state.stop(manifest.deployment_id, "stop-camera").status == "stopped"
    finally:
        state.close()

    recovered = EdgeAgentState(config)
    try:
        assert recovered.supervisor.active_channel_ids() == []
        assert recovered.runtime_status().recovered_deployment_ids == []
        assert recovered.stop(manifest.deployment_id, "stop-camera").status == "stopped"
    finally:
        recovered.close()
