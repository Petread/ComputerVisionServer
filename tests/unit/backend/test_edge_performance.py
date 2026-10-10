from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from cv_contracts import EdgeDeploymentManifest, EdgeRuntimeProcess
from edge_agent.persistence import EdgeStateStore
from edge_agent.runtime import EdgeRuntimeSupervisor, SourceFrame
from edge_agent.telemetry import EdgePerformanceCounters


def test_rates_measure_frames_over_time_and_expire_without_new_frames() -> None:
    clock = [100.0]
    counters = EdgePerformanceCounters(clock=lambda: clock[0])
    for index in range(100):
        clock[0] = 100.0 + index / 100
        counters.acquired_frame("2026-09-21T08:00:00Z", sampled_out=index % 4 != 0)
        if index % 4 == 0:
            counters.execution(
                {
                    "status": "completed",
                    "timing": {
                        "node_timings_ms": [
                            {
                                "node_key": "model",
                                "node_type": "inference.classification",
                                "total_ms": 20,
                            }
                        ]
                    },
                },
                elapsed_ms=22,
            )
    clock[0] = 101.0
    metrics = counters.snapshot()
    assert metrics.source_fps == 100
    assert metrics.processed_fps == 25  # Not 1000 / pipeline latency.
    assert metrics.acquired_frames == 100
    assert metrics.processed_frames == 25
    assert metrics.sampled_out_frames == 75
    assert metrics.nodes[0].mean_ms == 20
    clock[0] = 112.0
    idle = counters.snapshot()
    assert idle.source_fps == idle.processed_fps == 0
    assert idle.nodes == idle.stages == []
    assert idle.processed_frames == 25


def test_timing_samples_are_bounded_support_both_executors_and_count_errors() -> None:
    clock = [0.0]
    counters = EdgePerformanceCounters(clock=lambda: clock[0])
    for value in range(300):
        clock[0] += 0.001
        counters.execution(
            {
                "status": "success",
                "timing": {
                    "nodes": [
                        {"node_key": "legacy", "duration_ms": value},
                        {"node_key": "invalid", "duration_ms": float("nan")},
                    ]
                },
            },
            elapsed_ms=value,
        )
    counters.execution(
        {
            "status": "failed",
            "timing": {
                "node_timings_ms": [{"node_key": "skipped", "total_ms": 0, "status": "skipped"}]
            },
        },
        elapsed_ms=1,
    )
    counters.execution_error()
    metrics = counters.snapshot()
    legacy = next(node for node in metrics.nodes if node.key == "legacy")
    assert legacy.sample_count == 256
    assert legacy.mean_ms == 171.5
    assert legacy.p95_ms == 287
    assert legacy.max_ms == legacy.last_ms == 299
    assert len(metrics.nodes) == 2
    assert metrics.processed_frames == 300
    assert metrics.execution_errors == 2
    assert next(node for node in metrics.nodes if node.key == "skipped").status == "skipped"
    assert len(counters._rates) <= 102


@pytest.mark.parametrize("mode", ["realtime", "trigger"])
def test_supervisor_reports_performance_without_preview_and_counts_triggers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    store = EdgeStateStore(tmp_path / "state.sqlite3")
    frames = [SourceFrame(index, 1, 1, "2026-09-21T08:00:00Z", object()) for index in range(1, 4)]
    finished = threading.Event()

    class Source:
        def read(self, stop_event: threading.Event) -> SourceFrame | None:
            if frames:
                return frames.pop(0)
            finished.set()
            stop_event.wait(0.01)
            return None

        def close(self) -> None:
            pass

    def execute(frame: SourceFrame, **_kwargs: object) -> dict:
        return {
            "status": "completed",
            "timing": {"node_timings_ms": [{"node_key": "resize", "total_ms": 1.5}]},
        }

    graph = SimpleNamespace(mode=mode, execute=execute, close=lambda: None)
    emitted = []
    supervisor = EdgeRuntimeSupervisor(
        store=store,
        result_sink=lambda *_args: emitted.append(_args[-1]),
        graph_factory=lambda _manifest: graph,
    )
    monkeypatch.setattr(supervisor, "_create_source", lambda _manifest: Source())
    manifest = EdgeDeploymentManifest(
        deployment_id="dep-performance",
        workload_release_id="release-performance",
        channel_id="channel-performance",
        pipeline_version_id="pipeline-performance",
        idempotency_key="prepare-performance",
        created_at="2026-09-21T08:00:00Z",
        runtime_requirements={"frame_sample_every_n": 2},
    )
    try:
        supervisor.start(manifest)
        assert finished.wait(2)
        if mode == "trigger":
            supervisor.trigger(manifest.channel_id, payload={}, correlation_id="trigger-1")
        metrics = supervisor.performance(manifest.deployment_id)
        assert metrics is not None
        assert metrics.acquired_frames == 3
        assert metrics.processed_frames == 1
        assert metrics.sampled_out_frames == (2 if mode == "realtime" else 0)
        assert metrics.nodes[0].key == "resize"
        assert {stage.key for stage in metrics.stages} == {
            "acquisition",
            "pipeline",
            "result_persistence",
        }
        assert len(emitted) == 1
        assert store.processes()[0].state == "running"
        assert supervisor.stop(manifest.deployment_id)
        assert supervisor.performance(manifest.deployment_id) is None
    finally:
        supervisor.close()
        store.close()


def test_old_edge_process_payload_remains_accepted() -> None:
    process = EdgeRuntimeProcess.model_validate({"deployment_id": "dep-old", "state": "running"})
    assert process.metrics is None
    assert process.engines == []


def test_trigger_preview_uses_processed_frame_and_is_not_replaced_by_camera_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EdgeStateStore(tmp_path / "state.sqlite3")
    frames = [SourceFrame(index, 1, 1, "2026-09-21T08:00:00Z", object()) for index in range(1, 4)]
    finished = threading.Event()

    class Source:
        def read(self, stop_event: threading.Event) -> SourceFrame | None:
            if frames:
                return frames.pop(0)
            finished.set()
            stop_event.wait(0.01)
            return None

        def close(self) -> None:
            pass

    previews: list[tuple[SourceFrame, dict]] = []
    graph = SimpleNamespace(
        mode="trigger",
        execute=lambda frame, **_kwargs: {"status": "success", "correlation_id": "cycle-1"},
        close=lambda: None,
    )
    supervisor = EdgeRuntimeSupervisor(
        store=store,
        result_sink=lambda *_args: SimpleNamespace(sequence=1),
        preview_sink=lambda _manifest, frame, result: previews.append((frame, result)),
        preview_demand=lambda _channel_id: True,
        graph_factory=lambda _manifest: graph,
    )
    monkeypatch.setattr(supervisor, "_create_source", lambda _manifest: Source())
    manifest = EdgeDeploymentManifest(
        deployment_id="dep-trigger-preview",
        workload_release_id="release-trigger-preview",
        channel_id="channel-trigger-preview",
        pipeline_version_id="pipeline-trigger-preview",
        idempotency_key="prepare-trigger-preview",
        created_at="2026-09-21T08:00:00Z",
    )
    try:
        supervisor.start(manifest)
        assert finished.wait(2)
        assert previews == []
        supervisor.trigger(manifest.channel_id, payload={}, correlation_id="cycle-1")
        assert len(previews) == 1
        assert previews[0][0].index == 3
        assert previews[0][1]["runtime_metadata"]["trigger_preview"] is True
    finally:
        supervisor.close()
        store.close()
