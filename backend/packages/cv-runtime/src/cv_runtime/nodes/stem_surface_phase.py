# ruff: noqa: ANN401 -- OpenCV exposes dynamic Python bindings.

from __future__ import annotations

import math
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import numpy as np
from cv_contracts.runtime import (
    RuntimeNodeSpec,
    RuntimeStemCurvatureProcessCalibrationProfile,
    RuntimeStemSurfacePhaseCalibrationProfile,
)

from cv_runtime.context import NodeInputs, RuntimeExecutionContext
from cv_runtime.handlers import NodeExecutionResult
from cv_runtime.high_rate_phase import (
    HighRatePhaseSnapshot,
    HighRateStemPhaseBridge,
    StemPhaseSeed,
)
from cv_runtime.nodes.stem_temporal import (
    _active_stem_object,
    _axis_observation,
    _eroded_stem_mask,
    _feature_tracking_config,
    _frame_gray,
    _matching_image_contexts,
    _opencv,
    _parse_timestamp,
    _rotation_frame_sequence,
    _segment_mask,
    _track_surface_features,
)
from cv_runtime.payloads import (
    EventPayload,
    EventsPayload,
    FramePayload,
    OverlayPayload,
    OverlayPrimitive,
    ProvenanceLink,
    RuntimeObjectReference,
    SegmentPayload,
    StemGeometryPayload,
    StemRotationTranslationComponent,
    StemSurfacePhasePayload,
)
from cv_runtime.stem_process_coordinates import (
    image_points_to_process,
    process_points_to_image,
    process_profile_for_rectified_view,
    validate_stem_curvature_process_calibration,
)
from cv_runtime.stem_surface_phase import (
    PhaseRelockCandidate,
    RawSurfacePhaseConfig,
    estimate_raw_surface_phase_from_matches,
    phase_calibration_runtime_reasons,
    select_phase_relock_candidate,
    validate_stem_surface_phase_calibration,
)

_STATE_SCHEMA = "stem.surface-phase.state.v1"


