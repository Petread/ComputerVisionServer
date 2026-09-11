from cv_contracts.runtime import RuntimeLiveFrame, RuntimeLiveViewState
from cv_runtime.payloads import FramePayload, RuntimeExecutionResult, RuntimeImageContext
from cv_status import RuntimeObservedState

from runtime_admin_api.api.routes import (
    _frame_payload_from_live_state,
    _preview_live_state_from_result,
    _should_emit_mjpeg_snapshot,
)


def test_mjpeg_stream_repeats_unchanged_frame_as_keepalive() -> None:
    assert _should_emit_mjpeg_snapshot(
        7,
        last_sequence=None,
        now_perf=10.0,
        last_emit_perf=None,
    )
    assert not _should_emit_mjpeg_snapshot(
        7,
        last_sequence=7,
        now_perf=10.5,
        last_emit_perf=10.0,
        keepalive_seconds=1.0,
    )
    assert _should_emit_mjpeg_snapshot(
        7,
        last_sequence=7,
        now_perf=11.0,
        last_emit_perf=10.0,
        keepalive_seconds=1.0,
    )
    assert _should_emit_mjpeg_snapshot(
        8,
        last_sequence=7,
        now_perf=10.1,
        last_emit_perf=10.0,
        keepalive_seconds=1.0,
    )


def test_authoring_preview_builds_live_state_with_pipeline_and_config_overlays() -> None:
    fallback_state = RuntimeLiveViewState(
        channel_id="ch_1",
        actual_state=RuntimeObservedState.RUNNING,
        available=True,
        display_context_id="ctx_original",
        frame=RuntimeLiveFrame(
            image_base64="aW1hZ2U=",
            mime_type="image/jpeg",
            width=100,
            height=80,
            sequence=7,
            image_context={
                "context_id": "ctx_original",
                "kind": "original",
                "width": 100,
                "height": 80,
            },
            metadata={},
        ),
    )
    fallback_frame = _frame_payload_from_live_state("ch_1", fallback_state)
    assert fallback_frame is not None

    result = RuntimeExecutionResult(
        status="completed",
        channel_id="ch_1",
        pipeline_version_id="preview",
        runtime_execution_id="preview-ch_1",
        correlation_id="preview_1",
        started_at="2026-05-27T10:00:00Z",
        completed_at="2026-05-27T10:00:00Z",
        cv_edges=[
            {
                "node_key": "edges",
                "node_type": "computer_vision.canny_edge_detection",
                "output_port": "edges",
                "payload_type": "cv_edges",
                "payload": {
                    "payload_type": "cv_edges",
                    "image_context_id": "ctx_edges",
                    "source_image_context_id": "ctx_original",
                    "edge_mask_frame": {
                        "payload_type": "frame",
                        "image_base64": "bWFzaw==",
                        "mime_type": "image/png",
                        "width": 100,
                        "height": 80,
                        "metadata": {},
                    },
                    "contours": [],
                    "line_candidates": [
                        {
                            "line_id": "line_1",
                            "line": {"start": {"x": 10, "y": 20}, "end": {"x": 90, "y": 20}},
                            "length_px": 80,
                            "angle_degrees": 0,
                            "score": 0.8,
                            "method": "hough_line_segments",
                            "metadata": {},
                        }
                    ],
                    "edge_pixel_count": 42,
                    "width": 100,
                    "height": 80,
                    "algorithm": "canny",
                    "metadata": {},
                    "provenance": [],
                },
            }
        ],
        timing={"total_ms": 12.5, "node_timings_ms": [{"node_key": "edges", "status": "completed"}]},
    )

    live_state = _preview_live_state_from_result(
        channel_id="ch_1",
        fallback_state=fallback_state,
        fallback_frame=fallback_frame,
        result=result,
        graph_nodes=[
            {
                "node_key": "crop",
                "node_type": "preprocessing.crop",
                "config": {"x": 5, "y": 6, "width": 40, "height": 30},
            },
            {
                "node_key": "roi",
                "node_type": "preprocessing.roi_extract",
                "config": {"regions": [{"label": "Inspection", "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}]},
            },
            {
                "node_key": "zones",
                "node_type": "preprocessing.zone_filter",
                "config": {"zones": [{"label": "Reject", "x": 0.5, "y": 0.1, "width": 0.2, "height": 0.2}]},
            },
        ],
    )

    layers = {primitive.layer for primitive in live_state.overlay}
    assert {"cv_edge_mask", "cv_candidate_lines", "authoring_crop", "authoring_rois", "authoring_zones"} <= layers
    assert live_state.available is True
    assert live_state.source_frame is not None
    assert live_state.source_frame.width == 100
    assert live_state.source_frame.height == 80
    assert live_state.source_frame.image_context["context_id"] == "ctx_original"
    assert live_state.metrics.latency_ms == 12.5
    assert live_state.diagnostics.pipeline_node_timings == [{"node_key": "edges", "status": "completed"}]


def test_authoring_preview_uses_live_frame_context_for_pipeline_input() -> None:
    live_state = RuntimeLiveViewState(
        channel_id="ch_2",
        actual_state=RuntimeObservedState.RUNNING,
        available=True,
        frame=RuntimeLiveFrame(
            image_base64="aW1hZ2U=",
            mime_type="image/jpeg",
            width=320,
            height=240,
            sequence=3,
            image_context={"context_id": "ctx_ch_2", "kind": "original", "width": 320, "height": 240},
            metadata={"source": "test"},
        ),
    )

    frame = _frame_payload_from_live_state("ch_2", live_state)

    assert isinstance(frame, FramePayload)
    assert isinstance(frame.image_context, RuntimeImageContext)
    assert frame.image_context.context_id == "ctx_ch_2"
    assert frame.metadata["live_frame_sequence"] == 3


def test_authoring_preview_prefers_original_source_frame_over_processed_frame() -> None:
    live_state = RuntimeLiveViewState(
        channel_id="ch_crop",
        actual_state=RuntimeObservedState.RUNNING,
        available=True,
        frame=RuntimeLiveFrame(
            image_base64="Y3JvcHBlZA==",
            mime_type="image/jpeg",
            width=320,
            height=200,
            sequence=9,
            image_context={
                "context_id": "ctx_crop",
                "kind": "crop",
                "width": 320,
                "height": 200,
            },
        ),
        source_frame=RuntimeLiveFrame(
            image_base64="b3JpZ2luYWw=",
            mime_type="image/jpeg",
            width=1280,
            height=720,
            sequence=9,
            image_context={
                "context_id": "ctx_original",
                "kind": "original",
                "width": 1280,
                "height": 720,
            },
        ),
    )

    frame = _frame_payload_from_live_state("ch_crop", live_state)

    assert frame is not None
    assert frame.image_base64 == "b3JpZ2luYWw="
    assert frame.width == 1280
    assert frame.height == 720
    assert frame.image_context is not None
    assert frame.image_context.context_id == "ctx_original"
