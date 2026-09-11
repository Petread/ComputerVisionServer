from __future__ import annotations

import base64
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from cv_contracts.runtime import (
    RuntimeCameraIntrinsicCalibrationProfile,
    RuntimeGraphEdge,
    RuntimeImageViewProfile,
    RuntimeLiveDiagnostics,
    RuntimeLiveFrame,
    RuntimeLiveMetrics,
    RuntimeLiveSourceHealth,
    RuntimeLiveViewState,
    RuntimeLogicalModelRef,
    RuntimeNodeSpec,
    RuntimeOverlayPrimitiveState,
    RuntimeResultHistoryPolicy,
    RuntimeStemRotationPriorityControl,
    RuntimeStemRotationViewModel,
)
from cv_core import PlatformSettings
from cv_db import (
    RuntimeChannel,
    RuntimeExecution,
    build_runtime_model_resolver,
    clear_runtime_overlay_event,
    get_ready_runtime_execution_binding,
    mark_runtime_execution_failed_retryable,
    record_runtime_execution_heartbeat,
    record_runtime_execution_result,
    session_scope,
    write_runtime_live_view_state,
    write_runtime_overlay_event,
)
from cv_runtime import (
    ExecutablePipelineGraph,
    HttpInferenceGatewayClient,
    RuntimeExecutionContext,
    RuntimeInferenceClient,
    RuntimeModelBinding,
    apply_camera_distortion,
    undistorted_points_to_raw,
    validate_camera_intrinsic_calibration,
    validate_image_view_profile,
    view_overlay_geometry_to_source,
)
from cv_runtime.high_rate_phase import (
    HighRatePhaseConfig,
    HighRatePhaseFrame,
    HighRateStemPhaseBridge,
)
from cv_runtime.nodes.camera_undistort import preprocessing_camera_undistort
from cv_runtime.nodes.stem_process_coordinates import (
    preprocessing_process_coordinate_rectify,
)
from cv_runtime.payloads import FramePayload, RuntimeExecutionResult
from cv_runtime.siemens_sps import close_siemens_sps_connection_cache
from cv_status import (
    HealthState,
    RuntimeChannelDesiredState,
    RuntimeMode,
    RuntimeObservedState,
    RuntimeSourceType,
)
from cv_utils import utc_now_iso
from sqlalchemy.orm import Session

from realtime_executor import SERVICE_NAME
from realtime_executor.sources import (
    FramePacket,
    RealtimeFrameSource,
    SharedMemoryFrameSource,
    build_frame_source,
)
from realtime_executor.temporal_samples import (
    RealtimeTemporalSampleRecorder,
    attach_temporal_capture_plans,
    temporal_recorder_config_from_graph,
)

RealtimeSourceFactory = Callable[[str, dict[str, Any], dict[str, str]], RealtimeFrameSource]
InferenceClientFactory = Callable[[PlatformSettings], RuntimeInferenceClient]

_STATEFUL_PIPELINE_NODE_TYPES = frozenset(
    {
        "preprocessing.frame_sampler",
        "preprocessing.camera_undistort",
        "analysis.zone_entry_exit",
        "analysis.optical_flow_motion",
        "analysis.active_stem_tracker",
        "analysis.stem_surface_rotation",
        "analysis.stem_horizontal_curvature",
        "analysis.stem_surface_phase",
        "analysis.stem_slice_model",
        "analysis.stem_curvature_phase_solver",
        "analysis.stem_temporal_orientation",
        "analysis.stem_orientation_model_decision",
        "analysis.board_view_cut_constraints",
        "output.siemens_sps_write",
    }
)

_INFERENCE_PIPELINE_NODE_PREFIX = "inference."
_STEM_MEASUREMENT_PARALLEL_NODE_TYPES = (
    "analysis.stem_measurement_eligibility",
    "analysis.stem_mask_geometry",
    "analysis.stem_horizontal_curvature",
    "analysis.stem_surface_phase",
)
_ORDERED_STEM_OVERLAP_STATEFUL_NODE_TYPES = frozenset(
    {
        # The undistort handler only maintains a locked remap cache.  It is part of the
        # overlapped pure prefix and is intentionally not an order-barrier node.
        "preprocessing.camera_undistort",
        "analysis.active_stem_tracker",
        "analysis.stem_horizontal_curvature",
        "analysis.stem_surface_phase",
        "analysis.stem_slice_model",
    }
)
LOGGER = logging.getLogger(__name__)
_LIVE_STATE_WRITE_MIN_INTERVAL_SECONDS = 0.2
_METRICS_WINDOW_SECONDS = 10.0


@dataclass
class RealtimeMetrics:
    acquired_frame_count: int = 0
    processed_frame_count: int = 0
    dropped_frame_count: int = 0
    buffer_dropped_frame_count: int = 0
    inference_dropped_frame_count: int = 0
    pipeline_deadline_dropped_frame_count: int = 0
    skipped_frame_count: int = 0
    reconnect_count: int = 0
    source_error_count: int = 0
    last_frame_at: str | None = None
    last_error: str | None = None
    buffer_depth: int = 0
    inflight_count: int = 0
    oldest_inflight_ms: float | None = None
    latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=200))
    stage_timings_ms: dict[str, float] = field(default_factory=dict)
    source_read_ms: float | None = None
    source_frame_interval_ms: float | None = None
    started_perf: float = field(default_factory=time.perf_counter)
    last_heartbeat_perf: float = 0.0
    last_source_frame_perf: float = 0.0
    last_processed_perf: float = 0.0
    acquired_events: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=2_000))
    processed_events: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=2_000))
    dropped_events: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=2_000))
    skipped_events: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=2_000))

    def record_processed(self, latency_ms: float, *, frame_at: str | None) -> None:
        self.processed_frame_count += 1
        self.latencies_ms.append(latency_ms)
        self.last_frame_at = frame_at or utc_now_iso()
        self.last_processed_perf = time.perf_counter()
        self.processed_events.append((self.last_processed_perf, 1))

    def record_acquired(self) -> None:
        self.acquired_frame_count += 1
        self.acquired_events.append((time.perf_counter(), 1))

    def record_buffer_drops(self, dropped_count: int) -> None:
        previous = self.buffer_dropped_frame_count
        self.buffer_dropped_frame_count = max(int(dropped_count), 0)
        delta = max(self.buffer_dropped_frame_count - previous, 0)
        if delta:
            self.dropped_events.append((time.perf_counter(), delta))
        self._sync_drop_total()

    def record_inference_drop(self) -> None:
        self.inference_dropped_frame_count += 1
        self.dropped_events.append((time.perf_counter(), 1))
        self._sync_drop_total()

    def record_pipeline_deadline_drop(self) -> None:
        self.pipeline_deadline_dropped_frame_count += 1
        self.record_inference_drop()

    def record_skipped(self, count: int = 1) -> None:
        count = max(int(count), 0)
        if not count:
            return
        self.skipped_frame_count += count
        self.skipped_events.append((time.perf_counter(), count))

    def _sync_drop_total(self) -> None:
        self.dropped_frame_count = (
            self.buffer_dropped_frame_count + self.inference_dropped_frame_count
        )

    def record_stage_timings(self, timings_ms: dict[str, float]) -> None:
        merged_timings = dict(timings_ms)
        if self.source_read_ms is not None:
            merged_timings["source_read_ms"] = self.source_read_ms
        if self.source_frame_interval_ms is not None:
            merged_timings["source_frame_interval_ms"] = self.source_frame_interval_ms
        self.stage_timings_ms = {
            key: round(max(value, 0.0), 3) for key, value in merged_timings.items() if value >= 0
        }

    def record_source_read(self, latency_ms: float) -> None:
        self.source_read_ms = round(max(latency_ms, 0.0), 3)

    def record_source_frame(self) -> None:
        now = time.perf_counter()
        if self.last_source_frame_perf:
            self.source_frame_interval_ms = round(
                max((now - self.last_source_frame_perf) * 1000, 0.0),
                3,
            )
        self.last_source_frame_perf = now

    def to_live_metrics(self) -> RuntimeLiveMetrics:
        now = time.perf_counter()
        elapsed = max(min(now - self.started_perf, _METRICS_WINDOW_SECONDS), 0.001)
        acquired_recent = _window_event_count(self.acquired_events, now=now)
        processed_recent = _window_event_count(self.processed_events, now=now)
        dropped_recent = _window_event_count(self.dropped_events, now=now)
        skipped_recent = _window_event_count(self.skipped_events, now=now)
        total_seen_recent = processed_recent + dropped_recent + skipped_recent
        latest_latency = self.latencies_ms[-1] if self.latencies_ms else None
        return RuntimeLiveMetrics(
            fps=round(processed_recent / elapsed, 3),
            source_fps=round(acquired_recent / elapsed, 3),
            processed_fps=round(processed_recent / elapsed, 3),
            drop_rate_percent=round(
                (dropped_recent / max(total_seen_recent, 1)) * 100,
                3,
            ),
            latency_ms=round(latest_latency, 3) if latest_latency is not None else None,
            latency_p50_ms=_percentile(self.latencies_ms, 50),
            latency_p95_ms=_percentile(self.latencies_ms, 95),
            latency_p99_ms=_percentile(self.latencies_ms, 99),
            frame_count=self.processed_frame_count,
            acquired_frame_count=self.acquired_frame_count,
            dropped_frame_count=self.dropped_frame_count,
            reconnect_count=self.reconnect_count,
            source_error_count=self.source_error_count,
            buffer_depth=self.buffer_depth,
            output_stream_fps=None,
            stream_dropped_frame_count=0,
            encode_latency_ms=None,
            stream_publication_latency_ms=None,
            connected_clients=0,
            gpu_encoder_available=False,
            last_frame_at=self.last_frame_at,
            stage_timings_ms=dict(self.stage_timings_ms),
        )

    def to_db_metrics(self) -> dict[str, Any]:
        live = self.to_live_metrics()
        return {
            **live.model_dump(mode="json"),
            "metrics_source": "realtime_executor",
            "execution_mode": RuntimeMode.REALTIME.value,
            "drop_count": live.dropped_frame_count,
            "buffer_drop_count": self.buffer_dropped_frame_count,
            "inference_drop_count": self.inference_dropped_frame_count,
            "pipeline_deadline_drop_count": self.pipeline_deadline_dropped_frame_count,
            "skipped_frame_count": self.skipped_frame_count,
            "inflight_count": self.inflight_count,
            "oldest_inflight_ms": self.oldest_inflight_ms,
        }


@dataclass(frozen=True)
class _PipelineWorkResult:
    packet: FramePacket
    frame_payload: FramePayload
    result: RuntimeExecutionResult
    stage_timings_ms: dict[str, float]
    queued_perf: float


@dataclass
class _PendingPipelineWork:
    sequence: int
    queued_perf: float
    deadline_perf: float
    deadline_recorded: bool = False


class LatestFrameBuffer:
    def __init__(self, *, max_size: int, drop_policy: str) -> None:
        self.max_size = max(max_size, 1)
        self.drop_policy = drop_policy
        self._items: deque[FramePacket] = deque()
        self._condition = threading.Condition()
        self.dropped_count = 0

    def push(self, frame: FramePacket) -> None:
        with self._condition:
            if len(self._items) >= self.max_size:
                if self.drop_policy == "drop_newest":
                    self.dropped_count += 1
                    self._condition.notify_all()
                    return
                self._items.popleft()
                self.dropped_count += 1
            self._items.append(frame)
            self._condition.notify_all()

    def pop_latest(self, *, timeout_seconds: float) -> FramePacket | None:
        with self._condition:
            if not self._items:
                self._condition.wait(timeout=timeout_seconds)
            if not self._items:
                return None
            while len(self._items) > 1:
                self._items.popleft()
                self.dropped_count += 1
            return self._items.pop()

    def depth(self) -> int:
        with self._condition:
            return len(self._items)

    def notify(self) -> None:
        with self._condition:
            self._condition.notify_all()


@dataclass(frozen=True)
class _QueuedHighRateFrame:
    packet: FramePacket
    received_perf: float


class _OrderedHighRateFrameBuffer:
    """Small ordered queue; overload drops old work instead of growing latency."""

    def __init__(self, max_size: int) -> None:
        self._items: deque[_QueuedHighRateFrame] = deque()
        self._max_size = max(int(max_size), 1)
        self._condition = threading.Condition()
        self._closed = False

    def push(self, packet: FramePacket) -> int:
        dropped = 0
        with self._condition:
            if self._closed:
                return 0
            while len(self._items) >= self._max_size:
                self._items.popleft()
                dropped += 1
            self._items.append(
                _QueuedHighRateFrame(packet=packet, received_perf=time.perf_counter())
            )
            self._condition.notify()
        return dropped

    def pop(self, timeout_seconds: float = 0.1) -> _QueuedHighRateFrame | None:
        with self._condition:
            if not self._items and not self._closed:
                self._condition.wait(timeout=timeout_seconds)
            if not self._items:
                return None
            return self._items.popleft()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def clear(self) -> int:
        with self._condition:
            count = len(self._items)
            self._items.clear()
            return count


class _OrderedStatefulNodeGate:
    """Keep each selected stateful node ordered without serializing unrelated stages.

    Frames are registered before submission for every configured node. A later frame may enter
    its tracker as soon as the older tracker completed, even while older geometry is still being
    calculated. The same frame order is independently enforced again at curvature, phase, and
    slice-model state. ``complete_frame`` is a failure/cancellation safety net for nodes that a
    work item never reached.
    """

    def __init__(self, node_keys: tuple[str, ...]) -> None:
        if not node_keys or len(set(node_keys)) != len(node_keys):
            raise ValueError("Ordered stateful node keys must be unique and non-empty.")
        self._condition = threading.Condition()
        self._node_keys = tuple(node_keys)
        self._pending_by_node = {node_key: set() for node_key in self._node_keys}
        self._active_by_node: dict[str, int | None] = {
            node_key: None for node_key in self._node_keys
        }
        self._registered_sequences: set[int] = set()
        self._closed = False

    def register(self, sequence: int) -> None:
        sequence = int(sequence)
        with self._condition:
            if self._closed:
                raise RuntimeError("Ordered stateful node gate is closed.")
            if sequence in self._registered_sequences:
                raise RuntimeError(f"Frame sequence {sequence} is already registered.")
            self._registered_sequences.add(sequence)
            for pending in self._pending_by_node.values():
                pending.add(sequence)
            self._condition.notify_all()

    def enter(self, sequence: int, node_key: str) -> float:
        sequence = int(sequence)
        started = time.perf_counter()
        with self._condition:
            pending = self._pending_for(node_key)
            while True:
                if self._closed:
                    raise RuntimeError("Ordered stateful node gate closed while waiting.")
                if sequence not in pending:
                    raise RuntimeError(
                        f"Frame sequence {sequence} entered node {node_key} without registration."
                    )
                if self._active_by_node[node_key] is None and sequence == min(pending):
                    self._active_by_node[node_key] = sequence
                    return (time.perf_counter() - started) * 1000
                self._condition.wait(timeout=0.1)

    def complete_node(self, sequence: int, node_key: str) -> None:
        sequence = int(sequence)
        with self._condition:
            pending = self._pending_for(node_key)
            pending.discard(sequence)
            if self._active_by_node[node_key] == sequence:
                self._active_by_node[node_key] = None
            self._forget_completed_sequence(sequence)
            self._condition.notify_all()

    def complete_frame(self, sequence: int) -> None:
        sequence = int(sequence)
        with self._condition:
            for node_key, pending in self._pending_by_node.items():
                pending.discard(sequence)
                if self._active_by_node[node_key] == sequence:
                    self._active_by_node[node_key] = None
            self._registered_sequences.discard(sequence)
            self._condition.notify_all()

    def _pending_for(self, node_key: str) -> set[int]:
        try:
            return self._pending_by_node[node_key]
        except KeyError as exc:
            raise RuntimeError(f"Node {node_key} is not registered with the ordered gate.") from exc

    def _forget_completed_sequence(self, sequence: int) -> None:
        if all(sequence not in pending for pending in self._pending_by_node.values()):
            self._registered_sequences.discard(sequence)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            for pending in self._pending_by_node.values():
                pending.clear()
            for node_key in self._active_by_node:
                self._active_by_node[node_key] = None
            self._registered_sequences.clear()
            self._condition.notify_all()