class StemSurfacePhaseHandler:
    node_type = "analysis.stem_surface_phase"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        frame = inputs.get("frame")
        segments = inputs.get("segments")
        geometry = inputs.get("stem_geometry")
        events = inputs.get("events")
        config = node.config or {}
        high_rate_requested = (
            str(config.get("phase_estimator_version") or "LK_KEYFRAME_RELOCK_V1")
            == "CYLINDRICAL_SURFACE_V2"
            and str(config.get("execution_mode") or "SYNCHRONOUS")
            == "HIGH_RATE_SEEDED"
        )
        high_rate_bridge = context.metadata.get("high_rate_stem_phase_bridge")
        state = _node_state(context, node)
        if _session_ended(events):
            if high_rate_requested and isinstance(
                high_rate_bridge, HighRateStemPhaseBridge
            ):
                high_rate_bridge.reset("PHASE_SESSION_ENDED_RESET")
            _reset_state(state, track_id=None, increment_generation=True)
            return NodeExecutionResult(
                outputs={
                    "surface_phase": _payload(
                        node=node,
                        frame=frame if isinstance(frame, FramePayload) else None,
                        geometry=geometry if isinstance(geometry, StemGeometryPayload) else None,
                        tracking_mode="RESET",
                        relock_status="NOT_ATTEMPTED",
                        trace_generation=int(state["trace_generation"]),
                        reason_codes=["PHASE_SESSION_ENDED_RESET"],
                    )
                }
            )
        if (
            not isinstance(frame, FramePayload)
            or not isinstance(segments, SegmentPayload)
            or not isinstance(geometry, StemGeometryPayload)
        ):
            _lose_lock(state)
            return NodeExecutionResult(
                outputs={
                    "surface_phase": _payload(
                        node=node,
                        frame=frame if isinstance(frame, FramePayload) else None,
                        geometry=geometry if isinstance(geometry, StemGeometryPayload) else None,
                        tracking_mode="LOST",
                        trace_generation=int(state["trace_generation"]),
                        failure_count=int(state["failure_count"]),
                        reason_codes=["PHASE_INPUT_MISSING", "PHASE_TRACK_LOST"],
                    )
                }
            )

        source_object = _active_stem_object(segments)
        track_id = source_object.track_id if source_object is not None else None
        if (
            source_object is None
            or not track_id
            or not geometry.valid
            or geometry.track_id != track_id
        ):
            if high_rate_requested and isinstance(
                high_rate_bridge, HighRateStemPhaseBridge
            ):
                snapshot = high_rate_bridge.snapshot(
                    track_id,
                    geometry.frame_sequence if geometry is not None else None,
                )
                if snapshot.track_id is not None:
                    fallback_profile, fallback_profile_error = _process_profile(frame)
                    if fallback_profile is not None:
                        fallback_profile = process_profile_for_rectified_view(
                            fallback_profile,
                            image_width=int(frame.width or fallback_profile.image_width),
                            image_height=int(frame.height or fallback_profile.image_height),
                            transform_parameters=(
                                frame.image_context.transform.parameters
                                if frame.image_context
                                else None
                            ),
                        )
                    fallback_calibration, fallback_parse_reason = _calibration(node)
                    binding = state.get("high_rate_binding")
                    binding = binding if isinstance(binding, Mapping) else {}
                    same_binding_track = binding.get("track_id") == snapshot.track_id
                    fallback_reasons = [
                        item
                        for item in (
                            fallback_profile_error,
                            fallback_parse_reason,
                        )
                        if item
                    ]
                    if fallback_profile is not None and not fallback_reasons:
                        fallback_reasons.extend(
                            phase_calibration_runtime_reasons(
                                fallback_calibration,
                                image_width=int(frame.width or fallback_profile.image_width),
                                image_height=int(frame.height or fallback_profile.image_height),
                                intrinsic_calibration_hash=(
                                    fallback_profile.intrinsic_calibration_hash
                                ),
                                process_calibration_hash=fallback_profile.calibration_hash,
                                source_channel_id=context.channel_id,
                                diameter_mm=(
                                    _finite_motion_number(binding.get("diameter_mm"))
                                    if same_binding_track
                                    else None
                                ),
                                center_x_px=(
                                    _finite_motion_number(binding.get("center_x_px"))
                                    if same_binding_track
                                    else None
                                ),
                                center_y_px=(
                                    _finite_motion_number(binding.get("center_y_px"))
                                    if same_binding_track
                                    else None
                                ),
                                rotation_reference_view=(
                                    fallback_profile.rotation_reference_view
                                ),
                                positive_rotation=fallback_profile.positive_rotation,
                                phase_estimator_version="CYLINDRICAL_SURFACE_V2",
                            )
                        )
                    fallback_valid = (
                        fallback_calibration is not None and not fallback_reasons
                    )
                    return self._high_rate_result(
                        node=node,
                        frame=frame,
                        geometry=geometry,
                        process_profile=fallback_profile,
                        calibration=fallback_calibration,
                        calibration_valid=fallback_valid,
                        calibration_reasons=[
                            *fallback_reasons,
                            "PHASE_SEED_NOT_REFRESHED",
                        ],
                        track_id=snapshot.track_id,
                        source_object=source_object,
                        snapshot=snapshot,
                    )
            _lose_lock(state)
            return NodeExecutionResult(
                outputs={
                    "surface_phase": _payload(
                        node=node,
                        frame=frame,
                        geometry=geometry,
                        track_id=track_id,
                        source_object=source_object,
                        tracking_mode="LOST",
                        trace_generation=int(state["trace_generation"]),
                        failure_count=int(state["failure_count"]),
                        reason_codes=["PHASE_ACTIVE_STEM_AMBIGUOUS", "PHASE_TRACK_LOST"],
                    )
                }
            )

        process_profile, process_error = _process_profile(frame)
        binding_error = process_error or _binding_error(
            context, frame, segments, geometry, process_profile
        )
        if binding_error:
            _lose_lock(state)
            return NodeExecutionResult(
                outputs={
                    "surface_phase": _payload(
                        node=node,
                        frame=frame,
                        geometry=geometry,
                        track_id=track_id,
                        source_object=source_object,
                        tracking_mode="LOST",
                        trace_generation=int(state["trace_generation"]),
                        failure_count=int(state["failure_count"]),
                        reason_codes=[binding_error, "PHASE_TRACK_LOST"],
                    )
                }
            )
        process_profile = process_profile_for_rectified_view(
            process_profile,
            image_width=int(frame.width or process_profile.image_width),
            image_height=int(frame.height or process_profile.image_height),
            transform_parameters=(
                frame.image_context.transform.parameters if frame.image_context else None
            ),
        )

        try:
            cv2 = _opencv()
            axis = _axis_observation(geometry)
            gray = _frame_gray(frame, cv2)
            mask = _segment_mask(
                segments, image_width=axis.image_width, image_height=axis.image_height, cv2=cv2
            )
            mask = cv2.bitwise_and(mask, _surface_roi_mask(process_profile, cv2=cv2))
            sequence = _rotation_frame_sequence(context, frame, geometry)
            timestamp = geometry.timestamp or frame.timestamp or context.now_iso()
            if not high_rate_requested:
                tracking = _feature_tracking_config(node.config or {})
                eroded = _eroded_stem_mask(
                    mask,
                    median_width_px=axis.median_width_px,
                    erosion_fraction=tracking.mask_erosion_fraction,
                    cv2=cv2,
                )
                patch, patch_mask = _canonical_patch(gray, eroded, axis, cv2=cv2)
                patch_points, patch_descriptors = _keyframe_features(
                    patch, patch_mask, cv2=cv2
                )
        except (TypeError, ValueError, RuntimeError) as exc:
            _lose_lock(state)
            return NodeExecutionResult(
                outputs={
                    "surface_phase": _payload(
                        node=node,
                        frame=frame,
                        geometry=geometry,
                        process_profile=process_profile,
                        track_id=track_id,
                        source_object=source_object,
                        tracking_mode="LOST",
                        trace_generation=int(state["trace_generation"]),
                        failure_count=int(state["failure_count"]),
                        reason_codes=[
                            "PHASE_FRAME_MASK_OR_GEOMETRY_INVALID",
                            "PHASE_TRACK_LOST",
                            str(exc)[:120],
                        ],
                    )
                }
            )

        calibration, calibration_parse_reason = _calibration(node)
        diameter_mm = _metric_cross_section_diameter_mm(geometry, process_profile)
        if high_rate_requested:
            state["high_rate_binding"] = {
                "track_id": track_id,
                "diameter_mm": diameter_mm,
                "center_x_px": axis.center_x_px,
                "center_y_px": axis.center_y_px,
            }
        calibration_reasons = list(
            phase_calibration_runtime_reasons(
                calibration,
                image_width=axis.image_width,
                image_height=axis.image_height,
                intrinsic_calibration_hash=process_profile.intrinsic_calibration_hash,
                process_calibration_hash=process_profile.calibration_hash,
                source_channel_id=context.channel_id,
                diameter_mm=diameter_mm,
                center_x_px=axis.center_x_px,
                center_y_px=axis.center_y_px,
                rotation_reference_view=process_profile.rotation_reference_view,
                positive_rotation=process_profile.positive_rotation,
                phase_estimator_version=str(
                    config.get("phase_estimator_version")
                    or "LK_KEYFRAME_RELOCK_V1"
                ),
            )
        )
        if calibration_parse_reason:
            calibration_reasons.insert(0, calibration_parse_reason)
        calibration_valid = not calibration_reasons and calibration is not None

        if high_rate_requested:
            if not isinstance(high_rate_bridge, HighRateStemPhaseBridge):
                snapshot = HighRatePhaseSnapshot(
                    track_id=track_id,
                    frame_sequence=sequence,
                    timestamp=timestamp,
                    tracking_mode="LOST",
                    trace_generation=int(state.get("trace_generation") or 0),
                    reason_codes=(
                        "PHASE_HIGH_RATE_WORKER_UNAVAILABLE",
                        "PHASE_TRACK_LOST",
                    ),
                )
            else:
                tracking_metadata = segments.metadata.get("active_stem_tracking")
                session_id = (
                    str(tracking_metadata.get("session_id") or "").strip() or None
                    if isinstance(tracking_metadata, Mapping)
                    else None
                )
                high_rate_bridge.publish_seed(
                    StemPhaseSeed(
                        track_id=track_id,
                        session_id=session_id,
                        frame_sequence=sequence,
                        timestamp=timestamp,
                        gray=gray,
                        stem_mask=mask,
                        axis=axis,
                        confidence=min(
                            max(
                                float(
                                    getattr(geometry, "quality_score", 0.0)
                                    or source_object.score
                                    or 0.0
                                ),
                                0.0,
                            ),
                            1.0,
                        ),
                        image_context_id=geometry.image_context_id,
                        intrinsic_calibration_hash=process_profile.intrinsic_calibration_hash,
                        process_calibration_hash=process_profile.calibration_hash,
                    )
                )
                snapshot = high_rate_bridge.snapshot(track_id, sequence)
            return self._high_rate_result(
                node=node,
                frame=frame,
                geometry=geometry,
                process_profile=process_profile,
                calibration=calibration,
                calibration_valid=calibration_valid,
                calibration_reasons=calibration_reasons,
                track_id=track_id,
                source_object=source_object,
                snapshot=snapshot,
            )

        previous_track_id = str(state.get("track_id") or "") or None
        if previous_track_id is not None and previous_track_id != track_id:
            _reset_state(state, track_id=track_id, increment_generation=True)
            reason = "PHASE_TRACK_CHANGED_RESET"
        else:
            state["track_id"] = track_id
            reason = "PHASE_BASELINE_INITIALIZED"
        previous = state.get("previous")
        if not isinstance(previous, Mapping):
            state["raw_cumulative_phase"] = 0.0
            state["last_safe_raw_phase"] = 0.0
            state["phase_locked"] = True
            state["failure_count"] = 0
            _store_previous(state, gray, eroded, axis, sequence, timestamp, patch_points, patch)
            _maybe_store_keyframe(
                state,
                patch,
                patch_points,
                patch_descriptors,
                raw_phase=0.0,
                sequence=sequence,
                config=node.config or {},
            )
            return self._result(
                node=node,
                frame=frame,
                geometry=geometry,
                process_profile=process_profile,
                calibration=calibration,
                calibration_valid=calibration_valid,
                calibration_reasons=calibration_reasons,
                track_id=track_id,
                source_object=source_object,
                state=state,
                tracking_mode="INITIALIZING",
                relock_status="NOT_REQUIRED",
                delta_raw=0.0,
                cumulative_raw=0.0,
                confidence=1.0,
                detected_count=len(patch_points),
                reason_codes=[reason],
            )

        gap_reason = _phase_trace_gap_reason(
            previous,
            frame_sequence=sequence,
            timestamp=timestamp,
            maximum_frame_gap=tracking.maximum_frame_gap,
            maximum_time_gap_seconds=tracking.maximum_time_gap_seconds,
        )
        if gap_reason is not None:
            was_locked = bool(state.get("phase_locked"))
            _lose_lock(state)
            _store_previous(state, gray, eroded, axis, sequence, timestamp, patch_points, patch)
            return self._lost_or_relocked(
                node=node,
                frame=frame,
                geometry=geometry,
                process_profile=process_profile,
                calibration=calibration,
                calibration_valid=calibration_valid,
                calibration_reasons=calibration_reasons,
                track_id=track_id,
                source_object=source_object,
                state=state,
                patch_points=patch_points,
                patch_descriptors=patch_descriptors,
                sequence=sequence,
                base_reasons=[gap_reason],
                increment_generation_on_failure=was_locked,
            )

        previous_gray = previous.get("gray")
        previous_mask = previous.get("eroded_mask")
        previous_axis = previous.get("axis")
        if (
            not isinstance(previous_gray, np.ndarray)
            or not isinstance(previous_mask, np.ndarray)
            or previous_axis is None
        ):
            was_locked = bool(state.get("phase_locked"))
            _lose_lock(state)
            _store_previous(state, gray, eroded, axis, sequence, timestamp, patch_points, patch)
            return self._lost_or_relocked(
                node=node,
                frame=frame,
                geometry=geometry,
                process_profile=process_profile,
                calibration=calibration,
                calibration_valid=calibration_valid,
                calibration_reasons=calibration_reasons,
                track_id=track_id,
                source_object=source_object,
                state=state,
                patch_points=patch_points,
                patch_descriptors=patch_descriptors,
                sequence=sequence,
                base_reasons=["PHASE_STATE_INVALID"],
                increment_generation_on_failure=was_locked,
            )

        motion_allowed, motion_speed = _phase_motion_allowed(
            segments,
            config=node.config or {},
        )
        if bool(state.get("phase_locked")) and not motion_allowed:
            cumulative = float(state.get("raw_cumulative_phase") or 0.0)
            state["last_safe_raw_phase"] = cumulative
            state["failure_count"] = 0
            _store_previous(
                state,
                gray,
                eroded,
                axis,
                sequence,
                timestamp,
                patch_points,
                patch,
            )
            return self._result(
                node=node,
                frame=frame,
                geometry=geometry,
                process_profile=process_profile,
                calibration=calibration,
                calibration_valid=calibration_valid,
                calibration_reasons=calibration_reasons,
                track_id=track_id,
                source_object=source_object,
                state=state,
                tracking_mode="INCREMENTAL",
                relock_status="NOT_REQUIRED",
                delta_raw=0.0,
                cumulative_raw=cumulative,
                confidence=1.0,
                detected_count=len(patch_points),
                reason_codes=["PHASE_STATIONARY_HOLD"],
                motion_gate_status="stationary_hold",
                motion_speed_mm_per_frame=motion_speed,
            )

        tracked = _track_surface_features(
            previous_gray, gray, previous_mask, eroded, config=tracking, cv2=cv2
        )
        raw_config = RawSurfacePhaseConfig(
            minimum_matched_features=tracking.estimation.minimum_matched_features,
            minimum_inlier_ratio=tracking.estimation.minimum_inlier_ratio,
            maximum_tracking_error_px=tracking.estimation.maximum_tracking_error_px,
            maximum_longitudinal_shift_fraction=(
                tracking.estimation.maximum_longitudinal_shift_fraction
            ),
            absolute_inlier_tolerance_raw=tracking.estimation.absolute_inlier_tolerance_normalized,
            mad_multiplier=tracking.estimation.mad_multiplier,
            maximum_abs_delta_raw_phase=float(
                (node.config or {}).get("maximum_abs_delta_raw_phase", 0.75)
            ),
            minimum_confidence=tracking.estimation.minimum_confidence,
        )
        estimate = estimate_raw_surface_phase_from_matches(
            tracked.matches, previous_axis=previous_axis, current_axis=axis, config=raw_config
        )
        degree_jump = (
            abs(float(estimate.delta_raw_phase or 0.0) * float(calibration.degrees_per_raw_phase))
            if estimate.valid and calibration_valid and calibration is not None
            else 0.0
        )
        maximum_degree_jump = float((node.config or {}).get("maximum_abs_delta_phase_deg", 60.0))
        if degree_jump > maximum_degree_jump:
            estimate = estimate.__class__(
                **{
                    **estimate.__dict__,
                    "valid": False,
                    "delta_raw_phase": None,
                    "reason_codes": ("PHASE_STEP_IMPLAUSIBLE",),
                }
            )

        if (
            not estimate.valid
            or estimate.delta_raw_phase is None
            or not bool(state.get("phase_locked"))
        ):
            was_locked = bool(state.get("phase_locked"))
            _lose_lock(state)
            _store_previous(state, gray, eroded, axis, sequence, timestamp, patch_points, patch)
            return self._lost_or_relocked(
                node=node,
                frame=frame,
                geometry=geometry,
                process_profile=process_profile,
                calibration=calibration,
                calibration_valid=calibration_valid,
                calibration_reasons=calibration_reasons,
                track_id=track_id,
                source_object=source_object,
                state=state,
                patch_points=patch_points,
                patch_descriptors=patch_descriptors,
                sequence=sequence,
                base_reasons=list(estimate.reason_codes),
                estimate=estimate,
                detected_count=tracked.detected_feature_count,
                increment_generation_on_failure=was_locked,
            )

        delta_raw = float(estimate.delta_raw_phase)
        cumulative = float(state.get("raw_cumulative_phase") or 0.0) + delta_raw
        state["raw_cumulative_phase"] = cumulative
        state["last_safe_raw_phase"] = cumulative
        state["phase_locked"] = True
        state["failure_count"] = 0
        _store_previous(
            state,
            gray,
            eroded,
            axis,
            sequence,
            timestamp,
            np.asarray([item[2:] for item in tracked.vectors], dtype=np.float32),
            patch,
        )
        _maybe_store_keyframe(
            state,
            patch,
            patch_points,
            patch_descriptors,
            raw_phase=cumulative,
            sequence=sequence,
            config=node.config or {},
        )
        return self._result(
            node=node,
            frame=frame,
            geometry=geometry,
            process_profile=process_profile,
            calibration=calibration,
            calibration_valid=calibration_valid,
            calibration_reasons=calibration_reasons,
            track_id=track_id,
            source_object=source_object,
            state=state,
            tracking_mode="INCREMENTAL",
            relock_status="NOT_REQUIRED",
            delta_raw=delta_raw,
            cumulative_raw=cumulative,
            confidence=estimate.confidence,
            detected_count=tracked.detected_feature_count,
            estimate=estimate,
            reason_codes=[],
        )

    def _lost_or_relocked(
        self,
        *,
        node: RuntimeNodeSpec,
        frame: FramePayload,
        geometry: StemGeometryPayload,
        process_profile: RuntimeStemCurvatureProcessCalibrationProfile,
        calibration: RuntimeStemSurfacePhaseCalibrationProfile | None,
        calibration_valid: bool,
        calibration_reasons: list[str],
        track_id: str,
        source_object: RuntimeObjectReference,
        state: dict[str, Any],
        patch_points: np.ndarray,
        patch_descriptors: np.ndarray | None,
        sequence: int,
        base_reasons: list[str],
        estimate: Any = None,
        detected_count: int = 0,
        increment_generation_on_failure: bool = False,
    ) -> NodeExecutionResult:
        candidates = _relock_candidates(state, patch_points, patch_descriptors)
        maximum_jump = float((node.config or {}).get("relock_maximum_raw_jump", 0.75))
        if calibration_valid and calibration is not None:
            maximum_jump = min(
                maximum_jump,
                float((node.config or {}).get("relock_maximum_phase_jump_deg", 90.0))
                / abs(float(calibration.degrees_per_raw_phase)),
            )
        decision = select_phase_relock_candidate(
            candidates,
            last_safe_raw_phase=float(state.get("last_safe_raw_phase") or 0.0),
            minimum_score=float((node.config or {}).get("relock_minimum_score", 0.45)),
            uniqueness_margin=float((node.config or {}).get("relock_uniqueness_margin", 0.10)),
            maximum_raw_jump=maximum_jump,
        )
        if decision.candidate is not None:
            previous_safe = float(state.get("last_safe_raw_phase") or 0.0)
            cumulative = float(decision.candidate.raw_phase)
            delta_raw = cumulative - previous_safe
            state["raw_cumulative_phase"] = cumulative
            state["last_safe_raw_phase"] = cumulative
            state["phase_locked"] = True
            state["failure_count"] = 0
            state["relock_status"] = "RELOCKED"
            return self._result(
                node=node,
                frame=frame,
                geometry=geometry,
                process_profile=process_profile,
                calibration=calibration,
                calibration_valid=calibration_valid,
                calibration_reasons=calibration_reasons,
                track_id=track_id,
                source_object=source_object,
                state=state,
                tracking_mode="KEYFRAME_RELOCK",
                relock_status="RELOCKED",
                relock_keyframe_id=decision.candidate.keyframe_id,
                delta_raw=delta_raw,
                cumulative_raw=cumulative,
                confidence=decision.candidate.score,
                detected_count=max(
                    detected_count,
                    decision.candidate.matched_feature_count,
                ),
                estimate=estimate,
                matched_count_override=decision.candidate.matched_feature_count,
                inlier_ratio_override=decision.candidate.inlier_ratio,
                reason_codes=[*base_reasons, *decision.reason_codes],
            )
        if increment_generation_on_failure:
            state["trace_generation"] = int(state.get("trace_generation") or 0) + 1
        state["relock_status"] = decision.status
        return self._result(
            node=node,
            frame=frame,
            geometry=geometry,
            process_profile=process_profile,
            calibration=calibration,
            calibration_valid=False,
            calibration_reasons=calibration_reasons,
            track_id=track_id,
            source_object=source_object,
            state=state,
            tracking_mode="LOST",
            relock_status=decision.status,
            delta_raw=None,
            cumulative_raw=None,
            confidence=0.0,
            detected_count=detected_count,
            estimate=estimate,
            reason_codes=[*base_reasons, *decision.reason_codes],
        )

    def _result(self, **values: Any) -> NodeExecutionResult:
        state = values["state"]
        estimate = values.get("estimate")
        if (
            values.get("tracking_mode") == "INCREMENTAL"
            and values.get("delta_raw") is not None
            and bool(getattr(estimate, "valid", False))
        ):
            state["cumulative_translation_x_px"] = float(
                state.get("cumulative_translation_x_px") or 0.0
            ) + float(getattr(estimate, "translation_x_px", 0.0))
            state["cumulative_translation_y_px"] = float(
                state.get("cumulative_translation_y_px") or 0.0
            ) + float(getattr(estimate, "translation_y_px", 0.0))
        payload = _payload(
            **values,
            trace_generation=int(state["trace_generation"]),
            failure_count=int(state["failure_count"]),
            last_safe_raw=float(state.get("last_safe_raw_phase") or 0.0),
            keyframe_count=len(state.get("keyframes") or []),
            cumulative_translation_x_px=float(
                state.get("cumulative_translation_x_px") or 0.0
            ),
            cumulative_translation_y_px=float(
                state.get("cumulative_translation_y_px") or 0.0
            ),
        )
        outputs: dict[str, Any] = {"surface_phase": payload}
        if payload.relock_status == "RELOCKED":
            outputs["relock_events"] = EventsPayload(
                events=[
                    EventPayload(
                        event_type="stem_surface_phase_relocked",
                        key=payload.track_id or "unknown-track",
                        payload={
                            "track_id": payload.track_id,
                            "raw_phase": payload.cumulative_raw_phase,
                            "phase_deg": payload.cumulative_phase_deg,
                            "confidence": payload.confidence,
                            "keyframe_id": payload.relock_keyframe_id,
                            "trace_generation": payload.trace_generation,
                        },
                    )
                ]
            )
        if bool((values["node"].config or {}).get("debug_overlay", True)):
            outputs["overlay"] = _overlay(payload, values.get("process_profile"))
        return NodeExecutionResult(outputs=outputs)

    def _high_rate_result(
        self,
        *,
        node: RuntimeNodeSpec,
        frame: FramePayload,
        geometry: StemGeometryPayload,
        process_profile: RuntimeStemCurvatureProcessCalibrationProfile | None,
        calibration: RuntimeStemSurfacePhaseCalibrationProfile | None,
        calibration_valid: bool,
        calibration_reasons: list[str],
        track_id: str,
        source_object: RuntimeObjectReference | None,
        snapshot: HighRatePhaseSnapshot,
    ) -> NodeExecutionResult:
        estimate = SimpleNamespace(
            matched_feature_count=snapshot.matched_feature_count,
            inlier_feature_count=snapshot.inlier_feature_count,
            inlier_ratio=snapshot.inlier_ratio,
            median_tracking_error_px=snapshot.median_tracking_error_px,
            translation_x_px=snapshot.translation_x_px,
            translation_y_px=snapshot.translation_y_px,
            translation_magnitude_px=snapshot.translation_magnitude_px,
            axis_rotation_deg=snapshot.axis_rotation_deg,
        )
        payload = _payload(
            node=node,
            frame=frame,
            geometry=geometry,
            process_profile=process_profile,
            calibration=calibration,
            calibration_valid=calibration_valid,
            calibration_reasons=calibration_reasons,
            track_id=track_id,
            source_object=source_object,
            tracking_mode=snapshot.tracking_mode,
            relock_status=snapshot.relock_status,
            relock_keyframe_id=snapshot.relock_keyframe_id,
            delta_raw=snapshot.delta_raw_phase,
            cumulative_raw=snapshot.cumulative_raw_phase,
            last_safe_raw=snapshot.last_safe_raw_phase,
            confidence=snapshot.confidence,
            detected_count=snapshot.detected_feature_count,
            estimate=estimate,
            trace_generation=snapshot.trace_generation,
            failure_count=snapshot.failure_count,
            reason_codes=list(snapshot.reason_codes),
            high_rate_snapshot=snapshot,
        )
        outputs: dict[str, Any] = {"surface_phase": payload}
        if payload.relock_status == "RELOCKED":
            outputs["relock_events"] = EventsPayload(
                events=[
                    EventPayload(
                        event_type="stem_surface_phase_relocked",
                        key=payload.track_id or "unknown-track",
                        payload={
                            "track_id": payload.track_id,
                            "raw_phase": payload.cumulative_raw_phase,
                            "phase_deg": payload.cumulative_phase_deg,
                            "confidence": payload.confidence,
                            "keyframe_id": payload.relock_keyframe_id,
                            "trace_generation": payload.trace_generation,
                        },
                    )
                ]
            )
        if bool((node.config or {}).get("debug_overlay", True)):
            outputs["overlay"] = _overlay(payload, process_profile)
        return NodeExecutionResult(outputs=outputs)


