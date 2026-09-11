from __future__ import annotations

import base64
import os
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from cv_contracts import (
    ClassificationInferenceResponse,
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
    RuntimeLiveFrame,
    RuntimeLiveSourceHealth,
    RuntimeLiveViewState,
    RuntimeLogicalModelRef,
    RuntimeNodeSpec,
    RuntimePipelineDefinitionCreateRequest,
    RuntimePipelineGraph,
    RuntimePipelineVersionCreateRequest,
    RuntimeResultHistoryPolicy,
    RuntimeStemRotationViewModel,
)
from cv_core import PlatformSettings
from cv_db import (
    ResultRecord,
    RuntimeChannel,
    RuntimeExecution,
    create_pipeline_definition,
    create_pipeline_version,
    create_runtime_channel,
    get_runtime_live_view_state,
    publish_pipeline_version,
    request_runtime_channel_start,
    request_runtime_channel_stop,
    session_scope,
    trigger_live_view_runtime_id,
    write_runtime_live_view_state,
)
from cv_db.live_view_store import (
    _missing_live_state_recovery_hint,
    _with_split_frame_payload,
    runtime_live_view_frame_path,
    runtime_live_view_source_frame_path,
    runtime_live_view_undistorted_frame_path,
)
from cv_db.overlay_event_store import (
    read_runtime_overlay_event,
    write_runtime_overlay_event,
)
from cv_runtime import (
    RuntimeModelBinding,
    RuntimeNodeExecutionError,
    validate_basler_link_budget,
)
from cv_runtime.high_rate_phase import HighRatePhaseConfig, HighRateStemPhaseBridge
from cv_runtime.nodes.builtin import _prepare_inference_image, _restore_segmentation_geometry
from cv_runtime.payloads import (
    FramePayload,
    RuntimeExecutionResult,
    RuntimeImageContext,
    RuntimeImageTransform,
)
from cv_status import (
    DeploymentReleaseStatus,
    HealthState,
    RuntimeMode,
    RuntimeNodeCategory,
    RuntimeObservedState,
    RuntimeSourceType,
    TargetPath,
    TaskType,
)
from cv_utils import utc_now_iso
from realtime_executor import live as live_module
from realtime_executor.live import (
    LatestFrameBuffer,
    RealtimeRuntimeWorker,
    _CachedRuntimeModelResolver,
    _default_inference_client,
    _HighRateStemPhaseRuntime,
    _OrderedHighRateFrameBuffer,
    _OrderedStatefulNodeGate,
    _PendingPipelineWork,
    _pipeline_max_inflight,
    _stateful_pipeline_nodes,
    _stem_orientation_history_event_key,
    _stem_rotation_view_from_result,
    _transient_inference_drop_reason,
)
from realtime_executor.sources import (
    _DEFAULT_OPENCV_FFMPEG_CAPTURE_OPTIONS,
    FramePacket,
    OpenCvRtspFrameSource,
    OpenCvUsbFrameSource,
    RtspSourceConfigurationError,
    SourceHealth,
    _aravis_bridge_env_content,
    _aravis_rtsp_source_config,
    _basler_effective_camera_profile,
    _blackfly_device_selector,
    _blackfly_gentl_cti_paths,
    build_frame_source,
)
from runtime_orchestrator.supervisor import RuntimeControlOrchestrator, RuntimeExecutorSupervisor
from tests.conftest import build_test_settings, configure_sqlite_factories
from tests.support.runtime_model_seed import seed_deployed_runtime_model


def test_stem_decision_history_event_bypasses_sampling_once() -> None:
    result = RuntimeExecutionResult(
        status="completed",
        pipeline_version_id="plv_stem",
        correlation_id="corr_stem",
        started_at="2026-08-03T10:00:00Z",
        completed_at="2026-08-03T10:00:01Z",
        persisted_results=[
            {
                "node_key": "result",
                "payload": {
                    "payload_type": "result",
                    "result": {
                        "schema_version": "stem.orientation.v2",
                        "session_id": "session-7",
                        "decision_ready": True,
                        "decision": {"ready": True, "frame_sequence": 152},
                        "decision_frame": {"frame_sequence": 152},
                        "post_rotation_verification": None,
                    },
                },
            }
        ],
    )
    event_key = _stem_orientation_history_event_key(result)
    worker = object.__new__(RealtimeRuntimeWorker)
    worker._result_history_policy = RuntimeResultHistoryPolicy(
        enabled=True,
        retention_hours=24,
        sample_interval_seconds=60,
        max_records=100,
    )
    worker._last_history_persist_perf = time.perf_counter()
    worker._last_history_event_key = None

    assert event_key == "stem.orientation.v2:session-7:152:pre_entry"
    assert worker._history_result_persist_due(result) is True

    worker._last_history_event_key = event_key
    assert worker._history_result_persist_due(result) is False


def test_high_rate_phase_queue_is_ordered_and_bounded() -> None:
    queue = _OrderedHighRateFrameBuffer(3)

    def packet(sequence: int) -> FramePacket:
        return FramePacket(
            image_bytes=b"frame",
            mime_type="image/jpeg",
            width=1,
            height=1,
            source_timestamp=f"2026-08-19T10:00:00.{sequence:03d}Z",
            source_uri="test://phase",
            sequence=sequence,
            metadata={},
        )

    assert queue.push(packet(1)) == 0
    assert queue.push(packet(2)) == 0
    assert queue.push(packet(3)) == 0
    assert queue.push(packet(4)) == 1

    assert queue.pop(0).packet.sequence == 2
    assert queue.pop(0).packet.sequence == 3
    assert queue.pop(0).packet.sequence == 4

    assert queue.push(packet(5)) == 0
    assert queue.push(packet(6)) == 0
    assert queue.clear() == 2
    assert queue.pop(0) is None


def test_high_rate_phase_runtime_resets_bounded_state_after_camera_reconnect() -> None:
    runtime = object.__new__(_HighRateStemPhaseRuntime)
    runtime._last_source_reconnect_count = 0
    runtime._buffer = _OrderedHighRateFrameBuffer(3)
    runtime.bridge = HighRateStemPhaseBridge(HighRatePhaseConfig())
    packet = FramePacket(
        image_bytes=b"frame",
        mime_type="image/jpeg",
        width=1,
        height=1,
        source_timestamp="2026-08-19T10:00:00Z",
        source_uri="test://phase",
        sequence=1,
        metadata={},
    )
    runtime._buffer.push(packet)

    runtime.observe_source_reconnect_count(1)

    assert runtime._buffer.pop(0) is None
    assert runtime.bridge.snapshot().tracking_mode == "RESET"
    assert runtime.bridge.snapshot().reason_codes == ("PHASE_CAMERA_RECONNECT_RESET",)


def test_ordered_stateful_node_gate_orders_each_node_without_a_global_frame_lock() -> None:
    gate = _OrderedStatefulNodeGate(("tracker", "builder"))
    gate.register(10)
    gate.register(11)
    entered: list[tuple[str, int]] = []

    def enter(sequence: int, node_key: str) -> None:
        gate.enter(sequence, node_key)
        entered.append((node_key, sequence))
        gate.complete_node(sequence, node_key)

    newer_tracker = threading.Thread(target=enter, args=(11, "tracker"))
    older_tracker = threading.Thread(target=enter, args=(10, "tracker"))
    newer_tracker.start()
    time.sleep(0.01)
    assert entered == []
    older_tracker.start()
    older_tracker.join(timeout=1)
    newer_tracker.join(timeout=1)
    assert not older_tracker.is_alive()
    assert not newer_tracker.is_alive()

    # Tracker progress does not release the independent builder order.
    newer_builder = threading.Thread(target=enter, args=(11, "builder"))
    newer_builder.start()
    time.sleep(0.01)
    assert entered == [("tracker", 10), ("tracker", 11)]
    enter(10, "builder")
    newer_builder.join(timeout=1)
    assert not newer_builder.is_alive()

    assert entered == [
        ("tracker", 10),
        ("tracker", 11),
        ("builder", 10),
        ("builder", 11),
    ]


def test_ordered_stateful_node_gate_releases_unreached_nodes_for_failed_frame() -> None:
    gate = _OrderedStatefulNodeGate(("tracker", "builder"))
    gate.register(20)
    gate.register(21)
    entered: list[int] = []

    def enter_newer_builder() -> None:
        gate.enter(21, "builder")
        entered.append(21)
        gate.complete_node(21, "builder")

    newer = threading.Thread(target=enter_newer_builder)
    newer.start()
    time.sleep(0.01)
    assert entered == []

    gate.complete_frame(20)
    newer.join(timeout=1)

    assert not newer.is_alive()
    assert entered == [21]
    gate.complete_frame(21)


def test_curvature_minimum_v3_decision_has_its_own_history_event_key() -> None:
    result = RuntimeExecutionResult(
        status="completed",
        pipeline_version_id="plv_stem_v3",
        correlation_id="corr_stem_v3",
        started_at="2026-08-04T10:00:00Z",
        completed_at="2026-08-04T10:00:01Z",
        persisted_results=[
            {
                "node_key": "result",
                "payload": {
                    "payload_type": "result",
                    "result": {
                        "schema_version": "stem.orientation.v3",
                        "algorithm": "CURVATURE_PHASE_MINIMUM",
                        "session_id": "session-v3",
                        "decision_ready": True,
                        "decision": "ROTATE",
                        "decision_frame_sequence": 211,
                    },
                },
            }
        ],
    )

    assert _stem_orientation_history_event_key(result) == (
        "stem.orientation.v3:session-v3:211:pre_entry"
    )


def test_stem_rotation_history_key_includes_model_mode_and_control_revision() -> None:
    result = RuntimeExecutionResult(
        status="completed",
        pipeline_version_id="plv_rotation",
        correlation_id="corr-rotation",
        started_at="2026-08-20T10:00:00Z",
        completed_at="2026-08-20T10:00:00Z",
        persisted_results=[
            {
                "node_key": "rotation_result_output",
                "node_type": "output.result",
                "output_port": "result",
                "payload": {
                    "payload_type": "result",
                    "result": {
                        "schema_version": "stem.rotation-optimization.v1",
                        "result_ready": True,
                        "session_id": "stem-42",
                        "model_hash": "model-hash-42",
                        "priority_mode": "NACHSCHNITT_KRITISCH",
                        "priority_revision": 8,
                    },
                },
            }
        ],
    )

    assert _stem_orientation_history_event_key(result) == (
        "stem.rotation-optimization.v1:stem-42:model-hash-42:"
        "NACHSCHNITT_KRITISCH:8"
    )


def test_stem_rotation_hmi_view_is_exposed_in_the_live_state_path() -> None:
    view_model = RuntimeStemRotationViewModel(
        timestamp="2026-08-20T10:00:00Z",
        state="MEASURING",
        priority_source="FRONTEND",
        priority_mode="VORSCHNITT_KRITISCH",
        priority_axis="VERTICAL_Z",
        priority_revision=8,
        optimization_enable=True,
        rotation_direction="UNAVAILABLE",
        reason_code="MEASURING",
        diagnostics={},
    )
    result = RuntimeExecutionResult(
        status="completed",
        pipeline_version_id="plv_rotation",
        correlation_id="corr-rotation-live",
        started_at="2026-08-20T10:00:00Z",
        completed_at="2026-08-20T10:00:00Z",
        persisted_results=[
            {
                "node_key": "rotation_result_output",
                "payload": {
                    "payload_type": "result",
                    "result": {
                        "schema_version": "stem.rotation-optimization.v1",
                        "view_model": view_model.model_dump(mode="json"),
                    },
                },
            }
        ],
    )

    extracted = _stem_rotation_view_from_result(result)

    assert extracted == view_model
    assert extracted is not None
    assert extracted.state == "MEASURING"


