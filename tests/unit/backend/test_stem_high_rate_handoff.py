from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from cv_runtime.frame_transport import SharedFrameRingReader, SharedFrameRingWriter
from cv_runtime.high_rate_phase import (
    HighRatePhaseConfig,
    HighRatePhaseFrame,
    HighRateStemPhaseBridge,
    StemPhaseSeed,
    _PhaseTracker,
)
from cv_runtime.stem_rotation import StemAxisObservation


def _frame(sequence: int) -> HighRatePhaseFrame:
    image = np.random.default_rng(42).integers(0, 256, (120, 240, 3), dtype=np.uint8)
    return HighRatePhaseFrame(
        image, sequence, f"2026-09-14T12:00:00.{sequence * 30:03d}Z", time.perf_counter()
    )


def _seed(sequence: int, session: str = "session") -> StemPhaseSeed:
    frame = _frame(sequence)
    return StemPhaseSeed(
        "track",
        session,
        sequence,
        frame.timestamp,
        frame.image_bgr[:, :, 0].copy(),
        np.full((120, 240), 255, dtype=np.uint8),
        StemAxisObservation(
            center_x_px=120,
            center_y_px=60,
            direction_x=1,
            direction_y=0,
            length_px=230,
            median_width_px=100,
            image_width=240,
            image_height=120,
        ),
        1.0,
        "image-context",
        "intrinsic-v1",
        "process-v1",
    )


def _bridge() -> HighRateStemPhaseBridge:
    return HighRateStemPhaseBridge(
        HighRatePhaseConfig(
            minimum_blur_score=0, minimum_matched_features=4, phase_replay_max_frames=8
        )
    )


def _translation_step(self: _PhaseTracker, frame: HighRatePhaseFrame) -> None:
    state = self._state
    assert state is not None
    step = frame.frame_sequence - state.frame_sequence
    if step <= 0:
        return
    state.cumulative_translation_x_px += 10 * step
    state.cumulative_raw_phase += 0.1 * step
    state.frame_sequence = frame.frame_sequence
    state.timestamp = frame.timestamp
    self._snapshot = replace(
        self._snapshot,
        frame_sequence=frame.frame_sequence,
        timestamp=frame.timestamp,
        phase_locked=True,
        tracking_mode="INCREMENTAL",
        seed_status="PROPAGATED",
        translation_x_px=10 * step,
        cumulative_raw_phase=state.cumulative_raw_phase,
        cumulative_translation_x_px=state.cumulative_translation_x_px,
        cumulative_translation_magnitude_px=abs(state.cumulative_translation_x_px),
    )


def test_late_seed_restores_phase_and_translation_from_same_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_PhaseTracker, "_advance", _translation_step)
    bridge = _bridge()
    bridge.publish_seed(_seed(1))
    for sequence in (1, 2, 3):
        bridge.process_frame(_frame(sequence))
    bridge.publish_seed(_seed(2))
    result = bridge.process_frame(_frame(4))
    assert (
        result.cumulative_translation_x_px == 30
    )  # previously 40: newest translation replayed twice
    assert result.cumulative_raw_phase == pytest.approx(0.3)
    for sequence in (2, 3, 4):
        assert bridge.snapshot("track", sequence).cumulative_translation_x_px == (sequence - 1) * 10


def test_initial_replay_matches_immediate_processing() -> None:
    immediate, replay = _bridge(), _bridge()
    immediate.publish_seed(_seed(1))
    for sequence in (1, 2, 3):
        immediate.process_frame(_frame(sequence))
        replay.process_frame(_frame(sequence))
    replay.publish_seed(_seed(1))
    replay.process_pending_seed()
    for sequence in (1, 2, 3):
        expected, actual = immediate.snapshot("track", sequence), replay.snapshot("track", sequence)
        for field in (
            "cumulative_raw_phase",
            "cumulative_translation_x_px",
            "cumulative_translation_y_px",
            "phase_locked",
            "reason_codes",
            "matched_feature_count",
            "inlier_ratio",
            "trace_generation",
        ):
            assert getattr(actual, field) == getattr(expected, field), (sequence, field)


