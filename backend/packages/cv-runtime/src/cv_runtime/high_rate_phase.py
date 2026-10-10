from __future__ import annotations

# OpenCV is an optional runtime dependency with dynamic Python bindings.
# ruff: noqa: ANN401
import math
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

import numpy as np

from cv_runtime.stem_rotation import StemAxisObservation, SurfaceFeatureMatch
from cv_runtime.stem_surface_phase import (
    CylindricalSurfacePhaseConfig,
    PhaseRelockCandidate,
    RawSurfacePhaseEstimate,
    estimate_cylindrical_surface_phase_from_matches,
    select_phase_relock_candidate,
)


@dataclass(frozen=True)
class HighRatePhaseConfig:
    target_phase_fps: float = 25.0
    tracking_max_width_px: int = 960
    phase_frame_queue_size: int = 3
    phase_replay_window_ms: int = 500
    phase_replay_max_frames: int = 16
    maximum_seed_age_ms: int = 300
    maximum_features: int = 240
    feature_quality_level: float = 0.015
    feature_minimum_distance_px: float = 6.0
    feature_block_size: int = 7
    lk_window_size: int = 21
    lk_max_level: int = 3
    maximum_forward_backward_error_px: float = 1.5
    minimum_blur_score: float = 18.0
    mask_erosion_fraction: float = 0.15
    minimum_matched_features: int = 12
    minimum_inlier_ratio: float = 0.70
    maximum_tracking_error_px: float = 12.0
    minimum_feature_span_ratio: float = 0.35
    maximum_abs_delta_raw_phase: float = 0.70
    minimum_confidence: float = 0.45
    maximum_keyframes: int = 24
    keyframe_minimum_features: int = 15
    keyframe_minimum_raw_separation: float = 0.15
    relock_minimum_score: float = 0.60
    relock_uniqueness_margin: float = 0.15
    relock_maximum_raw_jump: float = 0.70

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> HighRatePhaseConfig:
        return cls(
            target_phase_fps=_number(value, "target_phase_fps", 25.0, 1.0, 120.0),
            tracking_max_width_px=_integer(value, "tracking_max_width_px", 960, 160, 4096),
            phase_frame_queue_size=_integer(value, "phase_frame_queue_size", 3, 1, 16),
            phase_replay_window_ms=_integer(value, "phase_replay_window_ms", 500, 50, 3000),
            phase_replay_max_frames=_integer(value, "phase_replay_max_frames", 16, 2, 120),
            maximum_seed_age_ms=_integer(value, "maximum_seed_age_ms", 300, 50, 3000),
            maximum_features=_integer(value, "maximum_features", 240, 16, 1000),
            feature_quality_level=_number(value, "feature_quality_level", 0.015, 0.001, 0.5),
            feature_minimum_distance_px=_number(
                value, "feature_minimum_distance_px", 6.0, 1.0, 100.0
            ),
            feature_block_size=_odd_integer(value, "feature_block_size", 7, 3, 31),
            lk_window_size=_odd_integer(value, "lk_window_size", 21, 5, 61),
            lk_max_level=_integer(value, "lk_max_level", 3, 0, 8),
            maximum_forward_backward_error_px=_number(
                value, "maximum_forward_backward_error_px", 1.5, 0.1, 20.0
            ),
            minimum_blur_score=_number(value, "minimum_blur_score", 18.0, 0.0, 100000.0),
            mask_erosion_fraction=_number(value, "mask_erosion_fraction", 0.15, 0.02, 0.45),
            minimum_matched_features=_integer(value, "minimum_matched_features", 12, 4, 500),
            minimum_inlier_ratio=_number(value, "minimum_inlier_ratio", 0.70, 0.0, 1.0),
            maximum_tracking_error_px=_number(value, "maximum_tracking_error_px", 12.0, 0.1, 500.0),
            minimum_feature_span_ratio=_number(
                value, "minimum_feature_span_ratio", 0.35, 0.05, 1.0
            ),
            maximum_abs_delta_raw_phase=_number(
                value, "maximum_abs_delta_raw_phase", 0.70, 0.01, math.pi
            ),
            minimum_confidence=_number(value, "minimum_rotation_confidence", 0.45, 0.0, 1.0),
            maximum_keyframes=_integer(value, "maximum_keyframes", 24, 2, 128),
            keyframe_minimum_features=_integer(value, "keyframe_minimum_features", 15, 4, 500),
            keyframe_minimum_raw_separation=_number(
                value, "keyframe_minimum_raw_separation", 0.15, 0.01, math.pi
            ),
            relock_minimum_score=_number(value, "relock_minimum_score", 0.60, 0.0, 1.0),
            relock_uniqueness_margin=_number(value, "relock_uniqueness_margin", 0.15, 0.0, 1.0),
            relock_maximum_raw_jump=_number(value, "relock_maximum_raw_jump", 0.70, 0.01, math.pi),
        )


@dataclass(frozen=True)
class HighRatePhaseFrame:
    image_bgr: np.ndarray
    frame_sequence: int
    timestamp: str
    received_perf: float


@dataclass(frozen=True)
class StemPhaseSeed:
    track_id: str
    session_id: str | None
    frame_sequence: int
    timestamp: str
    gray: np.ndarray
    stem_mask: np.ndarray
    axis: StemAxisObservation
    confidence: float
    image_context_id: str | None
    intrinsic_calibration_hash: str | None
    process_calibration_hash: str | None
    source_generation: str | None = None
    deadline_perf: float | None = None


@dataclass(frozen=True)
class HighRatePhaseSnapshot:
    track_id: str | None = None
    session_id: str | None = None
    frame_sequence: int | None = None
    timestamp: str | None = None
    delta_raw_phase: float | None = None
    cumulative_raw_phase: float | None = None
    last_safe_raw_phase: float | None = None
    phase_locked: bool = False
    tracking_mode: Literal[
        "INITIALIZING", "INCREMENTAL", "HOLDING", "KEYFRAME_RELOCK", "LOST", "RESET"
    ] = "LOST"
    relock_status: Literal["NOT_REQUIRED", "NOT_ATTEMPTED", "RELOCKED", "AMBIGUOUS", "FAILED"] = (
        "NOT_ATTEMPTED"
    )
    relock_keyframe_id: str | None = None
    trace_generation: int = 0
    failure_count: int = 0
    detected_feature_count: int = 0
    matched_feature_count: int = 0
    inlier_feature_count: int = 0
    inlier_ratio: float = 0.0
    median_tracking_error_px: float | None = None
    forward_backward_error_px: float | None = None
    feature_span_ratio: float = 0.0
    blur_score: float | None = None
    confidence: float = 0.0
    translation_x_px: float = 0.0
    translation_y_px: float = 0.0
    translation_magnitude_px: float = 0.0
    cumulative_translation_x_px: float = 0.0
    cumulative_translation_y_px: float = 0.0
    cumulative_translation_magnitude_px: float = 0.0
    axis_rotation_deg: float = 0.0
    source_phase_fps: float | None = None
    processed_phase_fps: float | None = None
    source_frame_gap: int | None = None
    seed_age_ms: float | None = None
    fast_path_latency_ms: float | None = None
    fast_path_dropped_frames: int = 0
    seed_status: Literal[
        "NOT_USED", "WAITING", "APPLIED", "PROPAGATED", "STALE", "TRACK_CHANGED"
    ] = "WAITING"
    reason_codes: tuple[str, ...] = ()


