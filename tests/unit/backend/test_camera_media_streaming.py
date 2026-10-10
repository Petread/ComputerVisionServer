from __future__ import annotations

import threading
import time
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import camera_media.gst_worker as gst_worker
import numpy as np
import pytest
from camera_media.frames import encoder_frame
from camera_media.gstreamer import NvencPreflightError, _require_elements
from camera_media.mediamtx import path_is_ready, path_reader_count
from camera_media.worker import (
    _apply_canonical_steps,
    _canonical_image_view_frame,
    _canonical_image_view_process_main,
    _image_view_ring_id,
    _pad_frame_for_nvenc,
    _strict_camera_source_config,
    _undistorted_image_view_frame,
    _undistorted_image_view_process_main,
    _undistorted_image_view_ring_id,
    _validated_canonical_steps,
)
from cv_contracts.runtime import (
    RuntimeCameraCalibrationBoardDefinition,
    RuntimeImageViewProfileCalculateRequest,
    RuntimeNormalizedImagePoint,
)
from cv_db.stream_store import ImageViewStreamBinding, _image_view_stream_binding
from cv_runtime import (
    LatestFrameRingReader,
    LatestFrameRingWriter,
    apply_image_view,
    automatic_stream_bitrate_kbps,
    calculate_image_view_profile,
    create_identity_camera_calibration,
    validate_basler_link_budget,
    validate_nvenc_dimensions,
)


def test_shared_frame_ring_is_latest_wins_and_preserves_capture_metadata(
    tmp_path: Path,
) -> None:
    first = np.zeros((6, 8, 3), dtype=np.uint8)
    second = np.full((6, 8, 3), 73, dtype=np.uint8)
    writer = LatestFrameRingWriter(
        runtime_dir=tmp_path,
        channel_id="chn_live",
        frame=first,
        slot_count=4,
        source_uri="basler://camera-a",
        source_metadata={"source_type": "basler_pylon"},
        configured_fps=30,
    )
    reader = LatestFrameRingReader(runtime_dir=tmp_path, channel_id="chn_live")
    try:
        writer.write(first, sequence=10, capture_monotonic_ns=100)
        writer.write(second, sequence=11, capture_monotonic_ns=200)

        shared = reader.read_latest(timeout_seconds=0.1)

        assert shared is not None
        assert shared.sequence == 11
        assert shared.capture_monotonic_ns == 200
        assert shared.descriptor.configured_fps == 30
        assert shared.descriptor.source_uri == "basler://camera-a"
        np.testing.assert_array_equal(shared.frame, second)
        assert reader.latest(after_sequence=11) is None
    finally:
        reader.close()
        writer.close()


def test_shared_frame_reader_reattaches_after_profile_generation_change(
    tmp_path: Path,
) -> None:
    writer = LatestFrameRingWriter(
        runtime_dir=tmp_path,
        channel_id="chn_live",
        frame=np.zeros((6, 8, 3), dtype=np.uint8),
    )
    reader = LatestFrameRingReader(runtime_dir=tmp_path, channel_id="chn_live")
    try:
        writer.write(np.zeros((6, 8, 3), dtype=np.uint8), sequence=1)
        assert reader.read_latest(timeout_seconds=0.1) is not None
        writer.close()
        writer = LatestFrameRingWriter(
            runtime_dir=tmp_path,
            channel_id="chn_live",
            frame=np.full((10, 12, 3), 9, dtype=np.uint8),
        )
        writer.write(np.full((10, 12, 3), 9, dtype=np.uint8), sequence=1)

        replacement = reader.read_latest(timeout_seconds=0.2)

        assert replacement is not None
        assert replacement.frame.shape == (10, 12, 3)
        assert replacement.descriptor.generation == writer.descriptor.generation
    finally:
        reader.close()
        writer.close()


def test_basler_link_budget_rejects_unsafe_rgb_but_accepts_bayer() -> None:
    assert (
        validate_basler_link_budget(
            {
                "fps": 30,
                "link_speed_mbps": 1000,
                "pixel_format": "BayerRG8",
            },
            required=False,
        )
        is None
    )
    with pytest.raises(ValueError, match="exceeds 80%"):
        validate_basler_link_budget(
            {
                "width": 1900,
                "height": 1200,
                "fps": 30,
                "link_speed_mbps": 1000,
                "pixel_format": "RGB8",
            },
            required=True,
        )

    payload = validate_basler_link_budget(
        {
            "width": 1900,
            "height": 1200,
            "fps": 30,
            "link_speed_mbps": 1000,
            "pixel_format": "BayerRG8",
        },
        required=True,
    )

    assert payload == pytest.approx(547.2)