def test_blocked_tracking_does_not_block_handoff_or_publish_after_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _bridge()
    bridge.publish_seed(_seed(1))
    bridge.process_frame(_frame(1))
    entered, release, handed_off = threading.Event(), threading.Event(), threading.Event()

    def blocked(tracker: _PhaseTracker, frame: HighRatePhaseFrame) -> None:
        entered.set()
        assert release.wait(3)
        _translation_step(tracker, frame)

    monkeypatch.setattr(_PhaseTracker, "_advance", blocked)
    worker = threading.Thread(target=lambda: bridge.process_frame(_frame(2)))
    worker.start()
    assert entered.wait(1)

    def handoff() -> None:
        bridge.record_source_frame()
        bridge.publish_seed(_seed(2))
        bridge.reset("TEST_RECONNECT")
        handed_off.set()

    acquisition = threading.Thread(target=handoff)
    acquisition.start()
    try:
        assert handed_off.wait(0.5), "optical flow blocked acquisition"
    finally:
        release.set()
        worker.join(2)
        acquisition.join(2)
    assert bridge.snapshot().reason_codes == ("TEST_RECONNECT",)
    assert bridge.metrics()["high_rate_phase_obsolete_frames"] == 1


def test_missing_seed_frame_is_measured_from_real_predecessor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_PhaseTracker, "_advance", _translation_step)
    bridge = _bridge()
    bridge.publish_seed(_seed(1))
    for sequence in (1, 3):
        bridge.process_frame(_frame(sequence))
    assert not bridge.snapshot("track", 2).phase_locked
    bridge.publish_seed(_seed(2))
    result = bridge.process_frame(_frame(4))
    assert result.frame_sequence == 4
    assert result.phase_locked
    assert result.cumulative_translation_x_px == 30
    assert bridge.snapshot("track", 2).cumulative_translation_x_px == 10
    assert bridge.metrics()["high_rate_phase_restored_seed_frames"] == 1
    assert bridge.generation == 0


def test_duplicate_frames_and_session_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_PhaseTracker, "_advance", _translation_step)
    bridge = _bridge()
    old_generation = bridge.publish_seed(_seed(1))
    bridge.process_frame(_frame(1))
    bridge.process_frame(_frame(2))
    bridge.process_frame(_frame(2))
    assert bridge.snapshot().cumulative_translation_x_px == 10
    assert bridge.metrics()["high_rate_phase_duplicate_frames"] == 1
    generation = bridge.publish_seed(_seed(1, "next-session"))
    assert generation == old_generation + 1
    assert not bridge.snapshot("track", 1, generation=old_generation).phase_locked
    bridge.process_frame(_frame(2), generation=old_generation)
    bridge.process_pending_seed()
    assert bridge.snapshot("track", 1).cumulative_translation_x_px == 0
    assert bridge.snapshot("track", 1).trace_generation == generation


def test_matching_wait_is_bounded_by_deadline_and_reset() -> None:
    bridge = _bridge()
    generation = bridge.publish_seed(_seed(1))
    started = time.perf_counter()
    result = bridge.wait_for_snapshot(
        "track", 1, generation=generation, deadline_perf=started + 0.015
    )
    assert time.perf_counter() - started < 0.3
    assert not result.phase_locked
    assert "PHASE_PAIR_DEADLINE_EXPIRED" in result.reason_codes
    bridge.reset()
    result = bridge.wait_for_snapshot("track", 1, generation=generation)
    assert "PHASE_FAST_PATH_GENERATION_MISMATCH" in result.reason_codes