@dataclass
class _Keyframe:
    keyframe_id: str
    raw_phase: float
    points: np.ndarray
    descriptors: np.ndarray


@dataclass
class _TrackingState:
    track_id: str
    session_id: str | None
    gray: np.ndarray
    mask: np.ndarray
    axis: StemAxisObservation
    points: np.ndarray
    frame_sequence: int
    timestamp: str
    seed_timestamp: str
    seed_sequence: int
    cumulative_raw_phase: float
    trace_generation: int
    cumulative_translation_x_px: float = 0.0
    cumulative_translation_y_px: float = 0.0
    failure_count: int = 0
    keyframes: list[_Keyframe] | None = None


class HighRateStemPhaseBridge:
    """Bounded mailbox; only the phase worker accesses the mutable tracker.

    Image processing never holds the mailbox condition. A reset invalidates
    in-flight work immediately; the worker discards it at publication.
    """

    def __init__(self, config: HighRatePhaseConfig) -> None:
        self.config = config
        self._condition = threading.Condition()
        self._worker_lock = threading.Lock()
        self._tracker = _PhaseTracker(config)
        self._generation = 0
        self._worker_generation = 0
        self._binding: tuple[object, ...] | None = None
        self._source_generation: str | None = None
        self._pending_seed: StemPhaseSeed | None = None
        self._snapshot = HighRatePhaseSnapshot()
        self._last_seed_sequence = -1
        self._snapshots: dict[int, HighRatePhaseSnapshot] = {}
        self._source_events: deque[float] = deque(maxlen=240)
        self._processed_events: deque[float] = deque(maxlen=240)
        self._counts: dict[str, int] = {}
        self._timings: dict[str, float] = {}
        self._paired: deque[tuple[int, str, int]] = deque(maxlen=240)
        self._processed_sequences: deque[tuple[int, int]] = deque(maxlen=240)
        self._profile_pairs: deque[tuple[int, str, int]] = deque(maxlen=240)
        self._wake_worker: Any = None

    @property
    def generation(self) -> int:
        with self._condition:
            return self._generation

    def set_worker_wakeup(self, callback: Any) -> None:
        self._wake_worker = callback

    def publish_seed(self, seed: StemPhaseSeed) -> int:
        binding = (
            seed.track_id,
            seed.session_id,
            seed.intrinsic_calibration_hash,
            seed.process_calibration_hash,
            seed.axis.image_width,
            seed.axis.image_height,
        )
        with self._condition:
            if (
                seed.source_generation is not None
                and seed.source_generation != self._source_generation
            ):
                self._count("obsolete_seeds")
                return -1
            if self._binding is not None and binding != self._binding:
                self.reset("PHASE_TRACK_CHANGED_RESET")
            if seed.frame_sequence < self._last_seed_sequence:
                self._count("obsolete_seeds")
                return -1
            self._binding = binding
            self._last_seed_sequence = seed.frame_sequence
            self._pending_seed = seed
            if self._snapshot.track_id is None:
                self._snapshot = HighRatePhaseSnapshot(
                    track_id=seed.track_id,
                    session_id=seed.session_id,
                    frame_sequence=seed.frame_sequence,
                    timestamp=seed.timestamp,
                    trace_generation=self._generation,
                    seed_status="WAITING",
                    reason_codes=("PHASE_SEED_PENDING",),
                )
            generation = self._generation
            self._condition.notify_all()
        if self._wake_worker is not None:
            self._wake_worker()
        return generation

    def observe_source_generation(self, generation: str) -> None:
        with self._condition:
            if self._source_generation is not None and self._source_generation != generation:
                self.reset("PHASE_CAMERA_RECONNECT_RESET")
            self._source_generation = generation

    def _count(self, name: str, count: int = 1) -> None:
        with self._condition:
            self._counts[name] = self._counts.get(name, 0) + max(int(count), 0)

    def record_source_frame(self) -> None:
        with self._condition:
            self._source_events.append(time.perf_counter())
            self._count("acquired_frames")

    def record_stage_timing(self, name: str, elapsed_ms: float) -> None:
        with self._condition:
            self._timings[name] = max(float(elapsed_ms), 0.0)

    def record_queue_drop(self, count: int = 1) -> None:
        self._count("queue_drops", count)

    def record_rate_skip(self, count: int = 1) -> None:
        self._count("rate_skips", count)

    def record_stale_frame(self) -> None:
        self._count("stale_frames")

    def record_duplicate_frame(self) -> None:
        self._count("duplicate_frames")

    def record_obsolete_frame(self) -> None:
        self._count("obsolete_frames")

    def record_measurement_pair(self, snapshot: HighRatePhaseSnapshot) -> None:
        with self._condition:
            if (
                snapshot.phase_locked
                and snapshot.frame_sequence is not None
                and snapshot.track_id is not None
                and snapshot.trace_generation == self._generation
            ):
                key = (self._generation, snapshot.track_id, snapshot.frame_sequence)
                if key not in self._paired:
                    self._paired.append(key)
                    self._count("valid_measurement_pairs")

    def record_profile_pair(self, track_id: str, sequence: int, generation: int) -> None:
        with self._condition:
            key = (generation, track_id, sequence)
            if generation == self._generation and key not in self._profile_pairs:
                self._profile_pairs.append(key)
                self._count("accepted_profile_pairs")

    def record_processing_failure(
        self,
        *,
        frame_sequence: int,
        timestamp: str,
        reason: str = "PHASE_FAST_PATH_PREPROCESSING_FAILED",
        generation: int | None = None,
    ) -> None:
        with self._condition:
            if generation is not None and generation != self._generation:
                return
            self._snapshot = replace(
                self._snapshot,
                frame_sequence=frame_sequence,
                timestamp=timestamp,
                phase_locked=False,
                confidence=0.0,
                tracking_mode="LOST",
                reason_codes=(reason, "PHASE_TRACK_LOST"),
            )
            self._snapshots[frame_sequence] = self._snapshot
            for sequence in sorted(self._snapshots)[: -self.config.phase_replay_max_frames]:
                del self._snapshots[sequence]
            self._count(
                "seed_deadline_expirations"
                if reason == "PHASE_SEED_HISTORY_EXPIRED" else "processing_failures"
            )
            self._condition.notify_all()

    def process_pending_seed(self) -> None:
        with self._condition:
            pending = self._pending_seed
            generation = self._generation
        if pending is not None:
            try:
                self._process(None, generation)
            except Exception:
                self.record_processing_failure(
                    frame_sequence=pending.frame_sequence,
                    timestamp=pending.timestamp,
                    generation=generation,
                    reason="PHASE_SEED_PROCESSING_FAILED",
                )

    def process_frame(
        self, frame: HighRatePhaseFrame, *, generation: int | None = None
    ) -> HighRatePhaseSnapshot:
        return self._process(frame, generation)

    def _process(
        self, frame: HighRatePhaseFrame | None, expected_generation: int | None
    ) -> HighRatePhaseSnapshot:
        with self._worker_lock:
            with self._condition:
                generation = self._generation
                if expected_generation is not None and expected_generation != generation:
                    self._count("obsolete_frames")
                    return self._snapshot
                pending = self._pending_seed
                self._pending_seed = None
            tracker = self._tracker
            generation_changed = self._worker_generation != generation
            if generation_changed:
                tracker.reset()
                tracker._snapshot = replace(tracker._snapshot, trace_generation=generation)
                self._worker_generation = generation
            if frame is not None and tracker._history:
                if frame.frame_sequence <= tracker._history[-1].frame_sequence:
                    self._count("duplicate_frames")
                    frame = None
            if pending is not None:
                reference = frame or (tracker._history[-1] if tracker._history else None)
                age = (
                    _timestamp_delta_ms(pending.timestamp, reference.timestamp)
                    if reference is not None
                    else 0.0
                )
                if (age is not None and age > self.config.phase_replay_window_ms) or (
                    pending.deadline_perf is not None
                    and time.perf_counter() >= pending.deadline_perf
                ):
                    self.record_processing_failure(
                        frame_sequence=pending.frame_sequence,
                        timestamp=pending.timestamp,
                        reason="PHASE_SEED_HISTORY_EXPIRED",
                        generation=generation,
                    )
                    pending = None
                else:
                    tracker.publish_seed(pending)
            if frame is None:
                if pending is None:
                    return self.snapshot()
                frame = (
                    tracker._history[-1]
                    if tracker._history
                    else HighRatePhaseFrame(
                        pending.gray, pending.frame_sequence, pending.timestamp, time.perf_counter()
                    )
                )
            replay_before = tracker.replayed_frames
            restored_before = tracker.restored_seed_frames
            tracking_started = time.perf_counter()
            try:
                result = tracker.process_frame(frame)
                if pending is not None and "PHASE_SEED_HISTORY_MISSING" in result.reason_codes:
                    # Continuity could not be reconstructed from real historical
                    # images. Start an explicitly new angle origin; never attach
                    # a zero phase to the old generation's measurements.
                    if (pending.deadline_perf is not None
                            and time.perf_counter() >= pending.deadline_perf):
                        self.record_processing_failure(
                            frame_sequence=pending.frame_sequence,
                            timestamp=pending.timestamp, generation=generation,
                            reason="PHASE_SEED_HISTORY_EXPIRED",
                        )
                        return self.snapshot()
                    with self._condition:
                        if generation != self._generation:
                            return self._snapshot
                        binding = self._binding
                        if self._pending_seed is not None:
                            self._count("obsolete_seeds")
                        self.reset("PHASE_CONTINUITY_LOST_RESET")
                        self._binding = binding
                        self._last_seed_sequence = pending.frame_sequence
                        generation = self._generation
                        self._worker_generation = generation
                        self._count("recovery_generations")
                    history = list(tracker._history)
                    tracker.reset()
                    tracker._history.extend(history)
                    tracker._snapshot = replace(tracker._snapshot, trace_generation=generation)
                    tracker.publish_seed(pending)
                    replay_before = restored_before = 0
                    result = tracker.process_frame(frame)
            except Exception:
                # An exception can interrupt an in-place tracking update. Do
                # not continue that trace with partially updated motion state.
                reason = (
                    "PHASE_SEED_PROCESSING_FAILED"
                    if pending is not None
                    else "PHASE_FAST_PATH_TRACKING_FAILED"
                )
                with self._condition:
                    if generation == self._generation:
                        self.record_processing_failure(
                            frame_sequence=frame.frame_sequence,
                            timestamp=frame.timestamp,
                            generation=generation,
                            reason=reason,
                        )
                        self.reset(reason)
                raise
            if pending is not None and "PHASE_SEED_HISTORY_EXPIRED" in result.reason_codes:
                self.record_processing_failure(
                    frame_sequence=pending.frame_sequence, timestamp=pending.timestamp,
                    generation=generation, reason="PHASE_SEED_HISTORY_EXPIRED",
                )
                return self.snapshot()
            self.record_stage_timing(
                "worker_tracking_ms", (time.perf_counter() - tracking_started) * 1000,
            )
            if generation_changed and pending is not None and result.seed_status == "APPLIED":
                result = replace(result, seed_status="TRACK_CHANGED")
            with self._condition:
                if generation != self._generation:
                    self._count("obsolete_frames")
                    return self._snapshot
                key = (generation, frame.frame_sequence)
                if key not in self._processed_sequences:
                    self._processed_sequences.append(key)
                    self._processed_events.append(time.perf_counter())
                    self._count("processed_frames")
                self._count("replayed_frames", tracker.replayed_frames - replay_before)
                self._count("restored_seed_frames", tracker.restored_seed_frames - restored_before)
                self._snapshot = replace(
                    result,
                    trace_generation=generation,
                    source_phase_fps=_event_rate(self._source_events),
                    processed_phase_fps=_event_rate(self._processed_events),
                    fast_path_dropped_frames=self._counts.get("queue_drops", 0),
                )
                for item in tracker._snapshot_history:
                    if item.frame_sequence is not None:
                        self._snapshots[item.frame_sequence] = replace(
                            item,
                            source_phase_fps=self._snapshot.source_phase_fps,
                            processed_phase_fps=self._snapshot.processed_phase_fps,
                            fast_path_dropped_frames=self._snapshot.fast_path_dropped_frames,
                        )
                if self._snapshot.frame_sequence is not None:
                    self._snapshots[self._snapshot.frame_sequence] = self._snapshot
                # Bound history by capture time and capacity. Waiters also have
                # a monotonic deadline, which continues to expire on idle sources.
                newest_time = _timestamp_seconds(frame.timestamp)
                self._snapshots = {
                    sequence: item
                    for sequence, item in self._snapshots.items()
                    if newest_time is None
                    or _timestamp_seconds(item.timestamp) is None
                    or newest_time - float(_timestamp_seconds(item.timestamp))
                    <= self.config.phase_replay_window_ms / 1000.0
                }
                for sequence in sorted(self._snapshots)[: -self.config.phase_replay_max_frames]:
                    del self._snapshots[sequence]
                self._condition.notify_all()
                return self._snapshot

    def snapshot(
        self,
        track_id: str | None = None,
        frame_sequence: int | None = None,
        *,
        generation: int | None = None,
        source_generation: str | None = None,
    ) -> HighRatePhaseSnapshot:
        with self._condition:
            value = (
                self._snapshot if frame_sequence is None else self._snapshots.get(frame_sequence)
            )
            if (generation is not None and generation != self._generation) or (
                source_generation is not None and source_generation != self._source_generation
            ):
                reason = "PHASE_FAST_PATH_GENERATION_MISMATCH"
            elif track_id is not None and self._snapshot.track_id not in {None, track_id}:
                reason = "PHASE_FAST_PATH_TRACK_MISMATCH"
            elif value is not None and (track_id is None or value.track_id == track_id):
                return value
            else:
                reason = "PHASE_EXACT_SNAPSHOT_MISSING"
            return HighRatePhaseSnapshot(
                track_id=track_id,
                frame_sequence=frame_sequence,
                trace_generation=self._generation,
                reason_codes=(reason, "PHASE_TRACK_LOST"),
            )

    def wait_for_snapshot(
        self,
        track_id: str,
        frame_sequence: int,
        *,
        generation: int,
        deadline_perf: float | None = None,
    ) -> HighRatePhaseSnapshot:
        deadline = time.perf_counter() + self.config.phase_replay_window_ms / 1000.0
        if deadline_perf is not None:
            deadline = min(deadline, deadline_perf)
        with self._condition:
            while self._generation == generation:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    self._count("pair_timeouts")
                    return HighRatePhaseSnapshot(
                        track_id=track_id,
                        frame_sequence=frame_sequence,
                        trace_generation=generation,
                        reason_codes=("PHASE_PAIR_DEADLINE_EXPIRED", "PHASE_TRACK_LOST"),
                    )
                value = self._snapshots.get(frame_sequence)
                if (
                    value is not None
                    and value.track_id == track_id
                    and value.trace_generation == generation
                ):
                    return value
                self._condition.wait(remaining)
            return self.snapshot(track_id, frame_sequence, generation=generation)

    def reset(self, reason: str = "PHASE_RUNTIME_RESET") -> None:
        with self._condition:
            self._generation += 1
            self._binding = None
            self._pending_seed = None
            self._last_seed_sequence = -1
            self._snapshots.clear()
            self._snapshot = HighRatePhaseSnapshot(
                trace_generation=self._generation,
                tracking_mode="RESET",
                reason_codes=(reason,),
            )
            self._condition.notify_all()

    def metrics(self) -> dict[str, float]:
        with self._condition:
            values = {
                f"high_rate_phase_{key}": float(self._counts.get(key, 0))
                for key in (
                    "acquired_frames",
                    "processed_frames",
                    "queue_drops",
                    "rate_skips",
                    "stale_frames",
                    "obsolete_frames",
                    "duplicate_frames",
                    "processing_failures",
                    "seed_deadline_expirations",
                    "recovery_generations",
                    "restored_seed_frames",
                    "valid_measurement_pairs",
                    "pair_timeouts",
                    "replayed_frames",
                    "obsolete_seeds",
                    "accepted_profile_pairs",
                )
            }
            for key, value in (
                ("fps", self._snapshot.processed_phase_fps),
                ("latency_ms", self._snapshot.fast_path_latency_ms),
                ("seed_age_ms", self._snapshot.seed_age_ms),
            ):
                if value is not None:
                    values[f"high_rate_phase_{key}"] = value
            values.update({f"high_rate_phase_{key}": value for key, value in self._timings.items()})
            return values