def test_shadow_mode_retains_each_semantic_v3_decision_once_when_history_is_disabled() -> None:
    result = RuntimeExecutionResult(
        status="completed",
        pipeline_version_id="plv_stem_shadow",
        correlation_id="corr_stem_shadow",
        started_at="2026-08-05T10:00:00Z",
        completed_at="2026-08-05T10:00:01Z",
        persisted_results=[
            {
                "node_key": "result",
                "payload": {
                    "payload_type": "result",
                    "result": {
                        "schema_version": "stem.orientation.v3",
                        "algorithm": "CURVATURE_PHASE_MINIMUM",
                        "session_id": "session-shadow",
                        "decision_ready": True,
                        "decision": "UNCERTAIN",
                        "decision_frame_sequence": 301,
                        "shadow_mode": True,
                    },
                },
            }
        ],
    )
    worker = object.__new__(RealtimeRuntimeWorker)
    worker._result_history_policy = RuntimeResultHistoryPolicy(
        enabled=False,
        retention_hours=24,
        sample_interval_seconds=60,
        max_records=500,
    )
    worker._shadow_mode_enabled = True
    worker._last_history_event_key = None

    assert worker._history_result_persist_due(result) is True
    worker._last_history_event_key = _stem_orientation_history_event_key(result)
    assert worker._history_result_persist_due(result) is False


def test_latest_frame_buffer_enforces_low_latency_drop_policy(sample_png_bytes: bytes) -> None:
    buffer = LatestFrameBuffer(max_size=2, drop_policy="drop_oldest")
    buffer.push(_frame(sample_png_bytes, sequence=1))
    buffer.push(_frame(sample_png_bytes, sequence=2))
    buffer.push(_frame(sample_png_bytes, sequence=3))

    packet = buffer.pop_latest(timeout_seconds=0.01)

    assert packet is not None
    assert packet.sequence == 3
    assert buffer.dropped_count == 2
    assert buffer.depth() == 0


