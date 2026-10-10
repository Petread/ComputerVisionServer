# ruff: noqa: ANN401 -- OpenCV exposes dynamic Python bindings.

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
from cv_contracts.runtime import RuntimeNodeSpec

from cv_runtime.context import NodeInputs, RuntimeExecutionContext
from cv_runtime.handlers import NodeExecutionResult
from cv_runtime.payloads import (
    EventsPayload,
    FramePayload,
    OverlayPayload,
    OverlayPrimitive,
    ProvenanceLink,
    ResultPayload,
    RuntimeObjectReference,
    SegmentPayload,
    StemGeometryPayload,
    StemRotationFeatureQuality,
    StemRotationPayload,
    StemRotationTranslationComponent,
    TemporalFeatureFrame,
    TemporalSequencePayload,
)
from cv_runtime.stem_rotation import (
    ROTATION_ALGORITHM_VERSION,
    StemAxisObservation,
    StemRotationCalibrationProfile,
    StemRotationReasonCode,
    SurfaceFeatureMatch,
    SurfaceRotationEstimate,
    SurfaceRotationEstimationConfig,
    estimate_surface_rotation_from_matches,
    stem_rotation_calibration_from_config,
)
from cv_runtime.stem_temporal import (
    TEMPORAL_ANALYZER_VERSION,
    StemTargetCandidate,
    StemTemporalBaselineConfig,
    StemTemporalDecision,
    StemTemporalObservation,
    evaluate_projected_pre_entry_baseline,
)

_ROTATION_STATE_SCHEMA = "stem.surface-rotation.state.v1"
_TEMPORAL_STATE_SCHEMA = "stem.temporal-orientation.state.v1"
_ORIENTATION_RESULT_SCHEMA = "stem.orientation.v2"
_PROCESS_ZONE_SCHEMA = "stem.process-zones.v1"
_BASELINE_REASON = "rotation_baseline_initialized"
_TRACK_RESET_REASON = "rotation_track_changed_reset"
_FRAME_GAP_REASON = "rotation_frame_gap_discontinuity"
_FEATURE_LOSS_REASON = "rotation_feature_trace_lost"
_TEMPORAL_FEATURE_SET_ID = "stem_orientation.frame_features.v1"
_TEMPORAL_FEATURE_SCHEMA = "temporal.frame-features.v1"


@dataclass(frozen=True)
class _FeatureTrackingConfig:
    maximum_features: int
    quality_level: float
    minimum_distance_px: float
    block_size: int
    lk_window_size: int
    lk_max_level: int
    mask_erosion_fraction: float
    minimum_blur_score: float
    maximum_frame_gap: int
    maximum_time_gap_seconds: float
    estimation: SurfaceRotationEstimationConfig
    debug_overlay: bool


@dataclass(frozen=True)
class _FeatureTrackingResult:
    detected_feature_count: int
    matches: tuple[SurfaceFeatureMatch, ...]
    vectors: tuple[tuple[float, float, float, float], ...]


@dataclass(frozen=True)
class _NormalizedBox:
    x_min: float
    y_min: float
    x_max: float
    y_max: float

    def contains(self, point: tuple[float, float]) -> bool:
        return self.x_min <= point[0] <= self.x_max and self.y_min <= point[1] <= self.y_max


@dataclass(frozen=True)
class _TemporalNodeConfig:
    observation_entry_rois: tuple[_NormalizedBox, ...]
    throw_in_rois: tuple[_NormalizedBox, ...]
    decision_rois: tuple[_NormalizedBox, ...]
    first_roller_rois: tuple[_NormalizedBox, ...]
    process_zone_calibration_id: str
    process_zone_calibration_version: str
    process_zone_intrinsic_hash: str
    process_configuration_hash: str
    rotation_viewpoint_convention: str
    positive_rotation_direction: str
    maximum_observations: int
    baseline: StemTemporalBaselineConfig
    debug_overlay: bool