class _PhaseTracker:
    """Worker-owned optical-flow state, never accessed by acquisition or nodes."""

    def __init__(self, config: HighRatePhaseConfig) -> None:
        self.config = config
        self._pending_seed: StemPhaseSeed | None = None
        self._history: deque[HighRatePhaseFrame] = deque(maxlen=config.phase_replay_max_frames)
        self._snapshot_history: deque[HighRatePhaseSnapshot] = deque(
            maxlen=max(config.phase_replay_max_frames * 2, 32)
        )
        self._state: _TrackingState | None = None
        self._snapshot = HighRatePhaseSnapshot()
        self._processed_events: deque[float] = deque(maxlen=240)
        self._source_events: deque[float] = deque(maxlen=240)
        self._dropped_frames = 0
        self.replayed_frames = 0
        self.restored_seed_frames = 0
        self._checkpoints: dict[int, tuple[_TrackingState, HighRatePhaseSnapshot]] = {}

    def publish_seed(self, seed: StemPhaseSeed) -> None:
        self._pending_seed = seed

    def reset(self) -> None:
        self.__init__(self.config)

    def process_frame(self, frame: HighRatePhaseFrame) -> HighRatePhaseSnapshot:
        self._trim_history(frame)
        if not self._history or frame.frame_sequence > self._history[-1].frame_sequence:
            self._history.append(frame)
        pending = self._pending_seed
        self._pending_seed = None
        if pending is not None:
            if self._seed_deadline_expired(pending):
                return self._snapshot
            self._restore_seed_predecessor(pending, frame)
            # A seed can belong to a source frame skipped by the rate limiter.
            # Observe that actual image before re-anchoring; never interpolate.
            if self._state is not None and pending.frame_sequence > self._state.frame_sequence:
                self._advance(
                    HighRatePhaseFrame(
                        pending.gray, pending.frame_sequence, pending.timestamp, frame.received_perf
                    )
                )
                self._snapshot_history.append(self._snapshot)
                self._remember_checkpoint()
            if self._seed_deadline_expired(pending):
                return self._snapshot
            self._apply_seed(pending)
            self._snapshot_history.append(self._snapshot)
            self._remember_checkpoint()
            if "PHASE_SEED_HISTORY_MISSING" in self._snapshot.reason_codes:
                return self._snapshot
            replay = [
                item for item in self._history if item.frame_sequence > pending.frame_sequence
            ]
            for item in replay:
                if self._seed_deadline_expired(pending):
                    return self._snapshot
                self._advance(item)
                self.replayed_frames += 1
                self._snapshot_history.append(self._snapshot)
                self._remember_checkpoint()
        elif self._state is not None and frame.frame_sequence > self._state.frame_sequence:
            self._advance(frame)
        self._processed_events.append(time.perf_counter())
        latency_ms = max((time.perf_counter() - frame.received_perf) * 1000.0, 0.0)
        self._snapshot = replace(
            self._snapshot,
            source_phase_fps=_event_rate(self._source_events),
            processed_phase_fps=_event_rate(self._processed_events),
            fast_path_latency_ms=latency_ms,
            fast_path_dropped_frames=self._dropped_frames,
        )
        self._snapshot_history.append(self._snapshot)
        self._remember_checkpoint()
        return self._snapshot

    def _seed_deadline_expired(self, seed: StemPhaseSeed) -> bool:
        if seed.deadline_perf is None or time.perf_counter() < seed.deadline_perf:
            return False
        self._snapshot = replace(
            self._snapshot, track_id=seed.track_id, session_id=seed.session_id,
            frame_sequence=seed.frame_sequence, timestamp=seed.timestamp,
            phase_locked=False, confidence=0.0, tracking_mode="LOST",
            reason_codes=("PHASE_SEED_HISTORY_EXPIRED", "PHASE_TRACK_LOST"),
        )
        return True

    def _remember_checkpoint(self) -> None:
        state, snapshot = self._state, self._snapshot
        if (state is None or not snapshot.phase_locked
                or snapshot.frame_sequence != state.frame_sequence):
            return
        # Image arrays are replaced, never edited by tracking. Only the keyframe
        # list and scalar state need a private copy for a bounded checkpoint.
        self._checkpoints[state.frame_sequence] = (
            replace(state, keyframes=list(state.keyframes or [])), snapshot
        )
        for sequence in sorted(self._checkpoints)[:-self.config.phase_replay_max_frames]:
            del self._checkpoints[sequence]

    def _restore_seed_predecessor(self, seed: StemPhaseSeed, current: HighRatePhaseFrame) -> None:
        state = self._state
        if state is None or seed.frame_sequence > state.frame_sequence:
            return
        if any(s.frame_sequence == seed.frame_sequence
               and s.track_id == seed.track_id and s.session_id == seed.session_id
               and s.trace_generation == state.trace_generation for s in self._snapshot_history):
            return
        candidates = [
            (sequence, checkpoint) for sequence, checkpoint in self._checkpoints.items()
            if sequence < seed.frame_sequence
            and checkpoint[0].track_id == seed.track_id
            and checkpoint[0].session_id == seed.session_id
            and checkpoint[0].trace_generation == state.trace_generation
            and (age := _timestamp_delta_ms(checkpoint[0].timestamp, current.timestamp)) is not None
            and 0 <= age <= self.config.phase_replay_window_ms
        ]
        if not candidates:
            return
        sequence, (previous, snapshot) = max(candidates, key=lambda item: item[0])
        self._state = replace(previous, keyframes=list(previous.keyframes or []))
        self._snapshot = snapshot
        self._snapshot_history = deque(
            (s for s in self._snapshot_history if s.frame_sequence is not None
             and s.frame_sequence <= sequence), maxlen=self._snapshot_history.maxlen,
        )
        self._checkpoints = {k: v for k, v in self._checkpoints.items() if k <= sequence}
        for intermediate in self._history:
            if sequence < intermediate.frame_sequence < seed.frame_sequence:
                if self._seed_deadline_expired(seed):
                    return
                self._advance(intermediate)
                self.replayed_frames += 1
                self._snapshot_history.append(self._snapshot)
                self._remember_checkpoint()
        self.restored_seed_frames += 1

    def _trim_history(self, current: HighRatePhaseFrame) -> None:
        current_time = _timestamp_seconds(current.timestamp)
        if current_time is None:
            return
        maximum_age = self.config.phase_replay_window_ms / 1000.0
        self._checkpoints = {
            k: v for k, v in self._checkpoints.items()
            if (stamp := _timestamp_seconds(v[0].timestamp)) is not None
            and 0 <= current_time - stamp <= maximum_age
        }
        while self._history:
            oldest_time = _timestamp_seconds(self._history[0].timestamp)
            if oldest_time is None or current_time - oldest_time <= maximum_age:
                break
            self._history.popleft()

    def _apply_seed(self, seed: StemPhaseSeed) -> None:
        previous_raw = 0.0
        previous_translation_x_px = 0.0
        previous_translation_y_px = 0.0
        previous_state = self._state
        historical: HighRatePhaseSnapshot | None = None
        previous_generation = (
            previous_state.trace_generation
            if previous_state is not None
            else self._snapshot.trace_generation
        )
        track_changed = previous_state is not None and (
            previous_state.track_id != seed.track_id or previous_state.session_id != seed.session_id
        )
        if not track_changed:
            if previous_state is not None:
                historical = next(
                    (
                        item
                        for item in reversed(self._snapshot_history)
                        if item.frame_sequence == seed.frame_sequence
                        and item.track_id == seed.track_id
                        and item.session_id == seed.session_id
                        and item.trace_generation == previous_generation
                    ),
                    None,
                )
                if historical is None or not historical.phase_locked:
                    self._snapshot = HighRatePhaseSnapshot(
                        track_id=seed.track_id,
                        session_id=seed.session_id,
                        frame_sequence=seed.frame_sequence,
                        timestamp=seed.timestamp,
                        trace_generation=previous_generation,
                        reason_codes=("PHASE_SEED_HISTORY_MISSING", "PHASE_TRACK_LOST"),
                    )
                    return
                previous_raw = float(historical.cumulative_raw_phase or 0.0)
                previous_translation_x_px = historical.cumulative_translation_x_px
                previous_translation_y_px = historical.cumulative_translation_y_px
        generation = previous_generation + int(track_changed)
        keyframes = (
            [
                item
                for item in (previous_state.keyframes or [])
                if int(item.keyframe_id.rsplit("-", 1)[-1]) <= seed.frame_sequence
            ]
            if previous_state is not None and not track_changed
            else []
        )
        scale = min(self.config.tracking_max_width_px / max(seed.axis.image_width, 1), 1.0)
        gray = _resize(seed.gray, scale, nearest=False)
        mask = _resize(seed.stem_mask, scale, nearest=True)
        axis = _scaled_axis(seed.axis, scale)
        mask = _erode_mask(mask, axis.median_width_px, self.config.mask_erosion_fraction)
        points = _detect_features(gray, mask, self.config)
        self._state = _TrackingState(
            track_id=seed.track_id,
            session_id=seed.session_id,
            gray=gray,
            mask=mask,
            axis=axis,
            points=points,
            frame_sequence=seed.frame_sequence,
            timestamp=seed.timestamp,
            seed_timestamp=seed.timestamp,
            seed_sequence=seed.frame_sequence,
            cumulative_raw_phase=previous_raw,
            trace_generation=generation,
            cumulative_translation_x_px=previous_translation_x_px,
            cumulative_translation_y_px=previous_translation_y_px,
            keyframes=keyframes,
        )
        blur = _masked_blur_score(gray, mask)
        self._store_keyframe(self._state)
        if historical is not None:
            self._snapshot = replace(historical, seed_age_ms=0.0, seed_status="APPLIED")
            return
        self._snapshot = HighRatePhaseSnapshot(
            track_id=seed.track_id,
            session_id=seed.session_id,
            frame_sequence=seed.frame_sequence,
            timestamp=seed.timestamp,
            delta_raw_phase=0.0,
            cumulative_raw_phase=previous_raw,
            last_safe_raw_phase=previous_raw,
            phase_locked=len(points) >= self.config.minimum_matched_features,
            tracking_mode="INITIALIZING",
            relock_status="NOT_REQUIRED",
            trace_generation=generation,
            detected_feature_count=len(points),
            blur_score=blur,
            confidence=min(max(seed.confidence, 0.0), 1.0),
            cumulative_translation_x_px=previous_translation_x_px,
            cumulative_translation_y_px=previous_translation_y_px,
            cumulative_translation_magnitude_px=math.hypot(
                previous_translation_x_px,
                previous_translation_y_px,
            ),
            seed_age_ms=0.0,
            seed_status="TRACK_CHANGED" if track_changed else "APPLIED",
            fast_path_dropped_frames=self._dropped_frames,
            reason_codes=(
                "PHASE_TRACK_CHANGED_RESET" if track_changed else "PHASE_HIGH_RATE_SEED_APPLIED",
            ),
        )

    def _advance(self, frame: HighRatePhaseFrame) -> None:
        state = self._state
        if state is None or frame.frame_sequence <= state.frame_sequence:
            return
        seed_age_ms = _timestamp_delta_ms(state.seed_timestamp, frame.timestamp)
        source_gap = frame.frame_sequence - state.frame_sequence
        if seed_age_ms is None:
            seed_age_ms = (
                max(
                    (frame.received_perf - self._processed_events[-1]) * 1000.0,
                    0.0,
                )
                if self._processed_events
                else 0.0
            )
        if seed_age_ms > self.config.maximum_seed_age_ms:
            self._hold(
                frame,
                seed_age_ms=seed_age_ms,
                source_gap=source_gap,
                reasons=("PHASE_SEED_STALE", "PHASE_TRACK_LOST"),
                stale=True,
            )
            return
        scale = state.gray.shape[1] / max(frame.image_bgr.shape[1], 1)
        gray_full = _gray(frame.image_bgr)
        gray = _resize(gray_full, scale, nearest=False)
        blur = _masked_blur_score(gray, state.mask)
        if blur < self.config.minimum_blur_score:
            self._hold(
                frame,
                seed_age_ms=seed_age_ms,
                source_gap=source_gap,
                reasons=("PHASE_FRAME_BLURRED", "PHASE_HOLD_LAST_GOOD"),
                blur=blur,
            )
            return
        points = state.points
        if len(points) < self.config.minimum_matched_features:
            points = _detect_features(state.gray, state.mask, self.config)
        tracked = _forward_backward_flow(
            state.gray,
            gray,
            points,
            state.mask,
            self.config,
        )
        matches, current_points, fb_error = tracked
        affine = _robust_affine_motion(matches)
        if affine is None:
            self._hold(
                frame,
                seed_age_ms=seed_age_ms,
                source_gap=source_gap,
                reasons=("PHASE_AFFINE_PROPAGATION_FAILED", "PHASE_HOLD_LAST_GOOD"),
                blur=blur,
                fb_error=fb_error,
            )
            return
        propagated_axis = _transform_axis(state.axis, affine)
        estimate = estimate_cylindrical_surface_phase_from_matches(
            matches,
            previous_axis=state.axis,
            current_axis=propagated_axis,
            config=CylindricalSurfacePhaseConfig(
                minimum_matched_features=self.config.minimum_matched_features,
                minimum_inlier_ratio=self.config.minimum_inlier_ratio,
                maximum_tracking_error_px=self.config.maximum_tracking_error_px,
                minimum_feature_span_ratio=self.config.minimum_feature_span_ratio,
                maximum_abs_delta_raw_phase=self.config.maximum_abs_delta_raw_phase,
                minimum_confidence=self.config.minimum_confidence,
            ),
        )
        if not estimate.valid or estimate.delta_raw_phase is None:
            relocked, relock_reasons = self._try_relock(frame, gray, blur, seed_age_ms, source_gap)
            if relocked:
                return
            self._hold(
                frame,
                seed_age_ms=seed_age_ms,
                source_gap=source_gap,
                reasons=(
                    *estimate.reason_codes,
                    *relock_reasons,
                    "PHASE_HOLD_LAST_GOOD",
                ),
                blur=blur,
                estimate=estimate,
                fb_error=fb_error,
            )
            return
        delta = float(estimate.delta_raw_phase)
        cumulative = state.cumulative_raw_phase + delta
        translation_scale = max(scale, 1.0e-9)
        state.cumulative_translation_x_px += estimate.translation_x_px / translation_scale
        state.cumulative_translation_y_px += estimate.translation_y_px / translation_scale
        axis = propagated_axis
        mask = _warp_mask(state.mask, affine)
        kept_points = np.asarray(
            [current_points[index] for index in estimate.inlier_match_indices],
            dtype=np.float32,
        ).reshape(-1, 1, 2)
        if len(kept_points) < max(self.config.minimum_matched_features * 2, 32):
            detected = _detect_features(gray, mask, self.config)
            if len(detected) > len(kept_points):
                kept_points = detected
        feature_span = _feature_span_ratio(kept_points, axis)
        state.gray = gray
        state.mask = mask
        state.axis = axis
        state.points = kept_points
        state.frame_sequence = frame.frame_sequence
        state.timestamp = frame.timestamp
        state.cumulative_raw_phase = cumulative
        state.failure_count = 0
        self._store_keyframe(state)
        self._snapshot = _snapshot_from_estimate(
            state,
            frame,
            estimate,
            cumulative=cumulative,
            mode="INCREMENTAL",
            relock_status="NOT_REQUIRED",
            blur=blur,
            fb_error=fb_error,
            feature_span=feature_span,
            seed_age_ms=seed_age_ms,
            source_gap=source_gap,
            dropped=self._dropped_frames,
            reasons=("PHASE_HIGH_RATE_PROPAGATED",),
            translation_scale=translation_scale,
        )

    def _hold(
        self,
        frame: HighRatePhaseFrame,
        *,
        seed_age_ms: float,
        source_gap: int,
        reasons: tuple[str, ...],
        stale: bool = False,
        blur: float | None = None,
        estimate: RawSurfacePhaseEstimate | None = None,
        fb_error: float | None = None,
    ) -> None:
        state = self._state
        if state is None:
            return
        state.failure_count += 1
        locked = not stale and seed_age_ms <= self.config.maximum_seed_age_ms
        self._snapshot = HighRatePhaseSnapshot(
            track_id=state.track_id,
            session_id=state.session_id,
            frame_sequence=frame.frame_sequence,
            timestamp=frame.timestamp,
            cumulative_raw_phase=state.cumulative_raw_phase,
            last_safe_raw_phase=state.cumulative_raw_phase,
            phase_locked=False,
            tracking_mode="HOLDING" if locked else "LOST",
            relock_status="FAILED" if estimate is not None else "NOT_ATTEMPTED",
            trace_generation=state.trace_generation,
            failure_count=state.failure_count,
            detected_feature_count=len(state.points),
            matched_feature_count=estimate.matched_feature_count if estimate else 0,
            inlier_feature_count=estimate.inlier_feature_count if estimate else 0,
            inlier_ratio=estimate.inlier_ratio if estimate else 0.0,
            median_tracking_error_px=estimate.median_tracking_error_px if estimate else None,
            forward_backward_error_px=fb_error,
            feature_span_ratio=_feature_span_ratio(state.points, state.axis),
            blur_score=blur,
            confidence=0.0,
            cumulative_translation_x_px=state.cumulative_translation_x_px,
            cumulative_translation_y_px=state.cumulative_translation_y_px,
            cumulative_translation_magnitude_px=math.hypot(
                state.cumulative_translation_x_px,
                state.cumulative_translation_y_px,
            ),
            source_frame_gap=max(source_gap, 0),
            seed_age_ms=max(seed_age_ms, 0.0),
            fast_path_dropped_frames=self._dropped_frames,
            seed_status="STALE" if stale else "PROPAGATED",
            reason_codes=tuple(dict.fromkeys(reasons)),
        )

    def _store_keyframe(self, state: _TrackingState) -> None:
        if len(state.points) < self.config.keyframe_minimum_features:
            return
        keyframes = state.keyframes if state.keyframes is not None else []
        state.keyframes = keyframes
        if (
            keyframes
            and abs(state.cumulative_raw_phase - keyframes[-1].raw_phase)
            < self.config.keyframe_minimum_raw_separation
        ):
            return
        detector = _cv2().ORB_create(nfeatures=320, fastThreshold=8, edgeThreshold=5)
        keypoints, descriptors = detector.detectAndCompute(state.gray, state.mask)
        if descriptors is None or len(keypoints) < self.config.keyframe_minimum_features:
            return
        keyframes.append(
            _Keyframe(
                keyframe_id=f"kf-{state.trace_generation}-{state.frame_sequence}",
                raw_phase=state.cumulative_raw_phase,
                points=np.asarray([item.pt for item in keypoints], dtype=np.float32),
                descriptors=descriptors.copy(),
            )
        )
        del keyframes[: max(0, len(keyframes) - self.config.maximum_keyframes)]

    def _try_relock(
        self,
        frame: HighRatePhaseFrame,
        gray: np.ndarray,
        blur: float,
        seed_age_ms: float,
        source_gap: int,
    ) -> tuple[bool, tuple[str, ...]]:
        state = self._state
        if state is None or not state.keyframes:
            return False, ("PHASE_RELOCK_KEYFRAME_MISSING",)
        cv2 = _cv2()
        detector = cv2.ORB_create(nfeatures=320, fastThreshold=8, edgeThreshold=5)
        current_keypoints, current_descriptors = detector.detectAndCompute(gray, state.mask)
        if (
            current_descriptors is None
            or len(current_keypoints) < self.config.keyframe_minimum_features
        ):
            return False, ("PHASE_RELOCK_FEATURES_INSUFFICIENT",)
        current_points = np.asarray([item.pt for item in current_keypoints], dtype=np.float32)
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        candidates: list[PhaseRelockCandidate] = []
        for keyframe in state.keyframes:
            pairs = matcher.knnMatch(keyframe.descriptors, current_descriptors, k=2)
            good = [
                pair[0]
                for pair in pairs
                if len(pair) == 2 and pair[0].distance < 0.72 * pair[1].distance
            ]
            if len(good) < self.config.keyframe_minimum_features:
                continue
            source = np.asarray([keyframe.points[item.queryIdx] for item in good], dtype=np.float32)
            target = np.asarray([current_points[item.trainIdx] for item in good], dtype=np.float32)
            _matrix, inliers = cv2.estimateAffinePartial2D(
                source, target, method=cv2.RANSAC, ransacReprojThreshold=2.5
            )
            inlier_count = int(np.count_nonzero(inliers)) if inliers is not None else 0
            ratio = inlier_count / len(good)
            if inlier_count < self.config.keyframe_minimum_features or ratio < 0.70:
                continue
            score = min(0.65 * ratio + 0.35 * min(inlier_count / 30.0, 1.0), 1.0)
            candidates.append(
                PhaseRelockCandidate(
                    keyframe.keyframe_id,
                    keyframe.raw_phase,
                    score,
                    len(good),
                    ratio,
                )
            )
        decision = select_phase_relock_candidate(
            candidates,
            last_safe_raw_phase=state.cumulative_raw_phase,
            minimum_score=self.config.relock_minimum_score,
            uniqueness_margin=self.config.relock_uniqueness_margin,
            maximum_raw_jump=self.config.relock_maximum_raw_jump,
        )
        if decision.candidate is None:
            return False, decision.reason_codes
        previous = state.cumulative_raw_phase
        cumulative = float(decision.candidate.raw_phase)
        state.gray = gray
        state.points = _detect_features(gray, state.mask, self.config)
        state.frame_sequence = frame.frame_sequence
        state.timestamp = frame.timestamp
        state.cumulative_raw_phase = cumulative
        state.failure_count = 0
        self._snapshot = HighRatePhaseSnapshot(
            track_id=state.track_id,
            session_id=state.session_id,
            frame_sequence=frame.frame_sequence,
            timestamp=frame.timestamp,
            delta_raw_phase=cumulative - previous,
            cumulative_raw_phase=cumulative,
            last_safe_raw_phase=cumulative,
            phase_locked=True,
            tracking_mode="KEYFRAME_RELOCK",
            relock_status="RELOCKED",
            relock_keyframe_id=decision.candidate.keyframe_id,
            trace_generation=state.trace_generation,
            detected_feature_count=len(state.points),
            matched_feature_count=decision.candidate.matched_feature_count,
            inlier_feature_count=int(
                round(decision.candidate.matched_feature_count * decision.candidate.inlier_ratio)
            ),
            inlier_ratio=decision.candidate.inlier_ratio,
            blur_score=blur,
            confidence=decision.candidate.score,
            cumulative_translation_x_px=state.cumulative_translation_x_px,
            cumulative_translation_y_px=state.cumulative_translation_y_px,
            cumulative_translation_magnitude_px=math.hypot(
                state.cumulative_translation_x_px,
                state.cumulative_translation_y_px,
            ),
            source_frame_gap=max(source_gap, 0),
            seed_age_ms=max(seed_age_ms, 0.0),
            fast_path_dropped_frames=self._dropped_frames,
            seed_status="PROPAGATED",
            reason_codes=decision.reason_codes,
        )
        return True, decision.reason_codes


