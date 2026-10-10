from __future__ import annotations

import time
from dataclasses import replace

import numpy as np
import pytest
from cv_runtime.high_rate_phase import (
    HighRatePhaseConfig,
    HighRatePhaseFrame,
    HighRateStemPhaseBridge,
    _PhaseTracker,
)
from cv_runtime.payloads import StemSliceModelPayload
from tests.unit.backend.test_stem_high_rate_handoff import _frame, _seed, _translation_step


def test_stale_trace_restarts_without_reusing_old_angle_or_translation() -> None:
    bridge = HighRateStemPhaseBridge(
        HighRatePhaseConfig(
            minimum_blur_score=0,
            minimum_matched_features=4,
            maximum_seed_age_ms=500,
        )
    )
    bridge.publish_seed(_seed(1))
    bridge.process_frame(_frame(1))
    old_generation = bridge.generation
    timestamp = "2026-09-14T12:00:00.660Z"
    bridge.process_frame(replace(_frame(2), timestamp=timestamp))
    assert "PHASE_SEED_STALE" in bridge.snapshot().reason_codes
    bridge.publish_seed(replace(_seed(2), timestamp=timestamp))
    bridge.process_pending_seed()
    current = bridge.snapshot("track", 2)
    assert current.phase_locked
    assert current.cumulative_raw_phase == 0
    assert current.cumulative_translation_x_px == 0
    assert current.trace_generation == old_generation + 1
    assert bridge._tracker._state.seed_sequence == 2
    assert not bridge.wait_for_snapshot("track", 2, generation=old_generation).phase_locked
    bridge.publish_seed(replace(_seed(3), timestamp="2026-09-14T12:00:00.690Z"))
    bridge.process_pending_seed()
    assert bridge.snapshot("track", 3).phase_locked
    assert bridge.generation == current.trace_generation


def test_predecessor_replay_preserves_all_intermediate_translations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_PhaseTracker, "_advance", _translation_step)
    bridge = HighRateStemPhaseBridge(
        HighRatePhaseConfig(minimum_blur_score=0, minimum_matched_features=4)
    )
    bridge.publish_seed(_seed(1))
    for seq in (1, 2, 4, 5):
        bridge.process_frame(_frame(seq))
    bridge.publish_seed(_seed(3))
    bridge.process_pending_seed()
    for seq in (2, 3, 4, 5):
        assert bridge.snapshot("track", seq).cumulative_translation_x_px == (seq - 1) * 10
    assert bridge.generation == 0


def test_expired_seed_does_not_restart_trace() -> None:
    bridge = HighRateStemPhaseBridge(HighRatePhaseConfig())
    bridge.publish_seed(replace(_seed(1), deadline_perf=time.perf_counter() - 1))
    bridge.process_pending_seed()
    assert bridge.generation == 0
    assert bridge.metrics()["high_rate_phase_recovery_generations"] == 0
    assert bridge.metrics()["high_rate_phase_processing_failures"] == 0


def test_checkpoints_remain_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_PhaseTracker, "_advance", _translation_step)
    bridge = HighRateStemPhaseBridge(HighRatePhaseConfig(phase_replay_max_frames=4))
    bridge.publish_seed(_seed(1))
    for seq in range(1, 20):
        bridge.process_frame(_frame(seq))
    assert len(bridge._tracker._checkpoints) <= 4
    assert min(bridge._tracker._checkpoints) >= 16