def test_stream_profile_validation_and_bitrate_bounds() -> None:
    validate_nvenc_dimensions(1900, 1200)
    with pytest.raises(ValueError, match="even stream dimensions"):
        validate_nvenc_dimensions(1901, 1200)
    assert automatic_stream_bitrate_kbps(320, 240, 5) == 2_000
    assert automatic_stream_bitrate_kbps(3840, 2160, 60) == 20_000
    assert automatic_stream_bitrate_kbps(1900, 1200, 30) == 8_208


def test_camera_frame_conversion_is_contiguous_bgr_without_scaling() -> None:
    gray = np.arange(48, dtype=np.uint8).reshape((6, 8))

    converted = encoder_frame(gray)

    assert converted.shape == (6, 8, 3)
    assert converted.flags.c_contiguous
    np.testing.assert_array_equal(converted[:, :, 0], gray)
    np.testing.assert_array_equal(converted[:, :, 1], gray)


def test_camera_frame_conversion_trims_odd_dimensions_for_nvenc() -> None:
    frame = np.arange(5 * 7 * 3, dtype=np.uint8).reshape((5, 7, 3))

    converted = encoder_frame(frame)

    assert converted.shape == (4, 6, 3)
    np.testing.assert_array_equal(converted, frame[:4, :6])


def test_camera_media_forces_direct_basler_and_rejects_cpu_aravis_bridge() -> None:
    strict = _strict_camera_source_config("basler_pylon", {"transport_mode": "auto"})

    assert strict["transport_mode"] == "pylon_direct"
    assert "fps" not in strict
    assert "link_speed_mbps" not in strict
    assert "pixel_format" not in strict
    with pytest.raises(ValueError, match="direct Pylon"):
        _strict_camera_source_config(
            "basler_pylon",
            {"transport_mode": "aravis_rtsp_bridge"},
        )
    with pytest.raises(ValueError, match="legacy Aravis"):
        _strict_camera_source_config("aravis_gige", {})


def test_canonical_stream_uses_runtime_pixel_preprocessing() -> None:
    source = np.arange(64 * 48 * 3, dtype=np.uint8).reshape((48, 64, 3))
    intrinsic_result = create_identity_camera_calibration(
        source_channel_id="chn_live",
        image_width=64,
        image_height=48,
        board_definition=RuntimeCameraCalibrationBoardDefinition(),
        generated_at="2026-08-07T10:00:00Z",
    )
    assert intrinsic_result.profile is not None
    profile_result = calculate_image_view_profile(
        source_channel_id="chn_live",
        request=RuntimeImageViewProfileCalculateRequest(
            view_profile_id="canonical-test-v1",
            source_image_width=64,
            source_image_height=48,
            intrinsic_calibration_hash=intrinsic_result.profile.calibration_hash,
            source_quadrilateral=[
                RuntimeNormalizedImagePoint(x=0.125, y=0.125),
                RuntimeNormalizedImagePoint(x=0.875, y=0.125),
                RuntimeNormalizedImagePoint(x=0.875, y=0.875),
                RuntimeNormalizedImagePoint(x=0.125, y=0.875),
            ],
            output_width=40,
            output_height=32,
            transform_type="PERSPECTIVE_RECTIFY",
        ),
        generated_at="2026-08-07T10:00:00Z",
    )
    assert profile_result.profile is not None

    actual = _canonical_image_view_frame(
        source,
        intrinsic_result.profile,
        profile_result.profile,
        undistort_interpolation="linear",
    )

    np.testing.assert_array_equal(
        actual,
        apply_image_view(source, profile_result.profile),
    )
    np.testing.assert_array_equal(
        _undistorted_image_view_frame(
            source,
            intrinsic_result.profile,
            undistort_interpolation="linear",
        ),
        source,
    )
    assert _image_view_ring_id("chn_live") == "chn_live.canonical-model"
    assert _undistorted_image_view_ring_id("chn_live") == "chn_live.undistorted-full"