def test_live_bgr_frame_is_letterboxed_before_jpeg_and_geometry_is_restored() -> None:
    source = np.zeros((120, 190, 3), dtype=np.uint8)
    source[:, :, 1] = 180
    frame = FramePayload.from_cv2_frame(source, jpeg_quality=82)
    binding = RuntimeModelBinding(
        deployment_release_id="rel_test",
        deployment_target="realtime",
        target_path=TargetPath.REALTIME,
        model_family_slug="segmentation",
        model_version_id="mvr_test",
        task_type=TaskType.SEGMENTATION,
        contract_major=1,
        capability={
            "architecture": {"family": "yolo"},
            "preprocessing": {"resize": 96},
        },
    )

    prepared = _prepare_inference_image(frame, binding, task_type=TaskType.SEGMENTATION)
    decoded = cv2.imdecode(np.frombuffer(prepared.image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)

    assert frame.cached_cv2_frame() is source
    assert prepared.letterboxed is True
    assert (prepared.input_width, prepared.input_height) == (96, 96)
    assert prepared.pad_left == 0
    assert prepared.pad_top > 0
    assert decoded is not None
    assert decoded.shape[:2] == (96, 96)

    source_x = 0.25
    source_y = 0.25
    model_x = (source_x * source.shape[1] * prepared.scale + prepared.pad_left) / 96
    model_y = (source_y * source.shape[0] * prepared.scale + prepared.pad_top) / 96
    restored = _restore_segmentation_geometry(
        [
            SegmentationPrediction(
                label="feature",
                score=0.9,
                polygon=[SegmentationPoint(x=model_x, y=model_y)] * 3,
            )
        ],
        prepared,
    )

    assert restored[0].polygon[0].x == pytest.approx(source_x)
    assert restored[0].polygon[0].y == pytest.approx(source_y)


def test_transient_inference_drop_reason_includes_http_status() -> None:
    result = RuntimeExecutionResult(
        status="failed",
        pipeline_version_id="plv_live",
        correlation_id="corr_live",
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:01Z",
        errors=[
            {
                "code": "inference_gateway_error",
                "message": "Inference gateway returned HTTP 503.",
                "node_type": "inference.classification",
                "retryable": True,
                "metadata": {"status_code": 503},
            }
        ],
    )

    assert _transient_inference_drop_reason(result) == "inference_gateway_error_http_503"


def test_diagnostic_overlay_does_not_hide_segmentation_in_live_view() -> None:
    result = RuntimeExecutionResult(
        status="completed",
        pipeline_version_id="plv_board_cut",
        correlation_id="corr_board_cut",
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:01Z",
        normalized_inference_outputs=[
            {
                "node_key": "feature_segmentation",
                "node_type": "inference.segmentation",
                "output_port": "segments",
                "payload": {
                    "payload_type": "segments",
                    "image_context_id": "ctx_board",
                    "segments": [
                        {
                            "label": "knot",
                            "score": 0.91,
                            "polygon": [
                                {"x": 0.20, "y": 0.30},
                                {"x": 0.32, "y": 0.30},
                                {"x": 0.27, "y": 0.45},
                            ],
                        }
                    ],
                },
            }
        ],
        overlay_outputs=[
            {
                "node_key": "board_coordinates",
                "node_type": "analysis.board_end_coordinate_frame",
                "output_port": "overlay",
                "payload": {
                    "payload_type": "overlay",
                    "display_context_id": "ctx_board",
                    "metadata": {},
                    "primitives": [
                        {
                            "primitive_type": "line",
                            "layer": "board_end",
                            "label": "Board end",
                            "geometry": {
                                "start": {"x": 100, "y": 80},
                                "end": {"x": 100, "y": 420},
                                "coordinate_system": "image_pixels",
                            },
                            "image_context_id": "ctx_board",
                        }
                    ],
                },
            }
        ],
    )

    overlay = live_module._overlay_from_result(
        result,
        graph_nodes=[],
        display_context_id="ctx_board",
    )

    assert {primitive.layer for primitive in overlay} == {"board_end", "segmentation"}
    segment = next(primitive for primitive in overlay if primitive.layer == "segmentation")
    assert segment.primitive_type == "polygon"
    assert segment.label == "knot"


def test_explicit_overlay_keeps_ownership_of_segment_layer_filter() -> None:
    result = RuntimeExecutionResult(
        status="completed",
        pipeline_version_id="plv_segment_overlay",
        correlation_id="corr_segment_overlay",
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:01Z",
        normalized_inference_outputs=[
            {
                "node_key": "feature_segmentation",
                "node_type": "inference.segmentation",
                "output_port": "segments",
                "payload": {
                    "payload_type": "segments",
                    "image_context_id": "ctx_board",
                    "segments": [
                        {
                            "label": "knot",
                            "score": 0.91,
                            "polygon": [
                                {"x": 0.20, "y": 0.30},
                                {"x": 0.32, "y": 0.30},
                                {"x": 0.27, "y": 0.45},
                            ],
                        }
                    ],
                },
            }
        ],
        overlay_outputs=[
            {
                "node_key": "segment_overlay",
                "node_type": "output.overlay",
                "output_port": "overlay",
                "payload": {
                    "payload_type": "overlay",
                    "display_context_id": "ctx_board",
                    "metadata": {
                        "source_payload_type": "segments",
                        "configured_layer_filter": ["measurements"],
                    },
                    "primitives": [],
                },
            }
        ],
    )

    overlay = live_module._overlay_from_result(
        result,
        graph_nodes=[],
        display_context_id="ctx_board",
    )

    assert overlay == []


def test_realtime_pipeline_inflight_is_limited_for_stateful_nodes(tmp_path: Path) -> None:
    settings = build_test_settings("runtime-orchestrator", tmp_path)
    settings.runtime.realtime_pipeline_max_inflight = 4
    settings.inference_limits.realtime_max_concurrency = 4
    settings.inference_limits.realtime_per_release_max_concurrency = 2
    stateful_nodes = _stateful_pipeline_nodes(
        [
            {"node_key": "input", "node_type": "source.channel_frame"},
            {"node_key": "sampler", "node_type": "preprocessing.frame_sampler"},
            {"node_key": "motion", "node_type": "analysis.optical_flow_motion"},
            {
                "node_key": "board_cut",
                "node_type": "analysis.board_view_cut_constraints",
            },
            {"node_key": "sps", "node_type": "output.siemens_sps_write"},
        ]
    )

    assert stateful_nodes == ["sampler", "motion", "board_cut", "sps"]
    assert _pipeline_max_inflight(settings, {}, stateful_nodes=[], inference_nodes=[]) == 4
    assert (
        _pipeline_max_inflight(
            settings,
            {"pipeline_max_inflight": 3},
            stateful_nodes=[],
            inference_nodes=[],
        )
        == 3
    )
    assert (
        _pipeline_max_inflight(
            settings,
            {"pipeline_max_inflight": 8},
            stateful_nodes=[],
            inference_nodes=[],
        )
        == 4
    )
    assert (
        _pipeline_max_inflight(
            settings,
            {},
            stateful_nodes=[],
            inference_nodes=["detect"],
        )
        == 2
    )
    assert (
        _pipeline_max_inflight(
            settings,
            {"pipeline_max_inflight": 3},
            stateful_nodes=[],
            inference_nodes=["detect"],
        )
        == 2
    )
    assert (
        _pipeline_max_inflight(
            settings,
            {},
            stateful_nodes=stateful_nodes,
            inference_nodes=["detect"],
        )
        == 1
    )
    assert (
        _pipeline_max_inflight(
            settings,
            {"pipeline_max_inflight": 3},
            stateful_nodes=["active_stem", "measure_curvature"],
            inference_nodes=["segment_stem"],
            ordered_stateful_overlap=True,
        )
        == 2
    )


def test_realtime_model_bindings_are_preloaded_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = build_test_settings("realtime-executor", tmp_path)
    model_ref = RuntimeLogicalModelRef(
        model_family_slug="defect-segmenter",
        target_path=TargetPath.REALTIME,
        required_task_type=TaskType.SEGMENTATION,
        selection_policy="explicit_release",
        deployment_release_id="rel_segmenter",
    )
    binding = RuntimeModelBinding(
        deployment_release_id="rel_segmenter",
        deployment_target=TargetPath.REALTIME.value,
        target_path=TargetPath.REALTIME,
        model_family_slug="defect-segmenter",
        model_version_id="mvr_segmenter",
        task_type=TaskType.SEGMENTATION,
        contract_major=1,
    )
    resolved: list[RuntimeLogicalModelRef] = []

    def build_resolver(
        _session: object,
    ) -> Callable[[RuntimeLogicalModelRef], RuntimeModelBinding]:
        def resolve(candidate: RuntimeLogicalModelRef) -> RuntimeModelBinding:
            resolved.append(candidate)
            return binding

        return resolve

    monkeypatch.setattr(
        live_module,
        "session_scope",
        lambda _settings: nullcontext(object()),
    )
    monkeypatch.setattr(live_module, "build_runtime_model_resolver", build_resolver)
    resolver = _CachedRuntimeModelResolver(settings=settings)

    resolver.preload([model_ref, model_ref])

    assert resolver(model_ref) is binding
    assert resolver(model_ref) is binding
    assert resolved == [model_ref]


def test_stateful_pipeline_work_stall_watchdog_marks_worker_error(tmp_path: Path) -> None:
    settings = build_test_settings("runtime-orchestrator", tmp_path)
    settings.runtime.executor_stale_after_seconds = 2
    worker = RealtimeRuntimeWorker(
        settings=settings,
        runtime_execution_id="rex_stateful_stall",
        channel_id="chn_stateful_stall",
        executor_instance_id="runtime-orchestrator-test",
    )
    queued_perf = time.perf_counter() - 11.0
    pending = {
        Future(): _PendingPipelineWork(
            sequence=42,
            queued_perf=queued_perf,
            deadline_perf=queued_perf + 1.0,
        )
    }

    stalled = worker._stateful_pipeline_work_stalled(
        pending,
        pipeline_work_deadline_seconds=1.0,
    )

    assert stalled is True
    assert worker.metrics.last_error is not None
    assert "Stateful realtime pipeline work stalled" in worker.metrics.last_error
    assert worker.metrics.stage_timings_ms["stateful_pipeline_stall_ms"] >= 10_000


def test_running_pipeline_work_is_retained_after_soft_deadline(tmp_path: Path) -> None:
    settings = build_test_settings("runtime-orchestrator", tmp_path)
    worker = RealtimeRuntimeWorker(
        settings=settings,
        runtime_execution_id="rex_soft_deadline",
        channel_id="chn_soft_deadline",
        executor_instance_id="runtime-orchestrator-test",
    )
    future: Future = Future()
    assert future.set_running_or_notify_cancel() is True
    queued_perf = time.perf_counter() - 2.0
    pending = {
        future: _PendingPipelineWork(
            sequence=7,
            queued_perf=queued_perf,
            deadline_perf=queued_perf + 1.0,
        )
    }

    assert worker._drop_timed_out_pipeline_work(pending) is True
    assert future in pending
    assert pending[future].deadline_recorded is True
    assert worker.metrics.pipeline_deadline_dropped_frame_count == 1

    assert worker._drop_timed_out_pipeline_work(pending) is False
    assert worker.metrics.pipeline_deadline_dropped_frame_count == 1


def test_source_health_without_error_does_not_clear_pipeline_error(tmp_path: Path) -> None:
    settings = build_test_settings("runtime-orchestrator", tmp_path)
    worker = RealtimeRuntimeWorker(
        settings=settings,
        runtime_execution_id="rex_source_health",
        channel_id="chn_source_health",
        executor_instance_id="runtime-orchestrator-test",
    )
    source = SimpleNamespace(
        health=SourceHealth(connected=True, status_text="Shared camera connected")
    )
    worker.metrics.last_error = "pipeline deadline exceeded"

    worker._sync_source_health(source)

    assert worker.metrics.last_error == "pipeline deadline exceeded"


def test_missing_live_state_reports_pipeline_deadline_instead_of_source_connection() -> None:
    hint = _missing_live_state_recovery_hint(
        source_health=RuntimeLiveSourceHealth(
            state=HealthState.DEGRADED,
            status_text="Shared camera connected",
            connected=True,
        ),
        metrics_summary={"pipeline_deadline_drop_count": 4},
    )

    assert "camera source is connected" in hint
    assert "pipeline deadline drops" in hint


def test_realtime_default_inference_client_waits_on_gateway_overload(tmp_path: Path) -> None:
    settings = build_test_settings("runtime-orchestrator", tmp_path)
    settings.inference_limits.realtime_timeout_ms = 750

    client = _default_inference_client(settings)

    assert client.overload_policy == "wait"
    assert client.overload_retry_interval_seconds == 0.025
    assert client.overload_retry_max_interval_seconds == 0.25
    assert client.timeout_seconds == 1.0


def test_realtime_live_view_state_write_is_throttled(monkeypatch, tmp_path: Path) -> None:
    settings = build_test_settings("realtime-executor", tmp_path)
    worker = RealtimeRuntimeWorker(
        settings=settings,
        runtime_execution_id="rex_live_write",
        channel_id="chn_live_write",
        executor_instance_id="realtime-executor-test",
    )
    calls: list[bool] = []

    def fake_write_runtime_live_view_state(
        settings_arg: PlatformSettings,
        live_state_arg: RuntimeLiveViewState,
        *,
        split_frame_payload: bool = False,
    ) -> Path:
        assert settings_arg is settings
        assert live_state_arg.channel_id == "chn_live_write"
        calls.append(split_frame_payload)
        return tmp_path / "live.json"

    monkeypatch.setattr(
        "realtime_executor.live.write_runtime_live_view_state",
        fake_write_runtime_live_view_state,
    )
    live_state = RuntimeLiveViewState(
        channel_id="chn_live_write",
        runtime_instance_id="rex_live_write",
        actual_state=RuntimeObservedState.RUNNING,
        available=True,
        updated_at=utc_now_iso(),
    )

    assert worker._write_live_view_state_if_due(live_state) is True  # noqa: SLF001
    assert worker._write_live_view_state_if_due(live_state) is False  # noqa: SLF001
    worker._last_live_state_write_perf = time.perf_counter() - 1.0  # noqa: SLF001
    assert worker._write_live_view_state_if_due(live_state) is True  # noqa: SLF001

    assert calls == [True, True]


def test_realtime_live_state_keeps_pipeline_input_separate_from_crop_result(
    tmp_path: Path,
) -> None:
    settings = build_test_settings("realtime-executor", tmp_path)
    worker = RealtimeRuntimeWorker(
        settings=settings,
        runtime_execution_id="rex_crop_state",
        channel_id="chn_crop_state",
        executor_instance_id="realtime-executor-test",
    )
    source_context = RuntimeImageContext(
        context_id="ctx_original",
        kind="original",
        width=1280,
        height=720,
    )
    crop_context = RuntimeImageContext(
        context_id="ctx_crop",
        kind="crop",
        width=320,
        height=200,
        parent_image_context_id="ctx_original",
        transform=RuntimeImageTransform(
            transform_type="crop",
            coordinate_projection="crop_offset",
            parameters={
                "x": 137,
                "y": 83,
                "width": 320,
                "height": 200,
                "source_width": 1280,
                "source_height": 720,
            },
        ),
    )
    source_frame = FramePayload(
        image_base64=base64.b64encode(b"original-frame").decode("ascii"),
        mime_type="image/jpeg",
        width=1280,
        height=720,
        image_context=source_context,
    )
    crop_frame = FramePayload(
        image_base64=base64.b64encode(b"crop-frame").decode("ascii"),
        mime_type="image/jpeg",
        width=320,
        height=200,
        image_context=crop_context,
    )
    result = RuntimeExecutionResult(
        status="completed",
        pipeline_version_id="plv_crop",
        correlation_id="corr_crop",
        started_at="2026-08-04T10:00:00Z",
        completed_at="2026-08-04T10:00:00Z",
        display_outputs=[
            {
                "node_key": "output",
                "node_type": "output.frame",
                "output_port": "display",
                "payload": {
                    "payload_type": "display",
                    "display_context_id": "ctx_crop",
                    "display_context": crop_context.model_dump(mode="json"),
                    "frame": crop_frame.model_dump(mode="json"),
                },
            }
        ],
    )
    packet = FramePacket(
        image_bytes=b"original-frame",
        mime_type="image/jpeg",
        width=1280,
        height=720,
        source_timestamp="2026-08-04T10:00:00Z",
        source_uri="rtsp://camera.local/live",
        sequence=17,
        metadata={"source_type": "rtsp"},
    )

    live_state = worker._live_state_from_result(  # noqa: SLF001
        packet=packet,
        frame_payload=source_frame,
        result=result,
        graph_nodes=[],
        source_type="rtsp",
        source_config={},
        sample_every_n=1,
        max_fps=10,
        source=None,
        pipeline_max_inflight=1,
        stateful_nodes=[],
        inference_nodes=[],
    )

    assert live_state.frame is not None
    assert live_state.frame.width == 320
    assert live_state.frame.height == 200
    assert live_state.frame.image_context["context_id"] == "ctx_crop"
    assert live_state.source_frame is not None
    assert live_state.source_frame.width == 1280
    assert live_state.source_frame.height == 720
    assert live_state.source_frame.image_context["context_id"] == "ctx_original"
    assert live_state.source_frame.metadata["frame_role"] == "pipeline_input"


def test_live_view_split_storage_preserves_processed_and_original_frames(tmp_path: Path) -> None:
    settings = build_test_settings("runtime-admin-api", tmp_path)
    display_bytes = b"processed-frame"
    source_bytes = b"original-frame"
    undistorted_bytes = b"undistorted-frame"
    state = RuntimeLiveViewState(
        channel_id="chn_crop_frames",
        runtime_instance_id="rex_crop_frames",
        actual_state=RuntimeObservedState.RUNNING,
        available=True,
        frame=RuntimeLiveFrame(
            image_base64=base64.b64encode(display_bytes).decode("ascii"),
            width=320,
            height=200,
        ),
        source_frame=RuntimeLiveFrame(
            image_base64=base64.b64encode(source_bytes).decode("ascii"),
            width=1280,
            height=720,
        ),
        undistorted_frame=RuntimeLiveFrame(
            image_base64=base64.b64encode(undistorted_bytes).decode("ascii"),
            width=1280,
            height=720,
        ),
        updated_at=utc_now_iso(),
    )

    state_path = write_runtime_live_view_state(
        settings,
        state,
        split_frame_payload=True,
    )
    persisted = RuntimeLiveViewState.model_validate_json(state_path.read_text(encoding="utf-8"))

    assert persisted.frame is not None
    assert persisted.frame.image_base64 is None
    assert persisted.frame.metadata["_split_frame_sequence"] == 0
    assert persisted.source_frame is not None
    assert persisted.source_frame.image_base64 is None
    assert persisted.undistorted_frame is not None
    assert persisted.undistorted_frame.image_base64 is None
    assert runtime_live_view_frame_path(settings, "rex_crop_frames").read_bytes() == display_bytes
    assert (
        runtime_live_view_source_frame_path(settings, "rex_crop_frames").read_bytes()
        == source_bytes
    )
    assert (
        runtime_live_view_undistorted_frame_path(
            settings,
            "rex_crop_frames",
        ).read_bytes()
        == undistorted_bytes
    )

    hydrated = _with_split_frame_payload(settings, persisted)

    assert hydrated.frame is not None
    assert base64.b64decode(hydrated.frame.image_base64 or "") == display_bytes
    assert hydrated.source_frame is not None
    assert base64.b64decode(hydrated.source_frame.image_base64 or "") == source_bytes
    assert hydrated.undistorted_frame is not None
    assert (
        base64.b64decode(hydrated.undistorted_frame.image_base64 or "")
        == undistorted_bytes
    )


def test_live_view_split_storage_binds_pixels_to_the_json_frame_sequence(
    tmp_path: Path,
) -> None:
    settings = build_test_settings("runtime-admin-api", tmp_path)

    def state(sequence: int, image_bytes: bytes, label: str) -> RuntimeLiveViewState:
        return RuntimeLiveViewState(
            channel_id="chn_atomic_frame",
            runtime_instance_id="rex_atomic_frame",
            actual_state=RuntimeObservedState.RUNNING,
            available=True,
            frame=RuntimeLiveFrame(
                image_base64=base64.b64encode(image_bytes).decode("ascii"),
                sequence=sequence,
                width=320,
                height=200,
            ),
            overlay=[
                {
                    "primitive_type": "label",
                    "layer": "classification",
                    "label": label,
                }
            ],
            updated_at=utc_now_iso(),
        )

    state_path = write_runtime_live_view_state(
        settings,
        state(10, b"photo-a", "class-a"),
        split_frame_payload=True,
    )
    previous_state = RuntimeLiveViewState.model_validate_json(
        state_path.read_text(encoding="utf-8")
    )

    write_runtime_live_view_state(
        settings,
        state(11, b"photo-b", "class-b"),
        split_frame_payload=True,
    )
    hydrated_previous = _with_split_frame_payload(settings, previous_state)

    assert hydrated_previous.frame is not None
    assert hydrated_previous.frame.sequence == 10
    assert base64.b64decode(hydrated_previous.frame.image_base64 or "") == b"photo-a"
    assert hydrated_previous.overlay[0].label == "class-a"


def test_overlay_event_strips_all_live_frame_pixel_payloads(tmp_path: Path) -> None:
    settings = build_test_settings("runtime-admin-api", tmp_path)
    encoded = base64.b64encode(b"frame-bytes").decode("ascii")
    state = RuntimeLiveViewState(
        channel_id="chn_overlay_metadata",
        runtime_instance_id="rex_overlay_metadata",
        actual_state=RuntimeObservedState.RUNNING,
        available=True,
        frame=RuntimeLiveFrame(image_base64=encoded, width=320, height=200),
        source_frame=RuntimeLiveFrame(image_base64=encoded, width=1280, height=720),
        undistorted_frame=RuntimeLiveFrame(
            image_base64=encoded,
            width=1280,
            height=720,
        ),
        updated_at=utc_now_iso(),
    )

    write_runtime_overlay_event(settings, state)
    persisted = read_runtime_overlay_event(settings, state.channel_id)

    assert persisted is not None
    assert persisted.frame is not None and persisted.frame.image_base64 is None
    assert persisted.source_frame is not None
    assert persisted.source_frame.image_base64 is None
    assert persisted.undistorted_frame is not None
    assert persisted.undistorted_frame.image_base64 is None


def test_rtsp_source_rejects_inline_credentials() -> None:
    try:
        build_frame_source(
            source_type="rtsp",
            config={"url": "rtsp://operator:secret@camera.local/live"},
            secret_refs={},
        )
    except RtspSourceConfigurationError as exc:
        assert "inline credentials" in str(exc)
    else:  # pragma: no cover - defensive assertion clarity
        raise AssertionError("Inline RTSP credentials must be rejected.")


def test_rtsp_source_prefers_ffmpeg_tcp_capture_options(monkeypatch) -> None:
    captures = []

    class FakeCapture:
        def __init__(self) -> None:
            self.backend = None
            self.opened = False
            self.properties = []
            captures.append(self)

        def set(self, prop, value) -> bool:
            self.properties.append((prop, value))
            return True

        def open(self, uri, backend=None) -> bool:
            self.uri = uri
            self.backend = backend
            self.opened = True
            return True

        def isOpened(self) -> bool:
            return self.opened

        def release(self) -> None:
            self.opened = False

    class FakeCv2:
        CAP_FFMPEG = 1900
        CAP_PROP_BUFFERSIZE = 1
        CAP_PROP_OPEN_TIMEOUT_MSEC = 2
        CAP_PROP_READ_TIMEOUT_MSEC = 3
        VideoCapture = FakeCapture

    monkeypatch.setitem(sys.modules, "cv2", FakeCv2)
    monkeypatch.delenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", raising=False)

    source = OpenCvRtspFrameSource(
        config={"url": "rtsp://camera.local/live", "read_timeout_seconds": 3},
        secret_refs={},
    )
    try:
        assert source._open_capture() is True
    finally:
        source.close()

    assert os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] == _DEFAULT_OPENCV_FFMPEG_CAPTURE_OPTIONS
    assert captures[0].uri == "rtsp://camera.local/live"
    assert captures[0].backend == FakeCv2.CAP_FFMPEG
    assert (FakeCv2.CAP_PROP_BUFFERSIZE, 1) in captures[0].properties
    assert (FakeCv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 3000) in captures[0].properties
    assert (FakeCv2.CAP_PROP_READ_TIMEOUT_MSEC, 3000) in captures[0].properties