@pytest.mark.parametrize("reset_during_optimization", [False, True])
def test_optimizer_never_releases_previous_generation(
    reset_during_optimization: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cv_runtime.nodes import stem_rotation_optimizer as optimizer
    from tests.unit.backend.test_runtime_stem_rotation_optimization import (
        _context,
        _model,
        _node,
        _priority,
    )

    context = _context()
    bridge = HighRateStemPhaseBridge(HighRatePhaseConfig())
    context.metadata["high_rate_stem_phase_bridge"] = bridge
    model = _model().model_copy(update={"trace_generation": 0})
    original = optimizer.optimize_stem_rotation
    if reset_during_optimization:

        def reset_and_compute(*args: object, **kwargs: object) -> object:
            bridge.reset("TEST_RECOVERY")
            return original(*args, **kwargs)

        monkeypatch.setattr(optimizer, "optimize_stem_rotation", reset_and_compute)
    else:
        bridge.reset("TEST_RECOVERY")
    result = (
        optimizer.StemRotationOptimizerHandler()
        .execute(
            context,
            _node("analysis.stem_rotation_optimizer"),
            {"slice_model": model, "priority": _priority("VORSCHNITT_KRITISCH")},
        )
        .outputs["result"]
    )
    assert not result.result["result_valid"]
    assert result.result["reason_code"] == "PHASE_GENERATION_MISMATCH"


def test_late_older_seed_cannot_replace_newer_seed() -> None:
    bridge = HighRateStemPhaseBridge(HighRatePhaseConfig())
    bridge.publish_seed(_seed(3))
    assert bridge.publish_seed(_seed(2)) == -1
    assert bridge._pending_seed.frame_sequence == 3


def test_skipped_seed_reconstruction_matches_immediate_optical_flow() -> None:
    config = HighRatePhaseConfig(minimum_blur_score=0, minimum_matched_features=4)
    immediate, delayed = HighRateStemPhaseBridge(config), HighRateStemPhaseBridge(config)
    gray = _seed(1).gray
    frames = {
        seq: replace(
            _frame(seq), image_bgr=np.repeat(np.roll(gray, seq - 1, axis=1)[:, :, None], 3, axis=2)
        )
        for seq in (1, 2, 3)
    }
    seeds = {seq: replace(_seed(seq), gray=frames[seq].image_bgr[:, :, 0].copy()) for seq in frames}
    for bridge in (immediate, delayed):
        bridge.publish_seed(seeds[1])
        bridge.process_frame(frames[1])
    immediate.process_frame(frames[2])
    immediate.publish_seed(seeds[2])
    immediate.process_pending_seed()
    for bridge in (immediate, delayed):
        bridge.process_frame(frames[3])
    delayed.publish_seed(seeds[2])
    delayed.process_pending_seed()
    assert delayed.generation == 0
    for seq in (2, 3):
        actual, expected = delayed.snapshot("track", seq), immediate.snapshot("track", seq)
        assert actual.phase_locked and expected.phase_locked
        assert actual.cumulative_raw_phase == pytest.approx(expected.cumulative_raw_phase)
        assert actual.cumulative_translation_x_px == pytest.approx(
            expected.cumulative_translation_x_px
        )
        assert actual.cumulative_translation_y_px == pytest.approx(
            expected.cumulative_translation_y_px
        )


def test_replay_stops_at_deadline_without_counting_a_technical_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1.0]
    monkeypatch.setattr(time, "perf_counter", lambda: clock[0])
    advanced = []

    def slow_step(tracker: _PhaseTracker, frame: HighRatePhaseFrame) -> None:
        advanced.append(frame.frame_sequence)
        _translation_step(tracker, frame)
        clock[0] += 0.2

    monkeypatch.setattr(_PhaseTracker, "_advance", slow_step)
    bridge = HighRateStemPhaseBridge(
        HighRatePhaseConfig(minimum_blur_score=0, minimum_matched_features=4)
    )
    for seq in (1, 2, 3):
        bridge.process_frame(_frame(seq))
    bridge.publish_seed(replace(_seed(1), deadline_perf=1.1))
    bridge.process_pending_seed()
    assert advanced == [2]
    assert not bridge.snapshot("track", 1).phase_locked
    assert bridge.metrics()["high_rate_phase_seed_deadline_expirations"] == 1
    assert bridge.metrics()["high_rate_phase_processing_failures"] == 0


def test_curvature_minimum_solver_rejects_an_old_generation() -> None:
    from cv_runtime.nodes.stem_curvature_phase_solver import StemCurvaturePhaseSolverHandler
    from tests.unit.backend.test_runtime_stem_curvature_phase_solver import (
        _context,
        _curvature,
        _execute,
        _node,
        _phase,
    )

    context, handler = _context(), StemCurvaturePhaseSolverHandler()
    for sequence in range(7):
        _execute(handler, context, sequence, sequence * 20)
    bridge = HighRateStemPhaseBridge(HighRatePhaseConfig())
    context.metadata["high_rate_stem_phase_bridge"] = bridge
    bridge.reset("TEST_RECOVERY")
    result = handler.execute(
        context,
        _node(),
        {
            "curvature": _curvature(8, 140),
            "surface_phase": _phase(8, 140).model_copy(
                update={"execution_mode": "HIGH_RATE_SEEDED"}
            ),
        },
    ).outputs["solution"]
    assert result.state == "INVALID"
    assert result.observation_count == 0
    assert result.orientation_result is None
    assert "PHASE_GENERATION_MISMATCH" in result.reason_codes


def test_new_generation_clears_frozen_model_and_late_old_phase_keeps_new_history() -> None:
    from cv_runtime.nodes.stem_slice_model import StemSliceModelHandler
    from tests.unit.backend.test_runtime_stem_slice_model import (
        _active_stem,
        _context,
        _curvature,
        _execute,
        _node,
        _phase,
    )

    context, node, handler = _context(), _node(), StemSliceModelHandler()
    for seq, angle in enumerate((0, 19, 51, 88, 117, 151, 180)):
        _execute(handler, context, node, seq, angle)
    frozen = _execute(handler, context, node, 7, 180, decision=True, freeze=True)
    assert frozen.valid and frozen.frozen
    bridge = HighRateStemPhaseBridge(HighRatePhaseConfig())
    context.metadata["high_rate_stem_phase_bridge"] = bridge
    bridge.reset("PHASE_CONTINUITY_LOST_RESET")

    def submit(sequence: int, generation: int) -> StemSliceModelPayload:
        return handler.execute(
            context,
            node,
            {
                "curvature": _curvature(sequence, 0),
                "surface_phase": _phase(sequence, 0, trace_generation=generation).model_copy(
                    update={"execution_mode": "HIGH_RATE_SEEDED"}
                ),
                "active_stem": _active_stem(),
            },
        ).outputs["slice_model"]

    rejected = submit(8, 0)
    assert not rejected.valid and not rejected.frozen
    assert rejected.observation_frame_count == 0
    accepted = submit(9, bridge.generation)
    assert accepted.observation_frame_count == 1
    assert not accepted.valid
    rejected = submit(8, 0)
    assert not rejected.valid
    assert context.state[node.node_key]["observation_frame_count"] == 1
    assert context.state[node.node_key]["identity"]["trace_generation"] == bridge.generation