def _process_profile(
    frame: FramePayload,
) -> tuple[RuntimeStemCurvatureProcessCalibrationProfile | None, str | None]:
    raw = frame.metadata.get("stem_curvature_process_calibration")
    if raw is None:
        return None, "PHASE_PROCESS_CALIBRATION_MISSING"
    try:
        return validate_stem_curvature_process_calibration(raw), None
    except Exception:
        return None, "PHASE_PROCESS_CALIBRATION_INVALID"


def _binding_error(
    context: RuntimeExecutionContext,
    frame: FramePayload,
    segments: SegmentPayload,
    geometry: StemGeometryPayload,
    profile: RuntimeStemCurvatureProcessCalibrationProfile | None,
) -> str | None:
    if profile is None:
        return "PHASE_PROCESS_CALIBRATION_MISSING"
    image_context = frame.image_context
    if image_context is None or image_context.transform.transform_type not in {
        "perspective_rectify",
        "masked_crop",
    }:
        return "PHASE_PROCESS_RECTIFIED_FRAME_REQUIRED"
    if not _matching_image_contexts(frame, segments, geometry):
        return "PHASE_IMAGE_CONTEXT_MISMATCH"
    expected_width = image_context.transform.parameters.get(
        "output_width", profile.image_width
    )
    expected_height = image_context.transform.parameters.get(
        "output_height", profile.image_height
    )
    if (frame.width, frame.height) != (expected_width, expected_height):
        return "PHASE_RESOLUTION_MISMATCH"
    if frame.metadata.get("process_calibration_hash") != profile.calibration_hash:
        return "PHASE_PROCESS_CALIBRATION_HASH_MISMATCH"
    if context.channel_id is not None and context.channel_id != profile.source_channel_id:
        return "PHASE_PROCESS_CALIBRATION_CHANNEL_MISMATCH"
    return None