def test_old_source_seed_cannot_rebind_after_reconnect() -> None:
    bridge = _bridge()
    bridge.observe_source_generation("camera-before")
    bridge.publish_seed(replace(_seed(1), source_generation="camera-before"))
    bridge.process_pending_seed()
    bridge.observe_source_generation("camera-after")
    rejected_generation = bridge.publish_seed(replace(_seed(1), source_generation="camera-before"))
    assert rejected_generation == -1
    assert not bridge.snapshot("track", 1, source_generation="camera-before").phase_locked
    bridge.publish_seed(replace(_seed(1), source_generation="camera-after"))
    bridge.process_pending_seed()
    assert bridge.snapshot("track", 1).phase_locked
    assert bridge.metrics()["high_rate_phase_obsolete_seeds"] == 1


def test_invalidated_history_does_not_fall_back_to_earlier_valid_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_PhaseTracker, "_advance", _translation_step)
    bridge = _bridge()
    bridge.publish_seed(_seed(1))
    bridge.process_frame(_frame(1))
    bridge.process_frame(_frame(2))
    bridge._tracker._snapshot_history.append(
        replace(bridge.snapshot("track", 2), phase_locked=False)
    )
    bridge.publish_seed(_seed(2))
    result = bridge.process_frame(_frame(3))
    assert result.trace_generation == 1
    assert not bridge.snapshot("track", 2, generation=0).phase_locked
    assert bridge.snapshot("track", 2).cumulative_translation_x_px == 0
    assert bridge.metrics()["high_rate_phase_recovery_generations"] == 1


def test_seed_for_rate_skipped_frame_uses_that_real_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_PhaseTracker, "_advance", _translation_step)
    bridge = _bridge()
    bridge.publish_seed(_seed(1))
    bridge.process_frame(_frame(1))
    bridge.publish_seed(_seed(2))
    bridge.process_pending_seed()
    assert bridge.snapshot("track", 2).cumulative_translation_x_px == 10
    assert bridge.snapshot("track", 2).phase_locked


def test_expired_seed_and_seed_failure_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = _bridge()
    bridge.publish_seed(replace(_seed(1), deadline_perf=time.perf_counter() - 1))
    bridge.process_pending_seed()
    assert "PHASE_SEED_HISTORY_EXPIRED" in bridge.snapshot().reason_codes
    assert bridge.metrics()["high_rate_phase_seed_deadline_expirations"] == 1
    assert bridge.metrics()["high_rate_phase_processing_failures"] == 0

    def broken_seed(*_args: object) -> None:
        raise ValueError("invalid mask")

    with monkeypatch.context() as patch:
        patch.setattr(_PhaseTracker, "_apply_seed", broken_seed)
        bridge.publish_seed(_seed(2))
        bridge.process_pending_seed()
    assert "PHASE_SEED_PROCESSING_FAILED" in bridge.snapshot().reason_codes
    bridge.publish_seed(_seed(3))
    bridge.process_pending_seed()
    assert bridge.snapshot("track", 3).phase_locked


def test_acquisition_rate_uses_capture_spacing_when_ring_is_drained() -> None:
    from realtime_executor.live import _HighRateStemPhaseRuntime, _OrderedHighRateFrameBuffer
    from realtime_executor.sources import FramePacket

    runtime = object.__new__(_HighRateStemPhaseRuntime)
    runtime.config = HighRatePhaseConfig(target_phase_fps=25, phase_frame_queue_size=8)
    runtime.bridge = HighRateStemPhaseBridge(runtime.config)
    runtime._buffer = _OrderedHighRateFrameBuffer(8)
    runtime._rate_lock = threading.Lock()
    runtime._rate_tokens = 1.0
    runtime._last_source_perf = 0.0
    runtime._last_source_sequence = 0
    runtime._last_source_reconnect_count = 0
    runtime._ring_metrics = {}
    for sequence in range(1, 5):
        runtime.submit(
            FramePacket(
                image_bytes=b"image",
                mime_type="image/jpeg",
                width=1,
                height=1,
                source_timestamp=_frame(sequence).timestamp,
                source_uri="test://ring",
                sequence=sequence,
                metadata={"capture_monotonic_ns": sequence * 40_000_000},
            )
        )
    assert [runtime._buffer.pop(0).packet.sequence for _ in range(4)] == [1, 2, 3, 4]
    assert runtime.metrics()["high_rate_phase_rate_skips"] == 0


