from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from cv_contracts import EdgeDeploymentManifest, EdgeRuntimeProcess
from cv_contracts.edge import EdgePlantResponsePolicy, EdgeRuntimeMetrics
from cv_runtime.projections import edge_http_response_policy

from edge_agent.cameras import CameraFrame as SourceFrame
from edge_agent.cameras import create_camera_source
from edge_agent.graph import EdgeGraphExecutor
from edge_agent.persistence import EdgeStateStore
from edge_agent.telemetry import EdgePerformanceCounters

_LOGGER = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class EdgeRuntimeTriggerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FrameSource(Protocol):
    def read(self, stop_event: threading.Event) -> SourceFrame | None: ...

    def close(self) -> None: ...


class PassiveSource:
    def __init__(self, interval_seconds: float) -> None:
        self._interval_seconds = interval_seconds
        self._index = 0

    def read(self, stop_event: threading.Event) -> SourceFrame | None:
        if stop_event.wait(self._interval_seconds):
            return None
        self._index += 1
        return SourceFrame(self._index, 0, 0, _utc_now())

    def close(self) -> None:
        return None


ResultSink = Callable[[EdgeDeploymentManifest, SourceFrame, dict[str, Any]], Any]
PreviewSink = Callable[[EdgeDeploymentManifest, SourceFrame, dict[str, Any] | None], None]
PreviewDemand = Callable[[str], bool]
GraphFactory = Callable[[EdgeDeploymentManifest], EdgeGraphExecutor | None]


@dataclass
class _RuntimeWorker:
    executor: EdgeGraphExecutor | None = None
    latest_frame: SourceFrame | None = None
    last_execution: dict[str, Any] | None = None
    execution_lock: threading.Lock = field(default_factory=threading.Lock)
    performance: EdgePerformanceCounters = field(default_factory=EdgePerformanceCounters)


@dataclass
class _WorkerHandle:
    stop_event: threading.Event
    thread: threading.Thread
    manifest: EdgeDeploymentManifest
    runtime: _RuntimeWorker = field(default_factory=_RuntimeWorker)