def _calibration(
    node: RuntimeNodeSpec,
) -> tuple[RuntimeStemSurfacePhaseCalibrationProfile | None, str | None]:
    raw = (node.config or {}).get("surface_phase_calibration")
    if raw is None:
        return None, None
    try:
        return validate_stem_surface_phase_calibration(raw), None
    except Exception:
        return None, "PHASE_CALIBRATION_INVALID"


def _phase_motion_allowed(
    segments: SegmentPayload,
    *,
    config: Mapping[str, Any],
) -> tuple[bool, float | None]:
    if not bool(config.get("require_active_stem_motion_for_phase", True)):
        return True, None
    tracking = segments.metadata.get("active_stem_tracking")
    if not isinstance(tracking, Mapping):
        # Older non-tracker pipelines have no process-motion contract. The production
        # stem template supplies it and therefore uses the stationary hold.
        return True, None
    movement = tracking.get("movement")
    if not isinstance(movement, Mapping):
        return True, None
    velocity_x = _finite_motion_number(movement.get("velocity_x_mm_per_frame"))
    velocity_y = _finite_motion_number(movement.get("velocity_y_mm_per_frame"))
    motion_axis = str(movement.get("process_motion_axis") or "unavailable").casefold()
    if velocity_x is None and velocity_y is None:
        return motion_axis != "stationary", None
    speed = math.hypot(velocity_x or 0.0, velocity_y or 0.0)
    try:
        minimum_speed = max(
            float(config.get("minimum_phase_motion_mm_per_frame", 3.0)),
            0.0,
        )
    except (TypeError, ValueError):
        minimum_speed = 3.0
    return motion_axis != "stationary" and speed >= minimum_speed, speed