def test_undistorted_and_canonical_transforms_run_as_independent_latest_frame_stages(
    tmp_path: Path,
) -> None:
    source = np.arange(64 * 48 * 3, dtype=np.uint8).reshape((48, 64, 3))
    intrinsic_result = create_identity_camera_calibration(
        source_channel_id="chn_live",
        image_width=64,
        image_height=48,
        board_definition=RuntimeCameraCalibrationBoardDefinition(),
        generated_at="2026-08-07T10:00:00Z",
    )
    assert intrinsic_result.profile is not None
    profile_result = calculate_image_view_profile(
        source_channel_id="chn_live",
        request=RuntimeImageViewProfileCalculateRequest(
            view_profile_id="canonical-pipeline-test-v1",
            source_image_width=64,
            source_image_height=48,
            intrinsic_calibration_hash=intrinsic_result.profile.calibration_hash,
            source_quadrilateral=[
                RuntimeNormalizedImagePoint(x=0.125, y=0.125),
                RuntimeNormalizedImagePoint(x=0.875, y=0.125),
                RuntimeNormalizedImagePoint(x=0.875, y=0.875),
                RuntimeNormalizedImagePoint(x=0.125, y=0.875),
            ],
            output_width=40,
            output_height=32,
            transform_type="PERSPECTIVE_RECTIFY",
        ),
    )
    assert profile_result.profile is not None
    binding = ImageViewStreamBinding(
        pipeline_version_id="piv_live",
        undistort_node_key="undistort",
        image_view_node_key="canonical",
        intrinsic_calibration=intrinsic_result.profile.model_dump(mode="json"),
        undistort_interpolation="linear",
        view_profile=profile_result.profile.model_dump(mode="json"),
    )
    settings = SimpleNamespace(
        runtime=SimpleNamespace(
            camera_media_runtime_dir=str(tmp_path),
            camera_media_frame_slots=4,
        )
    )
    raw_writer = LatestFrameRingWriter(
        runtime_dir=tmp_path,
        channel_id="chn_live",
        frame=source,
        configured_fps=30,
    )
    undistorted_stop = threading.Event()
    canonical_stop = threading.Event()
    undistorted_thread = threading.Thread(
        target=_undistorted_image_view_process_main,
        args=(settings, "chn_live", binding, undistorted_stop, Queue()),
        daemon=True,
    )
    canonical_thread = threading.Thread(
        target=_canonical_image_view_process_main,
        args=(settings, "chn_live", binding, canonical_stop, Queue()),
        daemon=True,
    )
    undistorted_reader = LatestFrameRingReader(
        runtime_dir=tmp_path,
        channel_id=_undistorted_image_view_ring_id("chn_live"),
    )
    canonical_reader = LatestFrameRingReader(
        runtime_dir=tmp_path,
        channel_id=_image_view_ring_id("chn_live"),
    )
    try:
        canonical_thread.start()
        undistorted_thread.start()
        raw_writer.write(source, sequence=17, capture_monotonic_ns=time.monotonic_ns())

        undistorted = undistorted_reader.read_latest(timeout_seconds=2.0)
        canonical = canonical_reader.read_latest(timeout_seconds=2.0)

        assert undistorted is not None
        assert canonical is not None
        assert undistorted.sequence == canonical.sequence == 17
        np.testing.assert_array_equal(undistorted.frame, source)
        np.testing.assert_array_equal(
            canonical.frame,
            apply_image_view(source, profile_result.profile),
        )
    finally:
        undistorted_stop.set()
        canonical_stop.set()
        undistorted_thread.join(timeout=2.0)
        canonical_thread.join(timeout=2.0)
        undistorted_reader.close()
        canonical_reader.close()
        raw_writer.close()


def test_camera_media_resolves_the_model_bound_canonical_pipeline_view() -> None:
    version = SimpleNamespace(
        id="pvr_stem",
        graph={
            "nodes": [
                {
                    "node_key": "source",
                    "node_type": "source.channel_frame",
                    "config": {},
                },
                {
                    "node_key": "undistort",
                    "node_type": "preprocessing.camera_undistort",
                    "config": {"calibration": {"calibration_hash": "a"}, "interpolation": "cubic"},
                },
                {
                    "node_key": "canonical",
                    "node_type": "preprocessing.process_coordinate_rectify",
                    "config": {"view_profile": {"view_profile_id": "stem-view-v1"}},
                },
                {
                    "node_key": "segment",
                    "node_type": "inference.segmentation",
                    "config": {},
                },
            ],
            "edges": [
                {"from_node_key": "source", "to_node_key": "undistort"},
                {"from_node_key": "undistort", "to_node_key": "canonical"},
                {"from_node_key": "canonical", "to_node_key": "segment"},
            ],
        },
    )

    class SessionStub:
        def get(self, _model: object, identifier: str) -> object | None:
            return version if identifier == "pvr_stem" else None

    binding = _image_view_stream_binding(
        SessionStub(),  # type: ignore[arg-type]
        SimpleNamespace(pipeline_version_id="pvr_stem"),  # type: ignore[arg-type]
    )

    assert binding is not None
    assert binding.undistort_node_key == "undistort"
    assert binding.image_view_node_key == "canonical"
    assert binding.undistort_interpolation == "cubic"
    assert [item["node_key"] for item in binding.canonical_preprocessing] == [
        "canonical"
    ]