class EdgeRuntimeSupervisor:
    def __init__(
        self,
        *,
        store: EdgeStateStore,
        result_sink: ResultSink,
        preview_sink: PreviewSink | None = None,
        preview_demand: PreviewDemand | None = None,
        graph_factory: GraphFactory | None = None,
        max_restarts: int = 3,
        base_backoff_seconds: float = 0.25,
    ) -> None:
        self._store = store
        self._result_sink = result_sink
        self._preview_sink = preview_sink
        self._preview_demand = preview_demand
        self._graph_factory = graph_factory
        self._max_restarts = max_restarts
        self._base_backoff_seconds = base_backoff_seconds
        self._handles: dict[str, _WorkerHandle] = {}
        self._lock = threading.RLock()
        self.recovered_deployment_ids: set[str] = set()

    def start(self, manifest: EdgeDeploymentManifest, *, recovered: bool = False) -> None:
        with self._lock:
            existing = self._handles.get(manifest.deployment_id)
            if existing is not None and existing.thread.is_alive():
                return
            stop_event = threading.Event()
            placeholder = threading.Thread()
            handle = _WorkerHandle(stop_event, placeholder, manifest)
            thread = threading.Thread(
                target=self._run,
                args=(handle,),
                name=f"edge-runtime-{manifest.deployment_id}",
                daemon=True,
            )
            handle.thread = thread
            self._handles[manifest.deployment_id] = handle
            if recovered:
                self.recovered_deployment_ids.add(manifest.deployment_id)
            thread.start()

    def stop(self, deployment_id: str, *, timeout_seconds: float = 2.0) -> bool:
        with self._lock:
            handle = self._handles.get(deployment_id)
        if handle is None:
            return True
        handle.stop_event.set()
        if handle.thread is not threading.current_thread():
            handle.thread.join(timeout=timeout_seconds)
        if handle.thread.is_alive():
            # Keep the worker addressable for another stop attempt. In particular,
            # a blocked SDK read can still own the camera after the join timed out.
            return False
        if handle.runtime.executor is not None:
            handle.runtime.executor.close()
            handle.runtime.executor = None
        self._persist(EdgeRuntimeProcess(deployment_id=deployment_id, state="stopped"))
        with self._lock:
            if self._handles.get(deployment_id) is handle:
                del self._handles[deployment_id]
        return True

    def close(self) -> None:
        with self._lock:
            deployment_ids = list(self._handles)
        for deployment_id in deployment_ids:
            self.stop(deployment_id)
        for deployment_id in deployment_ids:
            if not self.stop(deployment_id, timeout_seconds=30):
                raise RuntimeError("An Edge worker did not stop; durable storage remains open.")

    def recover(self, manifests: list[EdgeDeploymentManifest]) -> None:
        for manifest in manifests:
            self.start(manifest, recovered=True)

    def active_channel_ids(self) -> list[str]:
        with self._lock:
            return sorted(
                {
                    handle.manifest.channel_id
                    for handle in self._handles.values()
                    if handle.thread.is_alive()
                }
            )

    def performance(self, deployment_id: str) -> EdgeRuntimeMetrics | None:
        with self._lock:
            handle = self._handles.get(deployment_id)
        return handle.runtime.performance.snapshot() if handle is not None else None

    @staticmethod
    def _execute(
        handle: _WorkerHandle,
        frame: SourceFrame,
        *,
        trigger_payload: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            if handle.runtime.executor is None:
                raise RuntimeError("Edge graph executor is unavailable.")
            result = handle.runtime.executor.execute(
                frame, trigger_payload=trigger_payload, correlation_id=correlation_id
            )
        except Exception:
            handle.runtime.performance.execution_error()
            raise
        handle.runtime.performance.execution(
            result, elapsed_ms=(time.perf_counter() - started) * 1000
        )
        return result

    def _emit_result(
        self, handle: _WorkerHandle, frame: SourceFrame, execution: dict[str, Any]
    ) -> Any:  # noqa: ANN401
        started = time.perf_counter()
        try:
            return self._result_sink(handle.manifest, frame, execution)
        finally:
            handle.runtime.performance.stage(
                "result_persistence", (time.perf_counter() - started) * 1000
            )

    def _emit_preview(
        self, handle: _WorkerHandle, frame: SourceFrame, execution: dict[str, Any] | None
    ) -> None:
        started = time.perf_counter()
        try:
            if self._preview_sink is not None:
                self._preview_sink(handle.manifest, frame, execution)
        finally:
            handle.runtime.performance.stage("preview", (time.perf_counter() - started) * 1000)

    def trigger(
        self,
        channel_id: str,
        *,
        payload: dict[str, Any],
        correlation_id: str,
        idempotency_key: str | None = None,
    ) -> tuple[
        EdgeDeploymentManifest, SourceFrame, dict[str, Any], Any, EdgePlantResponsePolicy
    ]:
        handle = self._handle_for_channel(channel_id)
        runtime = handle.runtime
        frame = runtime.latest_frame
        if frame is None or frame.image is None:
            raise EdgeRuntimeTriggerError(
                "frame_unavailable", "The channel has no current camera frame."
            )
        if runtime.executor is None:
            raise EdgeRuntimeTriggerError(
                "runtime_not_ready", "The channel graph executor is not ready."
            )
        with runtime.execution_lock:
            graph = getattr(runtime.executor, "graph", None)
            nodes = graph.nodes if graph is not None else []
            if idempotency_key is None and any(
                node.node_type == "trigger.http_put"
                and node.config.get("require_idempotency_key", False)
                for node in nodes
            ):
                raise EdgeRuntimeTriggerError(
                    "trigger_idempotency_key_required",
                    "This HTTP Trigger node requires the X-Idempotency-Key header.",
                )
            try:
                response_policy = edge_http_response_policy(nodes)
            except ValueError as exc:
                raise EdgeRuntimeTriggerError("trigger_configuration_invalid", str(exc)) from exc
            execution = self._execute(
                handle,
                frame,
                trigger_payload=payload,
                correlation_id=correlation_id,
            )
            runtime.last_execution = execution
            envelope = self._emit_result(handle, frame, execution)
            if self._preview_sink is not None:
                preview_execution = {
                    **execution,
                    "runtime_metadata": {
                        **dict(execution.get("runtime_metadata") or {}),
                        "trigger_preview": True,
                    },
                }
                try:
                    self._emit_preview(handle, frame, preview_execution)
                except Exception:
                    _LOGGER.exception("Could not save trigger preview for channel %s", channel_id)
            return handle.manifest, frame, execution, envelope, response_policy

    def last_execution(self, channel_id: str) -> dict[str, Any] | None:
        handle = self._handle_for_channel(channel_id)
        return handle.runtime.last_execution

    def _handle_for_channel(self, channel_id: str) -> _WorkerHandle:
        with self._lock:
            matching = [
                handle
                for handle in self._handles.values()
                if handle.manifest.channel_id == channel_id
                and handle.thread.is_alive()
                and not handle.stop_event.is_set()
            ]
        if not matching:
            raise EdgeRuntimeTriggerError(
                "channel_not_running", f"No active edge workload exists for channel {channel_id}."
            )
        return matching[-1]

    def _run(self, handle: _WorkerHandle) -> None:
        try:
            self._run_worker(handle)
        finally:
            with handle.runtime.execution_lock:
                if handle.runtime.executor is not None:
                    handle.runtime.executor.close()
                    handle.runtime.executor = None

    def _run_worker(self, handle: _WorkerHandle) -> None:
        manifest, stop_event = handle.manifest, handle.stop_event
        restart_count = 0
        while not stop_event.is_set():
            started_at = _utc_now()
            self._persist(
                EdgeRuntimeProcess(
                    deployment_id=manifest.deployment_id,
                    state="starting",
                    restart_count=restart_count,
                    started_at=started_at,
                )
            )
            source: FrameSource | None = None
            try:
                if handle.runtime.executor is not None:
                    handle.runtime.executor.close()
                handle.runtime.executor = (
                    self._graph_factory(manifest) if self._graph_factory is not None else None
                )
                if stop_event.is_set():
                    break
                source = self._create_source(manifest)
                if stop_event.is_set():
                    break
                self._persist(
                    EdgeRuntimeProcess(
                        deployment_id=manifest.deployment_id,
                        state="running",
                        restart_count=restart_count,
                        started_at=started_at,
                        last_heartbeat_at=_utc_now(),
                    )
                )
                self._process_frames(handle, source, restart_count, started_at)
                if stop_event.is_set():
                    break
                raise RuntimeError("Edge source worker exited unexpectedly.")
            except Exception as exc:  # noqa: BLE001 - worker boundary persists all failures
                if stop_event.is_set():
                    break
                restart_count += 1
                if restart_count > self._max_restarts:
                    self._persist(
                        EdgeRuntimeProcess(
                            deployment_id=manifest.deployment_id,
                            state="crash_loop",
                            restart_count=restart_count,
                            started_at=started_at,
                            last_error=str(exc),
                        )
                    )
                    return
                backoff = self._base_backoff_seconds * (2 ** (restart_count - 1))
                next_restart = (datetime.now(UTC) + timedelta(seconds=backoff)).isoformat()
                self._persist(
                    EdgeRuntimeProcess(
                        deployment_id=manifest.deployment_id,
                        state="backoff",
                        restart_count=restart_count,
                        started_at=started_at,
                        next_restart_at=next_restart,
                        last_error=str(exc),
                    )
                )
                stop_event.wait(backoff)
            finally:
                if source is not None:
                    source.close()
        self._persist(
            EdgeRuntimeProcess(
                deployment_id=manifest.deployment_id,
                state="stopped",
                restart_count=restart_count,
            )
        )

    def _process_frames(
        self,
        handle: _WorkerHandle,
        source: FrameSource,
        restart_count: int,
        started_at: str,
    ) -> None:
        manifest, stop_event, runtime = handle.manifest, handle.stop_event, handle.runtime
        requirements = manifest.runtime_requirements
        sample_every = max(int(requirements.get("frame_sample_every_n", 1)), 1)
        emit_results = bool(requirements.get("emit_source_results", False))
        fail_after = requirements.get("simulate_runtime_failure_after_frames")
        mode = runtime.executor.mode if runtime.executor is not None else "realtime"
        processed = 0
        last_persisted = time.monotonic()
        while not stop_event.is_set():
            acquired_started = time.perf_counter()
            frame = source.read(stop_event)
            runtime.performance.stage(
                "acquisition", (time.perf_counter() - acquired_started) * 1000
            )
            if stop_event.is_set():
                break
            if time.monotonic() - last_persisted >= 1.0:
                self._persist(
                    EdgeRuntimeProcess(
                        deployment_id=manifest.deployment_id,
                        state="running",
                        restart_count=restart_count,
                        started_at=started_at,
                        last_heartbeat_at=_utc_now(),
                    )
                )
                last_persisted = time.monotonic()
            if frame is None:
                # A triggered camera can remain idle while the workload stays healthy.
                # Also bound polling for adapters that return immediately.
                stop_event.wait(0.01)
                continue
            runtime.latest_frame = frame
            runtime.performance.acquired_frame(
                frame.captured_at,
                sampled_out=mode != "trigger" and frame.index % sample_every != 0,
            )
            execution: dict[str, Any] | None = runtime.last_execution
            if frame.index % sample_every == 0 and mode != "trigger":
                processed += 1
                if runtime.executor is not None:
                    with runtime.execution_lock:
                        execution = self._execute(handle, frame)
                        runtime.last_execution = execution
                    self._emit_result(handle, frame, execution)
                    if execution.get("status") == "failed":
                        raise RuntimeError(
                            "Runtime pipeline execution failed; inspect node results."
                        )
                elif emit_results:
                    execution = {
                        "source_frame_index": frame.index,
                        "width": frame.width,
                        "height": frame.height,
                        "captured_at": frame.captured_at,
                        "sample_every_n": sample_every,
                    }
                    self._emit_result(handle, frame, execution)
                if fail_after is not None and processed >= int(fail_after):
                    raise RuntimeError("Simulated edge runtime worker failure.")
            if (
                mode != "trigger"
                and frame.image is not None
                and self._preview_sink is not None
                and self._preview_demand is not None
                and self._preview_demand(manifest.channel_id)
            ):
                self._emit_preview(handle, frame, execution)

    @staticmethod
    def _create_source(manifest: EdgeDeploymentManifest) -> FrameSource:
        source = manifest.runtime_requirements.get("source", {})
        if not isinstance(source, dict):
            raise RuntimeError("runtime_requirements.source must be an object.")
        source_type = str(source.get("type", "passive"))
        if source_type in {"passive", "synthetic"}:
            interval_ms = max(int(source.get("interval_ms", 1000)), 1)
            return PassiveSource(interval_ms / 1000)
        return create_camera_source(source)

    def _persist(self, process: EdgeRuntimeProcess) -> None:
        self._store.save_process(process, updated_at=_utc_now())