class _HighRateStemPhaseRuntime:
    """Minimal camera-to-canonical worker feeding the V2 phase bridge."""

    def __init__(
        self,
        *,
        channel_id: str,
        runtime_execution_id: str,
        pipeline_version_id: str,
        source_config: dict[str, Any],
        undistort_node: RuntimeNodeSpec,
        canonical_node: RuntimeNodeSpec,
        phase_node: RuntimeNodeSpec,
    ) -> None:
        self.channel_id = channel_id
        self.runtime_execution_id = runtime_execution_id
        self.pipeline_version_id = pipeline_version_id
        self.source_config = source_config
        self.undistort_node = undistort_node
        self.canonical_node = canonical_node
        self.phase_node = phase_node
        self.config = HighRatePhaseConfig.from_mapping(phase_node.config or {})
        self.bridge = HighRateStemPhaseBridge(self.config)
        self._buffer = _OrderedHighRateFrameBuffer(self.config.phase_frame_queue_size)
        self._state: dict[str, Any] = {}
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._last_error: str | None = None
        self._rate_lock = threading.Lock()
        self._rate_tokens = 1.0
        self._last_source_perf = 0.0
        self._last_source_reconnect_count = 0

    @classmethod
    def from_graph(
        cls,
        *,
        channel_id: str,
        runtime_execution_id: str,
        pipeline_version_id: str,
        source_config: dict[str, Any],
        nodes: Iterable[RuntimeNodeSpec],
        edges: Iterable[RuntimeGraphEdge],
    ) -> _HighRateStemPhaseRuntime | None:
        node_list = list(nodes)
        edge_list = list(edges)
        phase_nodes = [
            node
            for node in node_list
            if node.node_type == "analysis.stem_surface_phase"
            and str((node.config or {}).get("phase_estimator_version") or "")
            == "CYLINDRICAL_SURFACE_V2"
            and str((node.config or {}).get("execution_mode") or "")
            == "HIGH_RATE_SEEDED"
        ]
        if not phase_nodes:
            return None
        if len(phase_nodes) != 1:
            raise RuntimeError(
                "High-rate stem phase requires exactly one V2 surface-phase node."
            )
        nodes_by_key = {node.node_key: node for node in node_list}
        upstream_keys: set[str] = set()
        pending = deque([phase_nodes[0].node_key])
        while pending:
            current = pending.popleft()
            for edge in edge_list:
                if edge.to_node_key != current or edge.from_node_key in upstream_keys:
                    continue
                upstream_keys.add(edge.from_node_key)
                pending.append(edge.from_node_key)
        undistort_nodes = [
            nodes_by_key[key]
            for key in upstream_keys
            if nodes_by_key[key].node_type == "preprocessing.camera_undistort"
        ]
        canonical_nodes = [
            nodes_by_key[key]
            for key in upstream_keys
            if nodes_by_key[key].node_type == "preprocessing.process_coordinate_rectify"
        ]
        if len(undistort_nodes) != 1 or len(canonical_nodes) != 1:
            raise RuntimeError(
                "High-rate stem phase requires one unique upstream camera-undistort and "
                "canonical-view node."
            )
        return cls(
            channel_id=channel_id,
            runtime_execution_id=runtime_execution_id,
            pipeline_version_id=pipeline_version_id,
            source_config=source_config,
            undistort_node=undistort_nodes[0],
            canonical_node=canonical_nodes[0],
            phase_node=phase_nodes[0],
        )

    def start(self, stop_event: threading.Event) -> None:
        self._stop_event = stop_event
        self._thread = threading.Thread(
            target=self._run,
            name=f"stem-phase-fast-{self.runtime_execution_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def submit(self, packet: FramePacket) -> None:
        self.bridge.record_source_frame()
        now = time.perf_counter()
        with self._rate_lock:
            if self._last_source_perf:
                elapsed = max(now - self._last_source_perf, 0.0)
                self._rate_tokens = min(
                    self._rate_tokens + elapsed * self.config.target_phase_fps,
                    2.0,
                )
            self._last_source_perf = now
            if self._rate_tokens < 1.0:
                self.bridge.record_rate_skip()
                return
            self._rate_tokens -= 1.0
        dropped = self._buffer.push(packet)
        if dropped:
            self.bridge.record_queue_drop(dropped)

    def observe_source_reconnect_count(self, reconnect_count: int) -> None:
        reconnect_count = max(int(reconnect_count), 0)
        if reconnect_count <= self._last_source_reconnect_count:
            return
        self._last_source_reconnect_count = reconnect_count
        dropped = self._buffer.clear()
        self.bridge.reset("PHASE_CAMERA_RECONNECT_RESET")
        if dropped:
            self.bridge.record_queue_drop(dropped)

    def close(self) -> None:
        self._buffer.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.bridge.reset("PHASE_HIGH_RATE_WORKER_STOPPED")

    def metrics(self) -> dict[str, float]:
        return self.bridge.metrics()

    def _run(self) -> None:
        while self._stop_event is not None and not self._stop_event.is_set():
            queued = self._buffer.pop()
            if queued is None:
                continue
            packet = queued.packet
            try:
                frame = _frame_payload_from_packet(
                    packet,
                    runtime_execution_id=self.runtime_execution_id,
                )
                context = RuntimeExecutionContext(
                    correlation_id=(
                        f"live_phase_fast_{self.runtime_execution_id}_{packet.sequence}"
                    ),
                    channel_id=self.channel_id,
                    pipeline_version_id=self.pipeline_version_id,
                    runtime_execution_id=self.runtime_execution_id,
                    mode=RuntimeMode.REALTIME,
                    source_config=self.source_config,
                    input_frame=frame,
                    state=self._state,
                    metadata={
                        "executor_service": SERVICE_NAME,
                        "live_frame_sequence": packet.sequence,
                        "materialize_frame_node_outputs": False,
                        "materialize_live_display_frame": False,
                        "high_rate_phase_preprocessing": True,
                    },
                )
                undistorted = preprocessing_camera_undistort.execute(
                    context,
                    self.undistort_node,
                    {"frame": frame},
                ).outputs.get("frame")
                if not isinstance(undistorted, FramePayload):
                    raise RuntimeError("High-rate undistort did not produce a frame.")
                canonical = preprocessing_process_coordinate_rectify.execute(
                    context,
                    self.canonical_node,
                    {"frame": undistorted},
                ).outputs.get("frame")
                if not isinstance(canonical, FramePayload):
                    raise RuntimeError("High-rate canonical view did not produce a frame.")
                image = canonical.cached_cv2_frame()
                if image is None:
                    raise RuntimeError("High-rate canonical frame was unexpectedly materialized.")
                self.bridge.process_frame(
                    HighRatePhaseFrame(
                        image_bgr=image,
                        frame_sequence=packet.sequence,
                        timestamp=packet.source_timestamp,
                        received_perf=queued.received_perf,
                    )
                )
                self._last_error = None
            except Exception as exc:
                self._last_error = str(exc)
                self.bridge.record_processing_failure(
                    frame_sequence=packet.sequence,
                    timestamp=packet.source_timestamp,
                )
                LOGGER.warning(
                    "High-rate stem phase frame failed: channel_id=%s sequence=%s error=%s",
                    self.channel_id,
                    packet.sequence,
                    exc,
                )


class RealtimeRuntimeWorker:
    def __init__(
        self,
        *,
        settings: PlatformSettings,
        runtime_execution_id: str,
        channel_id: str,
        executor_instance_id: str,
        source_factory: RealtimeSourceFactory | None = None,
        inference_client_factory: InferenceClientFactory | None = None,
    ) -> None:
        self.settings = settings
        self.runtime_execution_id = runtime_execution_id
        self.channel_id = channel_id
        self.executor_instance_id = executor_instance_id
        self.source_factory = source_factory or _default_source_factory
        self._uses_shared_camera_source = source_factory is None
        self.inference_client_factory = inference_client_factory or _default_inference_client
        self.metrics = RealtimeMetrics()
        self._last_runtime_state_check_perf = 0.0
        self._last_source_wait_log_perf = 0.0
        self._last_live_state_write_perf = 0.0
        self._last_history_persist_perf = 0.0
        self._last_history_event_key: str | None = None
        self._result_history_policy = RuntimeResultHistoryPolicy()
        self._stem_rotation_priority_control = (
            RuntimeStemRotationPriorityControl().model_dump(mode="json")
        )
        self._shadow_mode_enabled = False
        self._first_frame_logged = False
        self._temporal_sample_recorder: RealtimeTemporalSampleRecorder | None = None
        self._high_rate_phase_runtime: _HighRateStemPhaseRuntime | None = None

    def run(self, stop_event: threading.Event) -> None:
        source: RealtimeFrameSource | None = None
        acquisition_thread: threading.Thread | None = None
        heartbeat_thread: threading.Thread | None = None
        inference_client: RuntimeInferenceClient | None = None
        pipeline_executor: ThreadPoolExecutor | None = None
        ordered_stateful_node_gate: _OrderedStatefulNodeGate | None = None
        buffer = LatestFrameBuffer(
            max_size=self.settings.runtime.realtime_frame_buffer_size,
            drop_policy=self.settings.runtime.realtime_drop_policy,
        )
        pipeline_state: dict[str, Any] = {}
        model_resolver = _CachedRuntimeModelResolver(settings=self.settings)
        try:
            with session_scope(self.settings) as session:
                execution, loaded = get_ready_runtime_execution_binding(
                    session,
                    channel_id=self.channel_id,
                    expected_mode=RuntimeMode.REALTIME,
                )
                source_type = loaded.source.source_type if loaded.source is not None else "rtsp"
                source_config = (
                    dict(loaded.source.config or {}) if loaded.source is not None else {}
                )
                source_secret_refs = (
                    dict(loaded.source.secret_refs or {}) if loaded.source is not None else {}
                )
                graph = loaded.graph
                graph_nodes = [node.model_dump(mode="json") for node in graph.nodes]
                self._shadow_mode_enabled = _stem_shadow_mode_enabled(graph_nodes)
                temporal_recorder_config = temporal_recorder_config_from_graph(
                    graph_nodes,
                    default_retention_days=(
                        self.settings.artifact_retention.runtime_temporal_sample_days
                    ),
                )
                if temporal_recorder_config is not None:
                    self._temporal_sample_recorder = RealtimeTemporalSampleRecorder(
                        capture_root=self.settings.storage.runtime_capture_root(),
                        config=temporal_recorder_config,
                        queue_capacity=(
                            self.settings.runtime.temporal_sample_spool_queue_capacity
                        ),
                    )
                test_photo_config = _test_photo_source_config(graph_nodes)
                if test_photo_config is not None:
                    source_type = "file_test"
                    source_config = test_photo_config
                    source_secret_refs = {}
                uses_shared_camera_source = (
                    self._uses_shared_camera_source and test_photo_config is None
                )
                executable = loaded.executable
                pipeline_version_id = loaded.version.id
                self._result_history_policy = RuntimeResultHistoryPolicy.model_validate(
                    loaded.channel.result_history_policy or {}
                )
                self._stem_rotation_priority_control = (
                    RuntimeStemRotationPriorityControl.model_validate(
                        loaded.channel.stem_rotation_priority_control or {}
                    ).model_dump(mode="json")
                )
            model_resolver.preload(
                node.logical_model_ref for node in graph.nodes if node.logical_model_ref is not None
            )
            self._high_rate_phase_runtime = _HighRateStemPhaseRuntime.from_graph(
                channel_id=self.channel_id,
                runtime_execution_id=self.runtime_execution_id,
                pipeline_version_id=pipeline_version_id,
                source_config=source_config,
                nodes=graph.nodes,
                edges=graph.edges,
            )
            LOGGER.info(
                "Starting realtime runtime worker: channel_id=%s runtime_execution_id=%s "
                "source_type=%s source_config=%s",
                self.channel_id,
                self.runtime_execution_id,
                source_type,
                _source_config_diagnostic_summary(source_type, source_config),
            )
            source = (
                SharedMemoryFrameSource(
                    runtime_dir=self.settings.runtime.camera_media_runtime_dir,
                    channel_id=self.channel_id,
                    source_type=source_type,
                )
                if uses_shared_camera_source
                else self.source_factory(source_type, source_config, source_secret_refs)
            )
            shared_source_startup_timeout = max(
                float(self.settings.runtime.camera_media_stale_after_seconds),
                3.0,
            )
            shared_source_first_frame_deadline = (
                time.perf_counter() + shared_source_startup_timeout
                if uses_shared_camera_source
                else None
            )
            inference_client = self.inference_client_factory(self.settings)
            if self._high_rate_phase_runtime is not None:
                self._high_rate_phase_runtime.start(stop_event)
            acquisition_thread = threading.Thread(
                target=self._acquire_frames,
                name=f"live-source-{self.runtime_execution_id}",
                kwargs={
                    "source": source,
                    "buffer": buffer,
                    "stop_event": stop_event,
                    "high_rate_phase_runtime": self._high_rate_phase_runtime,
                },
                daemon=True,
            )
            acquisition_thread.start()
            heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                name=f"live-heartbeat-{self.runtime_execution_id}",
                kwargs={"source": source, "stop_event": stop_event},
                daemon=True,
            )
            heartbeat_thread.start()
            sample_every_n = max(int(source_config.get("sample_every_n") or 1), 1)
            max_fps = float(source_config.get("max_fps") or 0)
            min_interval = 1.0 / max_fps if max_fps > 0 else 0.0
            stateful_nodes = _stateful_pipeline_nodes(graph_nodes)
            inference_nodes = _inference_pipeline_nodes(graph_nodes)
            ordered_stateful_node_keys = _ordered_stem_stateful_node_keys(executable)
            pipeline_max_inflight = _pipeline_max_inflight(
                self.settings,
                source_config,
                stateful_nodes=stateful_nodes,
                inference_nodes=inference_nodes,
                ordered_stateful_overlap=bool(ordered_stateful_node_keys),
            )
            if pipeline_max_inflight >= 2 and ordered_stateful_node_keys:
                ordered_stateful_node_gate = _OrderedStatefulNodeGate(
                    ordered_stateful_node_keys
                )
                for node_key in ordered_stateful_node_keys:
                    pipeline_state.setdefault(node_key, {})
            else:
                ordered_stateful_node_keys = ()
            pipeline_work_deadline_seconds = _pipeline_work_deadline_seconds(
                self.settings,
                graph_nodes,
            )
            pending_work: dict[Future[_PipelineWorkResult], _PendingPipelineWork] = {}
            pipeline_executor = ThreadPoolExecutor(
                max_workers=pipeline_max_inflight,
                thread_name_prefix=f"runtime-pipeline-{self.runtime_execution_id[:8]}",
            )
            latest_result_sequence = 0
            last_processed_perf = 0.0
            last_busy_drop_publish_perf = 0.0
            seen = 0

            while not stop_event.is_set():
                if not self._runtime_should_continue():
                    break
                latest_result_sequence = self._drain_completed_pipeline_work(
                    pending_work,
                    latest_result_sequence=latest_result_sequence,
                    graph_nodes=graph_nodes,
                    source_type=source_type,
                    source_config=source_config,
                    sample_every_n=sample_every_n,
                    max_fps=max_fps,
                    source=source,
                    pipeline_max_inflight=pipeline_max_inflight,
                    stateful_nodes=stateful_nodes,
                    inference_nodes=inference_nodes,
                    wait_timeout_seconds=0.0,
                )
                if not stateful_nodes:
                    self._drop_timed_out_pipeline_work(pending_work)
                self._sync_pending_work_metrics(pending_work)
                if self._pipeline_work_stalled(
                    pending_work,
                    pipeline_work_deadline_seconds=pipeline_work_deadline_seconds,
                    stateful=bool(stateful_nodes),
                ):
                    raise TimeoutError(self.metrics.last_error)
                if len(pending_work) >= pipeline_max_inflight:
                    latest_result_sequence = self._drain_completed_pipeline_work(
                        pending_work,
                        latest_result_sequence=latest_result_sequence,
                        graph_nodes=graph_nodes,
                        source_type=source_type,
                        source_config=source_config,
                        sample_every_n=sample_every_n,
                        max_fps=max_fps,
                        source=source,
                        pipeline_max_inflight=pipeline_max_inflight,
                        stateful_nodes=stateful_nodes,
                        inference_nodes=inference_nodes,
                        wait_timeout_seconds=0.05,
                    )
                    if not stateful_nodes:
                        self._drop_timed_out_pipeline_work(pending_work)
                    self._sync_pending_work_metrics(pending_work)
                    if self._pipeline_work_stalled(
                        pending_work,
                        pipeline_work_deadline_seconds=pipeline_work_deadline_seconds,
                        stateful=bool(stateful_nodes),
                    ):
                        raise TimeoutError(self.metrics.last_error)
                    if len(pending_work) < pipeline_max_inflight:
                        continue
                    busy_drop_publish_interval = _busy_drop_publish_interval_seconds(
                        min_interval=min_interval,
                    )
                    if (
                        time.perf_counter() - last_busy_drop_publish_perf
                        >= busy_drop_publish_interval
                    ):
                        packet = buffer.pop_latest(timeout_seconds=0.0)
                        self.metrics.record_buffer_drops(buffer.dropped_count)
                        self.metrics.buffer_depth = buffer.depth()
                        self._sync_source_health(source)
                        if packet is not None:
                            self._record_inference_busy_frame_drop(
                                packet=packet,
                                source=source,
                            )
                            last_busy_drop_publish_perf = time.perf_counter()
                    self._heartbeat_if_due(source=source)
                    continue
                wait_started = time.perf_counter()
                packet = buffer.pop_latest(timeout_seconds=0.05 if pending_work else 0.25)
                buffer_wait_ms = (time.perf_counter() - wait_started) * 1000
                self.metrics.record_buffer_drops(buffer.dropped_count)
                self.metrics.buffer_depth = buffer.depth()
                self._sync_source_health(source)
                if packet is None:
                    if (
                        shared_source_first_frame_deadline is not None
                        and not self._first_frame_logged
                        and time.perf_counter() >= shared_source_first_frame_deadline
                    ):
                        raise TimeoutError(
                            "Camera-media did not publish a first frame within "
                            f"{shared_source_startup_timeout:.1f} "
                            "seconds. Check the channel stream camera state and last error."
                        )
                    self._heartbeat_if_due(source=source)
                    continue
                seen += 1
                if sample_every_n > 1 and seen % sample_every_n != 0:
                    self.metrics.record_skipped()
                    continue
                now = time.perf_counter()
                throttle_wait_ms = 0.0
                if min_interval and last_processed_perf:
                    wait_seconds = min_interval - (now - last_processed_perf)
                    if wait_seconds > 0:
                        throttle_started = time.perf_counter()
                        if stop_event.wait(wait_seconds):
                            break
                        throttle_wait_ms += (time.perf_counter() - throttle_started) * 1000
                        replacement = buffer.pop_latest(timeout_seconds=0.0)
                        if replacement is not None:
                            self.metrics.record_skipped()
                            packet = replacement
                            self.metrics.record_buffer_drops(buffer.dropped_count)
                            self.metrics.buffer_depth = buffer.depth()
                            self._sync_source_health(source)
                        now = time.perf_counter()
                        if now - last_processed_perf < min_interval:
                            self.metrics.record_skipped()
                            continue
                last_processed_perf = now
                queued_perf = time.perf_counter()
                if ordered_stateful_node_gate is not None:
                    ordered_stateful_node_gate.register(packet.sequence)
                try:
                    future = pipeline_executor.submit(
                        self._execute_pipeline_work,
                        packet=packet,
                        buffer_wait_ms=buffer_wait_ms,
                        throttle_wait_ms=throttle_wait_ms,
                        queued_perf=queued_perf,
                        executable=executable,
                        pipeline_version_id=pipeline_version_id,
                        source_config=source_config,
                        inference_client=inference_client,
                        model_resolver=model_resolver,
                        pipeline_state=(
                            pipeline_state
                            if pipeline_max_inflight == 1 or ordered_stateful_node_gate is not None
                            else {}
                        ),
                        high_rate_phase_bridge=(
                            self._high_rate_phase_runtime.bridge
                            if self._high_rate_phase_runtime is not None
                            else None
                        ),
                        ordered_stateful_node_gate=ordered_stateful_node_gate,
                        ordered_stateful_node_keys=ordered_stateful_node_keys,
                    )
                except Exception:
                    if ordered_stateful_node_gate is not None:
                        ordered_stateful_node_gate.complete_frame(packet.sequence)
                    raise
                if ordered_stateful_node_gate is not None:
                    future.add_done_callback(
                        lambda _future,
                        *,
                        gate=ordered_stateful_node_gate,
                        sequence=packet.sequence: gate.complete_frame(sequence)
                    )
                pending_work[future] = _PendingPipelineWork(
                    sequence=packet.sequence,
                    queued_perf=queued_perf,
                    deadline_perf=queued_perf + pipeline_work_deadline_seconds,
                )
            self._drain_completed_pipeline_work(
                pending_work,
                latest_result_sequence=latest_result_sequence,
                graph_nodes=graph_nodes,
                source_type=source_type,
                source_config=source_config,
                sample_every_n=sample_every_n,
                max_fps=max_fps,
                source=source,
                pipeline_max_inflight=pipeline_max_inflight,
                stateful_nodes=stateful_nodes,
                inference_nodes=inference_nodes,
                wait_timeout_seconds=0.0,
            )
        except Exception as exc:
            self.metrics.last_error = str(exc)
            stop_event.set()
            self._record_failure_heartbeat(str(exc), source=source)
        finally:
            stop_event.set()
            buffer.notify()
            if self._high_rate_phase_runtime is not None:
                self._high_rate_phase_runtime.close()
                self._high_rate_phase_runtime = None
            if self._temporal_sample_recorder is not None:
                self._temporal_sample_recorder.close()
                self._temporal_sample_recorder = None
            clear_runtime_overlay_event(
                self.settings,
                self.channel_id,
                runtime_instance_id=self.runtime_execution_id,
            )
            if source is not None:
                source.close()
            if ordered_stateful_node_gate is not None:
                ordered_stateful_node_gate.close()
            if pipeline_executor is not None:
                pipeline_executor.shutdown(wait=False, cancel_futures=True)
            close_siemens_sps_connection_cache(pipeline_state)
            _close_if_supported(inference_client)
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=2.0)
            if acquisition_thread is not None:
                acquisition_thread.join(timeout=2.0)

    def _execute_pipeline_work(
        self,
        *,
        packet: FramePacket,
        buffer_wait_ms: float,
        throttle_wait_ms: float,
        queued_perf: float,
        executable: ExecutablePipelineGraph,
        pipeline_version_id: str,
        source_config: dict[str, Any],
        inference_client: RuntimeInferenceClient,
        model_resolver: Callable[[RuntimeLogicalModelRef], RuntimeModelBinding],
        pipeline_state: dict[str, Any],
        high_rate_phase_bridge: HighRateStemPhaseBridge | None,
        ordered_stateful_node_gate: _OrderedStatefulNodeGate | None = None,
        ordered_stateful_node_keys: tuple[str, ...] = (),
    ) -> _PipelineWorkResult:
        started = time.perf_counter()
        stage_timings_ms = {
            "buffer_wait_ms": buffer_wait_ms,
            "throttle_wait_ms": throttle_wait_ms,
            "pipeline_queue_wait_ms": (started - queued_perf) * 1000,
        }
        stage_started = started
        frame_payload = _frame_payload_from_packet(
            packet,
            runtime_execution_id=self.runtime_execution_id,
        )
        _record_stage_timing(stage_timings_ms, "frame_payload_prepare_ms", stage_started)
        _record_stage_timing(stage_timings_ms, "frame_payload_ms", stage_started)
        stage_started = time.perf_counter()
        context = RuntimeExecutionContext(
            correlation_id=f"live_{self.runtime_execution_id}_{packet.sequence}",
            channel_id=self.channel_id,
            pipeline_version_id=pipeline_version_id,
            runtime_execution_id=self.runtime_execution_id,
            mode=RuntimeMode.REALTIME,
            source_config=source_config,
            input_frame=frame_payload,
            inference_client=inference_client,
            model_resolver=model_resolver,
            state=pipeline_state,
            metadata={
                "executor_service": SERVICE_NAME,
                "live_frame_sequence": packet.sequence,
                "default_inference_timeout_ms": int(
                    self.settings.inference_limits.realtime_timeout_ms
                ),
                "materialize_frame_node_outputs": False,
                "materialize_live_display_frame": True,
                "plc_writes_allowed": bool(
                    self.settings.runtime.plc_writes_allowed
                    and not self._shadow_mode_enabled
                ),
                "shadow_mode": self._shadow_mode_enabled,
                "stem_rotation_priority_control": dict(
                    self._stem_rotation_priority_control
                ),
                "high_rate_stem_phase_bridge": high_rate_phase_bridge,
                "ordered_stateful_node_gate": ordered_stateful_node_gate,
                "ordered_stateful_node_keys": ordered_stateful_node_keys,
                "parallel_node_keys": _stem_measurement_parallel_node_keys(executable),
                "parallel_node_max_workers": 2,
            },
        )
        _record_stage_timing(stage_timings_ms, "context_build_ms", stage_started)
        stage_started = time.perf_counter()
        result = executable.execute(context)
        _record_stage_timing(stage_timings_ms, "pipeline_execute_ms", stage_started)
        ordered_wait_ms = _float_or_none(result.timing.get("ordered_stateful_wait_ms"))
        if ordered_wait_ms is not None:
            stage_timings_ms["ordered_stateful_wait_ms"] = ordered_wait_ms
        ordered_wait_by_node = result.timing.get("ordered_stateful_wait_by_node_ms")
        if isinstance(ordered_wait_by_node, dict):
            for node_key, raw_wait_ms in ordered_wait_by_node.items():
                node_wait_ms = _float_or_none(raw_wait_ms)
                if node_wait_ms is not None:
                    stage_timings_ms[f"ordered_stateful_wait_{node_key}_ms"] = node_wait_ms
        stage_timings_ms["pipeline_task_ms"] = (time.perf_counter() - started) * 1000
        return _PipelineWorkResult(
            packet=packet,
            frame_payload=frame_payload,
            result=result,
            stage_timings_ms=stage_timings_ms,
            queued_perf=queued_perf,
        )

    def _drain_completed_pipeline_work(
        self,
        pending_work: dict[Future[_PipelineWorkResult], _PendingPipelineWork],
        *,
        latest_result_sequence: int,
        graph_nodes: list[dict[str, Any]],
        source_type: str,
        source_config: dict[str, Any],
        sample_every_n: int,
        max_fps: float,
        source: RealtimeFrameSource | None,
        pipeline_max_inflight: int,
        stateful_nodes: list[str],
        inference_nodes: list[str],
        wait_timeout_seconds: float,
    ) -> int:
        if not pending_work:
            return latest_result_sequence
        if wait_timeout_seconds > 0:
            done, _pending = wait(
                pending_work,
                timeout=wait_timeout_seconds,
                return_when=FIRST_COMPLETED,
            )
        else:
            done = {future for future in pending_work if future.done()}
        for future in sorted(done, key=lambda item: pending_work[item].sequence):
            pending_work.pop(future, None)
            try:
                work = future.result()
            except Exception as exc:
                self.metrics.last_error = str(exc)
                self.metrics.record_skipped()
                self._heartbeat_if_due(source=source)
                continue
            try:
                latest_result_sequence = self._publish_pipeline_work_result(
                    work,
                    latest_result_sequence=latest_result_sequence,
                    graph_nodes=graph_nodes,
                    source_type=source_type,
                    source_config=source_config,
                    sample_every_n=sample_every_n,
                    max_fps=max_fps,
                    source=source,
                    pipeline_max_inflight=pipeline_max_inflight,
                    stateful_nodes=stateful_nodes,
                    inference_nodes=inference_nodes,
                )
            except Exception as exc:
                self.metrics.last_error = f"Realtime frame publication failed: {exc}"
                self.metrics.record_skipped()
                latest_result_sequence = max(latest_result_sequence, work.packet.sequence)
                self._heartbeat_if_due(source=source)
        return latest_result_sequence

    def _drop_timed_out_pipeline_work(
        self,
        pending_work: dict[Future[_PipelineWorkResult], _PendingPipelineWork],
    ) -> bool:
        now = time.perf_counter()
        timed_out = [
            (future, info)
            for future, info in list(pending_work.items())
            if now >= info.deadline_perf and not info.deadline_recorded
        ]
        running_future_timed_out = False
        for future, info in timed_out:
            if future.done():
                continue
            if future.cancel():
                pending_work.pop(future, None)
            else:
                # A Python thread cannot be cancelled once it is running.
                # Keep it in the pending set so its eventual result can still
                # be published. Removing it here used to orphan the work,
                # while the executor kept consuming CPU in the background.
                info.deadline_recorded = True
                running_future_timed_out = True
            self.metrics.record_pipeline_deadline_drop()
            elapsed_ms = max((now - info.queued_perf) * 1000, 0.0)
            self.metrics.last_error = (
                "Realtime pipeline work exceeded its soft frame deadline; "
                f"newer frames are being dropped while sequence {info.sequence} "
                f"finishes after {elapsed_ms:.0f} ms."
            )
        return running_future_timed_out

    def _sync_pending_work_metrics(
        self,
        pending_work: dict[Future[_PipelineWorkResult], _PendingPipelineWork],
    ) -> None:
        self.metrics.inflight_count = len(pending_work)
        if not pending_work:
            self.metrics.oldest_inflight_ms = None
            return
        oldest_queued_perf = min(info.queued_perf for info in pending_work.values())
        self.metrics.oldest_inflight_ms = round(
            max((time.perf_counter() - oldest_queued_perf) * 1000, 0.0),
            3,
        )

    def _stateful_pipeline_work_stalled(
        self,
        pending_work: dict[Future[_PipelineWorkResult], _PendingPipelineWork],
        *,
        pipeline_work_deadline_seconds: float,
    ) -> bool:
        return self._pipeline_work_stalled(
            pending_work,
            pipeline_work_deadline_seconds=pipeline_work_deadline_seconds,
            stateful=True,
        )

    def _pipeline_work_stalled(
        self,
        pending_work: dict[Future[_PipelineWorkResult], _PendingPipelineWork],
        *,
        pipeline_work_deadline_seconds: float,
        stateful: bool,
    ) -> bool:
        if not pending_work:
            return False
        now = time.perf_counter()
        watchdog_seconds = max(
            float(self.settings.runtime.executor_stale_after_seconds),
            pipeline_work_deadline_seconds * 4.0,
            10.0,
        )
        oldest = min(pending_work.values(), key=lambda item: item.queued_perf)
        age_seconds = max(now - oldest.queued_perf, 0.0)
        if age_seconds < watchdog_seconds:
            return False
        pipeline_kind = "Stateful realtime" if stateful else "Realtime"
        timing_key = "stateful_pipeline_stall_ms" if stateful else "pipeline_stall_ms"
        self.metrics.last_error = (
            f"{pipeline_kind} pipeline work stalled; restarting runtime worker "
            f"after {age_seconds:.1f}s."
        )
        self.metrics.record_stage_timings(
            {
                **self.metrics.stage_timings_ms,
                timing_key: age_seconds * 1000.0,
            }
        )
        LOGGER.error(
            "%s pipeline work stalled: channel_id=%s runtime_execution_id=%s "
            "sequence=%s age_seconds=%.1f",
            pipeline_kind,
            self.channel_id,
            self.runtime_execution_id,
            oldest.sequence,
            age_seconds,
        )
        return True

    def _record_inference_busy_frame_drop(
        self,
        *,
        packet: FramePacket,
        source: RealtimeFrameSource | None,
    ) -> None:
        started = time.perf_counter()
        try:
            self.metrics.record_inference_drop()
            self.metrics.record_stage_timings(
                {
                    "inference_busy_drop_ms": (time.perf_counter() - started) * 1000,
                    "inference_busy_drop_sequence": float(packet.sequence),
                    "total_ms": (time.perf_counter() - started) * 1000,
                }
            )
        except Exception as exc:
            self.metrics.last_error = f"Realtime busy-frame drop accounting failed: {exc}"
            self.metrics.record_skipped()
        finally:
            self._heartbeat_if_due(
                source=source,
                force=self.metrics.inference_dropped_frame_count == 1,
            )

    def _write_live_view_state_if_due(
        self,
        live_state: RuntimeLiveViewState,
        *,
        force: bool = False,
    ) -> bool:
        now = time.perf_counter()
        if (
            not force
            and self._last_live_state_write_perf
            and now - self._last_live_state_write_perf < _LIVE_STATE_WRITE_MIN_INTERVAL_SECONDS
        ):
            return False
        write_runtime_live_view_state(
            self.settings,
            live_state,
            split_frame_payload=True,
        )
        self._last_live_state_write_perf = time.perf_counter()
        return True

    def _live_view_state_write_due(self) -> bool:
        return bool(
            not self._last_live_state_write_perf
            or time.perf_counter() - self._last_live_state_write_perf
            >= _LIVE_STATE_WRITE_MIN_INTERVAL_SECONDS
        )

    def _publish_pipeline_work_result(
        self,
        work: _PipelineWorkResult,
        *,
        latest_result_sequence: int,
        graph_nodes: list[dict[str, Any]],
        source_type: str,
        source_config: dict[str, Any],
        sample_every_n: int,
        max_fps: float,
        source: RealtimeFrameSource | None,
        pipeline_max_inflight: int,
        stateful_nodes: list[str],
        inference_nodes: list[str],
    ) -> int:
        stage_timings_ms = dict(work.stage_timings_ms)
        high_rate_phase_runtime = getattr(self, "_high_rate_phase_runtime", None)
        if high_rate_phase_runtime is not None:
            stage_timings_ms.update(high_rate_phase_runtime.metrics())
        if work.packet.sequence <= latest_result_sequence:
            self.metrics.record_skipped()
            stage_timings_ms["stale_result_drop_ms"] = 0.0
            stage_timings_ms["total_ms"] = (time.perf_counter() - work.queued_perf) * 1000
            self.metrics.record_stage_timings(stage_timings_ms)
            return latest_result_sequence
        latency_ms = (time.perf_counter() - work.queued_perf) * 1000
        transient_inference_drop = _is_realtime_transient_inference_failure(work.result)
        if not transient_inference_drop and self._temporal_sample_recorder is not None:
            capture_plans = self._temporal_sample_recorder.observe(
                work.result,
                work.frame_payload,
            )
            attach_temporal_capture_plans(work.result, capture_plans)
        if transient_inference_drop:
            self.metrics.record_inference_drop()
            self.metrics.buffer_depth = max(self.metrics.buffer_depth, 0)
            stage_timings_ms["inference_drop_ms"] = latency_ms
        else:
            self.metrics.record_processed(latency_ms, frame_at=work.packet.source_timestamp)
            self.metrics.last_error = None
        self.metrics.record_stage_timings(stage_timings_ms)
        stage_started = time.perf_counter()
        materialize_frames = self._live_view_state_write_due()
        live_state = (
            self._live_state_from_frame_drop(
                packet=work.packet,
                frame_payload=work.frame_payload,
                graph_nodes=graph_nodes,
                source_type=source_type,
                source_config=source_config,
                sample_every_n=sample_every_n,
                max_fps=max_fps,
                source=source,
                pipeline_max_inflight=pipeline_max_inflight,
                stateful_nodes=stateful_nodes,
                inference_nodes=inference_nodes,
                reason=_transient_inference_drop_reason(work.result),
                result=work.result,
                materialize_frames=materialize_frames,
            )
            if transient_inference_drop
            else self._live_state_from_result(
                result=work.result,
                packet=work.packet,
                frame_payload=work.frame_payload,
                graph_nodes=graph_nodes,
                source_type=source_type,
                source_config=source_config,
                sample_every_n=sample_every_n,
                max_fps=max_fps,
                source=source,
                pipeline_max_inflight=pipeline_max_inflight,
                stateful_nodes=stateful_nodes,
                inference_nodes=inference_nodes,
                materialize_frames=materialize_frames,
            )
        )
        _record_stage_timing(stage_timings_ms, "live_state_build_ms", stage_started)
        self.metrics.record_stage_timings(stage_timings_ms)
        write_runtime_overlay_event(self.settings, live_state)
        stage_started = time.perf_counter()
        if materialize_frames and self._write_live_view_state_if_due(live_state, force=True):
            _record_stage_timing(stage_timings_ms, "live_state_write_ms", stage_started)
        else:
            stage_timings_ms["live_state_write_skipped_ms"] = 0.0
        history_event_key = _stem_orientation_history_event_key(work.result)
        retain_history = self._history_result_persist_due(work.result)
        if (
            retain_history
            or work.result.capture_intents
            or _plc_write_outputs_have_failure(work.result.plc_write_outputs)
            or (work.result.status == "failed" and not transient_inference_drop)
        ):
            stage_started = time.perf_counter()
            with session_scope(self.settings) as session:
                record_runtime_execution_result(
                    session,
                    work.result,
                    channel_id=self.channel_id,
                    runtime_execution_id=self.runtime_execution_id,
                    retain_history=retain_history,
                )
            if retain_history:
                self._last_history_persist_perf = time.perf_counter()
                if history_event_key is not None:
                    self._last_history_event_key = history_event_key
            _record_stage_timing(stage_timings_ms, "result_persist_ms", stage_started)
        stage_started = time.perf_counter()
        self._heartbeat_if_due(
            source=source,
            force=(
                self.metrics.processed_frame_count + self.metrics.inference_dropped_frame_count == 1
            ),
        )
        _record_stage_timing(stage_timings_ms, "heartbeat_ms", stage_started)
        stage_timings_ms["total_ms"] = (time.perf_counter() - work.queued_perf) * 1000
        self.metrics.record_stage_timings(stage_timings_ms)
        return work.packet.sequence

    def _history_result_persist_due(self, result: RuntimeExecutionResult) -> bool:
        policy = self._result_history_policy
        if not result.persisted_results:
            return False
        event_key = _stem_orientation_history_event_key(result)
        if event_key is not None:
            # A frozen pre-entry decision is a semantic event, not a periodic
            # frame sample. Persist its first occurrence immediately and avoid
            # storing the same frozen decision again on every sampling interval.
            return bool(policy.enabled or self._shadow_mode_enabled) and (
                event_key != self._last_history_event_key
            )
        if not policy.enabled:
            return False
        now = time.perf_counter()
        return (
            not self._last_history_persist_perf
            or now - self._last_history_persist_perf >= policy.sample_interval_seconds
        )

    def _acquire_frames(
        self,
        *,
        source: RealtimeFrameSource,
        buffer: LatestFrameBuffer,
        stop_event: threading.Event,
        high_rate_phase_runtime: _HighRateStemPhaseRuntime | None = None,
    ) -> None:
        while not stop_event.is_set():
            try:
                read_started = time.perf_counter()
                packet = source.read(stop_event)
                self.metrics.record_source_read((time.perf_counter() - read_started) * 1000)
            except Exception as exc:
                source.health.connected = False
                source.health.status_text = "Source read failed"
                source.health.last_error = str(exc)
                source.health.source_error_count += 1
                LOGGER.exception(
                    "Realtime source read raised an exception: channel_id=%s "
                    "runtime_execution_id=%s",
                    self.channel_id,
                    self.runtime_execution_id,
                )
                time.sleep(0.25)
                continue
            self._sync_source_health(source)
            if packet is None:
                self._log_source_wait_if_due(source)
                continue
            self.metrics.record_acquired()
            self.metrics.record_source_frame()
            if not self._first_frame_logged:
                self._first_frame_logged = True
                LOGGER.info(
                    "First realtime source frame acquired: channel_id=%s "
                    "runtime_execution_id=%s sequence=%s width=%s height=%s source_uri=%s "
                    "metadata=%s",
                    self.channel_id,
                    self.runtime_execution_id,
                    packet.sequence,
                    packet.width,
                    packet.height,
                    packet.source_uri,
                    packet.metadata,
                )
            if high_rate_phase_runtime is not None:
                high_rate_phase_runtime.observe_source_reconnect_count(
                    source.health.reconnect_count
                )
                high_rate_phase_runtime.submit(packet)
            buffer.push(packet)

    def _heartbeat_loop(
        self,
        *,
        source: RealtimeFrameSource,
        stop_event: threading.Event,
    ) -> None:
        interval = max(float(self.settings.runtime.executor_heartbeat_interval_seconds), 1.0)
        poll_interval = min(max(interval / 2.0, 1.0), interval)
        while not stop_event.is_set():
            try:
                self._heartbeat_if_due(source=source)
            except Exception as exc:
                self.metrics.last_error = f"Realtime heartbeat failed: {exc}"
            if stop_event.wait(poll_interval):
                break

    def _runtime_should_continue(self) -> bool:
        now = time.perf_counter()
        if now - self._last_runtime_state_check_perf < 1.0:
            return True
        self._last_runtime_state_check_perf = now
        with session_scope(self.settings) as session:
            execution = session.get(RuntimeExecution, self.runtime_execution_id)
            if execution is None:
                return False
            if execution.desired_state == RuntimeChannelDesiredState.STOPPED.value:
                return False
            channel = session.get(RuntimeChannel, execution.channel_id)
            if channel is None:
                return False
            if (
                channel.active_runtime_execution_id
                and channel.active_runtime_execution_id != execution.id
            ):
                return False
            if (
                channel.pipeline_version_id
                and channel.pipeline_version_id != execution.pipeline_version_id
            ):
                return False
            try:
                self._stem_rotation_priority_control = (
                    RuntimeStemRotationPriorityControl.model_validate(
                        channel.stem_rotation_priority_control or {}
                    ).model_dump(mode="json")
                )
            except ValueError:
                self._stem_rotation_priority_control = (
                    RuntimeStemRotationPriorityControl().model_dump(mode="json")
                )
            return True

    def _sync_source_health(self, source: RealtimeFrameSource) -> None:
        self.metrics.reconnect_count = int(source.health.reconnect_count)
        self.metrics.source_error_count = int(source.health.source_error_count)
        if source.health.last_error:
            self.metrics.last_error = source.health.last_error

    def _log_source_wait_if_due(self, source: RealtimeFrameSource) -> None:
        now = time.perf_counter()
        if now - self._last_source_wait_log_perf < 10.0:
            return
        self._last_source_wait_log_perf = now
        age_since_frame = (
            now - self.metrics.last_source_frame_perf
            if self.metrics.last_source_frame_perf
            else now - self.metrics.started_perf
        )
        LOGGER.warning(
            "Realtime source has not delivered a frame yet/recently: channel_id=%s "
            "runtime_execution_id=%s acquired_frames=%s age_since_frame_seconds=%.3f "
            "source_connected=%s status=%s last_error=%s reconnect_count=%s "
            "source_error_count=%s source_read_ms=%s",
            self.channel_id,
            self.runtime_execution_id,
            self.metrics.acquired_frame_count,
            max(age_since_frame, 0.0),
            source.health.connected,
            source.health.status_text,
            source.health.last_error,
            source.health.reconnect_count,
            source.health.source_error_count,
            self.metrics.source_read_ms,
        )

    def _heartbeat_if_due(
        self,
        *,
        source: RealtimeFrameSource | None,
        force: bool = False,
    ) -> None:
        interval = max(float(self.settings.runtime.executor_heartbeat_interval_seconds), 1.0)
        if not force and time.perf_counter() - self.metrics.last_heartbeat_perf < interval:
            return
        with session_scope(self.settings) as session:
            self._record_heartbeat(session, source=source)

    def _record_heartbeat(self, session: Session, *, source: RealtimeFrameSource | None) -> None:
        source_health = self._source_health(source)
        health_state, status_text, health_reasons = self._runtime_health(source_health)
        health_summary = {
            "state": health_state.value,
            "status_text": status_text,
            "executor_service": SERVICE_NAME,
            "source_health": source_health.model_dump(mode="json"),
            "health_reasons": health_reasons,
            "last_error": self.metrics.last_error,
        }
        record_runtime_execution_heartbeat(
            session,
            runtime_execution_id=self.runtime_execution_id,
            executor_instance_id=self.executor_instance_id,
            health_summary=health_summary,
            metrics_summary=self.metrics.to_db_metrics(),
        )
        self.metrics.last_heartbeat_perf = time.perf_counter()

    def _runtime_health(
        self,
        source_health: RuntimeLiveSourceHealth,
    ) -> tuple[HealthState, str, list[str]]:
        reasons: list[str] = []
        if not source_health.connected:
            reasons.append(source_health.status_text or "Live source is disconnected.")

        elapsed = max(time.perf_counter() - self.metrics.started_perf, 0.0)
        live_metrics = self.metrics.to_live_metrics()
        if elapsed >= 5.0 and (live_metrics.source_fps or 0.0) > 0:
            if (
                self.metrics.inference_dropped_frame_count > 0
                and (live_metrics.processed_fps or 0.0) <= 0
            ):
                reasons.append("Inference is dropping frames without producing recent results.")
            elif (
                self.metrics.dropped_frame_count >= 5
                and (live_metrics.drop_rate_percent or 0.0) >= 50.0
            ):
                reasons.append(f"Recent frame drop rate is {live_metrics.drop_rate_percent:.1f}%.")

        if not reasons:
            return HealthState.OK, source_health.status_text, []
        return HealthState.DEGRADED, reasons[0], reasons

    def _record_failure_heartbeat(self, error: str, *, source: RealtimeFrameSource | None) -> None:
        with session_scope(self.settings) as session:
            source_health = self._source_health(source)
            health_summary = {
                "state": HealthState.DEGRADED.value,
                "status_text": "Realtime executor loop reported an error",
                "executor_service": SERVICE_NAME,
                "source_health": source_health.model_dump(mode="json"),
                "last_error": error,
            }
            mark_runtime_execution_failed_retryable(
                session,
                runtime_execution_id=self.runtime_execution_id,
                executor_instance_id=self.executor_instance_id,
                error=error,
                health_summary=health_summary,
                metrics_summary=self.metrics.to_db_metrics(),
            )

    def _live_state_from_result(
        self,
        *,
        result: RuntimeExecutionResult,
        packet: FramePacket,
        frame_payload: FramePayload,
        graph_nodes: list[dict[str, Any]],
        source_type: str,
        source_config: dict[str, Any],
        sample_every_n: int,
        max_fps: float,
        source: RealtimeFrameSource | None,
        pipeline_max_inflight: int,
        stateful_nodes: list[str],
        inference_nodes: list[str],
        materialize_frames: bool = True,
    ) -> RuntimeLiveViewState:
        display = _display_selection_from_result(result)
        display_context_id = display.get("display_context_id")
        display_context = (
            display.get("display_context")
            if isinstance(display.get("display_context"), dict)
            else {}
        )
        display_frame = _display_frame_from_result(
            result,
            display_context_id=display_context_id,
            fallback_frame=frame_payload,
        )
        overlay = _overlay_from_result(
            result,
            graph_nodes=graph_nodes,
            display_context_id=display_context_id,
        )
        overlay = _synchronize_overlay_primitives(
            overlay,
            frame_id=str(packet.sequence),
            source_timestamp=packet.source_timestamp,
            display_context_id=display_context_id,
            width=display_frame.width,
            height=display_frame.height,
        )
        undistorted_frame = _undistorted_frame_from_result(result)
        if undistorted_frame is not None:
            source_overlay = _source_overlay_from_image_view(
                overlay,
                display_frame=display_frame,
                source_frame=undistorted_frame,
            )
            overlay_source_frame = undistorted_frame
        else:
            source_overlay = _source_overlay_from_crop(
                overlay,
                display_frame=display_frame,
                source_frame=frame_payload,
            )
            if source_overlay is None:
                source_overlay = (
                    list(overlay)
                    if (display_frame.width, display_frame.height)
                    == (frame_payload.width, frame_payload.height)
                    else _nonspatial_overlay_primitives(overlay)
                )
            overlay_source_frame = frame_payload
        source_overlay = _synchronize_overlay_primitives(
            source_overlay,
            frame_id=str(packet.sequence),
            source_timestamp=packet.source_timestamp,
            display_context_id=(
                overlay_source_frame.image_context.context_id
                if overlay_source_frame.image_context is not None
                else None
            ),
            width=overlay_source_frame.width,
            height=overlay_source_frame.height,
        )
        intrinsic_calibration = _intrinsic_calibration_from_graph(graph_nodes)
        raw_overlay = (
            _raw_overlay_from_undistorted(
                source_overlay,
                calibration=intrinsic_calibration,
                raw_frame=frame_payload,
            )
            if undistorted_frame is not None
            else list(source_overlay)
        )
        raw_overlay = _synchronize_overlay_primitives(
            raw_overlay,
            frame_id=str(packet.sequence),
            source_timestamp=packet.source_timestamp,
            display_context_id=(
                frame_payload.image_context.context_id
                if frame_payload.image_context is not None
                else None
            ),
            width=frame_payload.width,
            height=frame_payload.height,
        )
        layers = sorted(
            {primitive.layer for primitive in [*overlay, *source_overlay, *raw_overlay]}
        )
        source_health = self._source_health(source)
        return RuntimeLiveViewState(
            channel_id=self.channel_id,
            runtime_instance_id=self.runtime_execution_id,
            actual_state=RuntimeObservedState.RUNNING,
            available=True,
            stale=False,
            display_context_id=display_context_id,
            display_context=display_context,
            frame=RuntimeLiveFrame(
                image_base64=(display_frame.image_base64_value() if materialize_frames else None),
                mime_type=display_frame.mime_type,
                width=display_frame.width,
                height=display_frame.height,
                source_timestamp=display_frame.timestamp or packet.source_timestamp,
                captured_at=utc_now_iso(),
                sequence=packet.sequence,
                image_context=(
                    display_frame.image_context.model_dump(mode="json")
                    if display_frame.image_context is not None
                    else {}
                ),
                metadata={
                    **dict(display_frame.metadata or {}),
                    "source_uri": display_frame.source_uri or packet.source_uri,
                    "frame_role": "pipeline_display_output",
                },
            ),
            source_frame=RuntimeLiveFrame(
                image_base64=(frame_payload.image_base64_value() if materialize_frames else None),
                mime_type=frame_payload.mime_type,
                width=frame_payload.width,
                height=frame_payload.height,
                source_timestamp=frame_payload.timestamp or packet.source_timestamp,
                captured_at=utc_now_iso(),
                sequence=packet.sequence,
                image_context=(
                    frame_payload.image_context.model_dump(mode="json")
                    if frame_payload.image_context is not None
                    else {}
                ),
                metadata={
                    **dict(frame_payload.metadata or {}),
                    "source_uri": frame_payload.source_uri or packet.source_uri,
                    "frame_role": "pipeline_input",
                },
            ),
            undistorted_frame=(
                RuntimeLiveFrame(
                    image_base64=(
                        undistorted_frame.image_base64_value() if materialize_frames else None
                    ),
                    mime_type=undistorted_frame.mime_type,
                    width=undistorted_frame.width,
                    height=undistorted_frame.height,
                    source_timestamp=undistorted_frame.timestamp or packet.source_timestamp,
                    captured_at=utc_now_iso(),
                    sequence=packet.sequence,
                    image_context=(
                        undistorted_frame.image_context.model_dump(mode="json")
                        if undistorted_frame.image_context is not None
                        else {}
                    ),
                    metadata={
                        **dict(undistorted_frame.metadata or {}),
                        "source_uri": undistorted_frame.source_uri or packet.source_uri,
                        "frame_role": "intrinsically_undistorted_full_frame",
                    },
                )
                if undistorted_frame is not None
                else None
            ),
            overlay=overlay,
            source_overlay=source_overlay,
            raw_overlay=raw_overlay,
            layers_available=layers,
            metrics=self.metrics.to_live_metrics(),
            source_health=source_health,
            diagnostics=_live_diagnostics(
                settings=self.settings,
                source_type=source_type,
                source_config=source_config,
                graph_nodes=graph_nodes,
                sample_every_n=sample_every_n,
                max_fps=max_fps,
                pipeline_max_inflight=pipeline_max_inflight,
                stateful_nodes=stateful_nodes,
                inference_nodes=inference_nodes,
                stage_timings_ms=self.metrics.stage_timings_ms,
                pipeline_timing=result.timing,
                plc_write_outputs=result.plc_write_outputs,
            ),
            stem_rotation_view=_stem_rotation_view_from_result(result),
            recovery_hint=None
            if source_health.connected
            else "Check the live source and network path.",
            updated_at=utc_now_iso(),
        )

    def _live_state_from_frame_drop(
        self,
        *,
        packet: FramePacket,
        frame_payload: FramePayload,
        graph_nodes: list[dict[str, Any]],
        source_type: str,
        source_config: dict[str, Any],
        sample_every_n: int,
        max_fps: float,
        source: RealtimeFrameSource | None,
        pipeline_max_inflight: int,
        stateful_nodes: list[str],
        inference_nodes: list[str],
        reason: str,
        result: RuntimeExecutionResult | None = None,
        materialize_frames: bool = True,
    ) -> RuntimeLiveViewState:
        source_health = self._source_health(source)
        warning = f"Inference frame dropped ({reason}); live source remains running."
        frame_metadata: dict[str, Any] = {
            "source_uri": frame_payload.source_uri or packet.source_uri,
            "frame_drop_reason": reason,
        }
        error_summary = _runtime_error_summary(result)
        if error_summary is not None:
            frame_metadata["frame_drop_error"] = error_summary
        pipeline_timing = result.timing if result is not None else {}
        return RuntimeLiveViewState(
            channel_id=self.channel_id,
            runtime_instance_id=self.runtime_execution_id,
            actual_state=RuntimeObservedState.RUNNING,
            available=True,
            stale=False,
            frame=RuntimeLiveFrame(
                image_base64=(frame_payload.image_base64_value() if materialize_frames else None),
                mime_type=frame_payload.mime_type,
                width=frame_payload.width,
                height=frame_payload.height,
                source_timestamp=frame_payload.timestamp or packet.source_timestamp,
                captured_at=utc_now_iso(),
                sequence=packet.sequence,
                image_context=(
                    frame_payload.image_context.model_dump(mode="json")
                    if frame_payload.image_context is not None
                    else {}
                ),
                metadata=frame_metadata,
            ),
            overlay=[],
            layers_available=[],
            metrics=self.metrics.to_live_metrics(),
            source_health=source_health,
            diagnostics=_live_diagnostics(
                settings=self.settings,
                source_type=source_type,
                source_config=source_config,
                graph_nodes=graph_nodes,
                sample_every_n=sample_every_n,
                max_fps=max_fps,
                pipeline_max_inflight=pipeline_max_inflight,
                stateful_nodes=stateful_nodes,
                inference_nodes=inference_nodes,
                stage_timings_ms=self.metrics.stage_timings_ms,
                pipeline_timing=pipeline_timing,
                extra_warnings=[warning, *_runtime_error_warnings(result)],
            ),
            recovery_hint=None
            if source_health.connected
            else "Check the live source and network path.",
            updated_at=utc_now_iso(),
        )

    def _source_health(self, source: RealtimeFrameSource | None) -> RuntimeLiveSourceHealth:
        if source is None:
            return RuntimeLiveSourceHealth(
                state=HealthState.DEGRADED,
                status_text="Source is not initialized",
                connected=False,
                last_error=self.metrics.last_error,
                reconnect_count=self.metrics.reconnect_count,
                source_error_count=self.metrics.source_error_count,
            )
        state = HealthState.OK if source.health.connected else HealthState.DEGRADED
        return RuntimeLiveSourceHealth(
            state=state,
            status_text=source.health.status_text,
            connected=source.health.connected,
            last_error=source.health.last_error,
            reconnect_count=source.health.reconnect_count,
            source_error_count=source.health.source_error_count,
        )


