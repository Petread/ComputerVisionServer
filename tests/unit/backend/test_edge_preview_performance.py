from __future__ import annotations

import base64
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from cv_contracts import EdgeDeploymentManifest, EdgeDeviceAddress, EdgePreviewFrame
from edge_agent.runtime import SourceFrame
from edge_agent.state import EdgeAgentConfig, EdgeAgentState, _draw_overlays, _preview_overlays
from runtime_admin_api import edge_result_sync


@pytest.mark.parametrize("processed", [0, 42, None])
def test_preview_relay_uses_execution_counts_and_node_timings(
    monkeypatch: pytest.MonkeyPatch,
    processed: int | None,
) -> None:
    channel = SimpleNamespace(
        deleted_at=None,
        edge_device_id="edge-1",
        execution_target="edge_device",
        edge_workload_release_id="release-1",
        observed_state="running",
    )
    session = SimpleNamespace(get=lambda *_: channel)
    monkeypatch.setattr(edge_result_sync, "session_scope", lambda _: nullcontext(session))
    states = []
    monkeypatch.setattr(
        edge_result_sync,
        "write_runtime_live_view_state",
        lambda _, state, **kwargs: states.append(state),
    )
    nodes = [{"node_key": "model", "total_ms": 14.5, "status": "completed"}]
    preview = EdgePreviewFrame(
        channel_id="channel-1",
        workload_release_id="release-1",
        boot_id="boot-1",
        available=True,
        sequence=8,
        width=1,
        height=1,
        image_base64="aW1hZ2U=",
        metadata={
            "processed_frames": processed,
            "acquired_frames": 100,
            "source_fps": 50,
            "processed_fps": 21,
            "latency_ms": 15,
            "pipeline_node_timings": nodes,
        },
    )
    synchronizer = edge_result_sync.EdgeResultSynchronizer(SimpleNamespace())
    synchronizer._write_preview(SimpleNamespace(edge_device_id="edge-1"), preview)
    assert states[0].metrics.frame_count == (8 if processed is None else processed)
    assert states[0].metrics.acquired_frame_count == 100
    assert states[0].metrics.source_fps == 50
    assert states[0].metrics.processed_fps == 21
    assert states[0].diagnostics.pipeline_node_timings == nodes


def test_preview_relay_keeps_shared_runtime_overlay_with_producer_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = SimpleNamespace(
        deleted_at=None,
        edge_device_id="edge-1",
        execution_target="edge_device",
        edge_workload_release_id="release-1",
        observed_state="running",
    )
    monkeypatch.setattr(
        edge_result_sync,
        "session_scope",
        lambda _: nullcontext(SimpleNamespace(get=lambda *_: channel)),
    )
    states = []
    monkeypatch.setattr(
        edge_result_sync,
        "write_runtime_live_view_state",
        lambda _, state, **kwargs: states.append(state),
    )
    preview = EdgePreviewFrame(
        channel_id="channel-1",
        workload_release_id="release-1",
        boot_id="boot-1",
        available=True,
        sequence=9,
        width=200,
        height=100,
        image_base64="aW1hZ2U=",
        metadata={
            "overlay_included": True,
            "overlay_primitives": [
                {
                    "node_key": "overlay",
                    "node_type": "output.overlay",
                    "output_port": "overlay",
                    "primitive_type": "box",
                    "layer": "detections",
                    "label": "board",
                    "confidence": 0.9,
                    "geometry": {
                        "x_min": 0.1,
                        "y_min": 0.2,
                        "x_max": 0.5,
                        "y_max": 0.6,
                        "coordinate_system": "normalized_xy",
                    },
                    "style": {},
                    "metadata": {},
                }
            ],
        },
    )

    edge_result_sync.EdgeResultSynchronizer(SimpleNamespace())._write_preview(
        SimpleNamespace(edge_device_id="edge-1"), preview
    )

    assert len(states[0].overlay) == 1
    assert states[0].overlay[0].coordinate_system == "normalized_xy"
    assert states[0].overlay[0].metadata["producer"] == {
        "node_key": "overlay",
        "node_type": "output.overlay",
        "output_port": "overlay",
    }
    assert states[0].layers_available == ["detections"]


def test_edge_preview_projects_crop_overlay_and_draws_contract_box() -> None:
    overlays = _preview_overlays(
        {
            "image_contexts": [
                {
                    "context_id": "source",
                    "width": 200,
                    "height": 100,
                    "source_image_context_id": "source",
                    "transform": {"transform_type": "identity", "parameters": {}},
                },
                {
                    "context_id": "crop",
                    "width": 100,
                    "height": 50,
                    "source_image_context_id": "source",
                    "parent_image_context_id": "source",
                    "transform": {
                        "transform_type": "crop",
                        "parameters": {"x": 50, "y": 20, "width": 100, "height": 50},
                    },
                },
            ],
            "overlay_primitives": [
                {
                    "primitive_type": "box",
                    "layer": "detections",
                    "image_context_id": "crop",
                    "geometry": {
                        "x_min": 0.1,
                        "y_min": 0.2,
                        "x_max": 0.5,
                        "y_max": 0.6,
                        "coordinate_system": "normalized_xy",
                    },
                    "style": {},
                    "metadata": {},
                }
            ],
        },
        source_width=200,
        source_height=100,
    )
    rectangles = []
    cv2 = SimpleNamespace(
        rectangle=lambda _image, start, end, _color, _width: rectangles.append((start, end)),
    )
    image = SimpleNamespace(shape=(100, 200, 3))

    _draw_overlays(image, overlays, cv2)

    assert overlays[0]["geometry"]["coordinate_system"] == "image_pixels"
    assert rectangles == [((60, 30), (100, 50))]


def test_trigger_preview_stays_in_memory_without_an_active_viewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Image:
        shape = (1, 1, 3)

        def copy(self) -> Image:
            return self

    class Encoded:
        def tobytes(self) -> bytes:
            return b"jpeg"

    monkeypatch.setitem(
        sys.modules,
        "cv2",
        SimpleNamespace(IMWRITE_JPEG_QUALITY=1, imencode=lambda *_args: (True, Encoded())),
    )
    state = EdgeAgentState(
        EdgeAgentConfig(
            address=EdgeDeviceAddress(management_ip="192.168.50.21"),
            data_root=tmp_path,
            minimum_disk_free_bytes=0,
            engine_builder_mode="simulated",
        )
    )
    manifest = EdgeDeploymentManifest(
        deployment_id="dep-trigger-preview",
        workload_release_id="release-trigger-preview",
        channel_id="channel-trigger-preview",
        pipeline_version_id="pipeline-trigger-preview",
        idempotency_key="prepare-trigger-preview",
        created_at="2026-09-21T08:00:00Z",
    )
    frame = SourceFrame(7, 1, 1, "2026-09-21T08:00:01Z", Image())
    try:
        state._runtime_preview_sink(
            manifest,
            frame,
            {
                "correlation_id": "cycle-1",
                "overlay_primitives": [],
                "runtime_metadata": {"trigger_preview": True},
            },
        )
        preview = state._previews.get(manifest.channel_id)
        assert preview is not None
        assert base64.b64decode(preview.image_base64) == b"jpeg"
        assert preview.captured_at == frame.captured_at
        assert preview.metadata["trigger_preview"] is True
        assert not (tmp_path / "previews").exists()
        assert state.store.active_media_leases(manifest.channel_id, now=frame.captured_at) == []
    finally:
        state.close()