@pytest.mark.parametrize(
    "mode, phase_sequence, generation, accepted",
    [
        ("HIGH_RATE_SEEDED", 1, 0, False),
        ("HIGH_RATE_SEEDED", 2, 1, False),
        ("HIGH_RATE_SEEDED", 2, 0, True),
        ("SYNCHRONOUS", 1, 0, True),
    ],
)
def test_profile_pairs_require_exact_high_rate_frame_and_generation(
    mode: str,
    phase_sequence: int,
    generation: int,
    accepted: bool,
) -> None:
    from cv_runtime.nodes.stem_slice_model import StemSliceModelHandler
    from tests.unit.backend.test_runtime_stem_slice_model import (
        _active_stem,
        _context,
        _curvature,
        _node,
        _phase,
    )

    context = _context()
    bridge = _bridge()
    context.metadata["high_rate_stem_phase_bridge"] = bridge
    phase = _phase(phase_sequence, 0, trace_generation=generation).model_copy(
        update={"execution_mode": mode}
    )
    result = (
        StemSliceModelHandler()
        .execute(
            context,
            _node(),
            {
                "curvature": _curvature(2, 0),
                "surface_phase": phase,
                "active_stem": _active_stem(),
            },
        )
        .outputs["slice_model"]
    )
    assert (result.observation_frame_count > 0) is accepted
    assert bridge.metrics()["high_rate_phase_accepted_profile_pairs"] == int(
        accepted and mode == "HIGH_RATE_SEEDED"
    )


def test_ordered_ring_keeps_intermediate_frames_and_counts_overflow(tmp_path: Path) -> None:
    frame = np.zeros((8, 12, 3), dtype=np.uint8)
    writer = SharedFrameRingWriter(
        runtime_dir=tmp_path, channel_id="stem", frame=frame, slot_count=3
    )
    reader = SharedFrameRingReader(runtime_dir=tmp_path, channel_id="stem")
    latest = SharedFrameRingReader(runtime_dir=tmp_path, channel_id="stem")
    try:
        for sequence in range(1, 6):
            writer.write(np.full_like(frame, sequence))
        assert [reader.read_next(timeout_seconds=0).sequence for _ in range(3)] == [3, 4, 5]
        assert reader.overwritten_frames == 2
        assert reader.skipped_frames == 0
        assert reader.read_next(timeout_seconds=0) is None
        assert latest.read_latest(timeout_seconds=0).sequence == 5
        assert latest.overwritten_frames == 2
        assert latest.skipped_frames == 2
        writer.write(frame, sequence=8)
        assert reader.read_next(timeout_seconds=0).sequence == 8
    finally:
        reader.close()
        latest.close()
        writer.close()


def test_ring_reconnect_resets_sequence_and_exposes_generation(tmp_path: Path) -> None:
    from realtime_executor.sources import SharedMemoryFrameSource

    frame = np.zeros((8, 12, 3), dtype=np.uint8)
    source = SharedMemoryFrameSource(
        runtime_dir=tmp_path, channel_id="stem", source_type="rtsp", ordered=True
    )
    writer = SharedFrameRingWriter(runtime_dir=tmp_path, channel_id="stem", frame=frame)
    try:
        writer.write(frame, sequence=10)
        first = source.read(threading.Event())
        writer.close()
        writer = SharedFrameRingWriter(runtime_dir=tmp_path, channel_id="stem", frame=frame)
        writer.write(frame)
        second = source.read(threading.Event())
        assert second.sequence == 1
        assert first.metadata["frame_ring_generation"] != second.metadata["frame_ring_generation"]
        assert source.health.reconnect_count == 1
    finally:
        source.close()
        writer.close()