def _snapshot_from_estimate(
    state: _TrackingState,
    frame: HighRatePhaseFrame,
    estimate: RawSurfacePhaseEstimate,
    *,
    cumulative: float,
    mode: Literal["INCREMENTAL"],
    relock_status: Literal["NOT_REQUIRED"],
    blur: float,
    fb_error: float | None,
    feature_span: float,
    seed_age_ms: float,
    source_gap: int,
    dropped: int,
    reasons: tuple[str, ...],
    translation_scale: float,
) -> HighRatePhaseSnapshot:
    translation_x_px = estimate.translation_x_px / translation_scale
    translation_y_px = estimate.translation_y_px / translation_scale
    return HighRatePhaseSnapshot(
        track_id=state.track_id,
        session_id=state.session_id,
        frame_sequence=frame.frame_sequence,
        timestamp=frame.timestamp,
        delta_raw_phase=estimate.delta_raw_phase,
        cumulative_raw_phase=cumulative,
        last_safe_raw_phase=cumulative,
        phase_locked=True,
        tracking_mode=mode,
        relock_status=relock_status,
        trace_generation=state.trace_generation,
        failure_count=state.failure_count,
        detected_feature_count=len(state.points),
        matched_feature_count=estimate.matched_feature_count,
        inlier_feature_count=estimate.inlier_feature_count,
        inlier_ratio=estimate.inlier_ratio,
        median_tracking_error_px=estimate.median_tracking_error_px,
        forward_backward_error_px=fb_error,
        feature_span_ratio=feature_span,
        blur_score=blur,
        confidence=estimate.confidence,
        translation_x_px=translation_x_px,
        translation_y_px=translation_y_px,
        translation_magnitude_px=math.hypot(translation_x_px, translation_y_px),
        cumulative_translation_x_px=state.cumulative_translation_x_px,
        cumulative_translation_y_px=state.cumulative_translation_y_px,
        cumulative_translation_magnitude_px=math.hypot(
            state.cumulative_translation_x_px,
            state.cumulative_translation_y_px,
        ),
        axis_rotation_deg=estimate.axis_rotation_deg,
        source_frame_gap=max(source_gap, 0),
        seed_age_ms=max(seed_age_ms, 0.0),
        fast_path_dropped_frames=dropped,
        seed_status="PROPAGATED",
        reason_codes=reasons,
    )