@dataclass
class _CachedModelBinding:
    binding: RuntimeModelBinding
    expires_at_perf: float


class _CachedRuntimeModelResolver:
    def __init__(self, *, settings: PlatformSettings) -> None:
        self.settings = settings
        inference_limits = getattr(settings, "inference_limits", None)
        self.ttl_seconds = max(
            float(getattr(inference_limits, "routing_cache_ttl_seconds", 5) or 5),
            1.0,
        )
        self._prebound: dict[tuple[object, ...], RuntimeModelBinding] = {}
        self._cache: dict[tuple[object, ...], _CachedModelBinding] = {}
        self._lock = threading.Lock()

    def preload(self, model_refs: Iterable[RuntimeLogicalModelRef]) -> None:
        unique_refs = {_model_ref_cache_key(model_ref): model_ref for model_ref in model_refs}
        if not unique_refs:
            return
        with session_scope(self.settings) as session:
            resolve = build_runtime_model_resolver(session)
            bindings = {key: resolve(model_ref) for key, model_ref in unique_refs.items()}
        with self._lock:
            # Replace the dictionary as one immutable snapshot. Frame workers
            # only read it and therefore need neither a lock nor a DB session.
            self._prebound = {**self._prebound, **bindings}

    def __call__(self, model_ref: RuntimeLogicalModelRef) -> RuntimeModelBinding:
        key = _model_ref_cache_key(model_ref)
        prebound = self._prebound.get(key)
        if prebound is not None:
            return prebound
        now = time.perf_counter()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached.expires_at_perf > now:
                return cached.binding
            # Keep the miss resolution under the same lock. This is an
            # exceptional path after preload and prevents concurrent frames
            # from stampeding the capability tables.
            with session_scope(self.settings) as session:
                binding = build_runtime_model_resolver(session)(model_ref)
            self._cache[key] = _CachedModelBinding(
                binding=binding,
                expires_at_perf=time.perf_counter() + self.ttl_seconds,
            )
            return binding