def _finite_motion_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _surface_roi_mask(
    profile: RuntimeStemCurvatureProcessCalibrationProfile, *, cv2: Any
) -> np.ndarray:
    image_points = process_points_to_image(
        list(profile.surface_tracking_roi.process_points),
        profile,
    )
    points = np.asarray(
        [[point.x_px, point.y_px] for point in image_points],
        dtype=np.float64,
    )
    mask = np.zeros((profile.image_height, profile.image_width), dtype=np.uint8)
    cv2.fillPoly(mask, [np.rint(points).astype(np.int32)], 255)
    return mask


def _canonical_patch(
    gray: np.ndarray, mask: np.ndarray, axis: Any, *, cv2: Any
) -> tuple[np.ndarray, np.ndarray]:
    angle = math.degrees(math.atan2(axis.direction_y, axis.direction_x))
    matrix = cv2.getRotationMatrix2D((axis.center_x_px, axis.center_y_px), angle, 1.0)
    rotated_gray = cv2.warpAffine(
        gray, matrix, (axis.image_width, axis.image_height), flags=cv2.INTER_LINEAR
    )
    rotated_mask = cv2.warpAffine(
        mask, matrix, (axis.image_width, axis.image_height), flags=cv2.INTER_NEAREST
    )
    width = max(int(round(axis.length_px * 0.8)), 16)
    height = max(int(round(axis.median_width_px * 0.75)), 12)
    patch = cv2.getRectSubPix(rotated_gray, (width, height), (axis.center_x_px, axis.center_y_px))
    patch_mask = cv2.getRectSubPix(
        rotated_mask, (width, height), (axis.center_x_px, axis.center_y_px)
    )
    patch = cv2.resize(patch, (192, 64), interpolation=cv2.INTER_LINEAR)
    patch_mask = cv2.resize(patch_mask, (192, 64), interpolation=cv2.INTER_NEAREST)
    patch = cv2.bitwise_and(patch, patch, mask=patch_mask)
    return patch, patch_mask


def _keyframe_features(
    patch: np.ndarray, patch_mask: np.ndarray, *, cv2: Any
) -> tuple[np.ndarray, np.ndarray | None]:
    detector = cv2.ORB_create(nfeatures=240, fastThreshold=8, edgeThreshold=5)
    keypoints, descriptors = detector.detectAndCompute(patch, patch_mask)
    points = (
        np.asarray([item.pt for item in keypoints], dtype=np.float32)
        if keypoints
        else np.empty((0, 2), dtype=np.float32)
    )
    return points, descriptors


def _maybe_store_keyframe(
    state: dict[str, Any],
    patch: np.ndarray,
    points: np.ndarray,
    descriptors: np.ndarray | None,
    *,
    raw_phase: float,
    sequence: int,
    config: Mapping[str, Any],
) -> None:
    minimum_features = int(config.get("keyframe_minimum_features", 12))
    if descriptors is None or len(points) < minimum_features:
        return
    keyframes = state.setdefault("keyframes", [])
    separation = float(config.get("keyframe_minimum_raw_separation", 0.08))
    if keyframes and abs(raw_phase - float(keyframes[-1]["raw_phase"])) < separation:
        return
    keyframes.append(
        {
            "keyframe_id": f"kf-{int(state.get('trace_generation') or 0)}-{sequence}",
            "raw_phase": raw_phase,
            "canonical_patch": patch.copy(),
            "points": points.copy(),
            "descriptors": descriptors.copy(),
            "frame_sequence": sequence,
        }
    )
    del keyframes[: max(0, len(keyframes) - int(config.get("maximum_keyframes", 24)))]


def _relock_candidates(
    state: Mapping[str, Any],
    current_points: np.ndarray,
    current_descriptors: np.ndarray | None,
) -> list[PhaseRelockCandidate]:
    if current_descriptors is None or len(current_points) < 4:
        return []
    cv2 = _opencv()
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    candidates: list[PhaseRelockCandidate] = []
    for item in state.get("keyframes") or []:
        descriptors = item.get("descriptors")
        points = item.get("points")
        if (
            not isinstance(descriptors, np.ndarray)
            or not isinstance(points, np.ndarray)
            or len(descriptors) < 4
        ):
            continue
        pairs = matcher.knnMatch(descriptors, current_descriptors, k=2)
        good = [
            pair[0]
            for pair in pairs
            if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance
        ]
        if len(good) < 4:
            continue
        source = np.asarray([points[match.queryIdx] for match in good], dtype=np.float32)
        target = np.asarray([current_points[match.trainIdx] for match in good], dtype=np.float32)
        _matrix, inliers = cv2.estimateAffinePartial2D(
            source, target, method=cv2.RANSAC, ransacReprojThreshold=3.0
        )
        inlier_count = int(np.count_nonzero(inliers)) if inliers is not None else 0
        ratio = inlier_count / len(good)
        score = min(0.7 * ratio + 0.3 * min(inlier_count / 20.0, 1.0), 1.0)
        candidates.append(
            PhaseRelockCandidate(
                keyframe_id=str(item["keyframe_id"]),
                raw_phase=float(item["raw_phase"]),
                score=score,
                matched_feature_count=len(good),
                inlier_ratio=ratio,
            )
        )
    return candidates


def _node_state(context: RuntimeExecutionContext, node: RuntimeNodeSpec) -> dict[str, Any]:
    state = context.state.setdefault(node.node_key, {})
    if not isinstance(state, dict):
        state = {}
        context.state[node.node_key] = state
    state.setdefault("schema_version", _STATE_SCHEMA)
    state.setdefault("trace_generation", 0)
    state.setdefault("failure_count", 0)
    state.setdefault("keyframes", [])
    state.setdefault("phase_locked", False)
    state.setdefault("cumulative_translation_x_px", 0.0)
    state.setdefault("cumulative_translation_y_px", 0.0)
    return state


def _phase_trace_gap_reason(
    previous: Mapping[str, Any],
    *,
    frame_sequence: int,
    timestamp: str,
    maximum_frame_gap: int,
    maximum_time_gap_seconds: float,
) -> str | None:
    """Use capture time as the authoritative continuity check when available.

    Camera sequence numbers count every acquired frame, while a serialized realtime
    pipeline intentionally processes only the newest available frame.  A large sequence
    delta therefore does not by itself prove that optical-flow state is stale.
    """

    previous_sequence = int(previous.get("frame_sequence") or -1)
    if frame_sequence <= previous_sequence:
        return "PHASE_FRAME_SEQUENCE_NON_MONOTONIC"
    previous_timestamp = _parse_timestamp(previous.get("timestamp"))
    current_timestamp = _parse_timestamp(timestamp)
    if previous_timestamp is not None and current_timestamp is not None:
        delta_seconds = (current_timestamp - previous_timestamp).total_seconds()
        if delta_seconds <= 0:
            return "PHASE_TIMESTAMP_NON_MONOTONIC"
        if delta_seconds > maximum_time_gap_seconds:
            return "PHASE_TIME_GAP"
        return None
    if frame_sequence - previous_sequence > maximum_frame_gap:
        return "PHASE_FRAME_GAP"
    return None


def _reset_state(
    state: dict[str, Any], *, track_id: str | None, increment_generation: bool
) -> None:
    generation = int(state.get("trace_generation") or 0) + (1 if increment_generation else 0)
    state.clear()
    state.update(
        {
            "schema_version": _STATE_SCHEMA,
            "trace_generation": generation,
            "failure_count": 0,
            "track_id": track_id,
            "keyframes": [],
            "phase_locked": False,
            "relock_status": "NOT_ATTEMPTED",
            "cumulative_translation_x_px": 0.0,
            "cumulative_translation_y_px": 0.0,
        }
    )


def _lose_lock(state: dict[str, Any], *, increment_generation: bool = False) -> None:
    state["phase_locked"] = False
    state["failure_count"] = int(state.get("failure_count") or 0) + 1
    if increment_generation:
        state["trace_generation"] = int(state.get("trace_generation") or 0) + 1