def _forward_backward_flow(
    previous_gray: np.ndarray,
    current_gray: np.ndarray,
    points: np.ndarray,
    previous_mask: np.ndarray,
    config: HighRatePhaseConfig,
) -> tuple[list[SurfaceFeatureMatch], np.ndarray, float | None]:
    if points is None or len(points) == 0:
        return [], np.empty((0, 2), dtype=np.float32), None
    cv2 = _cv2()
    current, forward_status, forward_errors = cv2.calcOpticalFlowPyrLK(
        previous_gray,
        current_gray,
        points,
        None,
        winSize=(config.lk_window_size, config.lk_window_size),
        maxLevel=config.lk_max_level,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
    )
    if current is None or forward_status is None:
        return [], np.empty((0, 2), dtype=np.float32), None
    backward, backward_status, _backward_errors = cv2.calcOpticalFlowPyrLK(
        current_gray,
        previous_gray,
        current,
        None,
        winSize=(config.lk_window_size, config.lk_window_size),
        maxLevel=config.lk_max_level,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
    )
    if backward is None or backward_status is None:
        return [], np.empty((0, 2), dtype=np.float32), None
    previous_flat = points.reshape(-1, 2)
    current_flat = current.reshape(-1, 2)
    backward_flat = backward.reshape(-1, 2)
    fb = np.linalg.norm(previous_flat - backward_flat, axis=1)
    forward_error = (
        forward_errors.reshape(-1)
        if forward_errors is not None
        else np.zeros(len(previous_flat), dtype=np.float32)
    )
    allowed_mask = cv2.dilate(previous_mask, np.ones((11, 11), dtype=np.uint8))
    matches: list[SurfaceFeatureMatch] = []
    kept: list[np.ndarray] = []
    kept_fb: list[float] = []
    for index, (old, new) in enumerate(zip(previous_flat, current_flat, strict=True)):
        if not bool(forward_status.reshape(-1)[index]) or not bool(
            backward_status.reshape(-1)[index]
        ):
            continue
        if (
            not math.isfinite(float(fb[index]))
            or fb[index] > config.maximum_forward_backward_error_px
        ):
            continue
        x, y = int(round(float(new[0]))), int(round(float(new[1])))
        if not (0 <= x < allowed_mask.shape[1] and 0 <= y < allowed_mask.shape[0]):
            continue
        if int(allowed_mask[y, x]) == 0:
            continue
        matches.append(
            SurfaceFeatureMatch(
                previous_x_px=float(old[0]),
                previous_y_px=float(old[1]),
                current_x_px=float(new[0]),
                current_y_px=float(new[1]),
                tracking_error_px=max(float(forward_error[index]), 0.0),
            )
        )
        kept.append(new)
        kept_fb.append(float(fb[index]))
    return (
        matches,
        np.asarray(kept, dtype=np.float32),
        float(np.median(np.asarray(kept_fb, dtype=np.float64))) if kept_fb else None,
    )