def test_usb_opencv_source_opens_device_index_with_capture_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures = []

    class FakeFrame:
        shape = (720, 1280, 3)

    class FakeCapture:
        def __init__(self) -> None:
            self.opened = False
            self.properties = []
            captures.append(self)

        def set(self, prop: int, value: float | str) -> bool:
            self.properties.append((prop, value))
            return True

        def open(self, uri: object, backend: int | None = None) -> bool:
            self.uri = uri
            self.backend = backend
            self.opened = True
            return True

        def isOpened(self) -> bool:  # noqa: N802 - mirrors OpenCV's public API
            return self.opened

        def read(self) -> tuple[bool, FakeFrame]:
            return True, FakeFrame()

        def release(self) -> None:
            self.opened = False

    class FakeCv2:
        CAP_ANY = 0
        CAP_DSHOW = 700
        CAP_PROP_BUFFERSIZE = 1
        CAP_PROP_OPEN_TIMEOUT_MSEC = 2
        CAP_PROP_READ_TIMEOUT_MSEC = 3
        CAP_PROP_FRAME_WIDTH = 4
        CAP_PROP_FRAME_HEIGHT = 5
        CAP_PROP_FPS = 6
        CAP_PROP_FOURCC = 7
        VideoCapture = FakeCapture

        @staticmethod
        def VideoWriter_fourcc(*value: str) -> str:  # noqa: N802 - OpenCV API
            return "".join(value)

    monkeypatch.setitem(sys.modules, "cv2", FakeCv2)
    source = OpenCvUsbFrameSource(
        config={
            "device_index": 2,
            "backend": "dshow",
            "width": 1280,
            "height": 720,
            "fps": 30,
            "fourcc": "MJPG",
            "buffer_size": 2,
        }
    )
    try:
        packet = source.read(threading.Event())
    finally:
        source.close()

    assert packet is not None
    assert packet.source_uri == "usb-opencv://index/2"
    assert packet.metadata["source_type"] == "usb_opencv"
    assert captures[0].uri == 2
    assert captures[0].backend == FakeCv2.CAP_DSHOW
    assert (FakeCv2.CAP_PROP_FRAME_WIDTH, 1280.0) in captures[0].properties
    assert (FakeCv2.CAP_PROP_FRAME_HEIGHT, 720.0) in captures[0].properties
    assert (FakeCv2.CAP_PROP_FPS, 30.0) in captures[0].properties
    assert (FakeCv2.CAP_PROP_FOURCC, "MJPG") in captures[0].properties