def _store_previous(
    state: dict[str, Any],
    gray: np.ndarray,
    eroded_mask: np.ndarray,
    axis: Any,
    frame_sequence: int,
    timestamp: str,
    feature_points: np.ndarray,
    canonical_patch: np.ndarray,
) -> None:
    state["previous"] = {
        "gray": gray,
        "eroded_mask": eroded_mask,
        "axis": axis,
        "frame_sequence": frame_sequence,
        "timestamp": timestamp,
        "feature_points": feature_points,
        "canonical_patch": canonical_patch,
    }


def _session_ended(events: object) -> bool:
    if not isinstance(events, EventsPayload):
        return False
    endings = {"session_end", "stem_session_end", "track_completed", "stem_track_completed"}
    return any(item.event_type.lower() in endings for item in events.events)


def _direction(delta: float | None) -> str:
    if delta is None:
        return "unknown"
    if abs(delta) <= 1e-4:
        return "stationary"
    return "positive" if delta > 0 else "negative"


def _payload(
    *,
    node: RuntimeNodeSpec,
    frame: FramePayload | None = None,
    geometry: StemGeometryPayload | None = None,
    process_profile: RuntimeStemCurvatureProcessCalibrationProfile | None = None,
    calibration: RuntimeStemSurfacePhaseCalibrationProfile | None = None,
    calibration_valid: bool = False,
    calibration_reasons: list[str] | None = None,
    track_id: str | None = None,
    source_object: RuntimeObjectReference | None = None,
    tracking_mode: str,
    relock_status: str = "NOT_ATTEMPTED",
    relock_keyframe_id: str | None = None,
    delta_raw: float | None = None,
    cumulative_raw: float | None = None,
    last_safe_raw: float | None = None,
    confidence: float = 0.0,
    detected_count: int = 0,
    estimate: Any = None,
    matched_count_override: int | None = None,
    inlier_ratio_override: float | None = None,
    trace_generation: int = 0,
    failure_count: int = 0,
    keyframe_count: int = 0,
    reason_codes: list[str] | None = None,
    motion_gate_status: str | None = None,
    motion_speed_mm_per_frame: float | None = None,
    high_rate_snapshot: HighRatePhaseSnapshot | None = None,
    cumulative_translation_x_px: float = 0.0,
    cumulative_translation_y_px: float = 0.0,
    **_ignored: Any,
) -> StemSurfacePhasePayload:
    high_rate = high_rate_snapshot is not None
    raw_valid = cumulative_raw is not None and tracking_mode != "LOST"
    phase_locked = (
        bool(high_rate_snapshot.phase_locked)
        if high_rate_snapshot is not None
        else raw_valid
    )
    calibrated = raw_valid and calibration_valid and calibration is not None
    reference_axis = geometry.reference_axis if geometry is not None else None
    center_x_px = (
        (reference_axis.start.x + reference_axis.end.x) / 2.0
        if reference_axis is not None
        else None
    )
    center_y_px = (
        (reference_axis.start.y + reference_axis.end.y) / 2.0
        if reference_axis is not None
        else None
    )
    diameter_mm = (
        _metric_cross_section_diameter_mm(geometry, process_profile)
        if geometry is not None and process_profile is not None
        else None
    )
    delta_deg = (
        delta_raw * float(calibration.degrees_per_raw_phase)
        if calibrated and delta_raw is not None
        else None
    )
    cumulative_deg = (
        cumulative_raw * float(calibration.degrees_per_raw_phase)
        if calibrated and cumulative_raw is not None
        else None
    )
    reasons = list(dict.fromkeys([*(reason_codes or []), *(calibration_reasons or [])]))
    matched_count = (
        int(matched_count_override)
        if matched_count_override is not None
        else int(getattr(estimate, "matched_feature_count", 0))
    )
    inlier_ratio = (
        float(inlier_ratio_override)
        if inlier_ratio_override is not None
        else float(getattr(estimate, "inlier_ratio", 0.0))
    )
    provenance: list[ProvenanceLink] = []
    if process_profile is not None:
        provenance.append(
            ProvenanceLink(
                source_kind="operator_config",
                source_id=process_profile.calibration_hash,
                relationship="uses_stem_curvature_process_calibration",
                node_key=node.node_key,
            )
        )
    if calibration is not None:
        provenance.append(
            ProvenanceLink(
                source_kind="operator_config",
                source_id=calibration.calibration_hash,
                relationship="uses_stem_surface_phase_calibration",
                node_key=node.node_key,
            )
        )
    payload_frame_sequence = (
        high_rate_snapshot.frame_sequence
        if high_rate_snapshot is not None
        else (geometry.frame_sequence if geometry else None)
    )
    payload_timestamp = (
        high_rate_snapshot.timestamp
        if high_rate_snapshot is not None
        else (geometry.timestamp if geometry else (frame.timestamp if frame else None))
    )
    step_translation_x_px = float(
        high_rate_snapshot.translation_x_px
        if high_rate_snapshot is not None
        else getattr(estimate, "translation_x_px", 0.0)
    )
    step_translation_y_px = float(
        high_rate_snapshot.translation_y_px
        if high_rate_snapshot is not None
        else getattr(estimate, "translation_y_px", 0.0)
    )
    total_translation_x_px = float(
        high_rate_snapshot.cumulative_translation_x_px
        if high_rate_snapshot is not None
        else cumulative_translation_x_px
    )
    total_translation_y_px = float(
        high_rate_snapshot.cumulative_translation_y_px
        if high_rate_snapshot is not None
        else cumulative_translation_y_px
    )
    translation_component = _metric_translation_component(
        geometry=geometry,
        process_profile=process_profile,
        step_x_px=step_translation_x_px,
        step_y_px=step_translation_y_px,
        cumulative_x_px=total_translation_x_px,
        cumulative_y_px=total_translation_y_px,
    )
    return StemSurfacePhasePayload(
        algorithm_version=(
            "stem.surface-phase.cylindrical-high-rate.v2"
            if high_rate
            else "stem.surface-phase.lk-keyframe-relock.v1"
        ),
        phase_estimator_version=(
            "CYLINDRICAL_SURFACE_V2" if high_rate else "LK_KEYFRAME_RELOCK_V1"
        ),
        execution_mode="HIGH_RATE_SEEDED" if high_rate else "SYNCHRONOUS",
        raw_phase_valid=raw_valid,
        calibrated_phase_valid=calibrated,
        phase_locked=phase_locked,
        track_id=track_id or (geometry.track_id if geometry else None),
        source_object_id=source_object.object_id
        if source_object
        else (geometry.source_object_id if geometry else None),
        image_context_id=geometry.image_context_id
        if geometry
        else (frame.image_context.context_id if frame and frame.image_context else None),
        frame_sequence=payload_frame_sequence,
        timestamp=payload_timestamp,
        delta_raw_phase=delta_raw,
        cumulative_raw_phase=cumulative_raw,
        last_safe_raw_phase=last_safe_raw,
        delta_phase_deg=delta_deg,
        cumulative_phase_deg=cumulative_deg,
        direction=_direction(delta_deg if calibrated else delta_raw),
        tracking_mode=tracking_mode,
        relock_status=relock_status,
        relock_keyframe_id=relock_keyframe_id,
        keyframe_count=keyframe_count,
        detected_feature_count=detected_count,
        matched_feature_count=matched_count,
        inlier_feature_count=int(getattr(estimate, "inlier_feature_count", 0)),
        inlier_ratio=inlier_ratio,
        median_tracking_error_px=getattr(estimate, "median_tracking_error_px", None),
        forward_backward_error_px=(
            high_rate_snapshot.forward_backward_error_px
            if high_rate_snapshot is not None
            else None
        ),
        feature_span_ratio=(
            high_rate_snapshot.feature_span_ratio
            if high_rate_snapshot is not None
            else 0.0
        ),
        blur_score=(
            high_rate_snapshot.blur_score if high_rate_snapshot is not None else None
        ),
        translation_component=translation_component,
        compensated_axis_rotation_deg=float(getattr(estimate, "axis_rotation_deg", 0.0)),
        confidence=confidence,
        trace_generation=trace_generation,
        failure_count=failure_count,
        source_phase_fps=(
            high_rate_snapshot.source_phase_fps
            if high_rate_snapshot is not None
            else None
        ),
        processed_phase_fps=(
            high_rate_snapshot.processed_phase_fps
            if high_rate_snapshot is not None
            else None
        ),
        source_frame_gap=(
            high_rate_snapshot.source_frame_gap
            if high_rate_snapshot is not None
            else None
        ),
        seed_age_ms=(
            high_rate_snapshot.seed_age_ms if high_rate_snapshot is not None else None
        ),
        fast_path_latency_ms=(
            high_rate_snapshot.fast_path_latency_ms
            if high_rate_snapshot is not None
            else None
        ),
        fast_path_dropped_frames=(
            high_rate_snapshot.fast_path_dropped_frames
            if high_rate_snapshot is not None
            else 0
        ),
        seed_status=(
            high_rate_snapshot.seed_status if high_rate_snapshot is not None else "NOT_USED"
        ),
        calibration_id=calibration.calibration_id if calibration else None,
        calibration_version=calibration.calibration_version if calibration else None,
        calibration_hash=calibration.calibration_hash if calibration else None,
        calibration_fit_model=calibration.fit_model if calibration else None,
        calibration_mean_abs_error_deg=calibration.mean_abs_error_deg if calibration else None,
        calibration_rms_error_deg=calibration.rms_error_deg if calibration else None,
        calibration_maximum_abs_error_deg=calibration.maximum_abs_error_deg
        if calibration
        else None,
        intrinsic_calibration_hash=process_profile.intrinsic_calibration_hash
        if process_profile
        else None,
        process_calibration_hash=process_profile.calibration_hash if process_profile else None,
        rotation_reference_view=process_profile.rotation_reference_view
        if process_profile
        else None,
        positive_rotation=process_profile.positive_rotation if process_profile else None,
        reason_codes=reasons,
        provenance=provenance,
        metadata={
            "phase_calibration_capture": {
                "eligible": bool(
                    raw_valid
                    and phase_locked
                    and center_x_px is not None
                    and center_y_px is not None
                    and diameter_mm is not None
                ),
                "reference_eligible": bool(
                    raw_valid
                    and phase_locked
                    and detected_count > 0
                    and center_x_px is not None
                    and center_y_px is not None
                    and diameter_mm is not None
                ),
                # An INITIALIZING payload is useful as the relative zero reference,
                # but it is not a measured rotation sample.  Keep this distinction
                # explicit so commissioning cannot fit several tracker restarts as
                # one continuous raw-phase trace.
                "measurement_eligible": bool(
                    raw_valid
                    and phase_locked
                    and tracking_mode in {"INCREMENTAL", "KEYFRAME_RELOCK"}
                    and "PHASE_STATIONARY_HOLD" not in reasons
                    and detected_count > 0
                    and center_x_px is not None
                    and center_y_px is not None
                    and diameter_mm is not None
                ),
                "track_id": track_id or (geometry.track_id if geometry else None),
                "frame_sequence": payload_frame_sequence,
                "timestamp": payload_timestamp,
                "raw_phase": cumulative_raw,
                "delta_raw_phase": delta_raw,
                "phase_locked": phase_locked,
                "tracking_mode": tracking_mode,
                "relock_status": relock_status,
                "trace_generation": trace_generation,
                "failure_count": failure_count,
                "confidence": confidence,
                "motion_gate_status": motion_gate_status,
                "motion_speed_mm_per_frame": motion_speed_mm_per_frame,
                "detected_feature_count": detected_count,
                "inlier_ratio": inlier_ratio,
                "matched_feature_count": matched_count,
                "phase_estimator_version": (
                    "CYLINDRICAL_SURFACE_V2"
                    if high_rate
                    else "LK_KEYFRAME_RELOCK_V1"
                ),
                "execution_mode": "HIGH_RATE_SEEDED" if high_rate else "SYNCHRONOUS",
                "source_phase_fps": (
                    high_rate_snapshot.source_phase_fps
                    if high_rate_snapshot is not None
                    else None
                ),
                "processed_phase_fps": (
                    high_rate_snapshot.processed_phase_fps
                    if high_rate_snapshot is not None
                    else None
                ),
                "source_frame_gap": (
                    high_rate_snapshot.source_frame_gap
                    if high_rate_snapshot is not None
                    else None
                ),
                "seed_age_ms": (
                    high_rate_snapshot.seed_age_ms
                    if high_rate_snapshot is not None
                    else None
                ),
                "fast_path_latency_ms": (
                    high_rate_snapshot.fast_path_latency_ms
                    if high_rate_snapshot is not None
                    else None
                ),
                "fast_path_dropped_frames": (
                    high_rate_snapshot.fast_path_dropped_frames
                    if high_rate_snapshot is not None
                    else 0
                ),
                "forward_backward_error_px": (
                    high_rate_snapshot.forward_backward_error_px
                    if high_rate_snapshot is not None
                    else None
                ),
                "feature_span_ratio": (
                    high_rate_snapshot.feature_span_ratio
                    if high_rate_snapshot is not None
                    else 0.0
                ),
                "blur_score": (
                    high_rate_snapshot.blur_score
                    if high_rate_snapshot is not None
                    else None
                ),
                "seed_status": (
                    high_rate_snapshot.seed_status
                    if high_rate_snapshot is not None
                    else "NOT_USED"
                ),
                "diameter_mm": diameter_mm,
                "diameter_method": (
                    "PROJECTED_XY_CROSS_SECTION_MEDIAN_V1"
                    if diameter_mm is not None
                    else None
                ),
                "image_center_x_px": center_x_px,
                "image_center_y_px": center_y_px,
            }
        },
    )