def test_camera_media_resolves_crop_as_the_model_and_label_studio_view() -> None:
    version = SimpleNamespace(
        id="pvr_crop",
        graph={
            "nodes": [
                {"node_key": "source", "node_type": "source.channel_frame", "config": {}},
                {
                    "node_key": "crop",
                    "node_type": "preprocessing.crop",
                    "config": {"x": 457, "y": 5, "width": 1038, "height": 677},
                },
                {
                    "node_key": "classify",
                    "node_type": "inference.classification",
                    "config": {},
                },
            ],
            "edges": [
                {"from_node_key": "source", "to_node_key": "crop"},
                {"from_node_key": "crop", "to_node_key": "classify"},
            ],
        },
    )

    class SessionStub:
        def get(self, _model: object, identifier: str) -> object | None:
            return version if identifier == "pvr_crop" else None

    binding = _image_view_stream_binding(
        SessionStub(),  # type: ignore[arg-type]
        SimpleNamespace(pipeline_version_id="pvr_crop"),  # type: ignore[arg-type]
    )

    assert binding is not None
    assert binding.undistort_node_key is None
    assert binding.image_view_node_key == "crop"
    assert binding.canonical_preprocessing == (
        {
            "node_key": "crop",
            "node_type": "preprocessing.crop",
            "config": {"x": 457, "y": 5, "width": 1038, "height": 677},
        },
    )

    source = np.zeros((1200, 1900, 3), dtype=np.uint8)
    steps = _validated_canonical_steps(binding, channel_id="chn_crop")
    cropped = _apply_canonical_steps(source, steps)
    encoded = _pad_frame_for_nvenc(cropped)

    assert cropped.shape == (677, 1038, 3)
    assert encoded.shape == (678, 1038, 3)
    np.testing.assert_array_equal(encoded[:677], cropped)
    assert np.count_nonzero(encoded[677]) == 0


def test_nvenc_preflight_fails_when_any_required_element_is_missing() -> None:
    class Factory:
        @staticmethod
        def find(name: str) -> object | None:
            return object() if name != "nvcudah264enc" else None

    class Gst:
        ElementFactory = Factory

    with pytest.raises(NvencPreflightError, match="nvcudah264enc"):
        _require_elements(Gst(), ("appsrc", "cudaupload", "nvcudah264enc"))


def test_gstreamer_worker_is_system_python_safe_and_does_not_copy_into_buffer() -> None:
    source = Path(gst_worker.__file__).read_text(encoding="utf-8")

    assert "Gst.Buffer.new_wrapped" not in source  # Gst is held on the worker instance.
    assert "self.gst.Buffer.new_wrapped(frame.payload)" in source
    assert "nvcudah264enc preset=p1 tune=ultra-low-latency " in source
    assert '"zero-reorder-delay=true b-frames=0 rc-lookahead=0 "' in source
    assert 'identity name=encoded_meter silent=true' in source
    assert 'buffer.duration = self.frame_duration_ns' in source
    assert 'self.status["published_frames"] += 1' in source
    assert "buffer.fill(" not in source
    assert "import numpy" not in source
    assert "from cv_" not in source


def test_mediamtx_path_state_exposes_publication_and_browser_readers() -> None:
    path = {"ready": True, "readers": [{"id": "a"}, {"id": "b"}]}

    assert path_is_ready(path) is True
    assert path_reader_count(path) == 2
    assert path_is_ready(None) is False
    assert path_reader_count({"readerCount": 3}) == 3


def test_ring_write_does_not_wait_for_a_reader(tmp_path: Path) -> None:
    frame = np.zeros((120, 190, 3), dtype=np.uint8)
    writer = LatestFrameRingWriter(
        runtime_dir=tmp_path,
        channel_id="chn_unsubscribed",
        frame=frame,
    )
    try:
        started = time.perf_counter()
        for sequence in range(1, 101):
            writer.write(frame, sequence=sequence)
        elapsed = time.perf_counter() - started
    finally:
        writer.close()

    assert elapsed < 0.5