def _model_ref_cache_key(model_ref: RuntimeLogicalModelRef) -> tuple[object, ...]:
    return (
        model_ref.model_family_slug,
        model_ref.target_path.value,
        model_ref.required_task_type.value,
        model_ref.contract_major,
        model_ref.selection_policy,
        model_ref.deployment_release_id,
    )


def _close_if_supported(value: object | None) -> None:
    close = getattr(value, "close", None)
    if callable(close):
        close()


def _frame_payload_from_packet(
    packet: FramePacket,
    *,
    runtime_execution_id: str,
) -> FramePayload:
    metadata = {
        **packet.metadata,
        "runtime_execution_id": runtime_execution_id,
        "sequence": packet.sequence,
    }
    if packet.raw_frame is not None:
        return FramePayload.from_cv2_frame(
            packet.raw_frame,
            mime_type="image/jpeg",
            jpeg_quality=packet.jpeg_quality,
            source_uri=packet.source_uri,
            timestamp=packet.source_timestamp,
            metadata={**metadata, "raw_frame_payload": True},
        )
    packet = packet.ensure_encoded()
    if packet.image_bytes is None:
        raise RuntimeError("Frame packet did not produce encoded image bytes.")
    return FramePayload.from_bytes(
        packet.image_bytes,
        mime_type=packet.mime_type,
        width=packet.width,
        height=packet.height,
        source_uri=packet.source_uri,
        timestamp=packet.source_timestamp,
        metadata=metadata,
    )