def test_file_test_frame_source_repeats_image_as_live_frames(
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    image_path = tmp_path / "press-sample.png"
    image_path.write_bytes(sample_png_bytes)
    source = build_frame_source(
        source_type="file_test",
        config={"path": str(image_path), "max_fps": 120},
        secret_refs={},
    )
    try:
        packet = source.read(threading.Event())
    finally:
        source.close()

    assert packet is not None
    assert packet.image_bytes == sample_png_bytes
    assert packet.mime_type == "image/png"
    assert packet.source_uri == str(image_path)
    assert packet.metadata["source_type"] == "file_test"


def test_file_test_frame_source_repeats_embedded_pipeline_photo(
    sample_png_bytes: bytes,
) -> None:
    source = build_frame_source(
        source_type="file_test",
        config={
            "photo": {
                "data_url": "data:image/png;base64,"
                + base64.b64encode(sample_png_bytes).decode("ascii"),
                "name": "selected.png",
            },
            "fps": 30,
        },
        secret_refs={},
    )

    packet = source.read(threading.Event())

    assert packet is not None
    assert packet.image_bytes == sample_png_bytes
    assert packet.source_uri == "pipeline-test-photo://selected.png"


def test_aravis_gige_source_configures_managed_rtsp_bridge(
    tmp_path: Path,
    monkeypatch,
) -> None:
    commands: list[list[str]] = []

    class FakeRtspSource:
        def __init__(self, *, config, secret_refs=None):
            self.config = dict(config)
            self.secret_refs = dict(secret_refs or {})
            self.health = SourceHealth(connected=True, status_text="RTSP source connected")

        def read(self, _stop_event):
            return FramePacket(
                image_bytes=b"frame",
                mime_type="image/jpeg",
                width=1,
                height=1,
                source_timestamp="2026-05-01T12:00:00Z",
                source_uri=self.config["url"],
                sequence=1,
                metadata={"source_type": "rtsp"},
            )

        def close(self) -> None:
            self.health.connected = False

    def fake_systemctl(args):
        commands.append(args)
        if args[:2] == ["is-active", "--quiet"]:
            return type("Result", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("realtime_executor.sources._ARAVIS_RTSP_HELPER", Path(__file__))
    monkeypatch.setattr("realtime_executor.sources._ARAVIS_RTSP_UNIT_TEMPLATE", Path(__file__))
    monkeypatch.setattr("realtime_executor.sources._ARAVIS_RTSP_ENV_DIR", tmp_path / "aravis")
    monkeypatch.setattr("realtime_executor.sources._systemctl", fake_systemctl)
    monkeypatch.setattr("realtime_executor.sources.OpenCvRtspFrameSource", FakeRtspSource)
    config = {
        "camera_name": "Point Grey Research-22334455",
        "bridge_name": "src-bridge-test",
        "rtsp_host": "127.0.0.1",
        "rtsp_address": "127.0.0.1",
        "rtsp_port": 18554,
        "rtsp_mount": "/src-bridge-test",
        "pixel_format": "BayerRG8",
        "conversion_mode": "bayer",
        "packet_size": 1500,
        "packet_delay": 1000,
        "h264_bitrate_kbit": 6000,
        "h264_key_int_max": 30,
        "queue_size": 2,
    }
    source = build_frame_source(source_type="aravis_gige", config=config, secret_refs={})

    try:
        packet = source.read(threading.Event())
    finally:
        source.close()

    assert packet is not None
    assert packet.source_uri == "rtsp://127.0.0.1:18554/src-bridge-test"
    assert packet.metadata["source_type"] == "aravis_gige"
    assert packet.metadata["producer"] == "aravis"
    assert ["start", "ybb-aravis-rtsp@src-bridge-test.service"] in commands

    env_content = _aravis_bridge_env_content(config)
    assert 'ARAVIS_CAMERA_NAME="Point Grey Research-22334455"' in env_content
    assert "ARAVIS_FEATURES=PixelFormat=BayerRG8" in env_content
    assert 'ARAVIS_SRC_PROPERTIES="packet-size=1500 packet-delay=1000"' in env_content
    assert _aravis_rtsp_source_config(config)["url"] == "rtsp://127.0.0.1:18554/src-bridge-test"


def test_basler_pylon_source_reads_direct_camera_frame(monkeypatch) -> None:
    class FakeNode:
        def __init__(self) -> None:
            self.Value = None

        def SetValue(self, value) -> None:
            self.Value = value

    class FakeDeviceInfo:
        def GetSerialNumber(self):
            return "40123456"

        def GetUserDefinedName(self):
            return "Press Basler"

        def GetModelName(self):
            return "a2A1920-51gcBAS"

        def GetDeviceClass(self):
            return "BaslerGigE"

        def GetFriendlyName(self):
            return "Basler a2A1920-51gcBAS"

        def GetFullName(self):
            return "Basler a2A1920-51gcBAS 40123456"

        def GetIpAddress(self):
            return "192.168.10.42"

    class FakeFactory:
        def __init__(self) -> None:
            self.device_info = FakeDeviceInfo()

        def EnumerateDevices(self):
            return [self.device_info]

        def CreateDevice(self, device):
            return device

    class FakeGrabResult:
        def __init__(self) -> None:
            self.released = False

        def GrabSucceeded(self):
            return True

        def Release(self) -> None:
            self.released = True

    class FakeConverter:
        def __init__(self) -> None:
            self.OutputPixelFormat = None
            self.OutputBitAlignment = None

        def Convert(self, _grab_result):
            frame = np.full((2, 3, 3), 7, dtype=np.uint8)
            return SimpleNamespace(GetArray=lambda: frame)

    class FakeCamera:
        last_instance = None

        def __init__(self, _device) -> None:
            FakeCamera.last_instance = self
            self.AcquisitionMode = FakeNode()
            self.PixelFormat = FakeNode()
            self.TriggerMode = FakeNode()
            self.AcquisitionFrameRateEnable = FakeNode()
            self.AcquisitionFrameRate = FakeNode()
            self.ExposureAuto = FakeNode()
            self.ExposureTime = FakeNode()
            self.GainAuto = FakeNode()
            self.Gain = FakeNode()
            self.GevSCPSPacketSize = FakeNode()
            self.GevSCPD = FakeNode()
            self.MaxNumBuffer = FakeNode()
            self.OutputQueueSize = FakeNode()
            self.FrameRetention = FakeNode()
            self.opened = False
            self.grabbing = False
            self.grab_strategy = None

        def Open(self) -> None:
            self.opened = True

        def StartGrabbing(self, strategy=None) -> None:
            self.grabbing = True
            self.grab_strategy = strategy

        def RetrieveResult(self, _timeout_ms, _timeout_handling=None):
            return FakeGrabResult()

        def GetStreamGrabberNodeMap(self):
            return SimpleNamespace(GetNode=lambda name: getattr(self, name, None))

        def IsGrabbing(self):
            return self.grabbing

        def StopGrabbing(self) -> None:
            self.grabbing = False

        def IsOpen(self):
            return self.opened

        def Close(self) -> None:
            self.opened = False

    fake_factory = FakeFactory()
    fake_pylon = SimpleNamespace(
        TlFactory=SimpleNamespace(GetInstance=lambda: fake_factory),
        InstantCamera=FakeCamera,
        ImageFormatConverter=FakeConverter,
        PixelType_BGR8packed="BGR8packed",
        OutputBitAlignment_MsbAligned="MsbAligned",
        GrabStrategy_LatestImageOnly="latest",
        TimeoutHandling_Return="return",
    )
    monkeypatch.setitem(sys.modules, "pypylon", SimpleNamespace(pylon=fake_pylon))

    source = build_frame_source(
        source_type="basler_pylon",
        config={
            "serial_number": "40123456",
            "pixel_format": "BayerRG8",
            "acquisition_mode": "Continuous",
            "grab_strategy": "LatestImageOnly",
            "fps": 51,
            "exposure_time_us": 8000,
            "gain_db": 1.5,
            "packet_size": 1500,
            "inter_packet_delay": 1000,
            "buffer_count": 24,
            "output_queue_size": 2,
            "frame_retention_ms": 500,
            "grab_failure_reconnect_threshold": 3,
        },
        secret_refs={},
    )
    try:
        packet = source.read(threading.Event())
    finally:
        source.close()

    assert packet is not None
    assert packet.raw_frame is not None
    assert packet.raw_frame.shape == (2, 3, 3)
    assert packet.metadata["source_type"] == "basler_pylon"
    assert packet.metadata["transport"] == "pylon"
    assert packet.metadata["camera"]["serial_number"] == "40123456"
    assert packet.source_uri == "basler-pylon://Press Basler"
    camera = FakeCamera.last_instance
    assert camera is not None
    assert camera.grab_strategy == "latest"
    assert camera.PixelFormat.Value == "BayerRG8"
    assert camera.AcquisitionFrameRateEnable.Value is True
    assert camera.AcquisitionFrameRate.Value == 51.0
    assert camera.ExposureAuto.Value == "Off"
    assert camera.ExposureTime.Value == 8000.0
    assert camera.GainAuto.Value == "Off"
    assert camera.Gain.Value == 1.5
    assert camera.GevSCPSPacketSize.Value == 1500
    assert camera.GevSCPD.Value == 1000
    assert camera.MaxNumBuffer.Value == 24
    assert camera.OutputQueueSize.Value == 2
    assert camera.FrameRetention.Value == 500


def test_basler_effective_profile_reads_native_roi_before_grabbing() -> None:
    class FakeNode:
        def __init__(self, value) -> None:
            self.Value = value

    camera = SimpleNamespace(
        Width=FakeNode(1900),
        Height=FakeNode(1200),
        PixelFormat=FakeNode("BayerRG8"),
        BslResultingAcquisitionFrameRate=FakeNode(30.0),
        GevLinkSpeed=FakeNode(1_000_000_000),
    )

    profile = _basler_effective_camera_profile(camera, {})

    assert profile == {
        "width": 1900,
        "height": 1200,
        "fps": 30.0,
        "link_speed_mbps": 1000.0,
        "pixel_format": "BayerRG8",
    }
    assert validate_basler_link_budget(profile, required=True) == pytest.approx(547.2)


def test_basler_effective_profile_skips_optional_nodes_that_raise_when_missing() -> None:
    class FakeNode:
        def __init__(self, value) -> None:
            self.Value = value

    class PylonLikeCamera:
        Width = FakeNode(1920)
        Height = FakeNode(1080)
        PixelFormat = FakeNode("BayerRG8")
        AcquisitionFrameRate = FakeNode(20.0)

        def __getattr__(self, name: str):
            raise RuntimeError(f"Node not existing: {name}")

    profile = _basler_effective_camera_profile(
        PylonLikeCamera(),
        {"link_speed_mbps": 1000},
    )

    assert profile == {
        "width": 1920,
        "height": 1080,
        "fps": 20.0,
        "link_speed_mbps": 1000.0,
        "pixel_format": "BayerRG8",
    }


def test_basler_pylon_source_skips_incomplete_grab_without_reconnect(monkeypatch) -> None:
    class FakeNode:
        def __init__(self) -> None:
            self.Value = None

        def SetValue(self, value) -> None:
            self.Value = value

    class FakeDeviceInfo:
        def GetSerialNumber(self):
            return "40123456"

        def GetUserDefinedName(self):
            return "Press Basler"

        def GetModelName(self):
            return "a2A1920-51gcBAS"

    class FakeFactory:
        def __init__(self) -> None:
            self.device_info = FakeDeviceInfo()

        def EnumerateDevices(self):
            return [self.device_info]

        def CreateDevice(self, device):
            return device

    class FakeGrabResult:
        def __init__(self, succeeded: bool) -> None:
            self.succeeded = succeeded
            self.released = False

        def GrabSucceeded(self):
            return self.succeeded

        def GetErrorDescription(self):
            return "The buffer was incompletely grabbed."

        def Release(self) -> None:
            self.released = True

    class FakeConverter:
        def __init__(self) -> None:
            self.OutputPixelFormat = None
            self.OutputBitAlignment = None

        def Convert(self, _grab_result):
            frame = np.full((2, 3, 3), 7, dtype=np.uint8)
            return SimpleNamespace(GetArray=lambda: frame)

    class FakeCamera:
        last_instance = None

        def __init__(self, _device) -> None:
            FakeCamera.last_instance = self
            self.MaxNumBuffer = FakeNode()
            self.opened = False
            self.grabbing = False
            self.closed_count = 0
            self.retrieve_count = 0

        def Open(self) -> None:
            self.opened = True

        def StartGrabbing(self, strategy=None) -> None:
            self.grabbing = True

        def RetrieveResult(self, _timeout_ms, _timeout_handling=None):
            self.retrieve_count += 1
            return FakeGrabResult(self.retrieve_count > 1)

        def IsGrabbing(self):
            return self.grabbing

        def StopGrabbing(self) -> None:
            self.grabbing = False

        def IsOpen(self):
            return self.opened

        def Close(self) -> None:
            self.closed_count += 1
            self.opened = False

    fake_factory = FakeFactory()
    fake_pylon = SimpleNamespace(
        TlFactory=SimpleNamespace(GetInstance=lambda: fake_factory),
        InstantCamera=FakeCamera,
        ImageFormatConverter=FakeConverter,
        PixelType_BGR8packed="BGR8packed",
        OutputBitAlignment_MsbAligned="MsbAligned",
        GrabStrategy_LatestImageOnly="latest",
        TimeoutHandling_Return="return",
    )
    monkeypatch.setitem(sys.modules, "pypylon", SimpleNamespace(pylon=fake_pylon))

    source = build_frame_source(
        source_type="basler_pylon",
        config={"serial_number": "40123456", "grab_failure_reconnect_threshold": 3},
        secret_refs={},
    )
    try:
        first = source.read(threading.Event())
        camera = FakeCamera.last_instance
        assert camera is not None
        assert first is None
        assert camera.closed_count == 0
        assert camera.grabbing is True
        assert source.health.connected is True
        assert source.health.reconnect_count == 0
        assert source.health.source_error_count == 1

        second = source.read(threading.Event())
    finally:
        source.close()

    assert second is not None
    assert second.raw_frame is not None
    assert second.metadata["source_type"] == "basler_pylon"


def test_basler_pylon_source_executes_software_trigger_before_retrieve(monkeypatch) -> None:
    class FakeNode:
        def __init__(self) -> None:
            self.Value = None

        def SetValue(self, value) -> None:
            self.Value = value

    class FakeDeviceInfo:
        def GetSerialNumber(self):
            return "40123456"

        def GetUserDefinedName(self):
            return "Press Basler"

    class FakeFactory:
        def __init__(self) -> None:
            self.device_info = FakeDeviceInfo()

        def EnumerateDevices(self):
            return [self.device_info]

        def CreateDevice(self, device):
            return device

    class FakeGrabResult:
        def GrabSucceeded(self):
            return True

        def Release(self) -> None:
            pass

    class FakeConverter:
        def __init__(self) -> None:
            self.OutputPixelFormat = None
            self.OutputBitAlignment = None

        def Convert(self, _grab_result):
            frame = np.full((2, 3, 3), 9, dtype=np.uint8)
            return SimpleNamespace(GetArray=lambda: frame)

    class FakeCamera:
        last_instance = None

        def __init__(self, _device) -> None:
            FakeCamera.last_instance = self
            self.AcquisitionMode = FakeNode()
            self.TriggerMode = FakeNode()
            self.TriggerSource = FakeNode()
            self.MaxNumBuffer = FakeNode()
            self.opened = False
            self.grabbing = False
            self.waited_for_trigger = False
            self.software_trigger_count = 0
            self.retrieve_after_trigger = False

        def Open(self) -> None:
            self.opened = True

        def StartGrabbing(self, strategy=None) -> None:
            self.grabbing = True

        def WaitForFrameTriggerReady(self, _timeout_ms, _timeout_handling=None):
            self.waited_for_trigger = True
            return True

        def ExecuteSoftwareTrigger(self) -> None:
            assert self.waited_for_trigger is True
            self.software_trigger_count += 1

        def RetrieveResult(self, _timeout_ms, _timeout_handling=None):
            self.retrieve_after_trigger = self.software_trigger_count == 1
            return FakeGrabResult()

        def IsGrabbing(self):
            return self.grabbing

        def StopGrabbing(self) -> None:
            self.grabbing = False

        def IsOpen(self):
            return self.opened

        def Close(self) -> None:
            self.opened = False

    fake_factory = FakeFactory()
    fake_pylon = SimpleNamespace(
        TlFactory=SimpleNamespace(GetInstance=lambda: fake_factory),
        InstantCamera=FakeCamera,
        ImageFormatConverter=FakeConverter,
        PixelType_BGR8packed="BGR8packed",
        OutputBitAlignment_MsbAligned="MsbAligned",
        GrabStrategy_OneByOne="one-by-one",
        GrabStrategy_LatestImageOnly="latest",
        TimeoutHandling_Return="return",
        TimeoutHandling_ThrowException="throw",
    )
    monkeypatch.setitem(sys.modules, "pypylon", SimpleNamespace(pylon=fake_pylon))

    source = build_frame_source(
        source_type="basler_pylon",
        config={
            "serial_number": "40123456",
            "trigger_mode": "On",
            "trigger_source": "Software",
            "grab_strategy": "OneByOne",
        },
        secret_refs={},
    )
    try:
        packet = source.read(threading.Event())
    finally:
        source.close()

    assert packet is not None
    assert packet.raw_frame is not None
    camera = FakeCamera.last_instance
    assert camera is not None
    assert camera.TriggerMode.Value == "On"
    assert camera.TriggerSource.Value == "Software"
    assert camera.software_trigger_count == 1
    assert camera.retrieve_after_trigger is True


def test_basler_pylon_source_auto_falls_back_to_aravis_bridge(monkeypatch) -> None:
    import realtime_executor.sources as sources_module

    class FakeFactory:
        def EnumerateDevices(self):
            return []

    fake_pylon = SimpleNamespace(
        TlFactory=SimpleNamespace(GetInstance=lambda: FakeFactory()),
    )
    monkeypatch.setitem(sys.modules, "pypylon", SimpleNamespace(pylon=fake_pylon))

    def fake_ensure_bridge(config, *, health):
        health.connected = True
        health.status_text = "Aravis bridge test running"
        health.last_error = None
        assert config["camera_name"] == "40123456"
        return True

    class FakeRtspSource:
        def __init__(self, *, config, secret_refs=None) -> None:
            self.config = config
            self.secret_refs = secret_refs or {}
            self.health = SourceHealth(connected=True, status_text="RTSP bridge connected")

        def read(self, _stop_event):
            return FramePacket(
                image_bytes=b"jpeg",
                mime_type="image/jpeg",
                width=3,
                height=2,
                source_timestamp="2026-06-17T00:00:00Z",
                source_uri=self.config["url"],
                sequence=1,
                metadata={"source_type": "aravis_gige", "producer": "aravis"},
            )

        def close(self) -> None:
            self.health.connected = False

    monkeypatch.setattr(sources_module, "_ensure_aravis_rtsp_bridge", fake_ensure_bridge)
    monkeypatch.setattr(sources_module, "OpenCvRtspFrameSource", FakeRtspSource)

    source = build_frame_source(
        source_type="basler_pylon",
        config={
            "serial_number": "40123456",
            "transport_mode": "auto",
            "bridge_name": "basler-40123456",
            "rtsp_host": "127.0.0.1",
            "rtsp_port": 18554,
            "rtsp_mount": "/basler-40123456",
            "url": "rtsp://127.0.0.1:18554/basler-40123456",
            "pixel_format": "BayerRG8",
        },
        secret_refs={},
    )
    try:
        packet = source.read(threading.Event())
    finally:
        source.close()

    assert packet is not None
    assert packet.source_uri == "rtsp://127.0.0.1:18554/basler-40123456"
    assert packet.metadata["source_type"] == "basler_pylon"
    assert packet.metadata["transport"] == "aravis_rtsp"
    assert packet.metadata["producer"] == "aravis"
    assert packet.metadata["basler_transport_mode"] == "auto"


def test_blackfly_source_resolves_gentl_cti_paths_and_selects_devices(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cti_dir = tmp_path / "spinnaker-cti"
    cti_dir.mkdir()
    cti_file = cti_dir / "Spinnaker_GenTL.cti"
    cti_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("GENICAM_GENTL64_PATH", str(cti_dir))

    assert _blackfly_gentl_cti_paths({}) == [cti_file]

    selector, info = _blackfly_device_selector(
        {"serial_number": "22334455"},
        [
            {
                "serial_number": "22334455",
                "model": "Blackfly S BFS-PGE",
                "tl_type": "GEV",
            }
        ],
    )
    assert selector == {"serial_number": "22334455"}
    assert info["tl_type"] == "GEV"

    selector_by_ip, info_by_ip = _blackfly_device_selector(
        {"ip_address": "192.168.10.42"},
        [
            {
                "id_": "gev://192.168.10.42/22334455",
                "serial_number": "22334455",
                "model": "Blackfly S BFS-PGE",
            }
        ],
    )
    assert selector_by_ip == 0
    assert info_by_ip["serial_number"] == "22334455"


def test_runtime_control_start_drives_realtime_executor_live_state(
    sqlite_engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    settings, channel_id = _seed_live_channel(sqlite_engine, tmp_path)
    source = _ScriptedSource(
        [
            None,
            _frame(sample_png_bytes, sequence=1, source_uri="rtsp://camera.local/live"),
        ]
    )
    supervisor = RuntimeExecutorSupervisor(
        settings=settings,
        orchestrator_instance_id="runtime-orchestrator-test",
        realtime_source_factory=lambda *_args, **_kwargs: source,
        realtime_inference_client_factory=lambda _settings: _FakeInferenceClient(),
    )
    orchestrator = RuntimeControlOrchestrator(
        settings=settings,
        orchestrator_instance_id="runtime-orchestrator-test",
        supervisor=supervisor,
    )

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_one() is not None

        live_state = _wait_for_live_state(settings, channel_id)
        assert live_state.available is True
        assert live_state.runtime_instance_id is not None
        assert live_state.frame is not None
        assert live_state.frame.sequence == 1
        assert live_state.source_health.reconnect_count == 1
        assert live_state.metrics.frame_count >= 1
        assert live_state.metrics.drop_rate_percent is not None
        assert live_state.diagnostics.pipeline_max_inflight == min(
            settings.runtime.realtime_pipeline_max_inflight,
            settings.inference_limits.realtime_max_concurrency,
            settings.inference_limits.realtime_per_release_max_concurrency,
        )
        assert live_state.diagnostics.pipeline_configured_max_inflight == (
            settings.runtime.realtime_pipeline_max_inflight
        )
        assert live_state.diagnostics.pipeline_per_release_max_concurrency == (
            settings.inference_limits.realtime_per_release_max_concurrency
        )
        assert live_state.diagnostics.pipeline_inflight_limit_reason in {
            "channel_configuration",
            "per_release_concurrency",
        }
        assert live_state.diagnostics.pipeline_stateful_nodes == []
        assert any(
            item.get("node_key") == "detect" and item.get("execute_ms", 0) >= 0
            for item in live_state.diagnostics.pipeline_node_timings
        )
        assert live_state.metrics.stage_timings_ms.get("source_frame_interval_ms", 0) >= 0
        inference_timing = next(
            item
            for item in live_state.diagnostics.pipeline_inference_timings
            if item.get("node_key") == "detect"
        )
        assert inference_timing["gateway_total_ms"] == 15
        assert inference_timing["client_roundtrip_ms"] >= 0
        assert "detections" in live_state.layers_available
        boxes = [item for item in live_state.overlay if item.primitive_type == "box"]
        assert len(boxes) == 1
        assert boxes[0].layer == "detections"
        assert boxes[0].coordinate_system == "normalized_xy"
        assert boxes[0].geometry["coordinate_system"] == "normalized_xy"

        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            assert execution.frame_count >= 1
            assert execution.metrics_summary["metrics_source"] == "realtime_executor"
            request_runtime_channel_stop(session, channel_id, requested_by="operator")
        assert orchestrator.process_one() is not None

        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            assert execution.status == "stopped"
            persisted_state = get_runtime_live_view_state(session, settings, channel_id=channel_id)
            assert persisted_state.available is False
            assert persisted_state.actual_state.value == "stopped"
    finally:
        supervisor.shutdown()


def test_trigger_live_view_frame_survives_stopping_realtime_execution(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_live_channel(sqlite_engine, tmp_path)
    now = utc_now_iso()
    with session_scope(settings) as session:
        channel = session.get(RuntimeChannel, channel_id)
        assert channel is not None
        execution = RuntimeExecution(
            id="rex_stopping_live",
            channel_id=channel_id,
            pipeline_version_id=channel.pipeline_version_id or "plv_live",
            mode=RuntimeMode.REALTIME.value,
            executor_service="realtime-executor",
            status="stopping",
            actual_state=RuntimeObservedState.STOPPING.value,
            loading_state="stopping",
        )
        session.add(execution)
        channel.active_runtime_execution_id = execution.id
        channel.observed_state = RuntimeObservedState.STOPPING.value

    write_runtime_live_view_state(
        settings,
        RuntimeLiveViewState(
            channel_id=channel_id,
            runtime_instance_id=trigger_live_view_runtime_id(channel_id),
            actual_state=RuntimeObservedState.STOPPED,
            available=True,
            stale=False,
            frame=RuntimeLiveFrame(
                image_base64="ZmFrZS10cmlnZ2VyLWZyYW1l",
                mime_type="image/jpeg",
                width=320,
                height=180,
                captured_at=now,
                sequence=3,
                metadata={"trigger_endpoint_id": "tep_test"},
            ),
            source_health=RuntimeLiveSourceHealth(
                state="ok",
                status_text="Latest trigger frame",
                connected=True,
            ),
            updated_at=now,
        ),
    )

    with session_scope(settings) as session:
        state = get_runtime_live_view_state(session, settings, channel_id=channel_id)

    assert state.available is True
    assert state.runtime_instance_id == trigger_live_view_runtime_id(channel_id)
    assert state.actual_state == RuntimeObservedState.STOPPING
    assert state.frame is not None
    assert state.frame.image_base64 == "ZmFrZS10cmlnZ2VyLWZyYW1l"
    assert state.source_health.status_text == "Latest trigger frame"


def test_stale_trigger_live_view_does_not_cover_starting_realtime_execution(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_live_channel(sqlite_engine, tmp_path)
    settings.runtime.executor_stale_after_seconds = 2
    with session_scope(settings) as session:
        channel = session.get(RuntimeChannel, channel_id)
        assert channel is not None
        execution = RuntimeExecution(
            id="rex_starting_live",
            channel_id=channel_id,
            pipeline_version_id=channel.pipeline_version_id or "plv_live",
            mode=RuntimeMode.REALTIME.value,
            executor_service="realtime-executor",
            status="starting",
            actual_state=RuntimeObservedState.STARTING.value,
            loading_state="starting",
            metrics_summary={"processed_fps": 7.6, "latency_ms": 38.5},
            health_summary={
                "state": HealthState.DEGRADED.value,
                "status_text": "Start requested",
            },
        )
        session.add(execution)
        channel.active_runtime_execution_id = execution.id
        channel.observed_state = RuntimeObservedState.STARTING.value

    write_runtime_live_view_state(
        settings,
        RuntimeLiveViewState(
            channel_id=channel_id,
            runtime_instance_id=trigger_live_view_runtime_id(channel_id),
            actual_state=RuntimeObservedState.STOPPED,
            available=True,
            stale=False,
            frame=RuntimeLiveFrame(
                image_base64="ZmFrZS1vbGQtdHJpZ2dlci1mcmFtZQ==",
                mime_type="image/jpeg",
                width=320,
                height=180,
                captured_at="2026-06-05T08:00:00Z",
                sequence=3,
                metadata={"trigger_endpoint_id": "tep_old"},
            ),
            source_health=RuntimeLiveSourceHealth(
                state="ok",
                status_text="Latest trigger frame",
                connected=True,
            ),
            updated_at="2026-06-05T08:00:00Z",
        ),
    )

    with session_scope(settings) as session:
        state = get_runtime_live_view_state(session, settings, channel_id=channel_id)

    assert state.available is False
    assert state.runtime_instance_id == "rex_starting_live"
    assert state.actual_state == RuntimeObservedState.STARTING
    assert state.metrics.processed_fps == 7.6
    assert state.source_health.status_text == "Start requested"


def test_stale_live_view_uses_current_runtime_health(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_live_channel(sqlite_engine, tmp_path)
    settings.runtime.executor_stale_after_seconds = 2
    with session_scope(settings) as session:
        channel = session.get(RuntimeChannel, channel_id)
        assert channel is not None
        execution = RuntimeExecution(
            id="rex_stale_live_state",
            channel_id=channel_id,
            pipeline_version_id=channel.pipeline_version_id or "plv_live",
            mode=RuntimeMode.REALTIME.value,
            executor_service="realtime-executor",
            status="running",
            actual_state=RuntimeObservedState.RUNNING.value,
            desired_state="running",
            loading_state="ready",
            health_summary={
                "state": HealthState.DEGRADED.value,
                "status_text": "Realtime executor loop reported an error",
                "last_error": "Pipeline failed before publishing a live frame.",
            },
        )
        session.add(execution)
        channel.active_runtime_execution_id = execution.id
        channel.observed_state = RuntimeObservedState.RUNNING.value

    write_runtime_live_view_state(
        settings,
        RuntimeLiveViewState(
            channel_id=channel_id,
            runtime_instance_id="rex_stale_live_state",
            actual_state=RuntimeObservedState.RUNNING,
            available=True,
            stale=False,
            frame=RuntimeLiveFrame(
                image_base64="ZmFrZS1vbGQtZnJhbWU=",
                mime_type="image/jpeg",
                width=320,
                height=180,
                captured_at="2026-06-05T08:00:00Z",
                sequence=3,
            ),
            source_health=RuntimeLiveSourceHealth(
                state="ok",
                status_text="Old source health",
                connected=True,
            ),
            updated_at="2026-06-05T08:00:00Z",
        ),
    )

    with session_scope(settings) as session:
        state = get_runtime_live_view_state(session, settings, channel_id=channel_id)

    assert state.stale is True
    assert state.available is False
    assert state.source_health.state == HealthState.DEGRADED
    assert state.source_health.connected is False
    assert state.source_health.last_error == "Pipeline failed before publishing a live frame."
    assert any("Live frame state is stale" in warning for warning in state.diagnostics.warnings)


def test_realtime_gateway_overload_keeps_live_diagnostics_without_degrading_channel(
    sqlite_engine: object,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    settings, channel_id = _seed_live_channel(sqlite_engine, tmp_path)
    source = _ScriptedSource(
        [
            FramePacket(
                image_bytes=None,
                mime_type="image/jpeg",
                width=8,
                height=6,
                source_timestamp="2026-05-01T12:00:00Z",
                source_uri="rtsp://camera.local/live",
                sequence=1,
                metadata={"source_type": "rtsp"},
                raw_frame=np.zeros((6, 8, 3), dtype=np.uint8),
            ),
        ],
        read_delay_seconds=0.05,
    )
    inference_client = _AlwaysOverloadedInferenceClient()
    supervisor = RuntimeExecutorSupervisor(
        settings=settings,
        orchestrator_instance_id="runtime-orchestrator-test",
        realtime_source_factory=lambda *_args, **_kwargs: source,
        realtime_inference_client_factory=lambda _settings: inference_client,
    )
    orchestrator = RuntimeControlOrchestrator(
        settings=settings,
        orchestrator_instance_id="runtime-orchestrator-test",
        supervisor=supervisor,
    )
    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_one() is not None

        live_state = _wait_for_live_state(settings, channel_id)
        assert live_state.frame is not None
        assert live_state.frame.sequence == 1
        assert live_state.overlay == []
        assert "detections" not in live_state.layers_available
        assert any(
            item.get("node_type") == "inference.detection"
            and item.get("status") == "failed"
            and item.get("error_code") == "inference_gateway_overloaded"
            for item in live_state.diagnostics.pipeline_node_timings
        )
        assert any(
            "inference_gateway_overloaded" in warning for warning in live_state.diagnostics.warnings
        )
        assert live_state.frame.metadata["frame_drop_error"]["status_code"] == 429
        _wait_for_execution_metric(settings, "inference_drop_count", minimum=1)

        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            channel = session.query(RuntimeChannel).one()
            assert execution.status == "running"
            assert execution.actual_state == "running"
            assert execution.last_error is None
            assert execution.metrics_summary["inference_drop_count"] >= 1
            assert execution.metrics_summary["drop_count"] >= 1
            assert execution.metrics_summary["drop_rate_percent"] > 0
            assert session.query(ResultRecord).count() == 0
            assert channel.observed_state == "running"
            assert channel.health_state == "ok"
            assert channel.last_error is None
            request_runtime_channel_stop(session, channel_id, requested_by="operator")
        assert orchestrator.process_one() is not None
    finally:
        supervisor.shutdown()


def test_realtime_classification_timeout_drops_frame_and_updates_channel_health(
    sqlite_engine: object,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    settings, channel_id = _seed_live_channel(
        sqlite_engine,
        tmp_path,
        task_type=TaskType.CLASSIFICATION,
    )
    source = _ScriptedSource(
        [_frame(sample_png_bytes, sequence=1, source_uri="rtsp://camera.local/live")],
        read_delay_seconds=0.05,
    )
    supervisor = RuntimeExecutorSupervisor(
        settings=settings,
        orchestrator_instance_id="runtime-orchestrator-test",
        realtime_source_factory=lambda *_args, **_kwargs: source,
        realtime_inference_client_factory=lambda _settings: _ClassificationTimeoutClient(),
    )
    orchestrator = RuntimeControlOrchestrator(
        settings=settings,
        orchestrator_instance_id="runtime-orchestrator-test",
        supervisor=supervisor,
    )

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_one() is not None

        live_state = _wait_for_live_state(settings, channel_id)
        assert live_state.frame is not None
        assert live_state.frame.sequence == 1
        assert live_state.metrics.dropped_frame_count >= 1
        assert live_state.metrics.drop_rate_percent is not None
        assert live_state.metrics.drop_rate_percent > 0
        assert any(
            item.get("node_type") == "inference.classification"
            and item.get("status") == "failed"
            and item.get("error_code") == "inference_timeout"
            for item in live_state.diagnostics.pipeline_node_timings
        )
        assert any("inference_timeout" in warning for warning in live_state.diagnostics.warnings)
        assert live_state.frame.metadata["frame_drop_error"]["status_code"] == 504

        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            channel = session.query(RuntimeChannel).one()
            assert execution.status == "running"
            assert execution.actual_state == "running"
            assert execution.last_error is None
            assert execution.metrics_summary["inference_drop_count"] >= 1
            assert execution.metrics_summary["frame_count"] == 0
            assert session.query(ResultRecord).count() == 0
            assert channel.observed_state in {"running", "degraded"}
            assert channel.health_state in {"ok", "degraded"}
            assert channel.last_error is None
            request_runtime_channel_stop(session, channel_id, requested_by="operator")
        assert orchestrator.process_one() is not None
    finally:
        supervisor.shutdown()


def test_realtime_worker_keeps_heartbeat_and_drops_frames_while_inference_is_busy(
    sqlite_engine: object,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    settings, channel_id = _seed_live_channel(sqlite_engine, tmp_path)
    settings.runtime.executor_heartbeat_interval_seconds = 1
    settings.runtime.executor_stale_after_seconds = 2
    settings.inference_limits.realtime_timeout_ms = 250
    source = _ScriptedSource(
        [
            _frame(sample_png_bytes, sequence=sequence, source_uri="rtsp://camera.local/live")
            for sequence in range(1, 12)
        ],
        read_delay_seconds=0.05,
    )
    supervisor = RuntimeExecutorSupervisor(
        settings=settings,
        orchestrator_instance_id="runtime-orchestrator-test",
        realtime_source_factory=lambda *_args, **_kwargs: source,
        realtime_inference_client_factory=lambda _settings: _BlockingInferenceClient(
            block_seconds=3.0
        ),
    )
    orchestrator = RuntimeControlOrchestrator(
        settings=settings,
        orchestrator_instance_id="runtime-orchestrator-test",
        supervisor=supervisor,
    )

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_one() is not None

        _wait_for_execution_metric(settings, "inference_drop_count", minimum=1)
        time.sleep(2.2)
        orchestrator.reconcile()

        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            channel = session.query(RuntimeChannel).one()
            assert execution.status == "running"
            assert channel.observed_state in {"running", "degraded"}
            assert execution.metrics_summary["inference_drop_count"] >= 1
            assert execution.metrics_summary["pipeline_deadline_drop_count"] >= 1
            request_runtime_channel_stop(session, channel_id, requested_by="operator")
        assert orchestrator.process_one() is not None
    finally:
        supervisor.shutdown()


def test_realtime_worker_publishes_completed_result_after_newer_busy_drops(
    sqlite_engine: object,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    settings, channel_id = _seed_live_channel(sqlite_engine, tmp_path)
    settings.runtime.realtime_pipeline_max_inflight = 1
    settings.inference_limits.realtime_timeout_ms = 1_000
    source = _ScriptedSource(
        [
            _frame(sample_png_bytes, sequence=sequence, source_uri="rtsp://camera.local/live")
            for sequence in range(1, 41)
        ],
        read_delay_seconds=0.01,
    )
    supervisor = RuntimeExecutorSupervisor(
        settings=settings,
        orchestrator_instance_id="runtime-orchestrator-test",
        realtime_source_factory=lambda *_args, **_kwargs: source,
        realtime_inference_client_factory=lambda _settings: _BlockingInferenceClient(
            block_seconds=0.4
        ),
    )
    orchestrator = RuntimeControlOrchestrator(
        settings=settings,
        orchestrator_instance_id="runtime-orchestrator-test",
        supervisor=supervisor,
    )

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_one() is not None

        _wait_for_execution_metric(settings, "inference_drop_count", minimum=1)
        _wait_for_execution_metric(settings, "frame_count", minimum=1)

        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            assert execution.metrics_summary["frame_count"] >= 1
            assert execution.metrics_summary["inference_drop_count"] >= 1
            request_runtime_channel_stop(session, channel_id, requested_by="operator")
        assert orchestrator.process_one() is not None
    finally:
        supervisor.shutdown()


def test_realtime_terminal_inference_failure_is_persisted_as_failed_result(
    sqlite_engine: object,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    settings, channel_id = _seed_live_channel(sqlite_engine, tmp_path)
    source = _ScriptedSource(
        [_frame(sample_png_bytes, sequence=1, source_uri="rtsp://camera.local/live")],
        read_delay_seconds=0.05,
    )
    supervisor = RuntimeExecutorSupervisor(
        settings=settings,
        orchestrator_instance_id="runtime-orchestrator-test",
        realtime_source_factory=lambda *_args, **_kwargs: source,
        realtime_inference_client_factory=lambda _settings: _TerminalInferenceFailureClient(),
    )
    orchestrator = RuntimeControlOrchestrator(
        settings=settings,
        orchestrator_instance_id="runtime-orchestrator-test",
        supervisor=supervisor,
    )

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_one() is not None

        live_state = _wait_for_live_state(settings, channel_id)
        assert live_state.frame is not None
        assert live_state.metrics.dropped_frame_count == 0

        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            channel = session.query(RuntimeChannel).one()
            result = session.query(ResultRecord).one()
            assert execution.metrics_summary.get("inference_drop_count", 0) == 0
            assert result.status == "failed"
            assert result.result_type == "failed_execution"
            assert (
                result.result["errors"][0]["message"]
                == "Detection node requires a detection model."
            )
            assert channel.observed_state in {"running", "degraded"}
            assert channel.health_state in {"ok", "degraded"}
            if channel.health_state == "degraded":
                assert channel.health_summary["health_reasons"]
            request_runtime_channel_stop(session, channel_id, requested_by="operator")
        assert orchestrator.process_one() is not None
    finally:
        supervisor.shutdown()


def _wait_for_live_state(settings: PlatformSettings, channel_id: str):
    # Give the worker thread a first DB turn before polling the in-memory SQLite
    # fixture from the test thread.
    time.sleep(0.1)
    deadline = time.monotonic() + 5
    last_state = None
    while time.monotonic() < deadline:
        with session_scope(settings) as session:
            last_state = get_runtime_live_view_state(session, settings, channel_id=channel_id)
            if last_state.available:
                return last_state
        time.sleep(0.05)
    raise AssertionError(f"Live state did not become available: {last_state!r}")


def _wait_for_execution_metric(settings: PlatformSettings, key: str, *, minimum: int) -> None:
    deadline = time.monotonic() + 5
    last_value = None
    while time.monotonic() < deadline:
        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            last_value = execution.metrics_summary.get(key, 0)
            if last_value >= minimum:
                return
        time.sleep(0.05)
    raise AssertionError(f"Runtime execution metric {key!r} stayed below {minimum}: {last_value!r}")


class _ScriptedSource:
    def __init__(
        self,
        packets: list[FramePacket | None],
        *,
        read_delay_seconds: float = 0.0,
    ) -> None:
        self.health = SourceHealth(connected=True, status_text="Scripted RTSP source connected")
        self._packets = list(packets)
        self._lock = threading.Lock()
        self._read_delay_seconds = read_delay_seconds

    def read(self, stop_event: threading.Event) -> FramePacket | None:
        with self._lock:
            if self._packets:
                packet = self._packets.pop(0)
                if self._read_delay_seconds:
                    time.sleep(self._read_delay_seconds)
                if packet is None:
                    self.health.connected = False
                    self.health.status_text = "Scripted RTSP reconnect"
                    self.health.last_error = "simulated reconnect"
                    self.health.reconnect_count += 1
                    self.health.source_error_count += 1
                    time.sleep(0.02)
                    self.health.connected = True
                    self.health.status_text = "Scripted RTSP source connected"
                    self.health.last_error = None
                return packet
        stop_event.wait(0.05)
        return None

    def close(self) -> None:
        self.health.connected = False
        self.health.status_text = "Scripted source closed"


class _FakeInferenceClient:
    def classify(self, payload):  # pragma: no cover - not used
        raise AssertionError("Classification should not be called in this test.")

    def classify_bytes(self, **_kwargs):  # pragma: no cover - not used
        raise AssertionError("Classification bytes should not be called in this test.")

    def detect(self, payload: object) -> DetectionInferenceResponse:
        return DetectionInferenceResponse(
            request_id="req_live_detection",
            correlation_id="corr_live_detection",
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

    def segment(self, payload):  # pragma: no cover - not used
        raise AssertionError("Segmentation should not be called in this test.")


class _OverloadedOnceInferenceClient(_FakeInferenceClient):
    def __init__(self) -> None:
        self._overloaded = True

    def detect(self, payload) -> DetectionInferenceResponse:
        if self._overloaded:
            self._overloaded = False
            raise RuntimeNodeExecutionError(
                code="inference_gateway_overloaded",
                message="Inference gateway is overloaded.",
                retryable=True,
                metadata={"status_code": 429},
            )
        return super().detect(payload)


class _AlwaysOverloadedInferenceClient(_FakeInferenceClient):
    def detect(self, payload) -> DetectionInferenceResponse:
        raise RuntimeNodeExecutionError(
            code="inference_gateway_overloaded",
            message="Inference gateway is overloaded.",
            retryable=True,
            metadata={"status_code": 429},
        )


class _ClassificationTimeoutClient(_FakeInferenceClient):
    def classify_bytes(self, **_kwargs) -> ClassificationInferenceResponse:
        raise RuntimeNodeExecutionError(
            code="inference_timeout",
            message="The inference request exceeded its timeout budget.",
            retryable=True,
            metadata={"status_code": 504},
        )


class _BlockingInferenceClient(_FakeInferenceClient):
    def __init__(self, *, block_seconds: float) -> None:
        self.block_seconds = block_seconds

    def detect(self, payload) -> DetectionInferenceResponse:
        time.sleep(self.block_seconds)
        return super().detect(payload)


class _TerminalInferenceFailureClient(_FakeInferenceClient):
    def detect(self, payload) -> DetectionInferenceResponse:
        raise RuntimeNodeExecutionError(
            code="model_task_incompatible",
            message="Detection node requires a detection model.",
            retryable=False,
        )


def _seed_live_channel(
    sqlite_engine,
    tmp_path: Path,
    *,
    task_type: TaskType = TaskType.DETECTION,
) -> tuple[PlatformSettings, str]:
    configure_sqlite_factories(sqlite_engine, "runtime-orchestrator")
    settings = build_test_settings("runtime-orchestrator", tmp_path)
    with session_scope(settings) as session:
        if task_type == TaskType.CLASSIFICATION:
            seed_deployed_runtime_model(
                session,
                release_id="rel_classification",
                model_family_id="mf_quality_classifier",
                model_family_slug="quality-classifier",
                model_family_display_name="Quality Classifier",
                model_version_id="mvr_classification",
                task_type=TaskType.CLASSIFICATION,
            )
        else:
            seed_deployed_runtime_model(session)
        version_id = _create_live_pipeline(session, task_type=task_type)
        channel = create_runtime_channel(
            session,
            RuntimeChannelCreateRequest(
                name="Live press camera",
                technical_place="press-line-1",
                source_type=RuntimeSourceType.RTSP,
                source_config={"url": "rtsp://camera.local/live", "max_fps": 30},
                mode=RuntimeMode.REALTIME,
                assigned_pipeline="Live defect overlay",
                pipeline_version_id=version_id,
            ),
            created_by="tester",
        )
    return settings, channel.channel_id


def _create_live_pipeline(session, *, task_type: TaskType = TaskType.DETECTION) -> str:
    inference_node_key = "classify" if task_type == TaskType.CLASSIFICATION else "detect"
    inference_output_port = (
        "classifications" if task_type == TaskType.CLASSIFICATION else "detections"
    )
    definition = create_pipeline_definition(
        session,
        RuntimePipelineDefinitionCreateRequest(name="Live defect overlay"),
        created_by="tester",
    )
    version = create_pipeline_version(
        session,
        definition.pipeline_definition_id,
        RuntimePipelineVersionCreateRequest(
            mode=RuntimeMode.REALTIME,
            graph=RuntimePipelineGraph(
                nodes=[
                    _node("channel_input", "source.channel_frame"),
                    _inference_node(inference_node_key, task_type=task_type),
                    _node("overlay", "output.overlay"),
                ],
                edges=[
                    RuntimeGraphEdge(
                        from_node_key="channel_input",
                        from_port="frame",
                        to_node_key=inference_node_key,
                        to_port="frame",
                    ),
                    RuntimeGraphEdge(
                        from_node_key=inference_node_key,
                        from_port=inference_output_port,
                        to_node_key="overlay",
                        to_port="result",
                    ),
                ],
            ),
        ),
        created_by="tester",
    )
    return publish_pipeline_version(
        session, version.pipeline_version_id, published_by="tester"
    ).pipeline_version_id


def _node(
    node_key: str,
    node_type: str,
    *,
    config: dict[str, object] | None = None,
) -> RuntimeNodeSpec:
    category = (
        RuntimeNodeCategory.SOURCE
        if node_type.startswith("source.")
        else RuntimeNodeCategory.OUTPUT
    )
    return RuntimeNodeSpec(
        node_key=node_key,
        node_type=node_type,
        node_category=category,
        config=config or {},
    )


def _inference_node(
    node_key: str,
    *,
    task_type: TaskType = TaskType.DETECTION,
) -> RuntimeNodeSpec:
    if task_type == TaskType.CLASSIFICATION:
        return RuntimeNodeSpec(
            node_key=node_key,
            node_type="inference.classification",
            node_category=RuntimeNodeCategory.INFERENCE,
            logical_model_ref=RuntimeLogicalModelRef(
                model_family_slug="quality-classifier",
                target_path=TargetPath.REALTIME,
                required_task_type=TaskType.CLASSIFICATION,
                selection_policy="explicit_release",
                deployment_release_id="rel_classification",
            ),
        )
    return RuntimeNodeSpec(
        node_key=node_key,
        node_type="inference.detection",
        node_category=RuntimeNodeCategory.INFERENCE,
        logical_model_ref=RuntimeLogicalModelRef(
            model_family_slug="defect-detector",
            target_path=TargetPath.REALTIME,
            required_task_type=TaskType.DETECTION,
            selection_policy="explicit_release",
            deployment_release_id="rel_detection",
        ),
    )


def _frame(
    sample_png_bytes: bytes,
    *,
    sequence: int,
    source_uri: str | None = "rtsp://camera.local/live",
) -> FramePacket:
    return FramePacket(
        image_bytes=sample_png_bytes,
        mime_type="image/png",
        width=1,
        height=1,
        source_timestamp="2026-05-01T12:00:00Z",
        source_uri=source_uri,
        sequence=sequence,
        metadata={"source_type": "rtsp"},
    )