def _metric_translation_component(
    *,
    geometry: StemGeometryPayload | None,
    process_profile: RuntimeStemCurvatureProcessCalibrationProfile | None,
    step_x_px: float,
    step_y_px: float,
    cumulative_x_px: float,
    cumulative_y_px: float,
) -> StemRotationTranslationComponent:
    metric: dict[str, float | None] = {
        "x_mm": None,
        "y_mm": None,
        "magnitude_mm": None,
        "cumulative_x_mm": None,
        "cumulative_y_mm": None,
        "cumulative_magnitude_mm": None,
    }
    axis = geometry.reference_axis if geometry is not None else None
    if axis is not None and process_profile is not None:
        center = (
            (axis.start.x + axis.end.x) * 0.5,
            (axis.start.y + axis.end.y) * 0.5,
        )
        try:
            projected = image_points_to_process(
                [
                    center,
                    (center[0] - step_x_px, center[1] - step_y_px),
                    (
                        center[0] - cumulative_x_px,
                        center[1] - cumulative_y_px,
                    ),
                ],
                process_profile,
            )
        except (TypeError, ValueError, RuntimeError):
            projected = []
        if len(projected) == 3:
            step_x_mm = projected[0].x_mm - projected[1].x_mm
            step_y_mm = projected[0].y_mm - projected[1].y_mm
            total_x_mm = projected[0].x_mm - projected[2].x_mm
            total_y_mm = projected[0].y_mm - projected[2].y_mm
            if all(
                math.isfinite(item)
                for item in (step_x_mm, step_y_mm, total_x_mm, total_y_mm)
            ):
                metric = {
                    "x_mm": step_x_mm,
                    "y_mm": step_y_mm,
                    "magnitude_mm": math.hypot(step_x_mm, step_y_mm),
                    "cumulative_x_mm": total_x_mm,
                    "cumulative_y_mm": total_y_mm,
                    "cumulative_magnitude_mm": math.hypot(total_x_mm, total_y_mm),
                }
    return StemRotationTranslationComponent(
        x_px=step_x_px,
        y_px=step_y_px,
        magnitude_px=math.hypot(step_x_px, step_y_px),
        cumulative_x_px=cumulative_x_px,
        cumulative_y_px=cumulative_y_px,
        cumulative_magnitude_px=math.hypot(cumulative_x_px, cumulative_y_px),
        **metric,
    )