def _detect_features(gray: np.ndarray, mask: np.ndarray, config: HighRatePhaseConfig) -> np.ndarray:
    features = _cv2().goodFeaturesToTrack(
        gray,
        maxCorners=config.maximum_features,
        qualityLevel=config.feature_quality_level,
        minDistance=config.feature_minimum_distance_px,
        mask=mask,
        blockSize=config.feature_block_size,
    )
    return (
        features.astype(np.float32)
        if features is not None
        else np.empty((0, 1, 2), dtype=np.float32)
    )


def _feature_span_ratio(points: np.ndarray, axis: StemAxisObservation) -> float:
    if points is None or len(points) < 2:
        return 0.0
    local = points.reshape(-1, 2).astype(np.float64) - axis.center
    transverse = local @ axis.normal / max(axis.median_width_px, 1e-9)
    return min(max(float(np.ptp(transverse)), 0.0), 1.0)


def _masked_blur_score(gray: np.ndarray, mask: np.ndarray) -> float:
    cv2 = _cv2()
    values = cv2.Laplacian(gray, cv2.CV_64F)[mask > 0]
    return float(np.var(values)) if values.size else 0.0


def _robust_affine_motion(matches: list[SurfaceFeatureMatch]) -> np.ndarray | None:
    if len(matches) < 3:
        return None
    source = np.asarray(
        [(item.previous_x_px, item.previous_y_px) for item in matches],
        dtype=np.float32,
    )
    target = np.asarray(
        [(item.current_x_px, item.current_y_px) for item in matches],
        dtype=np.float32,
    )
    matrix, inliers = _cv2().estimateAffinePartial2D(
        source,
        target,
        method=_cv2().RANSAC,
        ransacReprojThreshold=2.5,
        maxIters=1000,
        confidence=0.99,
        refineIters=10,
    )
    if matrix is None or inliers is None or int(np.count_nonzero(inliers)) < 3:
        return None
    matrix = np.asarray(matrix, dtype=np.float64).reshape(2, 3)
    return matrix if np.all(np.isfinite(matrix)) else None


