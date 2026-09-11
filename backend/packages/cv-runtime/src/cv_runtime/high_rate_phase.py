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
            feature_quality_level=_number(
                value, "feature_quality_level", 0.015, 0.001, 0.5
            ),
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
            minimum_matched_features=_integer(
                value, "minimum_matched_features", 12, 4, 500
            ),
            minimum_inlier_ratio=_number(value, "minimum_inlier_ratio", 0.70, 0.0, 1.0),
            maximum_tracking_error_px=_number(
                value, "maximum_tracking_error_px", 12.0, 0.1, 500.0
            ),
            minimum_feature_span_ratio=_number(
                value, "minimum_feature_span_ratio", 0.35, 0.05, 1.0
            ),
            maximum_abs_delta_raw_phase=_number(
                value, "maximum_abs_delta_raw_phase", 0.70, 0.01, math.pi
            ),
            minimum_confidence=_number(value, "minimum_rotation_confidence", 0.45, 0.0, 1.0),
            maximum_keyframes=_integer(value, "maximum_keyframes", 24, 2, 128),
            keyframe_minimum_features=_integer(
                value, "keyframe_minimum_features", 15, 4, 500
            ),
            keyframe_minimum_raw_separation=_number(
                value, "keyframe_minimum_raw_separation", 0.15, 0.01, math.pi
            ),
            relock_minimum_score=_number(value, "relock_minimum_score", 0.60, 0.0, 1.0),
            relock_uniqueness_margin=_number(
                value, "relock_uniqueness_margin", 0.15, 0.0, 1.0
            ),
            relock_maximum_raw_jump=_number(
                value, "relock_maximum_raw_jump", 0.70, 0.01, math.pi
            ),
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
    relock_status: Literal[
        "NOT_REQUIRED", "NOT_ATTEMPTED", "RELOCKED", "AMBIGUOUS", "FAILED"
    ] = "NOT_ATTEMPTED"
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
    """Thread-safe seed bridge and single-owner high-rate optical-flow state."""

    def __init__(self, config: HighRatePhaseConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._pending_seed: StemPhaseSeed | None = None
        self._history: deque[HighRatePhaseFrame] = deque(
            maxlen=config.phase_replay_max_frames
        )
        self._phase_history: deque[tuple[int, str, float]] = deque(
            maxlen=max(config.phase_replay_max_frames * 2, 32)
        )
        self._snapshot_history: deque[HighRatePhaseSnapshot] = deque(
            maxlen=max(config.phase_replay_max_frames * 2, 32)
        )
        self._state: _TrackingState | None = None
        self._snapshot = HighRatePhaseSnapshot()
        self._source_events: deque[float] = deque(maxlen=240)
        self._processed_events: deque[float] = deque(maxlen=240)
        self._dropped_frames = 0
        self._rate_skipped_frames = 0

    def publish_seed(self, seed: StemPhaseSeed) -> None:
        with self._lock:
            track_changed = self._state is not None and (
                self._state.track_id != seed.track_id
                or self._state.session_id != seed.session_id
            )
            self._pending_seed = seed
            if self._state is None or track_changed:
                generation = self._snapshot.trace_generation + int(track_changed)
                self._snapshot = HighRatePhaseSnapshot(
                    track_id=seed.track_id,
                    session_id=seed.session_id,
                    frame_sequence=seed.frame_sequence,
                    timestamp=seed.timestamp,
                    cumulative_raw_phase=0.0,
                    last_safe_raw_phase=0.0,
                    tracking_mode="INITIALIZING",
                    trace_generation=generation,
                    seed_status="TRACK_CHANGED" if track_changed else "WAITING",
                    reason_codes=(
                        "PHASE_TRACK_CHANGED_RESET" if track_changed else "PHASE_SEED_PENDING",
                    ),
                )

    def record_source_frame(self) -> None:
        with self._lock:
            self._source_events.append(time.perf_counter())

    def record_queue_drop(self, count: int = 1) -> None:
        with self._lock:
            self._dropped_frames += max(int(count), 0)

    def record_rate_skip(self, count: int = 1) -> None:
        with self._lock:
            self._rate_skipped_frames += max(int(count), 0)

    def record_processing_failure(
        self,
        *,
        frame_sequence: int,
        timestamp: str,
        reason: str = "PHASE_FAST_PATH_PREPROCESSING_FAILED",
    ) -> None:
        """Expose preprocessing failures without counting them as queue overload."""

        with self._lock:
            failure_count = self._snapshot.failure_count + 1
            tracking_mode = "HOLDING" if self._snapshot.phase_locked else "LOST"
            reasons = tuple(dict.fromkeys((*self._snapshot.reason_codes, reason)))
            self._snapshot = replace(
                self._snapshot,
                frame_sequence=frame_sequence,
                timestamp=timestamp,
                tracking_mode=tracking_mode,
                failure_count=failure_count,
                fast_path_dropped_frames=self._dropped_frames,
                reason_codes=reasons,
            )
            self._snapshot_history.append(self._snapshot)

    def process_frame(self, frame: HighRatePhaseFrame) -> HighRatePhaseSnapshot:
        with self._lock:
            self._trim_history(frame)
            self._history.append(frame)
            pending = self._pending_seed
            self._pending_seed = None
            if pending is not None:
                self._apply_seed(pending)
                replay = [
                    item
                    for item in self._history
                    if item.frame_sequence > pending.frame_sequence
                ]
                for item in replay:
                    self._advance(item)
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
            return self._snapshot

    def snapshot(
        self,
        track_id: str | None = None,
        frame_sequence: int | None = None,
    ) -> HighRatePhaseSnapshot:
        with self._lock:
            value = self._snapshot
            if frame_sequence is not None:
                candidates = [
                    item
                    for item in self._snapshot_history
                    if item.frame_sequence is not None
                    and item.frame_sequence <= frame_sequence
                    and (track_id is None or item.track_id == track_id)
                ]
                if candidates:
                    value = candidates[-1]
            if track_id is not None and value.track_id not in {None, track_id}:
                return HighRatePhaseSnapshot(
                    track_id=track_id,
                    tracking_mode="LOST",
                    trace_generation=value.trace_generation + 1,
                    fast_path_dropped_frames=self._dropped_frames,
                    seed_status="TRACK_CHANGED",
                    reason_codes=("PHASE_FAST_PATH_TRACK_MISMATCH", "PHASE_TRACK_LOST"),
                )
            return value

    def reset(self, reason: str = "PHASE_RUNTIME_RESET") -> None:
        with self._lock:
            generation = self._snapshot.trace_generation + 1
            self._pending_seed = None
            self._history.clear()
            self._phase_history.clear()
            self._snapshot_history.clear()
            self._state = None
            self._snapshot = HighRatePhaseSnapshot(
                tracking_mode="RESET",
                trace_generation=generation,
                seed_status="WAITING",
                reason_codes=(reason,),
            )

    def metrics(self) -> dict[str, float]:
        with self._lock:
            snapshot = self._snapshot
            values = {
                "high_rate_phase_queue_drops": float(self._dropped_frames),
                "high_rate_phase_rate_skips": float(self._rate_skipped_frames),
            }
            if snapshot.processed_phase_fps is not None:
                values["high_rate_phase_fps"] = snapshot.processed_phase_fps
            if snapshot.fast_path_latency_ms is not None:
                values["high_rate_phase_latency_ms"] = snapshot.fast_path_latency_ms
            if snapshot.seed_age_ms is not None:
                values["high_rate_phase_seed_age_ms"] = snapshot.seed_age_ms
            return values

    def _trim_history(self, current: HighRatePhaseFrame) -> None:
        current_time = _timestamp_seconds(current.timestamp)
        if current_time is None:
            return
        maximum_age = self.config.phase_replay_window_ms / 1000.0
        while self._history:
            oldest_time = _timestamp_seconds(self._history[0].timestamp)
            if oldest_time is None or current_time - oldest_time <= maximum_age:
                break
            self._history.popleft()

    def _apply_seed(self, seed: StemPhaseSeed) -> None:
        scale = min(
            self.config.tracking_max_width_px / max(seed.axis.image_width, 1),
            1.0,
        )
        gray = _resize(seed.gray, scale, nearest=False)
        mask = _resize(seed.stem_mask, scale, nearest=True)
        axis = _scaled_axis(seed.axis, scale)
        mask = _erode_mask(mask, axis.median_width_px, self.config.mask_erosion_fraction)
        points = _detect_features(gray, mask, self.config)
        previous_raw = 0.0
        previous_translation_x_px = 0.0
        previous_translation_y_px = 0.0
        previous_state = self._state
        previous_generation = (
            previous_state.trace_generation
            if previous_state is not None
            else self._snapshot.trace_generation
        )
        track_changed = previous_state is not None and (
            previous_state.track_id != seed.track_id
            or previous_state.session_id != seed.session_id
        )
        if not track_changed:
            candidates = [item for item in self._phase_history if item[0] <= seed.frame_sequence]
            if candidates:
                previous_raw = candidates[-1][2]
            elif previous_state is not None:
                previous_raw = previous_state.cumulative_raw_phase
            if previous_state is not None:
                previous_translation_x_px = previous_state.cumulative_translation_x_px
                previous_translation_y_px = previous_state.cumulative_translation_y_px
        generation = previous_generation + int(track_changed)
        keyframes = (
            list(previous_state.keyframes or [])
            if previous_state is not None and not track_changed
            else []
        )
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
        self._phase_history.append((seed.frame_sequence, seed.timestamp, previous_raw))
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
            relocked, relock_reasons = self._try_relock(
                frame, gray, blur, seed_age_ms, source_gap
            )
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
        self._phase_history.append((frame.frame_sequence, frame.timestamp, cumulative))
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
            source = np.asarray(
                [keyframe.points[item.queryIdx] for item in good], dtype=np.float32
            )
            target = np.asarray(
                [current_points[item.trainIdx] for item in good], dtype=np.float32
            )
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
        self._phase_history.append((frame.frame_sequence, frame.timestamp, cumulative))
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
                round(
                    decision.candidate.matched_feature_count
                    * decision.candidate.inlier_ratio
                )
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


def _detect_features(
    gray: np.ndarray, mask: np.ndarray, config: HighRatePhaseConfig
) -> np.ndarray:
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
    kernel = _cv2().getStructuringElement(
        _cv2().MORPH_ELLIPSE, (kernel_size, kernel_size)
    )
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


def _integer(
    value: Mapping[str, Any], key: str, default: int, minimum: int, maximum: int
) -> int:
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