def _metric_cross_section_diameter_mm(
    geometry: StemGeometryPayload,
    process_profile: RuntimeStemCurvatureProcessCalibrationProfile,
) -> float | None:
    """Measure visible stem width after projecting cross-section endpoints to X/Y.

    A global pixels-to-Y scale is invalid under a projective process calibration.
    Each image-domain cross-section is therefore projected independently and its
    Euclidean X/Y distance is measured before the robust median is taken.
    """

    axis = geometry.reference_axis
    if axis is None or geometry.median_width_px is None:
        return None
    normal = np.asarray([axis.normal.x, axis.normal.y], dtype=np.float64)
    normal_length = float(np.linalg.norm(normal))
    if not math.isfinite(normal_length) or normal_length <= 1e-9:
        return None
    normal /= normal_length

    samples: list[tuple[float, float, float]] = []
    center_s = np.asarray(geometry.normalized_s, dtype=np.float64)
    center_x = np.asarray(
        [point.x for point in geometry.centerline_image_xy], dtype=np.float64
    )
    center_y = np.asarray(
        [point.y for point in geometry.centerline_image_xy], dtype=np.float64
    )
    width_s = geometry.width_normalized_s
    widths = geometry.width_profile_px
    valid_widths = geometry.width_valid_mask
    if (
        len(center_s) >= 2
        and len(center_s) == len(center_x) == len(center_y)
        and len(width_s) == len(widths)
    ):
        for index, (normalized_s, width_px) in enumerate(
            zip(width_s, widths, strict=True)
        ):
            if index < len(valid_widths) and not valid_widths[index]:
                continue
            if not math.isfinite(width_px) or width_px <= 0:
                continue
            samples.append(
                (
                    float(np.interp(normalized_s, center_s, center_x)),
                    float(np.interp(normalized_s, center_s, center_y)),
                    float(width_px),
                )
            )
    if not samples:
        samples.append(
            (
                (axis.start.x + axis.end.x) * 0.5,
                (axis.start.y + axis.end.y) * 0.5,
                float(geometry.median_width_px),
            )
        )

    image_endpoints: list[tuple[float, float]] = []
    for center_x_px, center_y_px, width_px in samples:
        half_width = width_px * 0.5
        image_endpoints.extend(
            (
                (
                    center_x_px - normal[0] * half_width,
                    center_y_px - normal[1] * half_width,
                ),
                (
                    center_x_px + normal[0] * half_width,
                    center_y_px + normal[1] * half_width,
                ),
            )
        )
    try:
        process_endpoints = image_points_to_process(image_endpoints, process_profile)
    except (TypeError, ValueError, RuntimeError):
        return None
    metric_widths = [
        math.hypot(right.x_mm - left.x_mm, right.y_mm - left.y_mm)
        for left, right in zip(
            process_endpoints[0::2], process_endpoints[1::2], strict=True
        )
    ]
    valid_metric_widths = [
        width for width in metric_widths if math.isfinite(width) and width > 0
    ]
    return (
        float(np.median(np.asarray(valid_metric_widths, dtype=np.float64)))
        if valid_metric_widths
        else None
    )


def _overlay(
    payload: StemSurfacePhasePayload,
    profile: RuntimeStemCurvatureProcessCalibrationProfile | None,
) -> OverlayPayload:
    raw_text = (
        f"raw={payload.cumulative_raw_phase:.4f}"
        if payload.cumulative_raw_phase is not None
        else "raw=lost"
    )
    degree_text = (
        f" phase={payload.cumulative_phase_deg:.1f} deg"
        if payload.cumulative_phase_deg is not None
        else " phase=uncalibrated"
    )
    primitives: list[OverlayPrimitive] = []
    if profile is not None:
        projected_roi = process_points_to_image(
            list(profile.surface_tracking_roi.process_points),
            profile,
        )
        width = max(profile.image_width - 1, 1)
        height = max(profile.image_height - 1, 1)
        primitives.append(
            OverlayPrimitive(
                primitive_type="polygon",
                layer="stem_surface_tracking_roi",
                label="Surface Tracking ROI",
                geometry={
                    "points": [
                        {
                            "x": min(max(point.x_px / width, 0.0), 1.0),
                            "y": min(max(point.y_px / height, 0.0), 1.0),
                        }
                        for point in projected_roi
                    ],
                    "coordinate_system": "normalized_xy",
                },
                image_context_id=payload.image_context_id,
                style={"stroke": "#7e57c2", "stroke_width": 2, "fill_opacity": 0.04},
                metadata={"process_calibration_hash": profile.calibration_hash},
            )
        )
    calibration_capture = dict(payload.metadata.get("phase_calibration_capture") or {})
    diameter_mm = calibration_capture.get("diameter_mm")
    diameter_text = (
        f" diameter={float(diameter_mm):.0f}mm"
        if isinstance(diameter_mm, int | float) and not isinstance(diameter_mm, bool)
        else ""
    )
    fast_path_text = (
        f" fast={payload.processed_phase_fps:.1f}fps"
        f" seed={payload.seed_age_ms:.0f}ms"
        f" drop={payload.fast_path_dropped_frames}"
        if payload.execution_mode == "HIGH_RATE_SEEDED"
        and payload.processed_phase_fps is not None
        and payload.seed_age_ms is not None
        else ""
    )
    primitives.append(
        OverlayPrimitive(
            primitive_type="label",
            layer="stem_surface_phase",
            geometry={"x": 18.0, "y": 28.0},
            label=(
                f"{payload.tracking_mode} {raw_text}{degree_text}{diameter_text} "
                f"conf={payload.confidence:.2f}{fast_path_text}"
            ),
            confidence=payload.confidence,
            image_context_id=payload.image_context_id,
            style={"color": "#66bb6a" if payload.phase_locked else "#ff7043"},
            metadata={
                **calibration_capture,
                "calibration_id": payload.calibration_id,
                "calibrated_phase_valid": payload.calibrated_phase_valid,
                "cumulative_phase_deg": payload.cumulative_phase_deg,
                "phase_estimator_version": payload.phase_estimator_version,
                "execution_mode": payload.execution_mode,
                "source_phase_fps": payload.source_phase_fps,
                "processed_phase_fps": payload.processed_phase_fps,
                "seed_age_ms": payload.seed_age_ms,
                "fast_path_latency_ms": payload.fast_path_latency_ms,
                "fast_path_dropped_frames": payload.fast_path_dropped_frames,
                "source_frame_gap": payload.source_frame_gap,
                "forward_backward_error_px": payload.forward_backward_error_px,
                "feature_span_ratio": payload.feature_span_ratio,
                "blur_score": payload.blur_score,
                "seed_status": payload.seed_status,
                "reason_codes": payload.reason_codes,
            },
        )
    )
    if payload.execution_mode == "HIGH_RATE_SEEDED":
        processed_fps = (
            f"{payload.processed_phase_fps:.1f}"
            if payload.processed_phase_fps is not None
            else "n/a"
        )
        source_fps = (
            f"{payload.source_phase_fps:.1f}"
            if payload.source_phase_fps is not None
            else "n/a"
        )
        seed_age = (
            f"{payload.seed_age_ms:.0f}ms"
            if payload.seed_age_ms is not None
            else "n/a"
        )
        latency = (
            f"{payload.fast_path_latency_ms:.0f}ms"
            if payload.fast_path_latency_ms is not None
            else "n/a"
        )
        blur = f"{payload.blur_score:.1f}" if payload.blur_score is not None else "n/a"
        roundtrip = (
            f"{payload.forward_backward_error_px:.2f}px"
            if payload.forward_backward_error_px is not None
            else "n/a"
        )
        primitives.append(
            OverlayPrimitive(
                primitive_type="label",
                layer="stem_surface_phase_fast_path",
                geometry={"x": 18.0, "y": 52.0},
                label=(
                    f"Phase V2 {processed_fps}/{source_fps} fps · seed {seed_age} "
                    f"· latency {latency} · queue drops {payload.fast_path_dropped_frames} "
                    f"· features {payload.matched_feature_count}/"
                    f"{payload.detected_feature_count} · blur {blur} · FB {roundtrip}"
                ),
                confidence=payload.confidence,
                image_context_id=payload.image_context_id,
                style={"color": "#80cbc4" if payload.phase_locked else "#ffb74d"},
                metadata={
                    "tracking_mode": payload.tracking_mode,
                    "seed_status": payload.seed_status,
                    "feature_span_ratio": payload.feature_span_ratio,
                    "reason_codes": payload.reason_codes,
                },
            )
        )
    return OverlayPayload(
        display_context_id=payload.image_context_id,
        primitives=primitives,
    )


analysis_stem_surface_phase = StemSurfacePhaseHandler()