def _source_config_diagnostic_summary(
    source_type: str,
    source_config: dict[str, Any],
) -> dict[str, Any]:
    if source_type == "basler_pylon":
        keys = (
            "transport_mode",
            "serial_number",
            "user_defined_name",
            "camera_name",
            "device_id",
            "id_",
            "ip_address",
            "model",
            "acquisition_mode",
            "grab_strategy",
            "pixel_format",
            "fps",
            "exposure_time_us",
            "gain_db",
            "trigger_mode",
            "trigger_source",
            "trigger_activation",
            "packet_size",
            "inter_packet_delay",
            "width",
            "height",
            "bridge_name",
            "rtsp_host",
            "rtsp_port",
            "rtsp_mount",
            "conversion_mode",
            "fetch_timeout_seconds",
            "reconnect_seconds",
            "sample_every_n",
            "max_fps",
        )
    else:
        keys = (
            "url",
            "path",
            "host",
            "port",
            "camera_name",
            "device_index",
            "device_path",
            "backend",
            "serial_number",
            "ip_address",
            "device_id",
            "id_",
            "width",
            "height",
            "fps",
            "fourcc",
            "buffer_size",
            "sample_every_n",
            "max_fps",
        )

    summary: dict[str, Any] = {}
    for key in keys:
        if _looks_sensitive_key(key):
            continue
        value = source_config.get(key)
        if value in (None, ""):
            continue
        summary[key] = _redact_source_config_value(key, value)
    return summary


def _test_photo_source_config(
    graph_nodes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for node in graph_nodes:
        if node.get("node_type") == "source.test_photo":
            return dict(node.get("config") or {})
    return None


def _looks_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in ("password", "secret", "token", "credential"))


def _redact_source_config_value(key: str, value: Any) -> Any:
    if key != "url" or not isinstance(value, str):
        return value
    parsed = urlparse(value)
    if not parsed.username and not parsed.password:
        return value
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return parsed._replace(netloc=f"***@{hostname}{port}").geturl()


def _record_stage_timing(
    timings_ms: dict[str, float],
    stage: str,
    started_perf: float,
) -> None:
    timings_ms[stage] = (time.perf_counter() - started_perf) * 1000


def _plc_write_outputs_have_failure(outputs: list[dict[str, Any]]) -> bool:
    for output in outputs:
        payload = output.get("payload")
        if not isinstance(payload, dict):
            continue
        writes = payload.get("writes")
        if not isinstance(writes, list):
            continue
        if any(isinstance(item, dict) and item.get("status") == "failed" for item in writes):
            return True
    return False


def _live_diagnostics(
    *,
    settings: PlatformSettings,
    source_type: str,
    source_config: dict[str, Any],
    graph_nodes: list[dict[str, Any]],
    sample_every_n: int,
    max_fps: float,
    pipeline_max_inflight: int,
    stateful_nodes: list[str],
    inference_nodes: list[str],
    stage_timings_ms: dict[str, float],
    pipeline_timing: dict[str, Any],
    plc_write_outputs: list[dict[str, Any]] | None = None,
    extra_warnings: list[str] | None = None,
) -> RuntimeLiveDiagnostics:
    configured_pipeline_inflight = _positive_int(
        source_config.get("pipeline_max_inflight"),
        default=int(settings.runtime.realtime_pipeline_max_inflight),
    )
    global_inference_limit = _positive_int(
        settings.inference_limits.realtime_max_concurrency,
        default=configured_pipeline_inflight,
    )
    per_release_inference_limit = _positive_int(
        settings.inference_limits.realtime_per_release_max_concurrency,
        default=global_inference_limit,
    )
    ordered_stateful_overlap = bool(pipeline_timing.get("ordered_stateful_execution"))
    if stateful_nodes and ordered_stateful_overlap:
        pipeline_inflight_limit_reason = "ordered_stateful_overlap"
    elif stateful_nodes:
        pipeline_inflight_limit_reason = "stateful_pipeline"
    elif pipeline_max_inflight == configured_pipeline_inflight:
        pipeline_inflight_limit_reason = "channel_configuration"
    elif inference_nodes and pipeline_max_inflight == per_release_inference_limit:
        pipeline_inflight_limit_reason = "per_release_concurrency"
    elif inference_nodes and pipeline_max_inflight == global_inference_limit:
        pipeline_inflight_limit_reason = "global_inference_concurrency"
    elif pipeline_max_inflight == 16 and configured_pipeline_inflight > 16:
        pipeline_inflight_limit_reason = "executor_safety_cap"
    else:
        pipeline_inflight_limit_reason = "executor_safety_cap"
    warnings: list[str] = [
        *(extra_warnings or []),
        *_source_configuration_warnings(source_type, source_config),
    ]
    pipeline_frame_samplers: list[dict[str, Any]] = []
    pipeline_node_timings = _pipeline_node_timings(pipeline_timing)
    pipeline_inference_timings = _pipeline_inference_timings(pipeline_timing)
    if max_fps > 0:
        warnings.append(f"Channel source max_fps is configured to process at most {max_fps:g} fps.")
    if sample_every_n > 1:
        warnings.append(
            f"Channel source sample_every_n processes only every {sample_every_n}th frame."
        )
    parallel_node_keys = pipeline_timing.get("parallel_node_keys")
    within_frame_parallel = isinstance(parallel_node_keys, list) and len(parallel_node_keys) >= 2
    if stateful_nodes:
        if ordered_stateful_overlap:
            barrier_keys = pipeline_timing.get("ordered_stateful_node_keys")
            barrier_label = (
                ", ".join(str(item) for item in barrier_keys)
                if isinstance(barrier_keys, list) and barrier_keys
                else ", ".join(stateful_nodes)
            )
            warnings.append(
                "Real segmentation and independent measurement stages overlap across frames; "
                "each stateful stem node keeps its own ordered sequence turn at: "
                f"{barrier_label}. No propagated masks or synthetic observations are used."
            )
        elif within_frame_parallel:
            warnings.append(
                "Frames remain ordered because stateful nodes are present, while the safe "
                "stem measurement DAG branches execute in parallel within each frame: "
                f"{', '.join(str(item) for item in parallel_node_keys)}."
            )
        else:
            warnings.append(
                "Pipeline parallelism is serialized because stateful nodes are present: "
                f"{', '.join(stateful_nodes)}. Side-effect nodes such as PLC writes stay ordered."
            )
    elif inference_nodes and pipeline_max_inflight == 1:
        warnings.append(
            "Realtime inference is limited to one in-flight frame for this channel; "
            "newer frames are dropped while inference is busy."
        )
    if inference_nodes and configured_pipeline_inflight > pipeline_max_inflight:
        warnings.append(
            f"Configured pipeline_max_inflight={configured_pipeline_inflight} is clamped to "
            f"{pipeline_max_inflight} by realtime inference concurrency limits."
        )

    throttle_wait_ms = _float_or_none(stage_timings_ms.get("throttle_wait_ms"))
    if max_fps > 0 and throttle_wait_ms is not None and throttle_wait_ms >= 10:
        warnings.append(
            f"Last frame waited {throttle_wait_ms:.0f} ms to honor source max_fps={max_fps:g}."
        )

    if _plc_write_realtime_throttled(plc_write_outputs):
        warnings.append(
            "PLC write node skipped this frame because its realtime write interval has not elapsed."
        )

    for node in graph_nodes:
        node_type = str(node.get("node_type") or "")
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        if node_type == "preprocessing.frame_sampler":
            node_sample_every = _positive_int(config.get("sample_every_n"), default=1)
            node_max_fps = _positive_float(config.get("max_fps"))
            sampler = {
                "node_key": node.get("node_key"),
                "sample_every_n": node_sample_every,
                "max_fps": node_max_fps,
            }
            pipeline_frame_samplers.append(sampler)
            limits: list[str] = []
            if node_sample_every > 1:
                limits.append(f"every {node_sample_every}th frame")
            if node_max_fps is not None:
                limits.append(f"{node_max_fps:g} fps")
            if limits:
                warnings.append(
                    f"Pipeline frame sampler {node.get('node_key')} limits processing to "
                    f"{' and '.join(limits)}."
                )
        if node_type in {"output.persisted_result", "output.result"}:
            warnings.append(
                f"Pipeline contains {node_type}; per-frame persistence can add DB latency."
            )
        if node_type.startswith("capture."):
            warnings.append(
                "Pipeline contains capture nodes; capture outputs can add storage and queue "
                "latency."
            )

    total_ms = stage_timings_ms.get("total_ms")
    if total_ms and total_ms > 0:
        theoretical_fps = 1000.0 / total_ms
        if theoretical_fps < 15:
            warnings.append(
                f"Last measured frame hotpath was {total_ms:.0f} ms "
                f"(~{theoretical_fps:.1f} fps ceiling before sampling)."
            )
    source_frame_interval_ms = _float_or_none(stage_timings_ms.get("source_frame_interval_ms"))
    pipeline_task_ms = _float_or_none(stage_timings_ms.get("pipeline_task_ms"))
    if (
        source_frame_interval_ms is not None
        and source_frame_interval_ms > 50
        and (pipeline_task_ms is None or source_frame_interval_ms > pipeline_task_ms * 1.5)
    ):
        source_ceiling_fps = 1000.0 / source_frame_interval_ms
        if max_fps > 0:
            warnings.append(
                f"Source delivered frames every {source_frame_interval_ms:.0f} ms "
                f"(~{source_ceiling_fps:.1f} fps); channel processing is additionally "
                f"capped by source max_fps={max_fps:g}."
            )
        else:
            warnings.append(
                f"Source produced frames every {source_frame_interval_ms:.0f} ms "
                f"(~{source_ceiling_fps:.1f} fps); downstream processing is waiting on camera "
                "delivery."
            )
    if pipeline_node_timings:
        slowest = pipeline_node_timings[0]
        slowest_ms = _float_or_none(slowest.get("total_ms"))
        if slowest_ms is not None and slowest_ms > 50:
            warnings.append(
                f"Slowest pipeline node {slowest.get('node_key')} "
                f"({slowest.get('node_type')}) took {slowest_ms:.0f} ms."
            )
        failed_inference = next(
            (
                item
                for item in pipeline_node_timings
                if item.get("status") == "failed"
                and str(item.get("node_type") or "").startswith("inference.")
            ),
            None,
        )
        if failed_inference is not None:
            error_code = failed_inference.get("error_code") or "inference_failed"
            error_detail = ""
            error_metadata = failed_inference.get("error_metadata")
            mismatches = (
                error_metadata.get("mismatches")
                if isinstance(error_metadata, dict)
                else None
            )
            if isinstance(mismatches, dict):
                mismatch_parts = []
                for key, mismatch in mismatches.items():
                    if not isinstance(mismatch, dict):
                        continue
                    mismatch_parts.append(
                        f"{key}: model={mismatch.get('expected')!s}, "
                        f"frame={mismatch.get('actual')!s}"
                    )
                if mismatch_parts:
                    error_detail = " " + "; ".join(mismatch_parts) + "."
            warnings.append(
                f"Inference node {failed_inference.get('node_key')} dropped one frame "
                f"({error_code}).{error_detail}"
            )
    if pipeline_inference_timings:
        slowest_inference = pipeline_inference_timings[0]
        roundtrip_ms = _float_or_none(slowest_inference.get("client_roundtrip_ms"))
        gateway_total_ms = _float_or_none(slowest_inference.get("gateway_total_ms"))
        if roundtrip_ms is not None and roundtrip_ms > 50:
            detail = f"{roundtrip_ms:.0f} ms client roundtrip"
            if gateway_total_ms is not None:
                detail += f", {gateway_total_ms:.0f} ms gateway total"
            gateway_phases = [
                (
                    "preprocessing",
                    _float_or_none(slowest_inference.get("gateway_preprocessing_ms")),
                ),
                (
                    "Triton roundtrip",
                    _float_or_none(slowest_inference.get("gateway_inference_ms")),
                ),
                (
                    "postprocessing",
                    _float_or_none(slowest_inference.get("gateway_postprocessing_ms")),
                ),
            ]
            dominant_phase, dominant_phase_ms = max(
                gateway_phases,
                key=lambda item: item[1] if item[1] is not None else -1.0,
            )
            if dominant_phase_ms is not None and dominant_phase_ms > 50:
                detail += f"; dominant gateway phase {dominant_phase} {dominant_phase_ms:.0f} ms"
            warnings.append(f"Inference node {slowest_inference.get('node_key')} spent {detail}.")

    source_max_fps = _positive_float(source_config.get("max_fps"))
    return RuntimeLiveDiagnostics(
        source_type=source_type,
        source_max_fps=source_max_fps,
        source_sample_every_n=sample_every_n,
        effective_source_limit_fps=max_fps if max_fps > 0 else None,
        pipeline_max_inflight=pipeline_max_inflight,
        pipeline_configured_max_inflight=configured_pipeline_inflight,
        pipeline_global_max_concurrency=global_inference_limit,
        pipeline_per_release_max_concurrency=per_release_inference_limit,
        pipeline_inflight_limit_reason=pipeline_inflight_limit_reason,
        pipeline_stateful_nodes=stateful_nodes,
        pipeline_frame_samplers=pipeline_frame_samplers,
        pipeline_node_timings=pipeline_node_timings,
        pipeline_inference_timings=pipeline_inference_timings,
        plc_write_outputs=plc_write_outputs or [],
        warnings=list(dict.fromkeys(warnings)),
        stream_transport="rtsp_webrtc_media_fanout",
    )