class StemSurfaceRotationHandler:
    node_type = "analysis.stem_surface_rotation"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        frame = inputs.get("frame")
        segments = inputs.get("segments")
        geometry = inputs.get("stem_geometry")
        if not isinstance(frame, FramePayload):
            generation = _break_rotation_trace(context, node=node, track_id=None)
            return _rotation_failure(
                node,
                reason="rotation_frame_missing",
                trace_discontinuity=True,
                trace_generation=generation,
            )
        if not isinstance(segments, SegmentPayload):
            generation = _break_rotation_trace(context, node=node, track_id=None)
            return _rotation_failure(
                node,
                reason="rotation_active_stem_missing",
                frame=frame,
                trace_discontinuity=True,
                trace_generation=generation,
            )
        if not isinstance(geometry, StemGeometryPayload):
            generation = _break_rotation_trace(context, node=node, track_id=None)
            return _rotation_failure(
                node,
                reason="rotation_geometry_missing",
                frame=frame,
                trace_discontinuity=True,
                trace_generation=generation,
            )
        try:
            calibration = stem_rotation_calibration_from_config(node.config or {})
            tracking_config = _feature_tracking_config(node.config or {})
        except (TypeError, ValueError) as exc:
            generation = _break_rotation_trace(
                context,
                node=node,
                track_id=geometry.track_id,
            )
            return _rotation_failure(
                node,
                reason="rotation_configuration_invalid",
                frame=frame,
                geometry=geometry,
                detail=str(exc),
                trace_discontinuity=True,
                trace_generation=generation,
            )
        if not calibration.valid:
            generation = _break_rotation_trace(
                context,
                node=node,
                track_id=geometry.track_id,
            )
            return _rotation_failure(
                node,
                reason_codes=list(calibration.reason_codes),
                frame=frame,
                geometry=geometry,
                calibration=calibration,
                trace_discontinuity=True,
                trace_generation=generation,
            )

        source_object = _active_stem_object(segments)
        track_id = source_object.track_id if source_object is not None else None
        if (
            len(segments.segments) != 1
            or source_object is None
            or not track_id
            or not geometry.valid
            or geometry.track_id != track_id
        ):
            generation = _break_rotation_trace(
                context,
                node=node,
                track_id=track_id,
            )
            reasons = ["rotation_active_stem_ambiguous"]
            if not geometry.valid:
                reasons.extend(geometry.reason_codes or ["rotation_geometry_invalid"])
            if geometry.track_id and track_id and geometry.track_id != track_id:
                reasons.append("rotation_track_id_mismatch")
            return _rotation_failure(
                node,
                reason_codes=reasons,
                frame=frame,
                geometry=geometry,
                calibration=calibration,
                track_id=track_id,
                source_object=source_object,
                trace_discontinuity=True,
                trace_generation=generation,
            )

        intrinsic_hash = _intrinsic_calibration_hash(frame, geometry)
        if intrinsic_hash != calibration.source_intrinsic_calibration_hash:
            generation = _break_rotation_trace(
                context,
                node=node,
                track_id=track_id,
            )
            return _rotation_failure(
                node,
                reason=StemRotationReasonCode.CALIBRATION_INTRINSIC_MISMATCH.value,
                frame=frame,
                geometry=geometry,
                calibration=calibration,
                track_id=track_id,
                source_object=source_object,
                intrinsic_hash=intrinsic_hash,
                trace_discontinuity=True,
                trace_generation=generation,
            )
        if not _is_undistorted_frame(frame):
            generation = _break_rotation_trace(
                context,
                node=node,
                track_id=track_id,
            )
            return _rotation_failure(
                node,
                reason="rotation_undistorted_frame_required",
                frame=frame,
                geometry=geometry,
                calibration=calibration,
                track_id=track_id,
                source_object=source_object,
                intrinsic_hash=intrinsic_hash,
                trace_discontinuity=True,
                trace_generation=generation,
            )
        if not _matching_image_contexts(frame, segments, geometry):
            generation = _break_rotation_trace(
                context,
                node=node,
                track_id=track_id,
            )
            return _rotation_failure(
                node,
                reason="rotation_image_context_mismatch",
                frame=frame,
                geometry=geometry,
                calibration=calibration,
                track_id=track_id,
                source_object=source_object,
                intrinsic_hash=intrinsic_hash,
                trace_discontinuity=True,
                trace_generation=generation,
            )

        try:
            cv2 = _opencv()
            gray = context.frame_analysis_cache.get(frame, "gray", lambda: _frame_gray(frame, cv2))
            axis = _axis_observation(geometry)
            if gray.shape[:2] != (axis.image_height, axis.image_width):
                raise ValueError("frame and stem geometry resolutions do not match")
            if segments.image_width not in {
                None,
                axis.image_width,
            } or segments.image_height not in {None, axis.image_height}:
                raise ValueError("segment and stem geometry resolutions do not match")
            mask = _segment_mask(
                segments,
                image_width=axis.image_width,
                image_height=axis.image_height,
                cv2=cv2,
                cache=context.frame_analysis_cache,
            )
            eroded_mask = _eroded_stem_mask(
                mask,
                median_width_px=axis.median_width_px,
                erosion_fraction=tracking_config.mask_erosion_fraction,
                cv2=cv2,
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            generation = _break_rotation_trace(
                context,
                node=node,
                track_id=track_id,
            )
            return _rotation_failure(
                node,
                reason="rotation_frame_or_mask_invalid",
                frame=frame,
                geometry=geometry,
                calibration=calibration,
                track_id=track_id,
                source_object=source_object,
                intrinsic_hash=intrinsic_hash,
                detail=str(exc),
                trace_discontinuity=True,
                trace_generation=generation,
            )

        try:
            frame_sequence = _rotation_frame_sequence(context, frame, geometry)
        except (TypeError, ValueError) as exc:
            generation = _break_rotation_trace(
                context,
                node=node,
                track_id=track_id,
            )
            return _rotation_failure(
                node,
                reason="rotation_frame_sequence_missing",
                frame=frame,
                geometry=geometry,
                calibration=calibration,
                track_id=track_id,
                source_object=source_object,
                intrinsic_hash=intrinsic_hash,
                trace_discontinuity=True,
                trace_generation=generation,
                detail=str(exc),
            )
        timestamp = geometry.timestamp or frame.timestamp or context.now_iso()
        blur_score = _blur_score(gray, cv2)
        eroded_fraction = _mask_fraction(eroded_mask, mask, cv2)
        state = context.state.setdefault(node.node_key, {})
        if not isinstance(state, dict):
            state = {}
            context.state[node.node_key] = state
        state.setdefault("schema_version", _ROTATION_STATE_SCHEMA)
        state.setdefault("trace_generation", 0)

        previous = state.get("previous")
        previous_track_id = str(state.get("track_id") or "") or None
        if previous_track_id is not None and previous_track_id != track_id:
            _reset_rotation_state(state, track_id=track_id, increment_generation=True)
            _store_rotation_baseline(
                state,
                gray=gray,
                eroded_mask=eroded_mask,
                axis=axis,
                frame_sequence=frame_sequence,
                timestamp=timestamp,
                blur_score=blur_score,
            )
            return _rotation_anchor(
                node,
                reason=_TRACK_RESET_REASON,
                frame=frame,
                geometry=geometry,
                calibration=calibration,
                track_id=track_id,
                source_object=source_object,
                intrinsic_hash=intrinsic_hash,
                cumulative_rotation_deg=0.0,
                trace_generation=int(state["trace_generation"]),
                eroded_mask_fraction=eroded_fraction,
            )
        state["track_id"] = track_id
        if not isinstance(previous, Mapping):
            _store_rotation_baseline(
                state,
                gray=gray,
                eroded_mask=eroded_mask,
                axis=axis,
                frame_sequence=frame_sequence,
                timestamp=timestamp,
                blur_score=blur_score,
            )
            return _rotation_anchor(
                node,
                reason=_BASELINE_REASON,
                frame=frame,
                geometry=geometry,
                calibration=calibration,
                track_id=track_id,
                source_object=source_object,
                intrinsic_hash=intrinsic_hash,
                cumulative_rotation_deg=0.0,
                trace_generation=int(state["trace_generation"]),
                eroded_mask_fraction=eroded_fraction,
            )

        if _rotation_trace_gap(
            previous,
            frame_sequence=frame_sequence,
            timestamp=timestamp,
            maximum_frame_gap=tracking_config.maximum_frame_gap,
            maximum_time_gap_seconds=tracking_config.maximum_time_gap_seconds,
        ):
            _reset_rotation_state(state, track_id=track_id, increment_generation=True)
            _store_rotation_baseline(
                state,
                gray=gray,
                eroded_mask=eroded_mask,
                axis=axis,
                frame_sequence=frame_sequence,
                timestamp=timestamp,
                blur_score=blur_score,
            )
            return _rotation_failure(
                node,
                reason=_FRAME_GAP_REASON,
                frame=frame,
                geometry=geometry,
                calibration=calibration,
                track_id=track_id,
                source_object=source_object,
                intrinsic_hash=intrinsic_hash,
                cumulative_rotation_deg=0.0,
                trace_discontinuity=True,
                trace_generation=int(state["trace_generation"]),
                eroded_mask_fraction=eroded_fraction,
            )
        if (
            blur_score < tracking_config.minimum_blur_score
            or float(previous.get("blur_score") or 0.0) < tracking_config.minimum_blur_score
        ):
            _reset_rotation_state(state, track_id=track_id, increment_generation=True)
            return _rotation_failure(
                node,
                reason="rotation_motion_blur",
                frame=frame,
                geometry=geometry,
                calibration=calibration,
                track_id=track_id,
                source_object=source_object,
                intrinsic_hash=intrinsic_hash,
                trace_discontinuity=True,
                trace_generation=int(state["trace_generation"]),
                current_blur_score=blur_score,
                previous_blur_score=float(previous.get("blur_score") or 0.0),
                eroded_mask_fraction=eroded_fraction,
            )

        previous_axis = previous.get("axis")
        previous_gray = previous.get("gray")
        previous_mask = previous.get("eroded_mask")
        if (
            not isinstance(previous_axis, StemAxisObservation)
            or not isinstance(previous_gray, np.ndarray)
            or not isinstance(previous_mask, np.ndarray)
        ):
            _reset_rotation_state(state, track_id=track_id, increment_generation=True)
            return _rotation_failure(
                node,
                reason="rotation_state_invalid",
                frame=frame,
                geometry=geometry,
                calibration=calibration,
                track_id=track_id,
                source_object=source_object,
                intrinsic_hash=intrinsic_hash,
                trace_discontinuity=True,
                trace_generation=int(state["trace_generation"]),
            )

        tracked = _track_surface_features(
            previous_gray,
            gray,
            previous_mask,
            eroded_mask,
            config=tracking_config,
            cv2=cv2,
        )
        estimate = estimate_surface_rotation_from_matches(
            tracked.matches,
            previous_axis=previous_axis,
            current_axis=axis,
            calibration=calibration,
            config=tracking_config.estimation,
        )
        if not estimate.valid or estimate.delta_rotation_deg is None:
            _reset_rotation_state(state, track_id=track_id, increment_generation=True)
            _store_rotation_baseline(
                state,
                gray=gray,
                eroded_mask=eroded_mask,
                axis=axis,
                frame_sequence=frame_sequence,
                timestamp=timestamp,
                blur_score=blur_score,
            )
            return _rotation_failure(
                node,
                reason_codes=[*estimate.reason_codes, _FEATURE_LOSS_REASON],
                frame=frame,
                geometry=geometry,
                calibration=calibration,
                track_id=track_id,
                source_object=source_object,
                intrinsic_hash=intrinsic_hash,
                trace_discontinuity=True,
                trace_generation=int(state["trace_generation"]),
                estimate=estimate,
                detected_feature_count=tracked.detected_feature_count,
                current_blur_score=blur_score,
                previous_blur_score=float(previous.get("blur_score") or 0.0),
                eroded_mask_fraction=eroded_fraction,
                vectors=tracked.vectors,
            )

        cumulative = float(state.get("cumulative_rotation_deg") or 0.0) + float(
            estimate.delta_rotation_deg
        )
        state["cumulative_rotation_deg"] = cumulative
        _store_rotation_baseline(
            state,
            gray=gray,
            eroded_mask=eroded_mask,
            axis=axis,
            frame_sequence=frame_sequence,
            timestamp=timestamp,
            blur_score=blur_score,
        )
        payload = _rotation_payload(
            valid=True,
            node=node,
            frame=frame,
            geometry=geometry,
            calibration=calibration,
            track_id=track_id,
            source_object=source_object,
            intrinsic_hash=intrinsic_hash,
            delta_rotation_deg=estimate.delta_rotation_deg,
            cumulative_rotation_deg=cumulative,
            trace_generation=int(state["trace_generation"]),
            estimate=estimate,
            detected_feature_count=tracked.detected_feature_count,
            current_blur_score=blur_score,
            previous_blur_score=float(previous.get("blur_score") or 0.0),
            eroded_mask_fraction=eroded_fraction,
        )
        outputs: dict[str, Any] = {"stem_rotation": payload}
        if tracking_config.debug_overlay:
            outputs["overlay"] = _rotation_overlay(
                payload,
                geometry=geometry,
                segments=segments,
                vectors=tracked.vectors,
                inlier_indices=set(estimate.inlier_match_indices),
            )
        return NodeExecutionResult(outputs=outputs)


class StemTemporalOrientationHandler:
    node_type = "analysis.stem_temporal_orientation"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        geometry = inputs.get("stem_geometry")
        rotation = inputs.get("stem_rotation")
        events = inputs.get("events")
        active_stem = inputs.get("active_stem")
        try:
            config = _temporal_node_config(node.config or {})
        except (TypeError, ValueError) as exc:
            return _temporal_result(
                node,
                context=context,
                config=None,
                geometry=geometry if isinstance(geometry, StemGeometryPayload) else None,
                rotation=rotation if isinstance(rotation, StemRotationPayload) else None,
                session_id=None,
                session_state="ABORTED",
                decision_ready=False,
                decision=None,
                reason_codes=["stem_temporal_configuration_invalid"],
                detail=str(exc),
            )
        if not isinstance(geometry, StemGeometryPayload) or not isinstance(
            rotation, StemRotationPayload
        ):
            return _temporal_result(
                node,
                context=context,
                config=config,
                geometry=geometry if isinstance(geometry, StemGeometryPayload) else None,
                rotation=rotation if isinstance(rotation, StemRotationPayload) else None,
                session_id=None,
                session_state="WAITING_FOR_LOG",
                decision_ready=False,
                decision=None,
                reason_codes=["stem_temporal_inputs_missing"],
            )

        track_id = geometry.track_id
        state = context.state.setdefault(node.node_key, {})
        if not isinstance(state, dict):
            state = {}
            context.state[node.node_key] = state
        state.setdefault("schema_version", _TEMPORAL_STATE_SCHEMA)
        state.setdefault("bounded_observation_buffer", [])
        state.setdefault("bounded_feature_buffer", [])
        state.setdefault("temporal_sequence_window_overflow", False)
        state.setdefault("trace_continuous", True)
        state.setdefault("calibration_valid", True)
        state.setdefault("rotation_trace_summary", {})
        state.setdefault("target_candidates", [])
        state.setdefault("best_target_candidate", None)
        state.setdefault("decision_frame", None)
        state.setdefault("frozen_pre_entry_estimate", None)
        state.setdefault("frozen_temporal_sequence", None)
        state.setdefault("post_rotation_verification", None)
        state.setdefault("last_recommendation", None)
        state.setdefault("lost_counter", 0)
        state.setdefault("reason_codes", [])

        if not track_id or rotation.track_id != track_id:
            _reset_temporal_state(state)
            return _temporal_result(
                node,
                context=context,
                config=config,
                geometry=geometry,
                rotation=rotation,
                session_id=None,
                session_state="ABORTED",
                decision_ready=False,
                decision=None,
                reason_codes=["stem_temporal_track_mismatch"],
            )
        if (
            geometry.frame_sequence is None
            or rotation.frame_sequence != geometry.frame_sequence
            or (
                geometry.image_context_id
                and rotation.image_context_id
                and geometry.image_context_id != rotation.image_context_id
            )
        ):
            _reset_temporal_state(state)
            return _temporal_result(
                node,
                context=context,
                config=config,
                geometry=geometry,
                rotation=rotation,
                session_id=None,
                session_state="ABORTED",
                decision_ready=False,
                decision=None,
                reason_codes=["stem_temporal_frame_identity_mismatch"],
            )
        if state.get("track_id") not in {None, track_id}:
            _reset_temporal_state(state)
            state["track_change_reset"] = True
        state["track_id"] = track_id

        convention_reasons = _temporal_convention_reasons(config, rotation)
        if convention_reasons:
            state["calibration_valid"] = False
        if rotation.trace_discontinuity:
            state["trace_continuous"] = False
            state.setdefault("reason_codes", []).append("rotation_trace_discontinuous")
        if not rotation.calibration_valid:
            state["calibration_valid"] = False

        position = _geometry_center_normalized(geometry)
        stage, zone_name = _process_stage(position, config)
        session_id = str(state.get("session_id") or "") or None
        if session_id is None and stage not in {0, 1}:
            return _temporal_result(
                node,
                context=context,
                config=config,
                geometry=geometry,
                rotation=rotation,
                session_id=None,
                session_state="WAITING_FOR_LOG",
                decision_ready=False,
                decision=None,
                reason_codes=["waiting_for_observation_entry"],
                current_zone=zone_name,
            )
        if session_id is None:
            session_id = _session_id(context, node=node, track_id=track_id, geometry=geometry)
            state.update(
                {
                    "session_id": session_id,
                    "session_state": "CANDIDATE_VISIBLE",
                    "started_at": geometry.timestamp or rotation.timestamp or context.now_iso(),
                    "trace_continuous": not rotation.trace_discontinuity,
                    "calibration_valid": rotation.calibration_valid and not convention_reasons,
                    "bounded_observation_buffer": [],
                    "bounded_feature_buffer": [],
                    "temporal_sequence_window_overflow": False,
                    "reason_codes": [],
                    "rotation_trace_summary": {},
                    "target_candidates": [],
                    "best_target_candidate": None,
                    "decision_frame": None,
                    "frozen_pre_entry_estimate": None,
                    "frozen_temporal_sequence": None,
                    "post_rotation_verification": None,
                    "last_recommendation": None,
                    "lost_counter": 0,
                }
            )

        frozen = state.get("frozen_pre_entry_estimate")
        if isinstance(frozen, Mapping):
            result_payload = ResultPayload(result=dict(frozen))
            outputs: dict[str, Any] = {"result": result_payload}
            frozen_sequence = state.get("frozen_temporal_sequence")
            if isinstance(frozen_sequence, Mapping):
                outputs["sequence"] = TemporalSequencePayload.model_validate(
                    dict(frozen_sequence)
                )
            if config.debug_overlay:
                outputs["overlay"] = _temporal_overlay(
                    result_payload.result,
                    config=config,
                    geometry=geometry,
                    current_zone=zone_name,
                )
            return NodeExecutionResult(outputs=outputs)

        observation = _temporal_observation(geometry, rotation, position)
        state["lost_counter"] = (
            0 if observation is not None else int(state.get("lost_counter") or 0) + 1
        )
        buffer = state.setdefault("bounded_observation_buffer", [])
        if not isinstance(buffer, list):
            buffer = []
            state["bounded_observation_buffer"] = buffer
        feature_buffer = state.setdefault("bounded_feature_buffer", [])
        if not isinstance(feature_buffer, list):
            feature_buffer = []
            state["bounded_feature_buffer"] = feature_buffer

        deadline_reached = stage is not None and stage >= 2
        if deadline_reached:
            buffered_observations = _observations_from_state(buffer)
            decision_observation = buffered_observations[-1] if buffered_observations else None
            decision_geometry, decision_rotation = _last_buffered_temporal_payloads(
                state,
                fallback_geometry=geometry,
                fallback_rotation=rotation,
            )
            decision = evaluate_projected_pre_entry_baseline(
                buffered_observations,
                decision_observation=decision_observation,
                calibration_valid=bool(state.get("calibration_valid", False)),
                trace_continuous=bool(state.get("trace_continuous", False)),
                config=config.baseline,
            )
            _update_temporal_state_summary(
                state,
                observations=buffered_observations,
                rotation=decision_rotation,
                decision=decision,
            )
            deadline_reasons = [*convention_reasons]
            if stage == 3:
                deadline_reasons.append("decision_line_missed_first_roller_reached")
            state["reason_codes"] = list(
                dict.fromkeys(
                    [
                        *[str(item) for item in state.get("reason_codes", [])],
                        *deadline_reasons,
                    ]
                )
            )
            result = _orientation_result(
                context,
                config=config,
                geometry=decision_geometry,
                rotation=decision_rotation,
                observations=buffered_observations,
                observation_started_at=str(state.get("started_at") or "") or None,
                trace_continuous=bool(state.get("trace_continuous", False)),
                session_id=session_id,
                session_state="PRE_ENTRY_DECISION_READY",
                decision_ready=True,
                decision=decision,
                extra_reason_codes=deadline_reasons,
                current_zone=zone_name,
            )
            sequence = _temporal_sequence_payload(
                state,
                geometry=decision_geometry,
                rotation=decision_rotation,
                config=config,
                session_id=session_id,
                decision_ready=True,
                decision_frame_index=(
                    decision_observation.frame_sequence
                    if decision_observation is not None
                    else None
                ),
            )
            state["session_state"] = "PRE_ENTRY_DECISION_READY"
            state["decision_frame"] = (
                _observation_to_state(decision_observation)
                if decision_observation is not None
                else None
            )
            state["frozen_pre_entry_estimate"] = result
            state["frozen_temporal_sequence"] = (
                sequence.model_dump(mode="json") if sequence is not None else None
            )
            state["last_recommendation"] = result.get("recommendation")
            output = _temporal_result_from_dict(
                result,
                config=config,
                geometry=geometry,
                current_zone=zone_name,
            )
            if sequence is not None:
                output.outputs["sequence"] = sequence
            return output

        if observation is not None:
            buffer.append(_observation_to_state(observation))
            feature_buffer.append(
                _temporal_feature_state(
                    geometry,
                    rotation,
                    active_stem if isinstance(active_stem, SegmentPayload) else None,
                    started_at=str(state.get("started_at") or "") or None,
                )
            )
            if len(buffer) > config.maximum_observations:
                state["temporal_sequence_window_overflow"] = True
                del buffer[: len(buffer) - config.maximum_observations]
                del feature_buffer[: len(feature_buffer) - config.maximum_observations]
            state["last_frame_sequence"] = observation.frame_sequence
            state["last_observation_at"] = observation.timestamp
        buffered_observations = _observations_from_state(buffer)
        provisional_decision = evaluate_projected_pre_entry_baseline(
            buffered_observations,
            decision_observation=buffered_observations[-1] if buffered_observations else None,
            calibration_valid=bool(state.get("calibration_valid", False)),
            trace_continuous=bool(state.get("trace_continuous", False)),
            config=config.baseline,
        )
        _update_temporal_state_summary(
            state,
            observations=buffered_observations,
            rotation=rotation,
            decision=provisional_decision,
        )
        if stage == 0:
            session_state = "ACTIVE_LOG_SELECTED"
        elif stage == 1:
            session_state = "THROW_IN_PROGRESS"
        else:
            session_state = "APPROACHING_DECISION_LINE"
        state["session_state"] = session_state
        transition_reasons = [*convention_reasons, *_event_reason_codes(events)]
        if bool(state.pop("track_change_reset", False)):
            transition_reasons.append("stem_temporal_track_changed_reset")
        result = _orientation_result(
            context,
            config=config,
            geometry=geometry,
            rotation=rotation,
            observations=buffered_observations,
            observation_started_at=str(state.get("started_at") or "") or None,
            trace_continuous=bool(state.get("trace_continuous", False)),
            session_id=session_id,
            session_state=session_state,
            decision_ready=False,
            decision=provisional_decision,
            extra_reason_codes=transition_reasons,
            current_zone=zone_name,
        )
        output = _temporal_result_from_dict(
            result,
            config=config,
            geometry=geometry,
            current_zone=zone_name,
        )
        sequence = _temporal_sequence_payload(
            state,
            geometry=geometry,
            rotation=rotation,
            config=config,
            session_id=session_id,
            decision_ready=False,
            decision_frame_index=None,
        )
        if sequence is not None:
            output.outputs["sequence"] = sequence
        return output


def _feature_tracking_config(config: Mapping[str, Any]) -> _FeatureTrackingConfig:
    return _FeatureTrackingConfig(
        maximum_features=_bounded_int(config, "maximum_features", 160, 16, 1000),
        quality_level=_bounded_float(config, "feature_quality_level", 0.02, 0.001, 0.5),
        minimum_distance_px=_bounded_float(config, "feature_minimum_distance_px", 7.0, 1.0, 100.0),
        block_size=_bounded_int(config, "feature_block_size", 7, 3, 31),
        lk_window_size=_bounded_int(config, "lk_window_size", 21, 5, 61),
        lk_max_level=_bounded_int(config, "lk_max_level", 3, 0, 8),
        mask_erosion_fraction=_bounded_float(config, "mask_erosion_fraction", 0.18, 0.02, 0.45),
        minimum_blur_score=_bounded_float(config, "minimum_blur_score", 20.0, 0.0, 100_000.0),
        maximum_frame_gap=_bounded_int(config, "maximum_frame_gap", 2, 1, 30),
        maximum_time_gap_seconds=_bounded_float(
            config, "maximum_time_gap_seconds", 2.0, 0.01, 30.0
        ),
        estimation=SurfaceRotationEstimationConfig(
            minimum_matched_features=_bounded_int(config, "minimum_matched_features", 8, 3, 500),
            minimum_inlier_ratio=_bounded_float(config, "minimum_inlier_ratio", 0.55, 0.0, 1.0),
            maximum_tracking_error_px=_bounded_float(
                config, "maximum_tracking_error_px", 20.0, 0.1, 500.0
            ),
            maximum_longitudinal_shift_fraction=_bounded_float(
                config, "maximum_longitudinal_shift_fraction", 0.18, 0.01, 1.0
            ),
            absolute_inlier_tolerance_normalized=_bounded_float(
                config,
                "absolute_inlier_tolerance_normalized",
                0.025,
                0.001,
                0.5,
            ),
            mad_multiplier=_bounded_float(config, "inlier_mad_multiplier", 3.5, 1.0, 10.0),
            maximum_abs_delta_rotation_deg=_bounded_float(
                config, "maximum_abs_delta_rotation_deg", 60.0, 1.0, 179.0
            ),
            minimum_confidence=_bounded_float(
                config, "minimum_rotation_confidence", 0.35, 0.0, 1.0
            ),
        ),
        debug_overlay=bool(config.get("debug_overlay", True)),
    )


def _temporal_node_config(config: Mapping[str, Any]) -> _TemporalNodeConfig:
    entry = _normalized_rois(config.get("observation_entry_roi"))
    throw_in = _normalized_rois(config.get("throw_in_roi"))
    decision = _normalized_rois(config.get("decision_roi"))
    first_roller = _normalized_rois(config.get("first_roller_roi"))
    if not entry or not throw_in or not decision or not first_roller:
        raise ValueError(
            "observation_entry_roi, throw_in_roi, decision_roi, and first_roller_roi "
            "must each contain at least one normalized ROI"
        )
    calibration_id = str(config.get("process_zone_calibration_id") or "").strip()
    calibration_version = str(config.get("process_zone_calibration_version") or "").strip()
    intrinsic_hash = str(config.get("process_zone_intrinsic_hash") or "").strip()
    viewpoint = str(config.get("rotation_viewpoint_convention") or "").strip()
    positive_direction = str(config.get("positive_rotation_direction") or "").strip()
    if not calibration_id or not calibration_version or not intrinsic_hash:
        raise ValueError("process-zone calibration ID, version, and intrinsic hash are required")
    if viewpoint not in {
        "viewed_from_configured_log_end_a",
        "viewed_from_configured_log_end_b",
    }:
        raise ValueError("rotation_viewpoint_convention is invalid")
    if positive_direction not in {
        "clockwise_viewed_from_configured_log_end",
        "counter_clockwise_viewed_from_configured_log_end",
    }:
        raise ValueError("positive_rotation_direction is invalid")
    process_hash = _process_configuration_hash(
        entry,
        throw_in,
        decision,
        first_roller,
        calibration_id=calibration_id,
        calibration_version=calibration_version,
        intrinsic_hash=intrinsic_hash,
        viewpoint=viewpoint,
        positive_direction=positive_direction,
    )
    return _TemporalNodeConfig(
        observation_entry_rois=entry,
        throw_in_rois=throw_in,
        decision_rois=decision,
        first_roller_rois=first_roller,
        process_zone_calibration_id=calibration_id,
        process_zone_calibration_version=calibration_version,
        process_zone_intrinsic_hash=intrinsic_hash,
        process_configuration_hash=process_hash,
        rotation_viewpoint_convention=viewpoint,
        positive_rotation_direction=positive_direction,
        maximum_observations=_bounded_int(config, "maximum_observations", 128, 8, 4096),
        baseline=StemTemporalBaselineConfig(
            minimum_observation_count=_bounded_int(config, "minimum_observation_count", 4, 3, 64),
            minimum_observed_rotation_deg=_bounded_float(
                config, "minimum_observed_rotation_deg", 20.0, 1.0, 179.0
            ),
            minimum_rotation_confidence=_bounded_float(
                config, "minimum_rotation_confidence", 0.45, 0.0, 1.0
            ),
            minimum_geometry_quality=_bounded_float(
                config, "minimum_geometry_quality", 0.5, 0.0, 1.0
            ),
            minimum_visible_fraction=_bounded_float(
                config, "minimum_visible_fraction", 0.3, 0.0, 1.0
            ),
            minimum_bow_relevance=_bounded_float(config, "minimum_bow_relevance", 0.12, 0.0, 10.0),
            minimum_target_improvement=_bounded_float(
                config, "minimum_target_improvement", 0.04, 0.0, 10.0
            ),
            target_ambiguity_margin=_bounded_float(
                config, "target_ambiguity_margin", 0.08, 0.0, 1.0
            ),
            target_merge_distance_deg=_bounded_float(
                config, "target_merge_distance_deg", 5.0, 0.0, 45.0
            ),
            minimum_result_confidence=_bounded_float(
                config, "minimum_result_confidence", 0.5, 0.0, 1.0
            ),
            deadband_deg=_bounded_float(config, "deadband_deg", 5.0, 0.0, 45.0),
        ),
        debug_overlay=bool(config.get("debug_overlay", True)),
    )


def _track_surface_features(
    previous_gray: np.ndarray,
    current_gray: np.ndarray,
    previous_mask: np.ndarray,
    current_mask: np.ndarray,
    *,
    config: _FeatureTrackingConfig,
    cv2: Any,
) -> _FeatureTrackingResult:
    features = cv2.goodFeaturesToTrack(
        previous_gray,
        maxCorners=config.maximum_features,
        qualityLevel=config.quality_level,
        minDistance=config.minimum_distance_px,
        mask=previous_mask,
        blockSize=config.block_size,
    )
    if features is None or len(features) == 0:
        return _FeatureTrackingResult(0, (), ())
    current, status, errors = cv2.calcOpticalFlowPyrLK(
        previous_gray,
        current_gray,
        features,
        None,
        winSize=(config.lk_window_size, config.lk_window_size),
        maxLevel=config.lk_max_level,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
    )
    if current is None or status is None:
        return _FeatureTrackingResult(len(features), (), ())
    matches: list[SurfaceFeatureMatch] = []
    vectors: list[tuple[float, float, float, float]] = []
    flat_errors = errors.reshape(-1) if errors is not None else np.zeros(len(features))
    for previous_point, current_point, keep, error in zip(
        features.reshape(-1, 2),
        current.reshape(-1, 2),
        status.reshape(-1),
        flat_errors,
        strict=True,
    ):
        if not bool(keep):
            continue
        x = int(round(float(current_point[0])))
        y = int(round(float(current_point[1])))
        if not (0 <= x < current_mask.shape[1] and 0 <= y < current_mask.shape[0]):
            continue
        if int(current_mask[y, x]) == 0:
            continue
        match = SurfaceFeatureMatch(
            previous_x_px=float(previous_point[0]),
            previous_y_px=float(previous_point[1]),
            current_x_px=float(current_point[0]),
            current_y_px=float(current_point[1]),
            tracking_error_px=max(float(error), 0.0),
        )
        matches.append(match)
        vectors.append(
            (
                match.previous_x_px,
                match.previous_y_px,
                match.current_x_px,
                match.current_y_px,
            )
        )
    return _FeatureTrackingResult(len(features), tuple(matches), tuple(vectors))


def _rotation_payload(
    *,
    valid: bool,
    node: RuntimeNodeSpec,
    frame: FramePayload | None,
    geometry: StemGeometryPayload | None,
    calibration: StemRotationCalibrationProfile | None,
    track_id: str | None,
    source_object: RuntimeObjectReference | None,
    intrinsic_hash: str | None,
    delta_rotation_deg: float | None = None,
    cumulative_rotation_deg: float | None = None,
    trace_discontinuity: bool = False,
    trace_generation: int = 0,
    estimate: SurfaceRotationEstimate | None = None,
    confidence_override: float | None = None,
    detected_feature_count: int = 0,
    current_blur_score: float | None = None,
    previous_blur_score: float | None = None,
    eroded_mask_fraction: float = 0.0,
    reason_codes: Sequence[str] = (),
    detail: str | None = None,
) -> StemRotationPayload:
    calibration_applies = bool(
        calibration
        and calibration.valid
        and intrinsic_hash == calibration.source_intrinsic_calibration_hash
    )
    confidence = (
        estimate.rotation_confidence
        if estimate is not None
        else min(max(float(confidence_override or 0.0), 0.0), 1.0)
    )
    direction = "unknown"
    if valid and delta_rotation_deg is not None:
        if abs(delta_rotation_deg) < 1e-6:
            direction = "stationary"
        else:
            direction = "positive" if delta_rotation_deg > 0 else "negative"
    provenance: list[ProvenanceLink] = []
    if geometry is not None and geometry.image_context_id:
        provenance.append(
            ProvenanceLink(
                source_kind="image_context",
                source_id=geometry.image_context_id,
                relationship="surface_rotation_observed_in_undistorted_frame",
                node_key=node.node_key,
            )
        )
    if source_object is not None:
        provenance.append(
            ProvenanceLink(
                source_kind="object",
                source_id=source_object.object_id,
                relationship="surface_rotation_tracked_for_active_stem",
                node_key=node.node_key,
            )
        )
    if calibration is not None and calibration.calibration_id:
        provenance.append(
            ProvenanceLink(
                source_kind="operator_config",
                source_id=calibration.calibration_id,
                relationship="uses_validated_stem_rotation_calibration",
                node_key=node.node_key,
                metadata={
                    "calibration_hash": calibration.calibration_hash,
                    "method": calibration.method,
                    "version": calibration.version,
                },
            )
        )
    return StemRotationPayload(
        valid=valid,
        track_id=track_id,
        source_object_id=source_object.object_id if source_object is not None else None,
        image_context_id=(geometry.image_context_id if geometry is not None else None),
        frame_sequence=(geometry.frame_sequence if geometry is not None else None),
        timestamp=(
            geometry.timestamp if geometry is not None else frame.timestamp if frame else None
        ),
        delta_rotation_deg=delta_rotation_deg,
        cumulative_rotation_deg_unwrapped=cumulative_rotation_deg,
        rotation_direction=direction,
        rotation_confidence=confidence,
        detected_feature_count=detected_feature_count,
        matched_feature_count=estimate.matched_feature_count if estimate else 0,
        inlier_feature_count=estimate.inlier_feature_count if estimate else 0,
        inlier_ratio=estimate.inlier_ratio if estimate else 0.0,
        feature_quality=StemRotationFeatureQuality(
            detected_feature_count=detected_feature_count,
            matched_feature_count=estimate.matched_feature_count if estimate else 0,
            inlier_feature_count=estimate.inlier_feature_count if estimate else 0,
            inlier_ratio=estimate.inlier_ratio if estimate else 0.0,
            median_tracking_error_px=(estimate.median_tracking_error_px if estimate else None),
            previous_blur_score=previous_blur_score,
            current_blur_score=current_blur_score,
            eroded_mask_fraction=eroded_mask_fraction,
            quality_score=confidence,
        ),
        translation_component=StemRotationTranslationComponent(
            x_px=estimate.translation_x_px if estimate else 0.0,
            y_px=estimate.translation_y_px if estimate else 0.0,
            magnitude_px=estimate.translation_magnitude_px if estimate else 0.0,
        ),
        compensated_axis_rotation_deg=estimate.axis_rotation_deg if estimate else 0.0,
        trace_discontinuity=trace_discontinuity,
        trace_generation=trace_generation,
        calibration_valid=calibration_applies,
        calibration_id=calibration.calibration_id if calibration else None,
        calibration_version=calibration.version if calibration else None,
        calibration_method=calibration.method if calibration else None,
        calibration_hash=calibration.calibration_hash if calibration else None,
        intrinsic_calibration_hash=intrinsic_hash,
        rotation_viewpoint_convention=(
            calibration.rotation_viewpoint_convention if calibration else None
        ),
        positive_rotation_direction=(
            calibration.positive_rotation_direction if calibration else None
        ),
        algorithm_version=ROTATION_ALGORITHM_VERSION,
        reason_codes=list(dict.fromkeys(str(reason) for reason in reason_codes)),
        provenance=provenance,
        metadata={
            "node_key": node.node_key,
            "projected_surface_motion_only": True,
            "metric_angle_calibrated": calibration_applies,
            "single_feature_angle_claimed": False,
            "detail": detail,
        },
    )


def _rotation_failure(
    node: RuntimeNodeSpec,
    *,
    reason: str | None = None,
    reason_codes: Sequence[str] = (),
    frame: FramePayload | None = None,
    geometry: StemGeometryPayload | None = None,
    calibration: StemRotationCalibrationProfile | None = None,
    track_id: str | None = None,
    source_object: RuntimeObjectReference | None = None,
    intrinsic_hash: str | None = None,
    cumulative_rotation_deg: float | None = None,
    trace_discontinuity: bool = False,
    trace_generation: int = 0,
    estimate: SurfaceRotationEstimate | None = None,
    detected_feature_count: int = 0,
    current_blur_score: float | None = None,
    previous_blur_score: float | None = None,
    eroded_mask_fraction: float = 0.0,
    vectors: Sequence[tuple[float, float, float, float]] = (),
    detail: str | None = None,
) -> NodeExecutionResult:
    reasons = [*reason_codes]
    if reason:
        reasons.append(reason)
    payload = _rotation_payload(
        valid=False,
        node=node,
        frame=frame,
        geometry=geometry,
        calibration=calibration,
        track_id=track_id,
        source_object=source_object,
        intrinsic_hash=intrinsic_hash,
        cumulative_rotation_deg=cumulative_rotation_deg,
        trace_discontinuity=trace_discontinuity,
        trace_generation=trace_generation,
        estimate=estimate,
        detected_feature_count=detected_feature_count,
        current_blur_score=current_blur_score,
        previous_blur_score=previous_blur_score,
        eroded_mask_fraction=eroded_mask_fraction,
        reason_codes=reasons,
        detail=detail,
    )
    outputs: dict[str, Any] = {"stem_rotation": payload}
    if bool((node.config or {}).get("debug_overlay", True)):
        outputs["overlay"] = _rotation_overlay(
            payload,
            geometry=geometry,
            segments=None,
            vectors=vectors,
            inlier_indices=set(estimate.inlier_match_indices) if estimate else set(),
        )
    return NodeExecutionResult(outputs=outputs)


def _rotation_anchor(
    node: RuntimeNodeSpec,
    *,
    reason: str,
    frame: FramePayload,
    geometry: StemGeometryPayload,
    calibration: StemRotationCalibrationProfile,
    track_id: str,
    source_object: RuntimeObjectReference,
    intrinsic_hash: str,
    cumulative_rotation_deg: float,
    trace_generation: int,
    eroded_mask_fraction: float,
) -> NodeExecutionResult:
    """Publish the calibrated zero reference without inventing an inter-frame delta."""
    payload = _rotation_payload(
        valid=True,
        node=node,
        frame=frame,
        geometry=geometry,
        calibration=calibration,
        track_id=track_id,
        source_object=source_object,
        intrinsic_hash=intrinsic_hash,
        cumulative_rotation_deg=cumulative_rotation_deg,
        trace_generation=trace_generation,
        confidence_override=geometry.quality_score,
        eroded_mask_fraction=eroded_mask_fraction,
        reason_codes=[reason],
    )
    outputs: dict[str, Any] = {"stem_rotation": payload}
    if bool((node.config or {}).get("debug_overlay", True)):
        outputs["overlay"] = _rotation_overlay(
            payload,
            geometry=geometry,
            segments=None,
            vectors=(),
            inlier_indices=set(),
        )
    return NodeExecutionResult(outputs=outputs)


def _rotation_overlay(
    payload: StemRotationPayload,
    *,
    geometry: StemGeometryPayload | None,
    segments: SegmentPayload | None,
    vectors: Sequence[tuple[float, float, float, float]],
    inlier_indices: set[int],
) -> OverlayPayload:
    primitives: list[OverlayPrimitive] = []
    common = {
        "calibration_id": payload.calibration_id,
        "calibration_hash": payload.calibration_hash,
        "rotation_viewpoint_convention": payload.rotation_viewpoint_convention,
        "positive_rotation_direction": payload.positive_rotation_direction,
        "reason_codes": payload.reason_codes,
    }
    if segments is not None and len(segments.segments) == 1:
        primitives.append(
            OverlayPrimitive(
                primitive_type="polygon",
                layer="stem_rotation_mask",
                label="Robust eroded tracking is restricted inside this active mask",
                geometry={
                    "points": [
                        {"x": float(point.x), "y": float(point.y)}
                        for point in segments.segments[0].polygon
                    ],
                    "coordinate_system": "normalized_polygon",
                },
                image_context_id=payload.image_context_id,
                style={"stroke": "#00bcd4", "fill": "rgba(0,188,212,0.05)"},
                metadata=common,
            )
        )
    if geometry is not None and geometry.reference_axis is not None:
        primitives.append(
            OverlayPrimitive(
                primitive_type="axis",
                layer="stem_rotation_axis",
                label="Translation/axis-rotation compensation reference",
                geometry={
                    "start": geometry.reference_axis.start.model_dump(mode="json"),
                    "end": geometry.reference_axis.end.model_dump(mode="json"),
                    "coordinate_system": "pixel_xy",
                },
                image_context_id=payload.image_context_id,
                style={"stroke": "#ffca28", "stroke_width": 2},
                metadata=common,
            )
        )
    for index, vector in enumerate(vectors[:40]):
        primitives.append(
            OverlayPrimitive(
                primitive_type="arrow",
                layer="stem_rotation_features",
                geometry={
                    "start": {"x": vector[0], "y": vector[1]},
                    "end": {"x": vector[2], "y": vector[3]},
                    "coordinate_system": "pixel_xy",
                },
                image_context_id=payload.image_context_id,
                style={
                    "stroke": "#66bb6a" if index in inlier_indices else "#ef5350",
                    "stroke_width": 1,
                },
                metadata={**common, "inlier": index in inlier_indices},
            )
        )
    center = _geometry_center_px(geometry)
    label = (
        f"relative rotation {payload.cumulative_rotation_deg_unwrapped:.1f} deg"
        if payload.valid and payload.cumulative_rotation_deg_unwrapped is not None
        else f"rotation unavailable: {', '.join(payload.reason_codes) or 'unknown'}"
    )
    primitives.append(
        OverlayPrimitive(
            primitive_type="point",
            layer="stem_rotation_status",
            label=(
                f"{label}; positive={payload.positive_rotation_direction}; "
                f"view={payload.rotation_viewpoint_convention}"
            ),
            confidence=payload.rotation_confidence,
            geometry={"x": center[0], "y": center[1], "coordinate_system": "pixel_xy"},
            image_context_id=payload.image_context_id,
            style={"stroke": "#66bb6a" if payload.valid else "#ff7043", "radius": 4},
            metadata=common,
        )
    )
    return OverlayPayload(
        primitives=primitives,
        display_context_id=payload.image_context_id,
        metadata={
            "schema_version": payload.schema_version,
            **common,
        },
    )


def _orientation_result(
    context: RuntimeExecutionContext,
    *,
    config: _TemporalNodeConfig,
    geometry: StemGeometryPayload,
    rotation: StemRotationPayload,
    observations: Sequence[StemTemporalObservation],
    observation_started_at: str | None,
    trace_continuous: bool,
    session_id: str,
    session_state: str,
    decision_ready: bool,
    decision: StemTemporalDecision,
    extra_reason_codes: Sequence[str],
    current_zone: str | None,
) -> dict[str, Any]:
    correction = decision.correction_deg if decision_ready and decision.valid_angle else None
    physical_direction = _physical_direction(
        decision.direction_sign if correction is not None else 0,
        config.positive_rotation_direction,
        valid=correction is not None,
    )
    target = _target_candidate_dict(decision.target_candidate)
    decision_frame = (
        {
            "frame_sequence": decision.decision_observation.frame_sequence,
            "timestamp": decision.decision_observation.timestamp,
            "cumulative_rotation_deg": (decision.decision_observation.cumulative_rotation_deg),
        }
        if decision.decision_observation is not None
        else None
    )
    reasons = list(dict.fromkeys([*decision.reason_codes, *extra_reason_codes]))
    compact_trace = _compact_temporal_trace(observations)
    selected_frames = _selected_frame_references(
        decision_frame=decision_frame,
        target=target,
        image_context_id=geometry.image_context_id,
    )
    result = {
        "schema_version": _ORIENTATION_RESULT_SCHEMA,
        "session_id": session_id,
        "track_id": geometry.track_id,
        "channel_id": context.channel_id,
        "pipeline_version_id": context.pipeline_version_id,
        "observation_started_at": observation_started_at,
        "decision_timestamp": decision_frame.get("timestamp") if decision_frame else None,
        "session_state": session_state,
        "operating_mode": "shadow",
        "decision_ready": decision_ready,
        "recommended_rotation_deg_signed": correction,
        "recommended_rotation_deg_abs": abs(correction) if correction is not None else None,
        "recommended_direction": physical_direction,
        "target_frame_or_interval_candidate": target,
        "decision_frame": decision_frame,
        "target_observed": decision.target_observed,
        "bow_relevant": decision.bow_relevant,
        "confidence": decision.confidence if decision_ready else 0.0,
        "estimate_method": "projected_baseline",
        "reason_codes": reasons,
        "decision": {
            "ready": decision_ready,
            "frame_sequence": (
                decision.decision_observation.frame_sequence
                if decision.decision_observation is not None
                else None
            ),
            "timestamp": (
                decision.decision_observation.timestamp
                if decision.decision_observation is not None
                else None
            ),
            "valid_until": None,
            "status": decision.status if decision_ready else "observing",
            "before_first_roller": current_zone != "first_roller",
        },
        "recommendation": {
            "rotate_required": correction is not None and abs(correction) > 0,
            "signed_rotation_deg": correction,
            "absolute_rotation_deg": abs(correction) if correction is not None else None,
            "direction": physical_direction,
            "rotation_viewpoint_convention": config.rotation_viewpoint_convention,
            "positive_rotation_direction": config.positive_rotation_direction,
            "deadband_deg": config.baseline.deadband_deg,
        },
        "target_estimate": {
            "observed": decision.target_observed,
            "frame_start": target.get("frame_start") if target else None,
            "frame_end": target.get("frame_end") if target else None,
            "target_rotation_deg": target.get("rotation_deg") if target else None,
            "estimate_method": "projected_baseline",
            "candidate_type": target.get("candidate_type") if target else None,
        },
        "current_observation": {
            "frame_sequence": geometry.frame_sequence,
            "signed_bow_normalized": geometry.signed_bow_normalized,
            "rms_residual_normalized": geometry.rms_residual_normalized,
            "geometry_quality": geometry.quality_score,
            "visible_fraction": geometry.visible_fraction,
            "shape_class": geometry.shape_class,
            "cumulative_rotation_deg": rotation.cumulative_rotation_deg_unwrapped,
            "rotation_confidence": rotation.rotation_confidence,
            "current_zone": current_zone,
        },
        "temporal_diagnostics": {
            "observed_rotation_span_deg": decision.observed_rotation_span_deg,
            "improvement_observed": decision.improvement_observed,
            "worsening_observed": decision.worsening_observed,
            "sign_change_observed": decision.sign_change_observed,
            "projected_minimum_bracketed": decision.projected_minimum_bracketed,
            "target_candidates": [
                _target_candidate_dict(candidate) for candidate in decision.target_candidates
            ],
            "trace_discontinuity": not trace_continuous,
            "rotation_trace_generation": rotation.trace_generation,
        },
        "compact_curvature_trace": {
            "source_observation_count": len(observations),
            "sample_count": len(compact_trace),
            "samples": [
                {
                    "frame_sequence": item["frame_sequence"],
                    "timestamp": item["timestamp"],
                    "signed_bow_normalized": item["signed_bow_normalized"],
                    "rms_residual_normalized": item["rms_residual_normalized"],
                    "geometry_quality": item["geometry_quality"],
                    "visible_fraction": item["visible_fraction"],
                    "shape_class": item["shape_class"],
                }
                for item in compact_trace
            ],
        },
        "compact_rotation_trace": {
            "source_observation_count": len(observations),
            "sample_count": len(compact_trace),
            "trace_generation": rotation.trace_generation,
            "trace_continuous": trace_continuous,
            "samples": [
                {
                    "frame_sequence": item["frame_sequence"],
                    "timestamp": item["timestamp"],
                    "cumulative_rotation_deg": item["cumulative_rotation_deg"],
                    "rotation_confidence": item["rotation_confidence"],
                    "inlier_ratio": item["inlier_ratio"],
                }
                for item in compact_trace
            ],
        },
        "post_rotation_verification": None,
        "versions": {
            "pipeline_version_id": context.pipeline_version_id,
            "intrinsic_calibration_hash": rotation.intrinsic_calibration_hash,
            "rotation_calibration_id": rotation.calibration_id,
            "rotation_calibration_version": rotation.calibration_version,
            "rotation_calibration_hash": rotation.calibration_hash,
            "rotation_calibration_method": rotation.calibration_method,
            "process_zone_calibration_id": config.process_zone_calibration_id,
            "process_zone_calibration_version": config.process_zone_calibration_version,
            "process_configuration_hash": config.process_configuration_hash,
            "segmentation_model_version_id": geometry.segmentation_route.get("model_version_id"),
            "feature_schema_version": geometry.feature_schema_version,
            "temporal_feature_schema_version": _TEMPORAL_FEATURE_SCHEMA,
            "rotation_schema_version": rotation.schema_version,
            "rotation_algorithm_version": rotation.algorithm_version,
            "analyzer_version": TEMPORAL_ANALYZER_VERSION,
            "temporal_model_version_id": None,
        },
        "direction_provenance": {
            "rotation_viewpoint_convention": config.rotation_viewpoint_convention,
            "positive_rotation_direction": config.positive_rotation_direction,
            "calibration_id": rotation.calibration_id,
            "calibration_method": rotation.calibration_method,
            "sign_semantics": (
                "positive correction follows positive_rotation_direction when viewed "
                "from rotation_viewpoint_convention"
            ),
        },
        "artifact_provenance": {
            "rotation_trace_schema": rotation.schema_version,
            "rotation_calibration_id": rotation.calibration_id,
            "rotation_calibration_hash": rotation.calibration_hash,
            "process_configuration_hash": config.process_configuration_hash,
            "inline_frames_or_masks_stored": False,
        },
        "asset_references": {
            "raw_video_ref": None,
            "diagnostic_video_ref": None,
            "feature_trace_ref": None,
            "rotation_trace_ref": None,
            "selected_frames": selected_frames,
            "status": "not_recorded",
            "reason_codes": ["temporal_asset_recorder_not_configured"],
        },
    }
    return result


def _compact_temporal_trace(
    observations: Sequence[StemTemporalObservation],
    *,
    maximum_samples: int = 32,
) -> list[dict[str, Any]]:
    """Return a deterministic bounded trace while retaining both endpoints."""
    if not observations:
        return []
    maximum_samples = max(int(maximum_samples), 2)
    if len(observations) <= maximum_samples:
        selected = list(observations)
    else:
        last_index = len(observations) - 1
        indices = {
            int(round(index * last_index / (maximum_samples - 1)))
            for index in range(maximum_samples)
        }
        selected = [observations[index] for index in sorted(indices)]
    return [_observation_to_state(observation) for observation in selected]


def _selected_frame_references(
    *,
    decision_frame: Mapping[str, Any] | None,
    target: Mapping[str, Any] | None,
    image_context_id: str | None,
) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    if decision_frame is not None:
        references.append(
            {
                "role": "decision",
                "frame_sequence": decision_frame.get("frame_sequence"),
                "timestamp": decision_frame.get("timestamp"),
                "image_context_id": image_context_id,
                "asset_ref": None,
            }
        )
    if target is not None:
        for role, key in (("target_start", "frame_start"), ("target_end", "frame_end")):
            sequence = target.get(key)
            if sequence is None:
                continue
            if any(
                item.get("role") == "target_start" and item.get("frame_sequence") == sequence
                for item in references
            ):
                continue
            references.append(
                {
                    "role": role,
                    "frame_sequence": sequence,
                    "timestamp": None,
                    "image_context_id": image_context_id,
                    "asset_ref": None,
                }
            )
    return references


def _temporal_result(
    node: RuntimeNodeSpec,
    *,
    context: RuntimeExecutionContext,
    config: _TemporalNodeConfig | None,
    geometry: StemGeometryPayload | None,
    rotation: StemRotationPayload | None,
    session_id: str | None,
    session_state: str,
    decision_ready: bool,
    decision: StemTemporalDecision | None,
    reason_codes: Sequence[str],
    current_zone: str | None = None,
    detail: str | None = None,
) -> NodeExecutionResult:
    result = {
        "schema_version": _ORIENTATION_RESULT_SCHEMA,
        "session_id": session_id,
        "track_id": geometry.track_id if geometry else rotation.track_id if rotation else None,
        "session_state": session_state,
        "operating_mode": "shadow",
        "decision_ready": decision_ready,
        "recommended_rotation_deg_signed": None,
        "recommended_rotation_deg_abs": None,
        "recommended_direction": "manual_required",
        "target_frame_or_interval_candidate": None,
        "decision_frame": None,
        "target_observed": False,
        "bow_relevant": False,
        "confidence": 0.0,
        "estimate_method": "projected_baseline",
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "detail": detail,
        "decision": {"ready": decision_ready, "status": "manual_required"},
        "recommendation": {
            "signed_rotation_deg": None,
            "absolute_rotation_deg": None,
            "direction": "manual_required",
            "rotation_viewpoint_convention": (
                config.rotation_viewpoint_convention if config else None
            ),
            "positive_rotation_direction": (config.positive_rotation_direction if config else None),
        },
        "target_estimate": {"observed": False, "estimate_method": "projected_baseline"},
        "current_observation": {"current_zone": current_zone},
        "versions": {
            "pipeline_version_id": context.pipeline_version_id,
            "rotation_schema_version": rotation.schema_version if rotation else None,
            "analyzer_version": TEMPORAL_ANALYZER_VERSION,
        },
    }
    return _temporal_result_from_dict(
        result,
        config=config,
        geometry=geometry,
        current_zone=current_zone,
    )


def _temporal_result_from_dict(
    result: dict[str, Any],
    *,
    config: _TemporalNodeConfig | None,
    geometry: StemGeometryPayload | None,
    current_zone: str | None,
) -> NodeExecutionResult:
    payload = ResultPayload(result=result)
    outputs: dict[str, Any] = {"result": payload}
    if config is not None and config.debug_overlay:
        outputs["overlay"] = _temporal_overlay(
            result,
            config=config,
            geometry=geometry,
            current_zone=current_zone,
        )
    return NodeExecutionResult(outputs=outputs)


def _temporal_overlay(
    result: Mapping[str, Any],
    *,
    config: _TemporalNodeConfig,
    geometry: StemGeometryPayload | None,
    current_zone: str | None,
) -> OverlayPayload:
    image_context_id = geometry.image_context_id if geometry is not None else None
    primitives: list[OverlayPrimitive] = []
    for label, color, boxes in (
        ("Observation entry", "#42a5f5", config.observation_entry_rois),
        ("Throw-in / roll", "#ab47bc", config.throw_in_rois),
        ("Decision deadline", "#ffca28", config.decision_rois),
        ("First roller (diagnostic only)", "#ef5350", config.first_roller_rois),
    ):
        for box in boxes:
            primitives.append(
                OverlayPrimitive(
                    primitive_type="zone",
                    layer="stem_process_zones",
                    label=label,
                    geometry={
                        "x_min": box.x_min,
                        "y_min": box.y_min,
                        "x_max": box.x_max,
                        "y_max": box.y_max,
                    },
                    image_context_id=image_context_id,
                    style={"stroke": color, "fill": "rgba(0,0,0,0.03)"},
                    metadata={
                        "process_configuration_hash": config.process_configuration_hash,
                        "intrinsic_calibration_hash": config.process_zone_intrinsic_hash,
                    },
                )
            )
    position = _geometry_center_normalized(geometry) if geometry is not None else None
    if position is not None:
        recommendation = result.get("recommendation")
        signed = (
            recommendation.get("signed_rotation_deg")
            if isinstance(recommendation, Mapping)
            else None
        )
        direction = (
            recommendation.get("direction")
            if isinstance(recommendation, Mapping)
            else "manual_required"
        )
        label = _pre_entry_overlay_label(
            signed=signed,
            direction=str(direction or "manual_required"),
            confidence=float(result.get("confidence") or 0.0),
            decision_ready=bool(result.get("decision_ready")),
            session_state=str(result.get("session_state") or "observing"),
        )
        primitives.append(
            OverlayPrimitive(
                primitive_type="point",
                layer="stem_pre_entry_decision",
                label=(
                    f"{label}; view={config.rotation_viewpoint_convention}; "
                    f"positive={config.positive_rotation_direction}"
                ),
                confidence=float(result.get("confidence") or 0.0),
                geometry={
                    "x": position[0],
                    "y": position[1],
                    "coordinate_system": "normalized_xy",
                },
                image_context_id=image_context_id,
                style={
                    "stroke": "#66bb6a" if result.get("decision_ready") else "#ffca28",
                    "radius": 5,
                },
                metadata={
                    "reason_codes": list(result.get("reason_codes") or []),
                    "current_zone": current_zone,
                    "estimate_method": "projected_baseline",
                    "process_configuration_hash": config.process_configuration_hash,
                },
            )
        )
    return OverlayPayload(
        primitives=primitives,
        display_context_id=image_context_id,
        metadata={
            "schema_version": _ORIENTATION_RESULT_SCHEMA,
            "decision_ready": bool(result.get("decision_ready")),
            "reason_codes": list(result.get("reason_codes") or []),
            "rotation_viewpoint_convention": config.rotation_viewpoint_convention,
            "positive_rotation_direction": config.positive_rotation_direction,
            "process_configuration_hash": config.process_configuration_hash,
        },
    )


def _pre_entry_overlay_label(
    *,
    signed: object,
    direction: str,
    confidence: float,
    decision_ready: bool,
    session_state: str,
) -> str:
    if not decision_ready:
        return f"Vor-Einzug-Beobachtung: {session_state}"
    if not isinstance(signed, int | float):
        return "Keine sichere Winkelentscheidung – Bediener erforderlich"
    confidence_text = f"{confidence:.2f}".replace(".", ",")
    if direction == "none":
        return f"Keine Drehung erforderlich – Konfidenz {confidence_text}"
    direction_label = (
        "im Uhrzeigersinn"
        if direction == "clockwise_viewed_from_configured_log_end"
        else "gegen den Uhrzeigersinn"
        if direction == "counter_clockwise_viewed_from_configured_log_end"
        else direction
    )
    angle_text = f"{abs(float(signed)):.1f}".replace(".", ",")
    return f"Drehe {angle_text}° {direction_label} – Konfidenz {confidence_text}"


def _active_stem_object(payload: SegmentPayload) -> RuntimeObjectReference | None:
    return payload.objects[0] if len(payload.objects) == 1 else None


def _axis_observation(geometry: StemGeometryPayload) -> StemAxisObservation:
    axis = geometry.reference_axis
    if (
        axis is None
        or geometry.median_width_px is None
        or geometry.image_width is None
        or geometry.image_height is None
    ):
        raise ValueError("valid stem reference axis, width, and image dimensions are required")
    if (
        axis.length_px <= 0
        or geometry.median_width_px <= 0
        or float(np.hypot(axis.direction.x, axis.direction.y)) <= 1e-9
    ):
        raise ValueError("stem reference axis and median width must be non-degenerate")
    return StemAxisObservation(
        center_x_px=(axis.start.x + axis.end.x) * 0.5,
        center_y_px=(axis.start.y + axis.end.y) * 0.5,
        direction_x=axis.direction.x,
        direction_y=axis.direction.y,
        length_px=axis.length_px,
        median_width_px=geometry.median_width_px,
        image_width=geometry.image_width,
        image_height=geometry.image_height,
    )


def _segment_mask(
    segments: SegmentPayload,
    *,
    image_width: int,
    image_height: int,
    cv2: Any,
    cache: Any = None,
) -> np.ndarray:
    if cache is not None:
        polygon_key = tuple(tuple((float(p.x), float(p.y)) for p in s.polygon)
                            for s in segments.segments)
        return cache.get(segments, ("surface_mask", image_width, image_height, polygon_key),
                         lambda: _segment_mask(segments, image_width=image_width,
                                               image_height=image_height, cv2=cv2))
    if len(segments.segments) != 1:
        raise ValueError("exactly one active segment is required")
    points = np.asarray(
        [(float(item.x), float(item.y)) for item in segments.segments[0].polygon],
        dtype=np.float64,
    )
    if points.shape[0] < 3 or not np.isfinite(points).all():
        raise ValueError("active stem polygon is invalid")
    if float(np.max(np.abs(points))) <= 1.5:
        points[:, 0] *= image_width
        points[:, 1] *= image_height
    polygon = np.rint(points).astype(np.int32)
    mask = np.zeros((image_height, image_width), dtype=np.uint8)
    cv2.fillPoly(mask, [polygon], 255)
    if int(cv2.countNonZero(mask)) <= 0:
        raise ValueError("active stem mask is empty")
    mask.flags.writeable = False
    return mask


def _eroded_stem_mask(
    mask: np.ndarray,
    *,
    median_width_px: float,
    erosion_fraction: float,
    cv2: Any,
) -> np.ndarray:
    erosion_px = max(int(round(median_width_px * erosion_fraction)), 1)
    kernel_size = erosion_px * 2 + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    eroded = cv2.erode(mask, kernel, iterations=1)
    if int(cv2.countNonZero(eroded)) == 0:
        raise ValueError("robust eroded stem mask is empty")
    return eroded


def _frame_gray(frame: FramePayload, cv2: Any) -> np.ndarray:
    cached = frame.cached_cv2_frame()
    if cached is None:
        encoded = np.frombuffer(frame.image_bytes(), dtype=np.uint8)
        image = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
    else:
        image = np.asarray(cached)
    if image is None:
        raise ValueError("frame could not be decoded")
    if image.ndim == 2:
        gray = image
    elif image.ndim == 3 and image.shape[2] == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    elif image.ndim == 3 and image.shape[2] == 4:
        gray = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    else:
        raise ValueError("frame must contain one, three, or four channels")
    result = np.ascontiguousarray(gray, dtype=np.uint8).view()
    result.flags.writeable = False
    return result


def _store_rotation_baseline(
    state: dict[str, Any],
    *,
    gray: np.ndarray,
    eroded_mask: np.ndarray,
    axis: StemAxisObservation,
    frame_sequence: int,
    timestamp: str,
    blur_score: float,
) -> None:
    state["previous"] = {
        "gray": gray,
        "eroded_mask": eroded_mask,
        "axis": axis,
        "frame_sequence": frame_sequence,
        "timestamp": timestamp,
        "blur_score": blur_score,
    }


def _reset_rotation_state(
    state: dict[str, Any],
    *,
    track_id: str,
    increment_generation: bool,
) -> None:
    generation = int(state.get("trace_generation") or 0)
    state.clear()
    state.update(
        {
            "schema_version": _ROTATION_STATE_SCHEMA,
            "track_id": track_id,
            "trace_generation": generation + int(increment_generation),
            "cumulative_rotation_deg": 0.0,
        }
    )


def _break_rotation_trace(
    context: RuntimeExecutionContext,
    *,
    node: RuntimeNodeSpec,
    track_id: str | None,
) -> int:
    state = context.state.setdefault(node.node_key, {})
    if not isinstance(state, dict):
        state = {}
        context.state[node.node_key] = state
    generation = int(state.get("trace_generation") or 0) + 1
    state.clear()
    state.update(
        {
            "schema_version": _ROTATION_STATE_SCHEMA,
            "track_id": track_id,
            "trace_generation": generation,
            "cumulative_rotation_deg": 0.0,
        }
    )
    return generation


def _rotation_trace_gap(
    previous: Mapping[str, Any],
    *,
    frame_sequence: int,
    timestamp: str,
    maximum_frame_gap: int,
    maximum_time_gap_seconds: float,
) -> bool:
    previous_sequence = int(previous.get("frame_sequence") or -1)
    if (
        frame_sequence <= previous_sequence
        or frame_sequence - previous_sequence > maximum_frame_gap
    ):
        return True
    previous_timestamp = _parse_timestamp(previous.get("timestamp"))
    current_timestamp = _parse_timestamp(timestamp)
    if previous_timestamp is None or current_timestamp is None:
        return False
    delta = (current_timestamp - previous_timestamp).total_seconds()
    return delta <= 0 or delta > maximum_time_gap_seconds


def _temporal_observation(
    geometry: StemGeometryPayload,
    rotation: StemRotationPayload,
    position: tuple[float, float] | None,
) -> StemTemporalObservation | None:
    if (
        not geometry.valid
        or not rotation.valid
        or geometry.frame_sequence is None
        or rotation.cumulative_rotation_deg_unwrapped is None
        or geometry.signed_bow_normalized is None
        or geometry.rms_residual_normalized is None
        or position is None
    ):
        return None
    return StemTemporalObservation(
        frame_sequence=geometry.frame_sequence,
        timestamp=geometry.timestamp or rotation.timestamp or "",
        cumulative_rotation_deg=rotation.cumulative_rotation_deg_unwrapped,
        rotation_confidence=rotation.rotation_confidence,
        inlier_ratio=rotation.feature_quality.inlier_ratio,
        signed_bow_normalized=geometry.signed_bow_normalized,
        rms_residual_normalized=geometry.rms_residual_normalized,
        geometry_quality=geometry.quality_score,
        visible_fraction=geometry.visible_fraction,
        shape_class=geometry.shape_class,
        center_x_normalized=position[0],
        center_y_normalized=position[1],
    )


def _observation_to_state(observation: StemTemporalObservation | None) -> dict[str, Any]:
    if observation is None:
        return {}
    return {
        "frame_sequence": observation.frame_sequence,
        "timestamp": observation.timestamp,
        "cumulative_rotation_deg": observation.cumulative_rotation_deg,
        "rotation_confidence": observation.rotation_confidence,
        "inlier_ratio": observation.inlier_ratio,
        "signed_bow_normalized": observation.signed_bow_normalized,
        "rms_residual_normalized": observation.rms_residual_normalized,
        "geometry_quality": observation.geometry_quality,
        "visible_fraction": observation.visible_fraction,
        "shape_class": observation.shape_class,
        "center_x_normalized": observation.center_x_normalized,
        "center_y_normalized": observation.center_y_normalized,
    }


def _temporal_feature_state(
    geometry: StemGeometryPayload,
    rotation: StemRotationPayload,
    active_stem: SegmentPayload | None,
    *,
    started_at: str | None,
) -> dict[str, Any]:
    timestamp = geometry.timestamp or rotation.timestamp or ""
    started = _parse_timestamp(started_at)
    current = _parse_timestamp(timestamp)
    elapsed = (
        max((current - started).total_seconds(), 0.0)
        if started is not None and current is not None
        else 0.0
    )
    tracking: dict[str, Any] = {}
    if active_stem is not None:
        candidate = active_stem.metadata.get("active_stem_tracking")
        if isinstance(candidate, Mapping) and candidate.get("track_id") == geometry.track_id:
            tracking = dict(candidate)
    return {
        "frame_index": int(geometry.frame_sequence or 0),
        "timestamp_seconds": elapsed,
        "source_timestamp": timestamp or None,
        "values": {
            "geometry": geometry.model_dump(mode="json"),
            "tracking": tracking,
            "rotation": rotation.model_dump(mode="json"),
        },
    }


def _temporal_sequence_payload(
    state: Mapping[str, Any],
    *,
    geometry: StemGeometryPayload,
    rotation: StemRotationPayload,
    config: _TemporalNodeConfig,
    session_id: str,
    decision_ready: bool,
    decision_frame_index: int | None,
) -> TemporalSequencePayload | None:
    values = state.get("bounded_feature_buffer")
    if not isinstance(values, list) or not values:
        return None
    frames: list[TemporalFeatureFrame] = []
    for value in values:
        try:
            frames.append(TemporalFeatureFrame.model_validate(value))
        except (TypeError, ValueError):
            continue
    if not frames:
        return None
    last_values = frames[-1].values
    last_geometry = (
        last_values.get("geometry") if isinstance(last_values, Mapping) else None
    )
    last_rotation = (
        last_values.get("rotation") if isinstance(last_values, Mapping) else None
    )
    if not isinstance(last_geometry, Mapping) or not isinstance(last_rotation, Mapping):
        return None
    overflow = bool(state.get("temporal_sequence_window_overflow", False))
    reasons = ["temporal_sequence_window_overflow"] if overflow else []
    return TemporalSequencePayload(
        sequence_id=session_id,
        group_id=session_id,
        track_id=str(last_geometry.get("track_id") or "") or None,
        feature_set_id=_TEMPORAL_FEATURE_SET_ID,
        feature_schema_version=_TEMPORAL_FEATURE_SCHEMA,
        rotation_schema_version=str(
            last_rotation.get("schema_version") or rotation.schema_version
        ),
        calibration_schema_version=str(
            last_rotation.get("calibration_version")
            or rotation.calibration_version
            or "unversioned"
        ),
        rotation_calibration_id=(
            str(last_rotation.get("calibration_id"))
            if last_rotation.get("calibration_id") is not None
            else None
        ),
        rotation_calibration_hash=(
            str(last_rotation.get("calibration_hash"))
            if last_rotation.get("calibration_hash") is not None
            else None
        ),
        rotation_viewpoint_convention=config.rotation_viewpoint_convention,
        positive_rotation_direction=config.positive_rotation_direction,
        frames=frames,
        current_frame_index=frames[-1].frame_index,
        decision_frame_index=decision_frame_index,
        decision_ready=decision_ready,
        trace_continuous=bool(state.get("trace_continuous", False)) and not overflow,
        calibration_valid=bool(state.get("calibration_valid", False)),
        reason_codes=reasons,
        metadata={
            "input_end": "decision_frame_or_earlier",
            "post_decision_frames_allowed": False,
            "source_observation_count": len(frames),
        },
    )


def _last_buffered_temporal_payloads(
    state: Mapping[str, Any],
    *,
    fallback_geometry: StemGeometryPayload,
    fallback_rotation: StemRotationPayload,
) -> tuple[StemGeometryPayload, StemRotationPayload]:
    values = state.get("bounded_feature_buffer")
    if not isinstance(values, list) or not values or not isinstance(values[-1], Mapping):
        return fallback_geometry, fallback_rotation
    frame_values = values[-1].get("values")
    if not isinstance(frame_values, Mapping):
        return fallback_geometry, fallback_rotation
    try:
        geometry = StemGeometryPayload.model_validate(frame_values.get("geometry"))
        rotation = StemRotationPayload.model_validate(frame_values.get("rotation"))
    except (TypeError, ValueError):
        return fallback_geometry, fallback_rotation
    return geometry, rotation


def _observations_from_state(values: Sequence[object]) -> list[StemTemporalObservation]:
    observations: list[StemTemporalObservation] = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        try:
            observations.append(StemTemporalObservation(**dict(item)))
        except (TypeError, ValueError):
            continue
    return observations


def _reset_temporal_state(state: dict[str, Any]) -> None:
    state.clear()
    state.update(
        {
            "schema_version": _TEMPORAL_STATE_SCHEMA,
            "bounded_observation_buffer": [],
            "bounded_feature_buffer": [],
            "temporal_sequence_window_overflow": False,
            "trace_continuous": True,
            "calibration_valid": True,
            "rotation_trace_summary": {},
            "target_candidates": [],
            "best_target_candidate": None,
            "decision_frame": None,
            "frozen_pre_entry_estimate": None,
            "frozen_temporal_sequence": None,
            "post_rotation_verification": None,
            "last_recommendation": None,
            "lost_counter": 0,
            "reason_codes": [],
        }
    )


def _update_temporal_state_summary(
    state: dict[str, Any],
    *,
    observations: Sequence[StemTemporalObservation],
    rotation: StemRotationPayload,
    decision: StemTemporalDecision,
) -> None:
    state["rotation_trace_summary"] = {
        "observation_count": len(observations),
        "first_frame_sequence": observations[0].frame_sequence if observations else None,
        "last_frame_sequence": observations[-1].frame_sequence if observations else None,
        "cumulative_rotation_deg": rotation.cumulative_rotation_deg_unwrapped,
        "observed_rotation_span_deg": decision.observed_rotation_span_deg,
        "trace_generation": rotation.trace_generation,
        "trace_continuous": bool(state.get("trace_continuous", False)),
        "calibration_valid": bool(state.get("calibration_valid", False)),
        "calibration_id": rotation.calibration_id,
        "calibration_hash": rotation.calibration_hash,
    }
    candidates = [_target_candidate_dict(candidate) for candidate in decision.target_candidates]
    state["target_candidates"] = candidates
    state["best_target_candidate"] = _target_candidate_dict(decision.target_candidate)
    state["reason_codes"] = list(dict.fromkeys(decision.reason_codes))


def _normalized_rois(value: object) -> tuple[_NormalizedBox, ...]:
    values = value if isinstance(value, list) else [value] if isinstance(value, Mapping) else []
    result: list[_NormalizedBox] = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        source = item.get("box") if isinstance(item.get("box"), Mapping) else item
        try:
            if {"x", "y", "width", "height"}.issubset(source):
                x_min = float(source["x"])
                y_min = float(source["y"])
                box = _NormalizedBox(
                    x_min=x_min,
                    y_min=y_min,
                    x_max=x_min + float(source["width"]),
                    y_max=y_min + float(source["height"]),
                )
            elif {"x_min", "y_min", "x_max", "y_max"}.issubset(source):
                box = _NormalizedBox(
                    x_min=float(source["x_min"]),
                    y_min=float(source["y_min"]),
                    x_max=float(source["x_max"]),
                    y_max=float(source["y_max"]),
                )
            else:
                continue
        except (TypeError, ValueError):
            continue
        if 0 <= box.x_min < box.x_max <= 1 and 0 <= box.y_min < box.y_max <= 1:
            result.append(box)
    return tuple(result)


def _process_stage(
    position: tuple[float, float] | None,
    config: _TemporalNodeConfig,
) -> tuple[int | None, str | None]:
    if position is None:
        return None, None
    matches = [
        (0, "observation_entry", config.observation_entry_rois),
        (1, "throw_in", config.throw_in_rois),
        (2, "decision", config.decision_rois),
        (3, "first_roller", config.first_roller_rois),
    ]
    selected = [
        (stage, label)
        for stage, label, boxes in matches
        if any(box.contains(position) for box in boxes)
    ]
    return max(selected, default=(None, None), key=lambda item: item[0] or 0)


def _geometry_center_normalized(
    geometry: StemGeometryPayload | None,
) -> tuple[float, float] | None:
    if (
        geometry is None
        or not geometry.centerline_image_xy
        or geometry.image_width is None
        or geometry.image_height is None
    ):
        return None
    return (
        float(np.mean([point.x for point in geometry.centerline_image_xy])) / geometry.image_width,
        float(np.mean([point.y for point in geometry.centerline_image_xy])) / geometry.image_height,
    )


def _geometry_center_px(
    geometry: StemGeometryPayload | None,
) -> tuple[float, float]:
    if geometry is None or not geometry.centerline_image_xy:
        return 0.0, 0.0
    return (
        float(np.mean([point.x for point in geometry.centerline_image_xy])),
        float(np.mean([point.y for point in geometry.centerline_image_xy])),
    )


def _temporal_convention_reasons(
    config: _TemporalNodeConfig,
    rotation: StemRotationPayload,
) -> list[str]:
    reasons: list[str] = []
    if rotation.rotation_viewpoint_convention != config.rotation_viewpoint_convention:
        reasons.append("rotation_viewpoint_convention_mismatch")
    if rotation.positive_rotation_direction != config.positive_rotation_direction:
        reasons.append("positive_rotation_direction_mismatch")
    if rotation.intrinsic_calibration_hash != config.process_zone_intrinsic_hash:
        reasons.append("process_zone_intrinsic_calibration_mismatch")
    return reasons


def _physical_direction(
    sign: int,
    positive_direction: str,
    *,
    valid: bool,
) -> str:
    if not valid:
        return "manual_required"
    if sign == 0:
        return "none"
    positive_is_clockwise = positive_direction.startswith("clockwise_")
    clockwise = positive_is_clockwise if sign > 0 else not positive_is_clockwise
    return (
        "clockwise_viewed_from_configured_log_end"
        if clockwise
        else "counter_clockwise_viewed_from_configured_log_end"
    )


def _target_candidate_dict(candidate: StemTargetCandidate | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {
        "rotation_deg": candidate.rotation_deg,
        "frame_start": candidate.frame_start,
        "frame_end": candidate.frame_end,
        "candidate_type": candidate.candidate_type,
        "score": candidate.score,
        "bow_magnitude": candidate.bow_magnitude,
    }


def _process_configuration_hash(
    entry: Sequence[_NormalizedBox],
    throw_in: Sequence[_NormalizedBox],
    decision: Sequence[_NormalizedBox],
    first_roller: Sequence[_NormalizedBox],
    *,
    calibration_id: str,
    calibration_version: str,
    intrinsic_hash: str,
    viewpoint: str,
    positive_direction: str,
) -> str:
    def boxes(values: Sequence[_NormalizedBox]) -> list[list[float]]:
        return [[item.x_min, item.y_min, item.x_max, item.y_max] for item in values]

    payload = {
        "schema_version": _PROCESS_ZONE_SCHEMA,
        "observation_entry_roi": boxes(entry),
        "throw_in_roi": boxes(throw_in),
        "decision_roi": boxes(decision),
        "first_roller_roi": boxes(first_roller),
        "calibration_id": calibration_id,
        "calibration_version": calibration_version,
        "intrinsic_hash": intrinsic_hash,
        "viewpoint": viewpoint,
        "positive_direction": positive_direction,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _session_id(
    context: RuntimeExecutionContext,
    *,
    node: RuntimeNodeSpec,
    track_id: str,
    geometry: StemGeometryPayload,
) -> str:
    digest = hashlib.sha256(
        "|".join(
            (
                context.runtime_execution_id or context.pipeline_version_id,
                context.channel_id or "no-channel",
                node.node_key,
                track_id,
                str(geometry.frame_sequence),
            )
        ).encode("utf-8")
    ).hexdigest()[:20]
    return f"stem_session_{digest}"


def _event_reason_codes(events: object) -> list[str]:
    if not isinstance(events, EventsPayload):
        return []
    return [f"machine_event_observed:{item.event_type}" for item in events.events]


def _matching_image_contexts(
    frame: FramePayload,
    segments: SegmentPayload,
    geometry: StemGeometryPayload,
) -> bool:
    frame_context = frame.image_context.context_id if frame.image_context is not None else None
    values = [
        item
        for item in (frame_context, segments.image_context_id, geometry.image_context_id)
        if item
    ]
    return len(set(values)) <= 1


def _is_undistorted_frame(frame: FramePayload) -> bool:
    image_context = frame.image_context
    return bool(
        image_context is not None
        and image_context.transform.transform_type == "undistort"
        and image_context.transform.parameters.get("calibration_hash")
    )


def _intrinsic_calibration_hash(
    frame: FramePayload,
    geometry: StemGeometryPayload,
) -> str | None:
    value = frame.metadata.get("calibration_hash") or geometry.metadata.get("calibration_hash")
    if frame.image_context is not None:
        value = frame.image_context.transform.parameters.get("calibration_hash", value)
    return str(value) if value else None


def _rotation_frame_sequence(
    context: RuntimeExecutionContext,
    frame: FramePayload,
    geometry: StemGeometryPayload,
) -> int:
    value = geometry.frame_sequence
    if value is None:
        value = frame.metadata.get("sequence", context.metadata.get("live_frame_sequence"))
    if value is None:
        raise ValueError("surface rotation requires a frame sequence")
    return int(value)


def _blur_score(gray: np.ndarray, cv2: Any) -> float:
    return max(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 0.0)


def _mask_fraction(eroded: np.ndarray, source: np.ndarray, cv2: Any) -> float:
    source_area = int(cv2.countNonZero(source))
    return int(cv2.countNonZero(eroded)) / source_area if source_area > 0 else 0.0


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _bounded_float(
    config: Mapping[str, Any],
    key: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    value = config.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be numeric")
    parsed = float(value)
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return parsed


def _bounded_int(
    config: Mapping[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = config.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return parsed


def _opencv() -> Any:
    try:
        import cv2
    except Exception as exc:  # pragma: no cover - package declares OpenCV
        raise RuntimeError("surface rotation requires OpenCV") from exc
    return cv2


analysis_stem_surface_rotation = StemSurfaceRotationHandler()
analysis_stem_temporal_orientation = StemTemporalOrientationHandler()