def _transform_axis(
    axis: StemAxisObservation,
    affine: np.ndarray,
) -> StemAxisObservation:
    linear = affine[:, :2]
    offset = affine[:, 2]
    center = linear @ axis.center + offset
    direction_vector = linear @ axis.direction
    normal_vector = linear @ axis.normal
    direction_scale = float(np.linalg.norm(direction_vector))
    normal_scale = float(np.linalg.norm(normal_vector))
    if direction_scale <= 1e-9 or normal_scale <= 1e-9:
        return axis
    direction = direction_vector / direction_scale
    return StemAxisObservation(
        center_x_px=float(center[0]),
        center_y_px=float(center[1]),
        direction_x=float(direction[0]),
        direction_y=float(direction[1]),
        length_px=max(axis.length_px * direction_scale, 1e-6),
        median_width_px=max(axis.median_width_px * normal_scale, 1e-6),
        image_width=axis.image_width,
        image_height=axis.image_height,
    )


def _warp_mask(mask: np.ndarray, affine: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    return cv2.warpAffine(
        mask,
        affine,
        (mask.shape[1], mask.shape[0]),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
    )


def _erode_mask(mask: np.ndarray, width_px: float, fraction: float) -> np.ndarray:
    kernel_size = max(int(round(width_px * fraction)), 1)
    if kernel_size % 2 == 0:
        kernel_size += 1
    kernel = _cv2().getStructuringElement(_cv2().MORPH_ELLIPSE, (kernel_size, kernel_size))
    return _cv2().erode(mask.astype(np.uint8), kernel)


def _gray(image_bgr: np.ndarray) -> np.ndarray:
    if image_bgr.ndim == 2:
        return np.ascontiguousarray(image_bgr)
    return _cv2().cvtColor(image_bgr, _cv2().COLOR_BGR2GRAY)


def _resize(image: np.ndarray, scale: float, *, nearest: bool) -> np.ndarray:
    if scale >= 0.999999:
        return np.ascontiguousarray(image)
    cv2 = _cv2()
    return cv2.resize(
        image,
        None,
        fx=scale,
        fy=scale,
        interpolation=cv2.INTER_NEAREST if nearest else cv2.INTER_AREA,
    )


def _scaled_axis(axis: StemAxisObservation, scale: float) -> StemAxisObservation:
    return StemAxisObservation(
        center_x_px=axis.center_x_px * scale,
        center_y_px=axis.center_y_px * scale,
        direction_x=axis.direction_x,
        direction_y=axis.direction_y,
        length_px=axis.length_px * scale,
        median_width_px=axis.median_width_px * scale,
        image_width=max(int(round(axis.image_width * scale)), 1),
        image_height=max(int(round(axis.image_height * scale)), 1),
    )


def _timestamp_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _timestamp_delta_ms(previous: str | None, current: str | None) -> float | None:
    left = _timestamp_seconds(previous)
    right = _timestamp_seconds(current)
    if left is None or right is None:
        return None
    return max((right - left) * 1000.0, 0.0)


def _event_rate(events: deque[float]) -> float | None:
    if len(events) < 2:
        return None
    now = events[-1]
    while len(events) > 2 and now - events[0] > 2.0:
        events.popleft()
    elapsed = now - events[0]
    return (len(events) - 1) / elapsed if elapsed > 1e-9 else None


def _number(
    value: Mapping[str, Any], key: str, default: float, minimum: float, maximum: float
) -> float:
    try:
        parsed = float(value.get(key, default))
    except (TypeError, ValueError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return min(max(parsed, minimum), maximum)


def _integer(value: Mapping[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value.get(key, default))
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def _odd_integer(
    value: Mapping[str, Any], key: str, default: int, minimum: int, maximum: int
) -> int:
    parsed = _integer(value, key, default, minimum, maximum)
    return parsed if parsed % 2 else min(parsed + 1, maximum)


def _cv2() -> Any:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("OpenCV is required for high-rate stem phase tracking") from exc
    return cv2