def _source_configuration_warnings(
    source_type: str,
    source_config: dict[str, Any],
) -> list[str]:
    if source_type != RuntimeSourceType.BASLER_PYLON.value:
        return []
    warnings: list[str] = []
    width = _positive_int(source_config.get("width"), default=0)
    height = _positive_int(source_config.get("height"), default=0)
    camera_fps = _positive_float(source_config.get("fps"))
    pixel_format = str(source_config.get("pixel_format") or "").strip()
    bits_per_pixel = {
        "mono8": 8,
        "bayerrg8": 8,
        "bayerbg8": 8,
        "bayergb8": 8,
        "bayergr8": 8,
        "yuv422packed": 16,
        "rgb8": 24,
        "bgr8": 24,
    }.get(pixel_format.casefold())
    if width and height and camera_fps and bits_per_pixel:
        payload_mbps = width * height * bits_per_pixel * camera_fps / 1_000_000
        link_mbps = _positive_float(source_config.get("link_speed_mbps")) or 1_000.0
        if payload_mbps >= link_mbps * 0.8:
            warnings.append(
                f"Basler payload is approximately {payload_mbps:.0f} Mbit/s before "
                f"transport overhead for {width}x{height} {pixel_format} at "
                f"{camera_fps:g} fps; this is unsafe for a {link_mbps:g}-Mbit/s link. "
                "Use Bayer/Mono8, a smaller ROI, or a lower camera FPS."
            )
    trigger_mode = str(source_config.get("trigger_mode") or "Off").strip().casefold()
    trigger_source = str(source_config.get("trigger_source") or "").strip().casefold()
    acquisition_mode = str(source_config.get("acquisition_mode") or "Continuous").strip().casefold()
    if (
        trigger_mode not in {"", "off", "false", "0"}
        and trigger_source == "software"
        and acquisition_mode == "continuous"
    ):
        warnings.append(
            "Basler Continuous acquisition uses a synchronous software trigger before "
            "each grab; disable TriggerMode for free-running live video unless explicit "
            "software-trigger semantics are required."
        )
    return warnings


def _plc_write_realtime_throttled(plc_write_outputs: list[dict[str, Any]] | None) -> bool:
    for output in plc_write_outputs or []:
        writes = output.get("writes")
        if not isinstance(writes, list):
            continue
        if any(
            isinstance(item, dict)
            and item.get("status") == "skipped"
            and item.get("reason") == "realtime_throttled"
            for item in writes
        ):
            return True
    return False


def _pipeline_node_timings(pipeline_timing: dict[str, Any]) -> list[dict[str, Any]]:
    raw_timings = pipeline_timing.get("node_timings_ms")
    if not isinstance(raw_timings, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in raw_timings:
        if not isinstance(item, dict):
            continue
        row: dict[str, Any] = {}
        for key in (
            "node_key",
            "node_type",
            "node_category",
            "status",
            "skip_reason",
            "error_code",
        ):
            value = item.get(key)
            if value is not None:
                row[key] = str(value)
        error_metadata = item.get("error_metadata")
        if isinstance(error_metadata, dict):
            row["error_metadata"] = error_metadata
        for key in (
            "total_ms",
            "execute_ms",
            "input_collect_ms",
            "output_materialize_ms",
        ):
            value = _float_or_none(item.get(key))
            if value is not None:
                row[key] = round(value, 3)
        output_count = item.get("output_count")
        if isinstance(output_count, int):
            row["output_count"] = output_count
        output_ports = item.get("output_ports")
        if isinstance(output_ports, list):
            row["output_ports"] = [str(port) for port in output_ports]
        missing_inputs = item.get("missing_inputs")
        if isinstance(missing_inputs, list):
            row["missing_inputs"] = [str(port) for port in missing_inputs]
        if row:
            rows.append(row)
    return sorted(rows, key=lambda row: float(row.get("total_ms") or 0), reverse=True)[:10]


def _synchronize_overlay_primitives(
    primitives: list[RuntimeOverlayPrimitiveState],
    *,
    frame_id: str,
    source_timestamp: str | None,
    display_context_id: str | None,
    width: int | None,
    height: int | None,
) -> list[RuntimeOverlayPrimitiveState]:
    resolution = {
        "width": width,
        "height": height,
    }
    synchronized: list[RuntimeOverlayPrimitiveState] = []
    for primitive in primitives:
        metadata = {
            **dict(primitive.metadata or {}),
            "synchronization": {
                "frame_id": frame_id,
                "source_timestamp": source_timestamp,
                "late_policy": "drop_if_older_than_current_frame",
                "stale_policy": "display_until_next_frame_with_stale_threshold",
            },
        }
        synchronized.append(
            primitive.model_copy(
                update={
                    "frame_id": primitive.frame_id or frame_id,
                    "source_timestamp": primitive.source_timestamp or source_timestamp,
                    "display_context_id": primitive.display_context_id or display_context_id,
                    "coordinate_system": primitive.coordinate_system or "image_pixels",
                    "resolution": primitive.resolution or resolution,
                    "metadata": metadata,
                }
            )
        )
    return synchronized


def _pipeline_inference_timings(pipeline_timing: dict[str, Any]) -> list[dict[str, Any]]:
    raw_timings = pipeline_timing.get("inference_timings_ms")
    if not isinstance(raw_timings, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in raw_timings:
        if not isinstance(item, dict):
            continue
        row: dict[str, Any] = {}
        for key in ("node_key", "node_type", "node_category", "output_port"):
            value = item.get(key)
            if value is not None:
                row[key] = str(value)
        for key, value in item.items():
            if key in row:
                continue
            numeric_value = _float_or_none(value)
            if numeric_value is not None:
                row[key] = round(numeric_value, 3)
        if row:
            rows.append(row)
    return sorted(
        rows,
        key=lambda row: float(
            row.get("client_roundtrip_ms")
            or row.get("gateway_total_ms")
            or row.get("total_ms")
            or 0
        ),
        reverse=True,
    )[:10]


def _stateful_pipeline_nodes(graph_nodes: list[dict[str, Any]]) -> list[str]:
    stateful_nodes: list[str] = []
    for node in graph_nodes:
        node_type = str(node.get("node_type") or "")
        if node_type in _STATEFUL_PIPELINE_NODE_TYPES:
            stateful_nodes.append(str(node.get("node_key") or node_type))
    return stateful_nodes


def _stem_measurement_parallel_node_keys(
    executable: ExecutablePipelineGraph,
) -> tuple[str, ...]:
    """Select only the proven independent post-tracker measurement branches.

    The active-stem tracker remains the ordered identity gate.  Its two consumers form the
    independent chains eligibility -> curvature and mask geometry -> surface phase.  Any extra
    cross-edge disables parallel execution fail closed instead of guessing about dependencies.
    """

    nodes_by_type: dict[str, list[str]] = {}
    for executable_node in executable.nodes:
        nodes_by_type.setdefault(executable_node.spec.node_type, []).append(
            executable_node.spec.node_key
        )
    active_keys = nodes_by_type.get("analysis.active_stem_tracker", [])
    if len(active_keys) != 1 or any(
        len(nodes_by_type.get(node_type, [])) != 1
        for node_type in _STEM_MEASUREMENT_PARALLEL_NODE_TYPES
    ):
        return ()

    active = active_keys[0]
    eligibility, geometry, curvature, phase = (
        nodes_by_type[node_type][0] for node_type in _STEM_MEASUREMENT_PARALLEL_NODE_TYPES
    )
    edges = {(edge.from_node_key, edge.to_node_key) for edge in executable.graph.edges}
    required_edges = {
        (active, eligibility),
        (active, geometry),
        (active, curvature),
        (active, phase),
        (eligibility, curvature),
        (geometry, phase),
    }
    if not required_edges.issubset(edges):
        return ()
    curvature_branch = {eligibility, curvature}
    phase_branch = {geometry, phase}
    if any(
        (source in curvature_branch and target in phase_branch)
        or (source in phase_branch and target in curvature_branch)
        for source, target in edges
    ):
        return ()
    return eligibility, geometry, curvature, phase


def _ordered_stem_stateful_node_keys(
    executable: ExecutablePipelineGraph,
) -> tuple[str, ...]:
    """Return per-node sequence barriers only for the proven stem-measurement graph.

    Undistort, rectification, segmentation, and independent mask geometry may overlap across
    frames. Tracker, curvature, phase, and slice-model state each retain their own strict frame
    order. Unknown inference or stateful nodes disable overlap so PLC writes and unrelated
    pipelines retain the legacy one-frame behavior.
    """

    if not _stem_measurement_parallel_node_keys(executable):
        return ()
    nodes_by_type: dict[str, list[str]] = {}
    for item in executable.nodes:
        nodes_by_type.setdefault(item.spec.node_type, []).append(item.spec.node_key)
    required_unique_types = (
        "preprocessing.camera_undistort",
        "preprocessing.process_coordinate_rectify",
        "inference.segmentation",
        "analysis.active_stem_tracker",
        "analysis.stem_horizontal_curvature",
        "analysis.stem_surface_phase",
        "analysis.stem_slice_model",
    )
    if any(len(nodes_by_type.get(node_type, [])) != 1 for node_type in required_unique_types):
        return ()
    inference_nodes = [
        item.spec for item in executable.nodes if item.spec.node_type.startswith("inference.")
    ]
    if len(inference_nodes) != 1 or inference_nodes[0].node_type != "inference.segmentation":
        return ()
    stateful_specs = [
        item.spec
        for item in executable.nodes
        if item.spec.node_type in _STATEFUL_PIPELINE_NODE_TYPES
    ]
    if any(
        item.node_type not in _ORDERED_STEM_OVERLAP_STATEFUL_NODE_TYPES for item in stateful_specs
    ):
        return ()

    undistort = nodes_by_type["preprocessing.camera_undistort"][0]
    canonical = nodes_by_type["preprocessing.process_coordinate_rectify"][0]
    segmentation = nodes_by_type["inference.segmentation"][0]
    active = nodes_by_type["analysis.active_stem_tracker"][0]
    edges = {(edge.from_node_key, edge.to_node_key) for edge in executable.graph.edges}
    if not {
        (undistort, canonical),
        (canonical, segmentation),
        (segmentation, active),
    }.issubset(edges):
        return ()

    barrier_keys = tuple(
        item.node_key
        for item in stateful_specs
        if item.node_type != "preprocessing.camera_undistort"
    )
    required_barrier_keys = {
        active,
        nodes_by_type["analysis.stem_horizontal_curvature"][0],
        nodes_by_type["analysis.stem_surface_phase"][0],
        nodes_by_type["analysis.stem_slice_model"][0],
    }
    if set(barrier_keys) != required_barrier_keys or not barrier_keys or barrier_keys[0] != active:
        return ()
    return barrier_keys


def _inference_pipeline_nodes(graph_nodes: list[dict[str, Any]]) -> list[str]:
    inference_nodes: list[str] = []
    for node in graph_nodes:
        node_type = str(node.get("node_type") or "")
        if node_type.startswith(_INFERENCE_PIPELINE_NODE_PREFIX):
            inference_nodes.append(str(node.get("node_key") or node_type))
    return inference_nodes


def _pipeline_max_inflight(
    settings: PlatformSettings,
    source_config: dict[str, Any],
    *,
    stateful_nodes: list[str],
    inference_nodes: list[str],
    ordered_stateful_overlap: bool = False,
) -> int:
    default_inflight = int(settings.runtime.realtime_pipeline_max_inflight)
    configured = _positive_int(
        source_config.get("pipeline_max_inflight"),
        default=default_inflight,
    )
    gateway_limit = _positive_int(
        getattr(settings.inference_limits, "realtime_max_concurrency", configured),
        default=configured,
    )
    release_limit = _positive_int(
        getattr(
            settings.inference_limits,
            "realtime_per_release_max_concurrency",
            gateway_limit,
        ),
        default=gateway_limit,
    )
    max_inflight = min(
        configured,
        gateway_limit,
        release_limit if inference_nodes else configured,
        16,
    )
    if stateful_nodes and not ordered_stateful_overlap:
        return 1
    return max(max_inflight, 1)


def _pipeline_work_deadline_seconds(
    settings: PlatformSettings,
    graph_nodes: list[dict[str, Any]],
) -> float:
    timeout_ms = int(settings.inference_limits.realtime_timeout_ms)
    for node in graph_nodes:
        node_type = str(node.get("node_type") or "")
        if not node_type.startswith(_INFERENCE_PIPELINE_NODE_PREFIX):
            continue
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        candidate = _positive_int(config.get("timeout_ms"), default=timeout_ms)
        timeout_ms = max(timeout_ms, candidate)
    return max((timeout_ms / 1000.0) + 0.5, settings.runtime.realtime_target_latency_ms / 1000.0)


def _busy_drop_publish_interval_seconds(
    *,
    min_interval: float,
) -> float:
    if min_interval > 0:
        return max(min_interval, 0.02)
    return _LIVE_STATE_WRITE_MIN_INTERVAL_SECONDS


def _positive_float(value: object) -> float | None:
    try:
        parsed = float(value) if value is not None and value != "" else 0.0
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value) if value is not None and value != "" else default
    except (TypeError, ValueError):
        return default
    return max(parsed, 1)


def _overlay_from_result(
    result: RuntimeExecutionResult,
    *,
    graph_nodes: list[dict[str, Any]],
    display_context_id: str | None,
) -> list[RuntimeOverlayPrimitiveState]:
    primitives: list[RuntimeOverlayPrimitiveState] = []
    for entry in result.overlay_outputs:
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if not isinstance(payload, dict):
            continue
        for primitive in payload.get("primitives") or []:
            if isinstance(primitive, dict):
                if _primitive_matches_display(primitive, display_context_id):
                    primitives.append(
                        _primitive_from_payload(
                            primitive,
                            default_layer="overlay",
                            display_context_id=display_context_id,
                        )
                    )
    # Explicit overlays own only the payload that was routed into output.overlay.
    # Diagnostic overlays (for example the board-end coordinate frame) must not
    # hide unrelated detection or segmentation results from the same execution.
    owned_inference_payload_types = _explicit_overlay_owned_payload_types(result.overlay_outputs)
    for entry in result.normalized_inference_outputs:
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if not isinstance(payload, dict):
            continue
        payload_type = payload.get("payload_type")
        if payload_type in owned_inference_payload_types:
            continue
        if payload_type == "detections":
            for index, item in enumerate(payload.get("detections") or []):
                if isinstance(item, dict):
                    image_context_id = _string_or_none(payload.get("image_context_id"))
                    if not _context_matches_display(image_context_id, display_context_id):
                        continue
                    object_ref = _object_ref_at(payload, index)
                    primitives.append(
                        RuntimeOverlayPrimitiveState(
                            primitive_type="box",
                            layer="detections",
                            label=_string_or_none(item.get("label")),
                            confidence=_float_or_none(item.get("score")),
                            geometry=dict(item.get("box") or {}),
                            coordinate_system="normalized_xy",
                            image_context_id=image_context_id,
                            display_context_id=display_context_id,
                            object_id=_string_or_none(object_ref.get("object_id"))
                            if object_ref
                            else None,
                        )
                    )
        if payload_type == "segments":
            for index, item in enumerate(payload.get("segments") or []):
                if isinstance(item, dict):
                    image_context_id = _string_or_none(payload.get("image_context_id"))
                    if not _context_matches_display(image_context_id, display_context_id):
                        continue
                    object_ref = _object_ref_at(payload, index)
                    primitives.append(
                        RuntimeOverlayPrimitiveState(
                            primitive_type="polygon",
                            layer="segmentation",
                            label=_string_or_none(item.get("label")),
                            confidence=_float_or_none(item.get("score")),
                            geometry={"points": item.get("polygon") or []},
                            coordinate_system="normalized_xy",
                            image_context_id=image_context_id,
                            display_context_id=display_context_id,
                            object_id=_string_or_none(object_ref.get("object_id"))
                            if object_ref
                            else None,
                        )
                    )
    for entry in result.derived_outputs:
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if not isinstance(payload, dict):
            continue
        payload_type = payload.get("payload_type")
        if payload_type == "positions":
            for item in payload.get("positions") or []:
                if isinstance(item, dict):
                    image_context_id = _string_or_none(item.get("source_image_context_id"))
                    if not _context_matches_display(image_context_id, display_context_id):
                        continue
                    primitives.append(
                        RuntimeOverlayPrimitiveState(
                            primitive_type="point",
                            layer="motion",
                            label=_string_or_none(item.get("label") or item.get("key")),
                            geometry={"x": item.get("x"), "y": item.get("y")},
                            image_context_id=image_context_id,
                            display_context_id=display_context_id,
                            object_id=_string_or_none(item.get("source_object_id")),
                            metadata=item,
                        )
                    )
        if payload_type == "measurements":
            for item in payload.get("measurements") or []:
                if isinstance(item, dict):
                    image_context_id = _string_or_none(item.get("source_image_context_id"))
                    if not _context_matches_display(image_context_id, display_context_id):
                        continue
                    primitive_type = (
                        "motion"
                        if str(item.get("key") or "").startswith("motion")
                        else "measurement"
                    )
                    primitives.append(
                        RuntimeOverlayPrimitiveState(
                            primitive_type=primitive_type,
                            layer="values",
                            label=_string_or_none(item.get("label") or item.get("key")),
                            geometry={},
                            image_context_id=image_context_id,
                            display_context_id=display_context_id,
                            object_id=_string_or_none(item.get("source_object_id")),
                            metadata=item,
                        )
                    )
    if not result.overlay_outputs:
        primitives.extend(
            _configured_zone_primitives(graph_nodes, display_context_id=display_context_id)
        )
    return _deduplicate_overlay_primitives(primitives)


def _explicit_overlay_owned_payload_types(
    overlay_outputs: list[dict[str, Any]],
) -> set[str]:
    """Return inference payload types intentionally handled by output.overlay.

    Other nodes may emit diagnostic overlays alongside inference results.  Those
    overlays must not claim ownership of detections or segments, otherwise the
    automatic live-view fallback silently hides model results.
    """

    supported_payload_types = {
        "detections",
        "segments",
        "classifications",
        "anomaly",
    }
    owned_payload_types: set[str] = set()
    for entry in overlay_outputs:
        if not isinstance(entry, dict) or entry.get("node_type") != "output.overlay":
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            continue
        source_payload_type = str(metadata.get("source_payload_type") or "")
        if source_payload_type in supported_payload_types:
            owned_payload_types.add(source_payload_type)
    return owned_payload_types


def _deduplicate_overlay_primitives(
    primitives: list[RuntimeOverlayPrimitiveState],
) -> list[RuntimeOverlayPrimitiveState]:
    unique: list[RuntimeOverlayPrimitiveState] = []
    seen: set[tuple[object, ...]] = set()
    for primitive in primitives:
        context_id = primitive.display_context_id or primitive.image_context_id
        object_id = primitive.object_id or primitive.source_object_id
        if object_id:
            identity: tuple[object, ...] = (
                "object",
                primitive.primitive_type,
                primitive.layer,
                context_id,
                object_id,
            )
        else:
            identity = (
                "geometry",
                primitive.primitive_type,
                primitive.layer,
                context_id,
                primitive.label,
                _freeze_overlay_value(primitive.geometry),
            )
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(primitive)
    return unique


def _freeze_overlay_value(value: object) -> object:
    if isinstance(value, dict):
        return tuple(sorted((str(key), _freeze_overlay_value(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_overlay_value(item) for item in value)
    if isinstance(value, float):
        return round(value, 9)
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


_REALTIME_TRANSIENT_INFERENCE_ERROR_CODES = frozenset(
    {
        "inference_gateway_overloaded",
        "inference_gateway_unreachable",
        "inference_timeout",
    }
)

_REALTIME_TRANSIENT_INFERENCE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def _is_realtime_transient_inference_failure(result: RuntimeExecutionResult) -> bool:
    if result.status != "failed":
        return False
    for error in result.errors:
        if not isinstance(error, dict):
            continue
        node_type = str(error.get("node_type") or "")
        node_category = str(error.get("node_category") or "")
        is_inference_error = (
            node_type.startswith("inference.")
            or node_category == "inference"
            or str(error.get("code") or "").startswith("inference_")
        )
        if not is_inference_error:
            continue
        metadata = error.get("metadata")
        code = str(error.get("code") or "")
        if code in _REALTIME_TRANSIENT_INFERENCE_ERROR_CODES:
            return True
        if bool(error.get("retryable")):
            return True
        if isinstance(metadata, dict):
            status_code = _int_or_none(metadata.get("status_code"))
            if status_code in _REALTIME_TRANSIENT_INFERENCE_STATUS_CODES:
                return True
    return False


def _transient_inference_drop_reason(result: RuntimeExecutionResult) -> str:
    for error in result.errors:
        if not isinstance(error, dict):
            continue
        code = str(error.get("code") or "").strip()
        metadata = error.get("metadata")
        status_code = None
        if isinstance(metadata, dict):
            status_code = _int_or_none(metadata.get("status_code"))
        if code:
            return _transient_inference_drop_reason_text(code, status_code=status_code)
        if status_code is not None:
            return f"http_{status_code}"
    return "transient_inference_failure"


def _transient_inference_drop_reason_text(
    code: str,
    *,
    status_code: int | None,
) -> str:
    if status_code is None:
        return code
    return f"{code}_http_{status_code}"


def _runtime_error_summary(result: RuntimeExecutionResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    for error in result.errors:
        if not isinstance(error, dict):
            continue
        metadata = error.get("metadata")
        metadata_dict = metadata if isinstance(metadata, dict) else {}
        return {
            "code": str(error.get("code") or ""),
            "message": _compact_diagnostic_text(str(error.get("message") or "")),
            "node_key": str(error.get("node_key") or ""),
            "node_type": str(error.get("node_type") or ""),
            "retryable": bool(error.get("retryable")),
            "status_code": _int_or_none(metadata_dict.get("status_code")),
            "detail": _compact_diagnostic_text(str(metadata_dict.get("detail") or "")),
        }
    return None


def _runtime_error_warnings(result: RuntimeExecutionResult | None) -> list[str]:
    if result is None:
        return []
    warnings: list[str] = []
    for error in result.errors:
        if not isinstance(error, dict):
            continue
        code = str(error.get("code") or "runtime_error")
        message = _compact_diagnostic_text(str(error.get("message") or "Runtime node failed."))
        metadata = error.get("metadata")
        metadata_dict = metadata if isinstance(metadata, dict) else {}
        status_code = _int_or_none(metadata_dict.get("status_code"))
        detail = _compact_diagnostic_text(str(metadata_dict.get("detail") or ""))
        node = str(error.get("node_key") or error.get("node_type") or "runtime")
        warning = f"Runtime node {node} failed ({code}): {message}"
        if status_code is not None:
            warning += f" HTTP {status_code}."
        if detail:
            warning += f" Detail: {detail}"
        warnings.append(warning)
    return warnings


def _compact_diagnostic_text(value: str, *, limit: int = 360) -> str:
    compacted = " ".join(str(value or "").split())
    if len(compacted) <= limit:
        return compacted
    return f"{compacted[: max(limit - 3, 0)]}..."


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _display_selection_from_result(result: RuntimeExecutionResult) -> dict[str, Any]:
    if result.display_outputs:
        payload = result.display_outputs[-1].get("payload")
        return dict(payload) if isinstance(payload, dict) else {}
    frame = _preferred_display_frame_payload(result)
    if frame and isinstance(frame.get("image_context"), dict):
        context = dict(frame["image_context"])
        return {
            "display_context_id": context.get("context_id"),
            "display_context": context,
            "frame": frame,
            "selection": "default_primary_visual_context",
        }
    return {}


def _display_frame_from_result(
    result: RuntimeExecutionResult,
    *,
    display_context_id: str | None,
    fallback_frame: FramePayload,
) -> FramePayload:
    for frame_payload in reversed(_frame_payloads_from_result(result)):
        context = frame_payload.get("image_context")
        context_id = context.get("context_id") if isinstance(context, dict) else None
        if display_context_id and context_id != display_context_id:
            continue
        try:
            return FramePayload.model_validate(frame_payload)
        except Exception:
            continue
    return fallback_frame


def _preferred_display_frame_payload(result: RuntimeExecutionResult) -> dict[str, Any] | None:
    frames = _frame_payloads_from_result(result)
    for frame in reversed(frames):
        context = frame.get("image_context")
        kind = context.get("kind") if isinstance(context, dict) else None
        if kind not in {"processed", "masked"}:
            return frame
    return frames[-1] if frames else None


def _frame_payloads_from_result(result: RuntimeExecutionResult) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for outputs in result.node_outputs.values():
        if not isinstance(outputs, dict):
            continue
        for payload in outputs.values():
            if isinstance(payload, dict) and payload.get("payload_type") == "frame":
                frames.append(payload)
    for entry in result.display_outputs:
        payload = entry.get("payload") if isinstance(entry, dict) else None
        frame = payload.get("frame") if isinstance(payload, dict) else None
        if isinstance(frame, dict) and frame.get("payload_type") == "frame":
            frames.append(frame)
    return frames


def _undistorted_frame_from_result(result: RuntimeExecutionResult) -> FramePayload | None:
    for payload in reversed(_frame_payloads_from_result(result)):
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("operation") != "camera_undistort":
            continue
        try:
            return FramePayload.model_validate(payload)
        except Exception:
            continue
    return None


def _nonspatial_overlay_primitives(
    primitives: list[RuntimeOverlayPrimitiveState],
) -> list[RuntimeOverlayPrimitiveState]:
    return [primitive for primitive in primitives if not primitive.geometry]


def _source_overlay_from_crop(
    primitives: list[RuntimeOverlayPrimitiveState],
    *,
    display_frame: FramePayload,
    source_frame: FramePayload,
) -> list[RuntimeOverlayPrimitiveState] | None:
    context = display_frame.image_context
    if context is None or context.transform.transform_type != "crop":
        return None
    parameters = context.transform.parameters
    try:
        x = float(parameters.get("x") or 0)
        y = float(parameters.get("y") or 0)
        crop_width = float(parameters.get("width") or display_frame.width)
        crop_height = float(parameters.get("height") or display_frame.height)
        source_width = float(parameters.get("source_width") or source_frame.width)
        source_height = float(parameters.get("source_height") or source_frame.height)
    except (TypeError, ValueError):
        return None
    if (
        crop_width <= 0
        or crop_height <= 0
        or source_width <= 0
        or source_height <= 0
        or (int(source_width), int(source_height))
        != (source_frame.width, source_frame.height)
    ):
        return None
    source_context_id = (
        source_frame.image_context.context_id
        if source_frame.image_context is not None
        else None
    )
    projected: list[RuntimeOverlayPrimitiveState] = []
    for primitive in primitives:
        updates: dict[str, Any] = {
            "resolution": {
                "width": source_frame.width,
                "height": source_frame.height,
            },
            "image_context_id": source_context_id,
            "display_context_id": source_context_id,
            "projected_from_context_id": (
                primitive.image_context_id
                or primitive.display_context_id
                or context.context_id
            ),
            "metadata": {
                **primitive.metadata,
                "projected_from_crop": {
                    "x": x,
                    "y": y,
                    "width": crop_width,
                    "height": crop_height,
                },
            },
        }
        if not primitive.geometry:
            projected.append(primitive.model_copy(update=updates))
            continue
        if primitive.primitive_type in {"mask", "heatmap"}:
            data = primitive.geometry.get("data")
            if not isinstance(data, str) or not data:
                continue
            try:
                projected_data = _project_crop_overlay_texture_to_source(
                    data,
                    crop_x=int(round(x)),
                    crop_y=int(round(y)),
                    crop_width=int(round(crop_width)),
                    crop_height=int(round(crop_height)),
                    source_width=source_frame.width,
                    source_height=source_frame.height,
                    nearest=primitive.primitive_type == "mask",
                )
            except ValueError:
                continue
            projected.append(
                primitive.model_copy(
                    update={
                        **updates,
                        "coordinate_system": "normalized_xy",
                        "geometry": {
                            **primitive.geometry,
                            "data": projected_data,
                            "coordinate_system": "normalized_xy",
                        },
                    }
                )
            )
            continue
        try:
            geometry = _crop_overlay_geometry_to_source(
                primitive.geometry,
                coordinate_system=primitive.coordinate_system,
                crop_x=x,
                crop_y=y,
                crop_width=crop_width,
                crop_height=crop_height,
                source_width=source_width,
                source_height=source_height,
            )
        except ValueError:
            continue
        projected.append(
            primitive.model_copy(
                update={
                    **updates,
                    "coordinate_system": str(geometry["coordinate_system"]),
                    "geometry": geometry,
                }
            )
        )
    return projected


def _crop_overlay_geometry_to_source(
    geometry: dict[str, Any],
    *,
    coordinate_system: str,
    crop_x: float,
    crop_y: float,
    crop_width: float,
    crop_height: float,
    source_width: float,
    source_height: float,
) -> dict[str, Any]:
    source_coordinates = str(
        geometry.get("coordinate_system") or coordinate_system or "normalized_xy"
    )
    normalized = source_coordinates in {
        "normalized",
        "normalized_xy",
        "normalized_xyxy",
        "normalized_polygon",
    }
    if not normalized and source_coordinates not in {
        "image_pixels",
        "pixel",
        "pixels",
        "pixel_xy",
    }:
        raise ValueError(f"unsupported overlay coordinate system: {source_coordinates}")

    def project_x(value: object) -> float:
        number = float(value)
        return (
            (crop_x + number * crop_width) / source_width
            if normalized
            else crop_x + number
        )

    def project_y(value: object) -> float:
        number = float(value)
        return (
            (crop_y + number * crop_height) / source_height
            if normalized
            else crop_y + number
        )

    def project_point(value: object) -> dict[str, Any]:
        if not isinstance(value, dict) or "x" not in value or "y" not in value:
            raise ValueError("overlay point must contain x and y")
        return {**value, "x": project_x(value["x"]), "y": project_y(value["y"])}

    projected = dict(geometry)
    if isinstance(geometry.get("points"), list):
        projected["points"] = [project_point(item) for item in geometry["points"]]
    for key in ("start", "end", "origin", "tip"):
        if key in geometry:
            projected[key] = project_point(geometry[key])
    for x_key, y_key in (
        ("x", "y"),
        ("x1", "y1"),
        ("x2", "y2"),
        ("x_min", "y_min"),
        ("x_max", "y_max"),
    ):
        if x_key in geometry and y_key in geometry:
            projected[x_key] = project_x(geometry[x_key])
            projected[y_key] = project_y(geometry[y_key])
    if "width" in geometry:
        projected["width"] = float(geometry["width"]) * (
            crop_width / source_width if normalized else 1.0
        )
    if "height" in geometry:
        projected["height"] = float(geometry["height"]) * (
            crop_height / source_height if normalized else 1.0
        )
    projected["coordinate_system"] = "normalized_xy" if normalized else "image_pixels"
    return projected


def _source_overlay_from_image_view(
    primitives: list[RuntimeOverlayPrimitiveState],
    *,
    display_frame: FramePayload,
    source_frame: FramePayload | None,
) -> list[RuntimeOverlayPrimitiveState]:
    if source_frame is None:
        return []
    raw_profile = display_frame.metadata.get("image_view_profile")
    if not isinstance(raw_profile, dict):
        return []
    try:
        profile: RuntimeImageViewProfile = validate_image_view_profile(raw_profile)
    except ValueError:
        return []
    if (source_frame.width, source_frame.height) != (
        profile.source_image_width,
        profile.source_image_height,
    ):
        return []
    source_context_id = (
        source_frame.image_context.context_id
        if source_frame.image_context is not None
        else None
    )
    projected: list[RuntimeOverlayPrimitiveState] = []
    for primitive in primitives:
        projected_from_context_id = (
            primitive.image_context_id
            or primitive.display_context_id
            or (
                display_frame.image_context.context_id
                if display_frame.image_context is not None
                else None
            )
        )
        common_updates: dict[str, Any] = {
            "coordinate_system": "normalized_xy",
            "resolution": {
                "width": profile.source_image_width,
                "height": profile.source_image_height,
            },
            "image_context_id": source_context_id,
            "display_context_id": source_context_id,
            "projected_from_context_id": projected_from_context_id,
            "metadata": {
                **primitive.metadata,
                "projected_from_view_profile_id": profile.view_profile_id,
                "view_profile_checksum": profile.profile_checksum,
            },
        }
        if primitive.primitive_type in {"mask", "heatmap"}:
            data = primitive.geometry.get("data")
            if not isinstance(data, str) or not data:
                continue
            try:
                projected_data = _project_overlay_texture_to_undistorted(
                    data,
                    profile=profile,
                    nearest=primitive.primitive_type == "mask",
                )
            except ValueError:
                continue
            projected.append(
                primitive.model_copy(
                    update={
                        **common_updates,
                        "geometry": {
                            **primitive.geometry,
                            "data": projected_data,
                            "coordinate_system": "normalized_xy",
                        },
                    }
                )
            )
            continue
        if not primitive.geometry:
            projected.append(primitive.model_copy(update=common_updates))
            continue
        geometry = {
            **primitive.geometry,
            "coordinate_system": primitive.coordinate_system,
        }
        try:
            projected_geometry = view_overlay_geometry_to_source(geometry, profile)
        except ValueError:
            continue
        projected_type = (
            "polygon"
            if primitive.primitive_type in {"box", "roi", "zone"}
            and isinstance(projected_geometry.get("points"), list)
            else primitive.primitive_type
        )
        projected.append(
            primitive.model_copy(
                update={
                    "primitive_type": projected_type,
                    "geometry": projected_geometry,
                    **common_updates,
                }
            )
        )
    return projected


def _intrinsic_calibration_from_graph(
    graph_nodes: list[dict[str, Any]],
) -> RuntimeCameraIntrinsicCalibrationProfile | None:
    candidates: list[RuntimeCameraIntrinsicCalibrationProfile] = []
    for node in graph_nodes:
        if node.get("node_type") != "preprocessing.camera_undistort":
            continue
        config = node.get("config")
        calibration = config.get("calibration") if isinstance(config, dict) else None
        if not isinstance(calibration, dict):
            continue
        try:
            candidates.append(validate_camera_intrinsic_calibration(calibration))
        except ValueError:
            continue
    return candidates[0] if len(candidates) == 1 else None


def _raw_overlay_from_undistorted(
    primitives: list[RuntimeOverlayPrimitiveState],
    *,
    calibration: RuntimeCameraIntrinsicCalibrationProfile | None,
    raw_frame: FramePayload,
) -> list[RuntimeOverlayPrimitiveState]:
    if calibration is None or (raw_frame.width, raw_frame.height) != (
        calibration.image_width,
        calibration.image_height,
    ):
        return []
    raw_context_id = (
        raw_frame.image_context.context_id if raw_frame.image_context is not None else None
    )
    projected: list[RuntimeOverlayPrimitiveState] = []
    for primitive in primitives:
        common_updates: dict[str, Any] = {
            "coordinate_system": "normalized_xy",
            "resolution": {
                "width": calibration.image_width,
                "height": calibration.image_height,
            },
            "image_context_id": raw_context_id,
            "display_context_id": raw_context_id,
            "projected_from_context_id": (
                primitive.image_context_id or primitive.display_context_id
            ),
            "metadata": {
                **primitive.metadata,
                "projected_from_intrinsic_calibration_hash": calibration.calibration_hash,
            },
        }
        if primitive.primitive_type in {"mask", "heatmap"}:
            data = primitive.geometry.get("data")
            if not isinstance(data, str) or not data:
                continue
            try:
                projected_data = _project_overlay_texture_to_raw(
                    data,
                    calibration=calibration,
                    nearest=primitive.primitive_type == "mask",
                )
            except ValueError:
                continue
            projected.append(
                primitive.model_copy(
                    update={
                        **common_updates,
                        "geometry": {
                            **primitive.geometry,
                            "data": projected_data,
                            "coordinate_system": "normalized_xy",
                        },
                    }
                )
            )
            continue
        if not primitive.geometry:
            projected.append(primitive.model_copy(update=common_updates))
            continue
        try:
            geometry = _undistorted_overlay_geometry_to_raw(
                {
                    **primitive.geometry,
                    "coordinate_system": primitive.coordinate_system,
                },
                calibration=calibration,
            )
        except ValueError:
            continue
        projected.append(
            primitive.model_copy(
                update={
                    **common_updates,
                    "geometry": geometry,
                }
            )
        )
    return projected


def _undistorted_overlay_geometry_to_raw(
    geometry: dict[str, Any],
    *,
    calibration: RuntimeCameraIntrinsicCalibrationProfile,
) -> dict[str, Any]:
    coordinate_system = str(geometry.get("coordinate_system") or "normalized_xy")

    def raw_point(value: object) -> dict[str, float]:
        if not isinstance(value, dict):
            raise ValueError("overlay point must be an object")
        try:
            x = float(value.get("x"))
            y = float(value.get("y"))
        except (TypeError, ValueError) as exc:
            raise ValueError("overlay point coordinates must be numeric") from exc
        if coordinate_system in {
            "normalized",
            "normalized_xy",
            "normalized_xyxy",
            "normalized_polygon",
        }:
            x *= max(calibration.image_width - 1, 1)
            y *= max(calibration.image_height - 1, 1)
        elif coordinate_system not in {"image_pixels", "pixel", "pixels", "pixel_xy"}:
            raise ValueError(f"unsupported overlay coordinate system: {coordinate_system}")
        projected = undistorted_points_to_raw([(x, y)], calibration)[0]
        return {
            "x": float(projected[0]) / max(calibration.image_width - 1, 1),
            "y": float(projected[1]) / max(calibration.image_height - 1, 1),
        }

    projected = dict(geometry)
    if isinstance(geometry.get("points"), list):
        projected["points"] = [raw_point(item) for item in geometry["points"]]
    for key in ("start", "end", "origin", "tip"):
        if key in geometry:
            projected[key] = raw_point(geometry[key])
    if "x" in geometry and "y" in geometry:
        projected.update(raw_point({"x": geometry["x"], "y": geometry["y"]}))
    projected["coordinate_system"] = "normalized_xy"
    projected["projected_from_intrinsic_calibration_hash"] = calibration.calibration_hash
    return projected


def _project_overlay_texture_to_undistorted(
    data: str,
    *,
    profile: RuntimeImageViewProfile,
    nearest: bool,
) -> str:
    cv2, np = _overlay_texture_modules()
    texture = _decode_overlay_texture(data, cv2=cv2, np=np)
    if texture.shape[:2] != (profile.output_height, profile.output_width):
        raise ValueError("canonical overlay texture resolution does not match the view profile")
    try:
        projected = cv2.warpPerspective(
            texture,
            np.asarray(profile.view_to_source_homography, dtype=np.float64).reshape(3, 3),
            (profile.source_image_width, profile.source_image_height),
            flags=cv2.INTER_NEAREST if nearest else cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
    except Exception as exc:
        raise ValueError("canonical overlay texture projection failed") from exc
    return _encode_overlay_texture(projected, cv2=cv2)


def _project_overlay_texture_to_raw(
    data: str,
    *,
    calibration: RuntimeCameraIntrinsicCalibrationProfile,
    nearest: bool,
) -> str:
    cv2, np = _overlay_texture_modules()
    texture = _decode_overlay_texture(data, cv2=cv2, np=np)
    try:
        projected = apply_camera_distortion(
            texture,
            calibration,
            interpolation="nearest" if nearest else "linear",
        )
    except Exception as exc:
        raise ValueError("raw overlay texture projection failed") from exc
    return _encode_overlay_texture(projected, cv2=cv2)


def _project_crop_overlay_texture_to_source(
    data: str,
    *,
    crop_x: int,
    crop_y: int,
    crop_width: int,
    crop_height: int,
    source_width: int,
    source_height: int,
    nearest: bool,
) -> str:
    cv2, np = _overlay_texture_modules()
    texture = _decode_overlay_texture(data, cv2=cv2, np=np)
    if texture.shape[:2] != (crop_height, crop_width):
        texture = cv2.resize(
            texture,
            (crop_width, crop_height),
            interpolation=cv2.INTER_NEAREST if nearest else cv2.INTER_LINEAR,
        )
    channels = () if texture.ndim == 2 else (texture.shape[2],)
    projected = np.zeros((source_height, source_width, *channels), dtype=texture.dtype)
    left = max(crop_x, 0)
    top = max(crop_y, 0)
    right = min(crop_x + crop_width, source_width)
    bottom = min(crop_y + crop_height, source_height)
    if left >= right or top >= bottom:
        raise ValueError("canonical crop lies outside the source frame")
    texture_left = left - crop_x
    texture_top = top - crop_y
    projected[top:bottom, left:right] = texture[
        texture_top : texture_top + (bottom - top),
        texture_left : texture_left + (right - left),
    ]
    return _encode_overlay_texture(projected, cv2=cv2)


def _overlay_texture_modules() -> tuple[Any, Any]:
    try:
        import cv2
        import numpy as np
    except Exception as exc:
        raise ValueError("overlay texture projection requires OpenCV and NumPy") from exc
    return cv2, np


def _decode_overlay_texture(data: str, *, cv2: Any, np: Any) -> Any:  # noqa: ANN401
    encoded = data.split(",", 1)[1] if data.startswith("data:") and "," in data else data
    try:
        payload = base64.b64decode(encoded.encode("ascii"), validate=True)
    except Exception as exc:
        raise ValueError("overlay texture is not valid base64") from exc
    try:
        texture = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    except Exception as exc:
        raise ValueError("overlay texture could not be decoded") from exc
    if texture is None:
        raise ValueError("overlay texture could not be decoded")
    return texture


def _encode_overlay_texture(texture: Any, *, cv2: Any) -> str:  # noqa: ANN401
    try:
        success, encoded = cv2.imencode(".png", texture)
    except Exception as exc:
        raise ValueError("overlay texture could not be encoded") from exc
    if not success:
        raise ValueError("overlay texture could not be encoded")
    return base64.b64encode(encoded.tobytes()).decode("ascii")


def _configured_zone_primitives(
    graph_nodes: list[dict[str, Any]],
    *,
    display_context_id: str | None,
) -> list[RuntimeOverlayPrimitiveState]:
    primitives: list[RuntimeOverlayPrimitiveState] = []
    for node in graph_nodes:
        config = node.get("config") if isinstance(node, dict) else None
        if not isinstance(config, dict):
            continue
        for key, layer, primitive_type in (
            ("zones", "zones", "zone"),
            ("regions", "rois", "roi"),
            ("rois", "rois", "roi"),
        ):
            for index, item in enumerate(config.get(key) or [], start=1):
                if not isinstance(item, dict):
                    continue
                geometry = _box_geometry(item)
                if not geometry:
                    continue
                primitives.append(
                    RuntimeOverlayPrimitiveState(
                        primitive_type=primitive_type,
                        layer=layer,
                        label=_string_or_none(
                            item.get("label") or item.get("key") or f"{key}_{index}"
                        ),
                        geometry=geometry,
                        display_context_id=display_context_id,
                        metadata={"node_key": node.get("node_key")},
                    )
                )
    return primitives


def _primitive_matches_display(
    primitive: dict[str, Any],
    display_context_id: str | None,
) -> bool:
    primitive_context_id = _string_or_none(
        primitive.get("display_context_id") or primitive.get("image_context_id")
    )
    return _context_matches_display(primitive_context_id, display_context_id)


def _context_matches_display(
    image_context_id: str | None,
    display_context_id: str | None,
) -> bool:
    if not display_context_id or not image_context_id:
        return True
    return image_context_id == display_context_id


def _object_ref_at(payload: dict[str, Any], index: int) -> dict[str, Any] | None:
    objects = payload.get("objects")
    if not isinstance(objects, list):
        return None
    if 0 <= index < len(objects) and isinstance(objects[index], dict):
        return objects[index]
    return None


def _primitive_from_payload(
    payload: dict[str, Any],
    *,
    default_layer: str,
    display_context_id: str | None,
) -> RuntimeOverlayPrimitiveState:
    primitive_type = str(payload.get("primitive_type") or "label")
    if primitive_type not in {
        "box",
        "polygon",
        "point",
        "label",
        "measurement",
        "heatmap",
        "mask",
        "line",
        "axis",
        "arrow",
        "roi",
        "zone",
        "motion",
    }:
        primitive_type = "label"
    layer = _string_or_none(payload.get("layer")) or _layer_for_primitive(
        primitive_type,
        default_layer,
    )
    geometry = dict(payload.get("geometry") or {})
    metadata = dict(payload.get("metadata") or {})
    object_ref = metadata.get("object_ref")
    object_coordinate_system = (
        _string_or_none(object_ref.get("coordinate_system"))
        if isinstance(object_ref, dict)
        else None
    )
    coordinate_system = (
        _string_or_none(payload.get("coordinate_system"))
        or _string_or_none(geometry.get("coordinate_system"))
        or object_coordinate_system
        or "image_pixels"
    )
    return RuntimeOverlayPrimitiveState(
        primitive_type=primitive_type,  # type: ignore[arg-type]
        layer=layer,
        label=_string_or_none(payload.get("label")),
        confidence=_float_or_none(payload.get("confidence")),
        geometry=geometry,
        coordinate_system=coordinate_system,
        image_context_id=_string_or_none(payload.get("image_context_id")),
        display_context_id=display_context_id or _string_or_none(payload.get("display_context_id")),
        object_id=_string_or_none(payload.get("object_id")),
        source_object_id=_string_or_none(payload.get("source_object_id")),
        projected_from_context_id=_string_or_none(payload.get("projected_from_context_id")),
        style=dict(payload.get("style") or {}),
        metadata=metadata,
    )


def _layer_for_primitive(primitive_type: str, default_layer: str) -> str:
    return {
        "box": "detections",
        "polygon": "segmentation",
        "point": "motion",
        "measurement": "values",
        "motion": "motion",
        "zone": "zones",
        "roi": "rois",
    }.get(primitive_type, default_layer)


def _box_geometry(item: dict[str, Any]) -> dict[str, Any]:
    angle_degrees = _box_angle_degrees(item)
    if {"x_min", "y_min", "x_max", "y_max"}.issubset(item):
        geometry = {
            "x_min": item["x_min"],
            "y_min": item["y_min"],
            "x_max": item["x_max"],
            "y_max": item["y_max"],
        }
        if angle_degrees:
            geometry["angle_degrees"] = angle_degrees
        return geometry
    if {"x", "y", "width", "height"}.issubset(item):
        geometry = {
            "x_min": item["x"],
            "y_min": item["y"],
            "x_max": float(item["x"]) + float(item["width"]),
            "y_max": float(item["y"]) + float(item["height"]),
        }
        if angle_degrees:
            geometry["angle_degrees"] = angle_degrees
        return geometry
    if isinstance(item.get("box"), dict):
        geometry = dict(item["box"])
        if angle_degrees:
            geometry["angle_degrees"] = angle_degrees
        return geometry
    return {}


def _box_angle_degrees(item: dict[str, Any]) -> float:
    for key in ("angle_degrees", "rotation_degrees", "angle"):
        parsed = _float_or_none(item.get(key))
        if parsed is not None:
            return _normalize_angle_degrees(parsed)
    nested_box = item.get("box")
    if isinstance(nested_box, dict):
        for key in ("angle_degrees", "rotation_degrees", "angle"):
            parsed = _float_or_none(nested_box.get(key))
            if parsed is not None:
                return _normalize_angle_degrees(parsed)
    return 0.0


def _normalize_angle_degrees(value: float) -> float:
    return round(((value + 180.0) % 360.0) - 180.0, 6)


def _window_event_count(
    events: deque[tuple[float, int]],
    *,
    now: float,
    window_seconds: float = _METRICS_WINDOW_SECONDS,
) -> int:
    cutoff = now - max(float(window_seconds), 0.1)
    while events and events[0][0] < cutoff:
        events.popleft()
    return sum(count for _timestamp, count in tuple(events))


def _percentile(values: deque[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    index = min(max(round((percentile / 100) * (len(ordered) - 1)), 0), len(ordered) - 1)
    return round(ordered[index], 3)


def _stem_shadow_mode_enabled(graph_nodes: list[dict[str, Any]]) -> bool:
    return any(
        node.get("node_type") == "analysis.stem_rotation_optimizer"
        or (
            node.get("node_type") == "analysis.stem_curvature_phase_solver"
            and bool((node.get("config") or {}).get("shadow_mode_enabled", False))
        )
        for node in graph_nodes
        if isinstance(node, dict)
    )


def _stem_rotation_view_from_result(
    result: RuntimeExecutionResult,
) -> RuntimeStemRotationViewModel | None:
    """Return the latest validated HMI projection emitted by the optimizer node."""
    for entry in reversed(result.persisted_results):
        if not isinstance(entry, dict):
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        value = payload.get("result")
        if not isinstance(value, dict):
            continue
        if value.get("schema_version") != "stem.rotation-optimization.v1":
            continue
        view_model = value.get("view_model")
        if not isinstance(view_model, dict):
            continue
        try:
            return RuntimeStemRotationViewModel.model_validate(view_model)
        except ValueError:
            LOGGER.warning(
                "Ignoring invalid stem rotation HMI view model",
                exc_info=True,
            )
            return None
    return None


def _stem_orientation_history_event_key(result: RuntimeExecutionResult) -> str | None:
    """Identify one immutable pre-entry decision (or later post-check) in history outputs."""
    for entry in result.persisted_results:
        if not isinstance(entry, dict):
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        value = payload.get("result")
        if not isinstance(value, dict):
            continue
        if value.get("schema_version") == "stem.rotation-optimization.v1":
            if not bool(value.get("result_ready")):
                continue
            session_id = str(value.get("session_id") or value.get("stem_id") or "").strip()
            model_hash = str(value.get("model_hash") or "no-model").strip()
            priority_mode = str(value.get("priority_mode") or "NONE").strip()
            priority_revision = value.get("priority_revision")
            if not session_id:
                continue
            return (
                f"stem.rotation-optimization.v1:{session_id}:{model_hash}:"
                f"{priority_mode}:{priority_revision}"
            )
        if value.get("schema_version") not in {
            "stem.orientation.v2",
            "stem.orientation.v3",
        }:
            continue
        schema_version = str(value["schema_version"])
        decision = value.get("decision")
        decision_ready = bool(value.get("decision_ready")) or (
            isinstance(decision, dict) and bool(decision.get("ready"))
        )
        if not decision_ready:
            continue
        session_id = str(value.get("session_id") or "").strip()
        if not session_id:
            continue
        if schema_version == "stem.orientation.v3":
            frame_sequence = value.get("decision_frame_sequence")
        else:
            decision_frame = value.get("decision_frame")
            frame_sequence = (
                decision_frame.get("frame_sequence")
                if isinstance(decision_frame, dict)
                else decision.get("frame_sequence")
                if isinstance(decision, dict)
                else None
            )
        phase = (
            "post_rotation"
            if value.get("post_rotation_verification") is not None
            else "pre_entry"
        )
        return f"{schema_version}:{session_id}:{frame_sequence}:{phase}"
    return None


def _default_source_factory(
    source_type: str,
    config: dict[str, Any],
    secret_refs: dict[str, str],
) -> RealtimeFrameSource:
    return build_frame_source(source_type=source_type, config=config, secret_refs=secret_refs)


def _default_inference_client(settings: PlatformSettings) -> HttpInferenceGatewayClient:
    timeout_seconds = max((settings.inference_limits.realtime_timeout_ms / 1000) + 0.25, 0.3)
    bearer_token = settings.runtime.inference_gateway_bearer_token()
    if settings.api_keys.enabled and not bearer_token:
        LOGGER.warning(
            "Realtime executor is calling the inference gateway without a machine token "
            "while API key authentication is enabled. Configure "
            "runtime.inference_gateway_bearer_token_file."
        )
    return HttpInferenceGatewayClient(
        base_url=settings.runtime.inference_gateway_realtime_url,
        bearer_token=bearer_token,
        timeout_seconds=timeout_seconds,
        overload_policy="wait",
        overload_retry_interval_seconds=0.025,
        overload_retry_max_interval_seconds=0.25,
    )


def _string_or_none(value: object) -> str | None:
    return str(value) if value is not None else None


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
