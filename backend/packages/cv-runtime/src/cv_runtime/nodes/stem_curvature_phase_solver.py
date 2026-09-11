from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cv_contracts.runtime import RuntimeNodeSpec

from cv_runtime.context import NodeInputs, RuntimeExecutionContext
from cv_runtime.handlers import NodeExecutionResult
from cv_runtime.payloads import (
    EventsPayload,
    OverlayPayload,
    OverlayPrimitive,
    ProvenanceLink,
    SegmentPayload,
    StemCurvaturePhaseDiagnosticCurvePoint,
    StemCurvaturePhaseDiagnosticGraph,
    StemCurvaturePhaseDiagnosticPoint,
    StemCurvaturePhaseHarmonicFit,
    StemCurvaturePhaseSolutionPayload,
    StemHorizontalCurvaturePayload,
    StemOrientationResultV3,
    StemSurfacePhasePayload,
)
from cv_runtime.stem_curvature_phase_solver import (
    ALGORITHM_VERSION,
    HarmonicFit,
    StemCurvaturePhaseFitResult,
    StemCurvaturePhaseObservation,
    StemCurvaturePhaseSolverConfig,
    solve_stem_curvature_phase,
    wrap_to_minus_90_plus_90,
)

_STATE_SCHEMA = "stem.curvature-phase-session.state.v3"
_SESSION_START_EVENTS = {
    "session_start",
    "stem_session_start",
    "track_started",
    "stem_track_started",
}
_SESSION_END_EVENTS = {
    "session_end",
    "stem_session_end",
    "track_completed",
    "stem_track_completed",
}
_RESET_EVENTS = {
    "pipeline_reset",
    "runtime_reset",
    "pipeline_stopped",
}
_FREEZE_EVENTS = {
    "curvature_freeze",
    "curvature_freeze_gate",
    "curvature_freeze_gate_reached",
    "stem_curvature_freeze_gate_reached",
}
_DECISION_EVENTS = {
    "decision_gate",
    "decision_gate_reached",
    "stem_decision_gate_reached",
}
_LANDED_EVENTS = {
    "stem_landed",
    "stem_settled",
    "stem_impact_complete",
}


@dataclass(frozen=True)
class _ActiveStemInput:
    present: bool
    unique: bool
    track_id: str | None
    confidence: float
    metadata: Mapping[str, Any]


class StemCurvaturePhaseSolverHandler:
    node_type = "analysis.stem_curvature_phase_solver"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        state = _node_state(context, node)
        events = inputs.get("events")
        event_types = _event_types(events)
        event_session_id = _event_session_id(events)
        curvature = inputs.get("curvature")
        phase = inputs.get("surface_phase")
        active_stem = inputs.get("active_stem")

        if event_types & _RESET_EVENTS:
            _reset_state(state)
            _remember_reasons(state, ["PIPELINE_RESET"])
            return NodeExecutionResult(
                outputs={
                    "solution": _payload_from_state(
                        state,
                        fit=solve_stem_curvature_phase([]),
                        state_override="RESET",
                        reasons=["PIPELINE_RESET"],
                    )
                }
            )

        if event_types & _SESSION_END_EVENTS:
            if state.get("session_started"):
                config = _solver_config(node.config or {})
                session_config = _session_config(node.config or {})
                fit = _solve_state(state, config)
                if not isinstance(state.get("frozen_result"), Mapping):
                    result = _build_orientation_result(
                        state,
                        fit,
                        phase=None,
                        session_config=session_config,
                        decision_reasons=["SESSION_ENDED_BEFORE_DECISION"],
                        shadow_mode=bool((node.config or {}).get("shadow_mode_enabled", False)),
                        shadow_evaluation_approved=bool(
                            (node.config or {}).get("shadow_evaluation_approved", False)
                        ),
                        minimum_validation=(node.config or {}).get(
                            "minimum_principle_validation"
                        ),
                        force_uncertain=True,
                    )
                    state["frozen_result"] = result.model_dump(mode="json")
                state["session_state"] = "DONE"
                state["session_completed"] = True
                _remember_reasons(state, ["SESSION_DONE"])
                return NodeExecutionResult(
                    outputs={
                        "solution": _payload_from_state(
                            state,
                            fit=fit,
                            state_override="FROZEN",
                            reasons=["SESSION_DONE"],
                            node=node,
                        )
                    }
                )
            _reset_state(state)
            _remember_reasons(state, ["SESSION_ENDED_RESET"])
            return NodeExecutionResult(
                outputs={
                    "solution": _payload_from_state(
                        state,
                        fit=solve_stem_curvature_phase([]),
                        state_override="RESET",
                        reasons=["SESSION_ENDED_RESET"],
                    )
                }
            )

        reasons: list[str] = []
        if state.get("session_completed"):
            _reset_state(state)
            reasons.append("PREVIOUS_SESSION_DONE_RESET")
        try:
            config = _solver_config(node.config or {})
            session_config = _session_config(node.config or {})
        except (TypeError, ValueError) as exc:
            reasons.append("SESSION_CONFIGURATION_INVALID")
            state["last_error_detail"] = str(exc)[:160]
            _remember_reasons(state, reasons)
            return NodeExecutionResult(
                outputs={
                    "solution": _payload_from_state(
                        state,
                        fit=solve_stem_curvature_phase([]),
                        state_override="INVALID",
                        reasons=reasons,
                    )
                }
            )
        state.pop("last_error_detail", None)

        active = _active_stem(active_stem)
        incoming_track = _incoming_track_id(curvature, phase, active)
        frozen_result = state.get("frozen_result")
        if isinstance(frozen_result, Mapping):
            if incoming_track and incoming_track != state.get("track_id"):
                _reset_state(state)
                reasons.append("PREVIOUS_SESSION_DONE_NEW_TRACK")
            else:
                if (
                    isinstance(phase, StemSurfacePhasePayload)
                    and phase.track_id == state.get("track_id")
                    and phase.calibrated_phase_valid
                    and phase.phase_locked
                ):
                    phase_diameter_mm = _optional_number(phase.metadata.get("diameter_mm"))
                    if phase_diameter_mm is not None:
                        state["diameter_mm"] = phase_diameter_mm
                    state["current_phase_deg"] = phase.cumulative_phase_deg
                    state["last_phase_confidence"] = phase.confidence
                    state["last_phase_inlier_ratio"] = phase.inlier_ratio
                    state["last_phase_frame_sequence"] = phase.frame_sequence
                    state["last_phase_timestamp"] = phase.timestamp
                    state["relock_status"] = phase.relock_status
                    _remember_phase_sample(state, phase, session_config)
                    reasons.append("PHASE_TRACKING_CONTINUED_AFTER_DECISION")
                elif isinstance(phase, StemSurfacePhasePayload):
                    reasons.append("POST_DECISION_PHASE_UPDATE_REJECTED")
                state["session_state"] = "RESULT_READY"
                solution = _payload_from_state(
                    state,
                    fit=_solve_state(state, config),
                    curvature=(
                        curvature
                        if isinstance(curvature, StemHorizontalCurvaturePayload)
                        else None
                    ),
                    phase=(phase if isinstance(phase, StemSurfacePhasePayload) else None),
                    state_override="FROZEN",
                    reasons=["SESSION_RESULT_ALREADY_FROZEN", *reasons],
                    node=node,
                )
                return NodeExecutionResult(
                    outputs={
                        "solution": solution,
                        "overlay": _diagnostic_overlay(
                            solution,
                            active_stem=(
                                active_stem if isinstance(active_stem, SegmentPayload) else None
                            ),
                        ),
                    }
                )

        if not isinstance(curvature, StemHorizontalCurvaturePayload) or not isinstance(
            phase, StemSurfacePhasePayload
        ):
            reasons.append("SESSION_MEASUREMENT_INPUT_MISSING")
            fit = _solve_state(state, config)
            _persist_fit_state(state, fit)
            _remember_reasons(state, [*reasons, *fit.reason_codes])
            return NodeExecutionResult(
                outputs={
                    "solution": _payload_from_state(
                        state,
                        fit=fit,
                        state_override="INVALID",
                        reasons=reasons,
                        node=node,
                    )
                }
            )

        track_id = curvature.track_id or phase.track_id
        if not track_id or curvature.track_id != phase.track_id:
            reasons.append("SESSION_MEASUREMENT_TRACK_MISMATCH")
            fit = _solve_state(state, config)
            _persist_fit_state(state, fit)
            _remember_reasons(state, [*reasons, *fit.reason_codes])
            return NodeExecutionResult(
                outputs={
                    "solution": _payload_from_state(
                        state,
                        fit=fit,
                        curvature=curvature,
                        phase=phase,
                        state_override="INVALID",
                        reasons=reasons,
                        node=node,
                    )
                }
            )

        if active.present and active.track_id is not None and active.track_id != track_id:
            reasons.append("SESSION_ACTIVE_TRACK_MISMATCH")
            fit = _solve_state(state, config)
            _persist_fit_state(state, fit)
            _remember_reasons(state, [*reasons, *fit.reason_codes])
            return NodeExecutionResult(
                outputs={
                    "solution": _payload_from_state(
                        state,
                        fit=fit,
                        curvature=curvature,
                        phase=phase,
                        state_override="INVALID",
                        reasons=reasons,
                        node=node,
                    )
                }
            )

        active_track = state.get("track_id")
        if active_track is not None and active_track != track_id:
            if state.get("session_started") and active.present:
                result = _build_orientation_result(
                    state,
                    _solve_state(state, config),
                    phase=None,
                    session_config=session_config,
                    decision_reasons=["TRACK_CHANGED_BEFORE_DECISION"],
                    shadow_mode=bool((node.config or {}).get("shadow_mode_enabled", False)),
                    shadow_evaluation_approved=bool(
                        (node.config or {}).get("shadow_evaluation_approved", False)
                    ),
                    minimum_validation=(node.config or {}).get(
                        "minimum_principle_validation"
                    ),
                    force_uncertain=True,
                )
                state["frozen_result"] = result.model_dump(mode="json")
                state["session_state"] = "RESULT_READY"
                state["decision_gate_reached"] = True
                _remember_reasons(state, ["TRACK_CHANGED_BEFORE_DECISION"])
                return NodeExecutionResult(
                    outputs={
                        "solution": _payload_from_state(
                            state,
                            fit=_solve_state(state, config),
                            state_override="FROZEN",
                            reasons=["TRACK_CHANGED_BEFORE_DECISION"],
                            node=node,
                        )
                    }
                )
            _reset_state(state)
            reasons.extend(["SESSION_TRACK_CHANGED_RESET", "SOLVER_TRACK_CHANGED_RESET"])
        state["track_id"] = track_id
        phase_diameter_mm = _optional_number(phase.metadata.get("diameter_mm"))
        if phase_diameter_mm is not None:
            state["diameter_mm"] = phase_diameter_mm
        state.setdefault(
            "session_id", event_session_id or _runtime_session_id(context, track_id=track_id)
        )

        active_quality_ok = (
            active.unique and active.confidence >= session_config["minimum_active_stem_confidence"]
        )
        if active.present:
            state["active_stem_confidence"] = active.confidence
            state["rotation_reference_view"] = active.metadata.get("rotation_reference_view")
            state["positive_rotation"] = active.metadata.get("positive_rotation")
            state["remaining_distance_to_decision_gate_mm"] = _optional_number(
                active.metadata.get("remaining_distance_to_decision_gate_mm")
            )
            state["decision_margin_mm"] = _optional_number(
                active.metadata.get("decision_margin_mm")
            )
            state["decision_margin_entered"] = bool(active.metadata.get("decision_margin_entered"))
            if state.get("session_state") == "WAITING":
                state["session_state"] = "CANDIDATE"
        else:
            # Solver-only pipelines from before v3 remain usable, but cannot publish a
            # trusted angle result without the Active Stem quality/gate contract.
            active_quality_ok = True
            state.setdefault("active_stem_confidence", 0.0)
            reasons.append("SESSION_ACTIVE_STEM_INPUT_NOT_CONNECTED")

        start_reached = bool(event_types & _SESSION_START_EVENTS) or bool(
            active.metadata.get("session_start_gate_reached")
        )
        if not active.present:
            start_reached = True
        if start_reached and active_quality_ok:
            if not state.get("session_started"):
                reasons.append("SESSION_STARTED")
                state["session_started_timestamp"] = curvature.timestamp or phase.timestamp
                state["session_started_frame_sequence"] = _frame_sequence(curvature, phase)
            state["session_started"] = True
            state["session_state"] = "OBSERVING_CURVATURE"
        elif start_reached:
            reasons.append("SESSION_ACTIVE_STEM_QUALITY_LOW")
        elif active.present:
            reasons.append("SESSION_START_GATE_PENDING")

        previous_trace_generation = state.get("phase_trace_generation")
        if (
            previous_trace_generation is not None
            and int(previous_trace_generation) != phase.trace_generation
        ):
            _clear_fit_observations(state)
            reasons.append("SOLVER_PHASE_TRACE_CHANGED_RESET")
        state["phase_trace_generation"] = phase.trace_generation

        calibration_changed = any(
            previous is not None and current is not None and previous != current
            for previous, current in (
                (state.get("process_calibration_hash"), curvature.process_calibration_hash),
                (state.get("phase_calibration_hash"), phase.calibration_hash),
            )
        )
        if calibration_changed:
            _clear_fit_observations(state)
            reasons.append("SESSION_CALIBRATION_CHANGED_RESET")

        if (
            curvature.process_calibration_hash
            and phase.process_calibration_hash
            and curvature.process_calibration_hash != phase.process_calibration_hash
        ):
            reasons.append("SOLVER_PROCESS_CALIBRATION_MISMATCH")

        if phase.calibrated_phase_valid and phase.phase_locked:
            state["current_phase_deg"] = phase.cumulative_phase_deg
            state["last_phase_confidence"] = phase.confidence
            state["last_phase_inlier_ratio"] = phase.inlier_ratio
            state["last_phase_frame_sequence"] = phase.frame_sequence
            state["last_phase_timestamp"] = phase.timestamp
            state["relock_status"] = phase.relock_status
            state["phase_algorithm_version"] = phase.algorithm_version
            state["rotation_reference_view"] = phase.rotation_reference_view or state.get(
                "rotation_reference_view"
            )
            state["positive_rotation"] = phase.positive_rotation or state.get("positive_rotation")
            if phase.relock_status == "RELOCKED":
                state["phase_relocked_after_impact"] = True
            _remember_phase_sample(state, phase, session_config)
        else:
            reasons.append("SOLVER_CALIBRATED_PHASE_REQUIRED")
        if event_types & _LANDED_EVENTS:
            state["phase_landed"] = True
            reasons.append("SESSION_STEM_LANDED")

        incoming_frozen = (
            curvature.observation_state == "curvature_frozen"
            or "curvature_freeze_gate_reached" in curvature.reason_codes
            or bool(event_types & _FREEZE_EVENTS)
            or bool(active.metadata.get("curvature_freeze_gate_reached"))
        )
        canonical_view_exit_reached = bool(
            incoming_frozen
            and curvature.metadata.get("measurement_geometry_mode")
            == "CANONICAL_VIEW_CORRIDOR_V1"
        )
        decision_reached = (
            bool(event_types & _DECISION_EVENTS)
            or bool(active.metadata.get("decision_gate_reached"))
            or canonical_view_exit_reached
        )
        if decision_reached:
            state["decision_gate_reached"] = True
            reasons.extend(["SESSION_DECISION_GATE_REACHED", "SOLVER_DECISION_GATE_REACHED"])
            if canonical_view_exit_reached:
                state["remaining_distance_to_decision_gate_mm"] = 0.0
                state["decision_margin_entered"] = True
                reasons.append("SESSION_CANONICAL_VIEW_EXIT_REACHED")
        if incoming_frozen:
            if not state.get("curvature_frozen"):
                state["curvature_freeze_timestamp"] = curvature.timestamp or phase.timestamp
                state["curvature_freeze_frame_sequence"] = _frame_sequence(curvature, phase)
            state["curvature_frozen"] = True
            reasons.append("SESSION_CURVATURE_FROZEN")

        accepted_new = False
        track_common_coverage = _track_common_coverage(state, curvature)
        observation_reason = _observation_rejection_reason(
            curvature,
            phase,
            config=config,
            track_common_coverage=track_common_coverage,
            already_frozen=bool(state.get("curvature_frozen")),
            incoming_frozen=incoming_frozen,
            calibration_mismatch="SOLVER_PROCESS_CALIBRATION_MISMATCH" in reasons,
            session_started=bool(state.get("session_started")),
        )
        if observation_reason is None:
            observation = _observation(
                curvature,
                phase,
                common_coverage=track_common_coverage,
            )
            frame_ids = state.setdefault("frame_ids", [])
            if observation.frame_id in frame_ids:
                duplicate_reason = "SOLVER_DUPLICATE_OBSERVATION_IGNORED"
                reasons.append(duplicate_reason)
                _remember_diagnostic_observation(
                    state,
                    curvature,
                    phase,
                    common_coverage=track_common_coverage,
                    included=False,
                    reason_code=duplicate_reason,
                    maximum_points=2 * config.maximum_observations,
                )
            else:
                observations = state.setdefault("observations", [])
                observations.append(asdict(observation))
                frame_ids.append(observation.frame_id)
                if len(observations) > config.maximum_observations:
                    del observations[: len(observations) - config.maximum_observations]
                    retained = {str(item["frame_id"]) for item in observations}
                    state["frame_ids"] = [item for item in frame_ids if item in retained]
                accepted_new = True
                state["last_geometry_confidence"] = curvature.geometry_confidence
                state["curvature_algorithm_version"] = curvature.algorithm_version
                state["common_segment_length_mm"] = curvature.common_segment_descriptor.length_mm
                state["common_segment_coverage"] = track_common_coverage
                _remember_diagnostic_observation(
                    state,
                    curvature,
                    phase,
                    common_coverage=track_common_coverage,
                    included=True,
                    reason_code=None,
                    maximum_points=2 * config.maximum_observations,
                )
        else:
            reasons.append(observation_reason)
            _remember_diagnostic_observation(
                state,
                curvature,
                phase,
                common_coverage=track_common_coverage,
                included=False,
                reason_code=observation_reason,
                maximum_points=2 * config.maximum_observations,
            )

        state["last_frame_sequence"] = _frame_sequence(curvature, phase)
        state["last_timestamp"] = curvature.timestamp or phase.timestamp
        state["process_calibration_id"] = curvature.calibration_id
        state["process_calibration_hash"] = curvature.process_calibration_hash
        state["phase_calibration_id"] = phase.calibration_id
        state["phase_calibration_hash"] = phase.calibration_hash
        fit = _solve_state(state, config)
        _persist_fit_state(state, fit)
        reasons.extend(fit.reason_codes)

        if state.get("curvature_frozen"):
            state["session_state"] = "TRACKING_PHASE"
            phase_ready = _phase_ready(state, session_config)
            if phase_ready and fit.state in {"SOLVED", "BOW_NOT_RELEVANT"}:
                state["session_state"] = "READY_TO_DECIDE"
        if decision_reached and active.present:
            result = _build_orientation_result(
                state,
                fit,
                phase=phase,
                session_config=session_config,
                decision_reasons=reasons,
                shadow_mode=bool((node.config or {}).get("shadow_mode_enabled", False)),
                shadow_evaluation_approved=bool(
                    (node.config or {}).get("shadow_evaluation_approved", False)
                ),
                minimum_validation=(node.config or {}).get(
                    "minimum_principle_validation"
                ),
            )
            state["frozen_result"] = result.model_dump(mode="json")
            state["session_state"] = "RESULT_READY"
            reasons.extend(result.reason_codes)
        elif decision_reached:
            reasons.append("SESSION_V3_RESULT_REQUIRES_ACTIVE_STEM_INPUT")

        _remember_reasons(state, reasons)
        output_state = "FROZEN" if state.get("curvature_frozen") and fit.state == "SOLVED" else None
        solution = _payload_from_state(
            state,
            fit=fit,
            curvature=curvature,
            phase=phase,
            accepted_new=accepted_new,
            state_override=output_state,
            reasons=reasons,
            node=node,
        )
        return NodeExecutionResult(
            outputs={
                "solution": solution,
                "overlay": _diagnostic_overlay(
                    solution,
                    active_stem=(active_stem if isinstance(active_stem, SegmentPayload) else None),
                ),
            }
        )


def _solver_config(config: dict[str, Any]) -> StemCurvaturePhaseSolverConfig:
    return StemCurvaturePhaseSolverConfig(
        minimum_observations=_integer(config, "minimum_observations", 7),
        minimum_phase_span_deg=_number(config, "minimum_phase_span_deg", 90.0),
        minimum_geometry_confidence=_number(config, "minimum_geometry_confidence", 0.4),
        minimum_phase_confidence=_number(config, "minimum_phase_confidence", 0.4),
        minimum_common_coverage=_number(config, "minimum_common_coverage", 0.4),
        maximum_observations=_integer(config, "maximum_observations", 240),
        robust_huber_delta=_number(config, "robust_huber_delta", 1.5),
        robust_iterations=_integer(config, "robust_iterations", 8),
        maximum_fit_condition=_number(config, "maximum_fit_condition", 1.0e8),
        maximum_signed_residual_ratio=_number(config, "maximum_signed_residual_ratio", 0.45),
        maximum_energy_residual_ratio=_number(config, "maximum_energy_residual_ratio", 0.4),
        minimum_curvature_span_mm=_number(config, "minimum_curvature_span_mm", 1.5),
        bow_relevance_threshold_mm=_number(config, "bow_relevance_threshold_mm", 3.0),
        target_observation_tolerance_deg=_number(config, "target_observation_tolerance_deg", 12.0),
        maximum_target_extrapolation_deg=_number(config, "maximum_target_extrapolation_deg", 30.0),
        s_bow_residual_minimum_mm=_number(config, "s_bow_residual_minimum_mm", 2.0),
        s_bow_residual_ratio=_number(config, "s_bow_residual_ratio", 0.3),
    ).validate()


def _session_config(config: dict[str, Any]) -> dict[str, float | int]:
    result: dict[str, float | int] = {
        "minimum_active_stem_confidence": _number(config, "minimum_active_stem_confidence", 0.55),
        "minimum_decision_phase_confidence": _number(
            config, "minimum_decision_phase_confidence", 0.65
        ),
        "minimum_secure_tracking_confidence": _number(
            config, "minimum_secure_tracking_confidence", 0.8
        ),
        "minimum_phase_inlier_ratio": _number(config, "minimum_phase_inlier_ratio", 0.55),
        "settled_phase_window_frames": _integer(config, "settled_phase_window_frames", 3),
        "settled_phase_span_deg": _number(config, "settled_phase_span_deg", 2.0),
        "no_rotation_tolerance_deg": _number(config, "no_rotation_tolerance_deg", 2.0),
        "minimum_overall_confidence": _number(config, "minimum_overall_confidence", 0.55),
        "result_expiry_seconds": _number(config, "result_expiry_seconds", 5.0),
        "first_roller_distance_from_decision_gate_mm": _number(
            config, "first_roller_distance_from_decision_gate_mm", 0.0
        ),
        "conveyor_speed_mm_per_second": _number(config, "conveyor_speed_mm_per_second", 0.0),
    }
    for key in (
        "minimum_active_stem_confidence",
        "minimum_decision_phase_confidence",
        "minimum_secure_tracking_confidence",
        "minimum_phase_inlier_ratio",
        "minimum_overall_confidence",
    ):
        if not 0 <= float(result[key]) <= 1:
            raise ValueError(f"{key} must be between zero and one")
    if not 1 <= int(result["settled_phase_window_frames"]) <= 120:
        raise ValueError("settled_phase_window_frames must be in [1, 120]")
    if not 0 <= float(result["settled_phase_span_deg"]) <= 90:
        raise ValueError("settled_phase_span_deg must be in [0, 90]")
    if not 0 <= float(result["no_rotation_tolerance_deg"]) <= 90:
        raise ValueError("no_rotation_tolerance_deg must be in [0, 90]")
    for key in (
        "result_expiry_seconds",
        "first_roller_distance_from_decision_gate_mm",
        "conveyor_speed_mm_per_second",
    ):
        if float(result[key]) < 0:
            raise ValueError(f"{key} must not be negative")
    return result


def _active_stem(value: object) -> _ActiveStemInput:
    if not isinstance(value, SegmentPayload):
        return _ActiveStemInput(False, False, None, 0.0, {})
    metadata = value.metadata.get("active_stem_tracking")
    tracking = metadata if isinstance(metadata, Mapping) else {}
    track_id = tracking.get("track_id")
    if not isinstance(track_id, str) or not track_id.strip():
        object_track_ids = {
            item.track_id
            for item in value.objects
            if isinstance(item.track_id, str) and item.track_id.strip()
        }
        track_id = next(iter(object_track_ids)) if len(object_track_ids) == 1 else None
    confidence = _optional_number(tracking.get("selection_confidence")) or 0.0
    unique = (
        tracking.get("status") == "active"
        and len(value.segments) == 1
        and isinstance(track_id, str)
        and bool(track_id)
        and not bool(tracking.get("fail_closed"))
    )
    return _ActiveStemInput(
        present=True,
        unique=unique,
        track_id=str(track_id) if track_id else None,
        confidence=min(max(confidence, 0.0), 1.0),
        metadata=tracking,
    )


def _incoming_track_id(
    curvature: object,
    phase: object,
    active: _ActiveStemInput,
) -> str | None:
    if active.track_id:
        return active.track_id
    for value in (curvature, phase):
        track_id = getattr(value, "track_id", None)
        if isinstance(track_id, str) and track_id:
            return track_id
    return None


def _observation_rejection_reason(
    curvature: StemHorizontalCurvaturePayload,
    phase: StemSurfacePhasePayload,
    *,
    config: StemCurvaturePhaseSolverConfig,
    track_common_coverage: float,
    already_frozen: bool,
    incoming_frozen: bool,
    calibration_mismatch: bool,
    session_started: bool,
) -> str | None:
    if not session_started:
        return "SOLVER_SESSION_NOT_STARTED"
    if already_frozen or incoming_frozen:
        return "SOLVER_CURVATURE_ALREADY_FROZEN"
    if calibration_mismatch:
        return "SOLVER_PROCESS_CALIBRATION_MISMATCH"
    if (
        not curvature.calibration_id
        or not curvature.process_calibration_hash
        or not phase.process_calibration_hash
    ):
        return "SOLVER_PROCESS_CALIBRATION_REQUIRED"
    if not curvature.valid or curvature.observation_state != "accepted":
        return "SOLVER_CURVATURE_OBSERVATION_INVALID"
    if (
        curvature.signed_bow_mm is None
        or curvature.curvature_rms_mm is None
        or not curvature.common_segment_descriptor.comparable
    ):
        return "SOLVER_CURVATURE_METRICS_INCOMPLETE"
    if curvature.geometry_confidence < config.minimum_geometry_confidence:
        return "SOLVER_GEOMETRY_CONFIDENCE_LOW"
    if curvature.common_segment_descriptor.common_segment_ratio < (config.minimum_common_coverage):
        return "SOLVER_COMMON_COVERAGE_LOW"
    if track_common_coverage < config.minimum_common_coverage:
        return "SOLVER_COMMON_SEGMENT_OVERLAP_LOW"
    if (
        not phase.calibrated_phase_valid
        or not phase.phase_locked
        or phase.cumulative_phase_deg is None
        or not phase.calibration_id
        or not phase.calibration_hash
        or not phase.rotation_reference_view
        or not phase.positive_rotation
    ):
        return "SOLVER_CALIBRATED_PHASE_REQUIRED"
    if phase.confidence < config.minimum_phase_confidence:
        return "SOLVER_PHASE_CONFIDENCE_LOW"
    if (
        curvature.frame_sequence is not None
        and phase.frame_sequence is not None
        and curvature.frame_sequence != phase.frame_sequence
    ):
        return "SOLVER_FRAME_MISMATCH"
    return None


def _observation(
    curvature: StemHorizontalCurvaturePayload,
    phase: StemSurfacePhasePayload,
    *,
    common_coverage: float,
) -> StemCurvaturePhaseObservation:
    frame_id = (
        str(curvature.frame_sequence)
        if curvature.frame_sequence is not None
        else curvature.timestamp or phase.timestamp or "unknown-frame"
    )
    return StemCurvaturePhaseObservation(
        track_id=curvature.track_id or "",
        frame_id=frame_id,
        timestamp=curvature.timestamp or phase.timestamp,
        cumulative_phase_deg=float(phase.cumulative_phase_deg),
        signed_bow_mm=float(curvature.signed_bow_mm),
        curvature_rms_mm=float(curvature.curvature_rms_mm),
        curvature_maxabs_mm=curvature.curvature_maxabs_mm,
        curvature_peak_to_peak_mm=curvature.curvature_peak_to_peak_mm,
        geometry_confidence=curvature.geometry_confidence,
        phase_confidence=phase.confidence,
        common_coverage=common_coverage,
        segment_x_start_mm=curvature.common_segment_descriptor.x_start_mm,
        segment_x_end_mm=curvature.common_segment_descriptor.x_end_mm,
        bow_shape_hint=curvature.bow_shape_hint,
    ).validate()


def _remember_diagnostic_observation(
    state: dict[str, Any],
    curvature: StemHorizontalCurvaturePayload,
    phase: StemSurfacePhasePayload,
    *,
    common_coverage: float,
    included: bool,
    reason_code: str | None,
    maximum_points: int,
) -> None:
    frame_sequence = _frame_sequence(curvature, phase)
    frame_id = (
        str(frame_sequence)
        if frame_sequence is not None
        else curvature.timestamp or phase.timestamp or "unknown-frame"
    )
    geometry_confidence = min(max(float(curvature.geometry_confidence), 0.0), 1.0)
    phase_confidence = min(max(float(phase.confidence), 0.0), 1.0)
    coverage = min(max(float(common_coverage), 0.0), 1.0)
    entries = state.setdefault("diagnostic_observations", [])
    entries.append(
        {
            "frame_id": frame_id,
            "frame_sequence": frame_sequence,
            "timestamp": curvature.timestamp or phase.timestamp,
            "cumulative_phase_deg": _optional_number(phase.cumulative_phase_deg),
            "curvature_rms_mm": _optional_number(curvature.curvature_rms_mm),
            "signed_bow_mm": _optional_number(curvature.signed_bow_mm),
            "geometry_confidence": geometry_confidence,
            "phase_confidence": phase_confidence,
            "common_coverage": coverage,
            "base_weight": geometry_confidence * phase_confidence * coverage,
            "included": bool(included),
            "reason_codes": [reason_code] if reason_code else [],
        }
    )
    limit = min(max(int(maximum_points), 2), 2000)
    if len(entries) > limit:
        del entries[: len(entries) - limit]


def _node_state(context: RuntimeExecutionContext, node: RuntimeNodeSpec) -> dict[str, Any]:
    value = context.state.setdefault(node.node_key, {})
    if not isinstance(value, dict) or value.get("schema_version") not in {
        None,
        _STATE_SCHEMA,
    }:
        value = {}
        context.state[node.node_key] = value
    value.setdefault("schema_version", _STATE_SCHEMA)
    value.setdefault("observations", [])
    value.setdefault("diagnostic_observations", [])
    value.setdefault("frame_ids", [])
    value.setdefault("reason_code_history", [])
    value.setdefault("curvature_frozen", False)
    value.setdefault("decision_gate_reached", False)
    value.setdefault("session_started", False)
    value.setdefault("session_state", "WAITING")
    value.setdefault("phase_samples", [])
    value.setdefault("phase_settled", False)
    value.setdefault("phase_landed", False)
    value.setdefault("phase_relocked_after_impact", False)
    value.setdefault("frozen_result", None)
    value.setdefault("session_completed", False)
    return value


def _reset_state(state: dict[str, Any]) -> None:
    state.clear()
    state.update(
        {
            "schema_version": _STATE_SCHEMA,
            "observations": [],
            "diagnostic_observations": [],
            "frame_ids": [],
            "reason_code_history": [],
            "curvature_frozen": False,
            "decision_gate_reached": False,
            "session_started": False,
            "session_state": "WAITING",
            "phase_samples": [],
            "phase_settled": False,
            "phase_landed": False,
            "phase_relocked_after_impact": False,
            "frozen_result": None,
            "session_completed": False,
        }
    )


def _clear_fit_observations(state: dict[str, Any]) -> None:
    state["observations"] = []
    state["frame_ids"] = []
    state["diagnostic_observations"] = []


def _track_common_coverage(
    state: dict[str, Any], curvature: StemHorizontalCurvaturePayload
) -> float:
    descriptor = curvature.common_segment_descriptor
    intervals: list[tuple[float, float]] = []
    for item in state.get("observations", []):
        if not isinstance(item, dict):
            continue
        start = item.get("segment_x_start_mm")
        end = item.get("segment_x_end_mm")
        if isinstance(start, int | float) and isinstance(end, int | float) and end > start:
            intervals.append((float(start), float(end)))
    if descriptor.x_start_mm is not None and descriptor.x_end_mm is not None:
        intervals.append((descriptor.x_start_mm, descriptor.x_end_mm))
    if len(intervals) < 2:
        return descriptor.common_segment_ratio
    common_length = max(
        0.0,
        min(end for _start, end in intervals) - max(start for start, _end in intervals),
    )
    overlap = min(common_length / max(end - start, 1.0e-9) for start, end in intervals)
    return min(descriptor.common_segment_ratio, max(0.0, min(1.0, overlap)))


def _solve_state(
    state: dict[str, Any], config: StemCurvaturePhaseSolverConfig
) -> StemCurvaturePhaseFitResult:
    stored = [item for item in state.get("observations", []) if isinstance(item, dict)]
    shared_coverage = _stored_common_coverage(stored)
    observations = [
        StemCurvaturePhaseObservation(
            **{
                **item,
                "common_coverage": min(float(item.get("common_coverage", 0.0)), shared_coverage),
            }
        )
        for item in stored
    ]
    return solve_stem_curvature_phase(observations, config=config)


def _persist_fit_state(state: dict[str, Any], fit: StemCurvaturePhaseFitResult) -> None:
    state.update(
        {
            "solver_state": fit.state,
            "observed_phase_span_deg": fit.observed_phase_span_deg,
            "observed_min_curvature_mm": fit.observed_min_curvature_mm,
            "observed_max_curvature_mm": fit.observed_max_curvature_mm,
            "fitted_min_curvature_mm": fit.fitted_min_curvature_mm,
            "fitted_max_curvature_mm": fit.fitted_max_curvature_mm,
            "curvature_span_mm": fit.curvature_span_mm,
            "target_phase_deg": fit.target_phase_deg,
            "maximum_phase_deg": fit.maximum_phase_deg,
            "minimum_unique": fit.minimum_unique,
            "bow_relevant": fit.bow_relevant,
            "confidence": fit.confidence,
            "signed_fit": asdict(fit.signed_fit) if fit.signed_fit is not None else None,
            "energy_fit": asdict(fit.energy_fit) if fit.energy_fit is not None else None,
        }
    )


def _remember_phase_sample(
    state: dict[str, Any],
    phase: StemSurfacePhasePayload,
    session_config: Mapping[str, float | int],
) -> None:
    if phase.cumulative_phase_deg is None:
        return
    samples = state.setdefault("phase_samples", [])
    samples.append(float(phase.cumulative_phase_deg))
    window = int(session_config["settled_phase_window_frames"])
    if len(samples) > window:
        del samples[:-window]
    stable_window = len(samples) >= window and max(samples) - min(samples) <= float(
        session_config["settled_phase_span_deg"]
    )
    state["phase_settled"] = bool(
        stable_window or state.get("phase_landed") or phase.relock_status == "RELOCKED"
    )


def _phase_ready(state: Mapping[str, Any], session_config: Mapping[str, float | int]) -> bool:
    confidence = float(state.get("last_phase_confidence") or 0.0)
    inlier_ratio = float(state.get("last_phase_inlier_ratio") or 0.0)
    confidently_tracked = confidence >= float(
        session_config["minimum_secure_tracking_confidence"]
    ) and inlier_ratio >= float(session_config["minimum_phase_inlier_ratio"])
    settled = bool(state.get("phase_settled") or state.get("phase_landed"))
    return (
        confidence >= float(session_config["minimum_decision_phase_confidence"])
        and (settled or confidently_tracked)
        and state.get("current_phase_deg") is not None
    )


def _build_orientation_result(
    state: Mapping[str, Any],
    fit: StemCurvaturePhaseFitResult,
    *,
    phase: StemSurfacePhasePayload | None,
    session_config: Mapping[str, float | int],
    decision_reasons: list[str],
    shadow_mode: bool = False,
    shadow_evaluation_approved: bool = False,
    minimum_validation: object = None,
    force_uncertain: bool = False,
) -> StemOrientationResultV3:
    reasons = list(decision_reasons)
    track_id = str(state.get("track_id") or "unknown-track")
    session_id = str(state.get("session_id") or f"unknown-session:{track_id}")
    current_phase = _optional_number(state.get("current_phase_deg"))
    active_confidence = min(max(float(state.get("active_stem_confidence") or 0.0), 0.0), 1.0)
    geometry_confidence = min(max(float(state.get("last_geometry_confidence") or 0.0), 0.0), 1.0)
    phase_confidence = min(max(float(state.get("last_phase_confidence") or 0.0), 0.0), 1.0)
    fit_confidence = fit.confidence
    if fit.state == "BOW_NOT_RELEVANT":
        fit_confidence = _bow_irrelevance_confidence(fit)

    phase_is_ready = _phase_ready(state, session_config)
    active_is_ready = active_confidence >= float(session_config["minimum_active_stem_confidence"])
    deadline_met = bool(state.get("decision_gate_reached")) and not force_uncertain
    positive_rotation = state.get("positive_rotation")
    convention_valid = positive_rotation in {"CLOCKWISE", "COUNTER_CLOCKWISE"} and bool(
        state.get("rotation_reference_view")
    )
    if fit.state == "BOW_NOT_RELEVANT":
        overall_confidence = min(active_confidence, geometry_confidence, fit_confidence)
    else:
        overall_confidence = min(
            active_confidence,
            geometry_confidence,
            phase_confidence,
            fit_confidence,
        )
    phase_distance = (
        wrap_to_minus_90_plus_90(fit.target_phase_deg - current_phase)
        if fit.target_phase_deg is not None and current_phase is not None
        else None
    )

    decision = "UNCERTAIN"
    signed_angle: float | None = None
    absolute_angle: float | None = None
    direction = "UNKNOWN"
    if force_uncertain:
        reasons.append("SESSION_FORCED_UNCERTAIN")
    elif not deadline_met:
        reasons.append("DECISION_DEADLINE_NOT_MET")
    elif not active_is_ready:
        reasons.append("DECISION_ACTIVE_STEM_CONFIDENCE_LOW")
    elif fit.state == "BOW_NOT_RELEVANT":
        if overall_confidence >= float(session_config["minimum_overall_confidence"]):
            decision = "NO_ROTATION"
            signed_angle = 0.0
            absolute_angle = 0.0
            direction = "NONE"
            reasons.append("BOW_NOT_RELEVANT")
        else:
            reasons.append("DECISION_BOW_RELEVANCE_CONFIDENCE_LOW")
    elif fit.state != "SOLVED" or fit.target_phase_deg is None or not fit.minimum_unique:
        reasons.append("DECISION_CURVATURE_MINIMUM_UNAVAILABLE")
    elif not phase_is_ready or current_phase is None:
        reasons.append("DECISION_CURRENT_PHASE_UNCERTAIN")
    elif not convention_valid:
        reasons.append("DECISION_ROTATION_CONVENTION_INVALID")
    elif overall_confidence < float(session_config["minimum_overall_confidence"]):
        reasons.append("DECISION_OVERALL_CONFIDENCE_LOW")
    else:
        signed_angle = phase_distance
        assert signed_angle is not None
        absolute_angle = abs(signed_angle)
        if absolute_angle <= float(session_config["no_rotation_tolerance_deg"]):
            decision = "NO_ROTATION"
            signed_angle = 0.0
            absolute_angle = 0.0
            direction = "NONE"
            reasons.append("TARGET_WITHIN_NO_ROTATION_TOLERANCE")
        else:
            decision = "ROTATE"
            direction = _physical_rotation_direction(signed_angle, str(positive_rotation))
            reasons.append("CURVATURE_MINIMUM_CORRECTION_READY")

    decision_timestamp = _decision_timestamp(state)
    expiry = _add_seconds(decision_timestamp, float(session_config["result_expiry_seconds"]))
    remaining_distance = float(session_config["first_roller_distance_from_decision_gate_mm"])
    remaining_distance_value = remaining_distance if remaining_distance > 0 else None
    speed = float(session_config["conveyor_speed_mm_per_second"])
    remaining_time_ms = (
        remaining_distance / speed * 1000.0 if remaining_distance > 0 and speed > 0 else None
    )
    curvature_min = (
        fit.fitted_min_curvature_mm
        if fit.fitted_min_curvature_mm is not None
        else fit.observed_min_curvature_mm
    )
    curvature_max = (
        fit.fitted_max_curvature_mm
        if fit.fitted_max_curvature_mm is not None
        else fit.observed_max_curvature_mm
    )
    common_length_mm = float(state.get("common_segment_length_mm") or 0.0)
    used_observations = [
        item for item in state.get("observations", []) if isinstance(item, Mapping)
    ]
    used_frames = [
        int(str(item["frame_id"]))
        for item in used_observations
        if str(item.get("frame_id") or "").isdigit()
    ]
    used_phases = [
        float(item["cumulative_phase_deg"])
        for item in used_observations
        if isinstance(item.get("cumulative_phase_deg"), int | float)
    ]
    decision_frame_sequence = _optional_integer(state.get("last_frame_sequence"))

    return StemOrientationResultV3(
        shadow_mode=shadow_mode,
        plc_writes_suppressed=shadow_mode,
        shadow_evaluation_approved=shadow_evaluation_approved,
        minimum_principle_validation_id=_validation_field(
            minimum_validation, "artifact_id"
        ),
        minimum_principle_validation_version=_validation_field(
            minimum_validation, "artifact_version"
        ),
        minimum_principle_validation_hash=_validation_field(
            minimum_validation, "artifact_hash"
        ),
        minimum_principle_validation_status=_validation_status(minimum_validation),
        session_id=session_id,
        track_id=track_id,
        decision=decision,
        signed_correction_deg=signed_angle,
        recommended_rotation_deg_signed=signed_angle,
        recommended_rotation_deg_abs=absolute_angle,
        direction=direction,
        rotation_reference_view=(
            str(state["rotation_reference_view"]) if state.get("rotation_reference_view") else None
        ),
        positive_rotation=(
            str(state["positive_rotation"]) if state.get("positive_rotation") else None
        ),
        curvature_min_mm=curvature_min,
        curvature_max_mm=curvature_max,
        curvature_span_mm=fit.curvature_span_mm,
        curvature_min_normalized=(
            curvature_min / common_length_mm
            if curvature_min is not None and common_length_mm > 0
            else None
        ),
        curvature_max_normalized=(
            curvature_max / common_length_mm
            if curvature_max is not None and common_length_mm > 0
            else None
        ),
        target_phase_deg=fit.target_phase_deg,
        maximum_phase_deg=fit.maximum_phase_deg,
        current_phase_deg=current_phase,
        phase_distance_to_target_deg_signed=phase_distance,
        observed_rotation_span_deg=fit.observed_phase_span_deg,
        target_observed=fit.target_phase_source == "observed",
        phase_relocked_after_impact=bool(state.get("phase_relocked_after_impact")),
        relock_status=str(state.get("relock_status") or "NOT_ATTEMPTED"),
        fit_residual=(fit.signed_fit.residual_rms if fit.signed_fit else None),
        fit_residual_mm2=(fit.energy_fit.residual_rms if fit.energy_fit else None),
        active_stem_confidence=active_confidence,
        geometry_confidence=geometry_confidence,
        phase_confidence=phase_confidence,
        fit_confidence=fit_confidence,
        overall_confidence=overall_confidence,
        calibration_id=(
            str(state["process_calibration_id"]) if state.get("process_calibration_id") else None
        ),
        process_calibration_hash=(
            str(state["process_calibration_hash"])
            if state.get("process_calibration_hash")
            else None
        ),
        rotation_calibration_id=(
            str(state["phase_calibration_id"]) if state.get("phase_calibration_id") else None
        ),
        rotation_calibration_hash=(
            str(state["phase_calibration_hash"]) if state.get("phase_calibration_hash") else None
        ),
        curvature_algorithm_version=str(state.get("curvature_algorithm_version") or "unknown"),
        phase_algorithm_version=str(state.get("phase_algorithm_version") or "unknown"),
        solver_algorithm_version=ALGORITHM_VERSION,
        observation_count=fit.observation_count,
        common_segment_coverage=min(
            max(float(state.get("common_segment_coverage") or 0.0), 0.0), 1.0
        ),
        used_frame_sequences=used_frames,
        used_phase_points_deg=used_phases,
        fit_parameters=_fit_parameters(fit),
        diagnostic_graph=_diagnostic_graph(
            state,
            fit,
            reasons=list(dict.fromkeys(reason for reason in reasons if reason)),
        ),
        session_started_timestamp=(
            str(state["session_started_timestamp"])
            if state.get("session_started_timestamp")
            else None
        ),
        session_started_frame_sequence=_optional_integer(
            state.get("session_started_frame_sequence")
        ),
        decision_timestamp=decision_timestamp,
        decision_frame_sequence=decision_frame_sequence,
        decision_frame={
            "frame_sequence": decision_frame_sequence,
            "timestamp": decision_timestamp,
            "gate": "decision_gate",
        },
        curvature_freeze_timestamp=(
            str(state["curvature_freeze_timestamp"])
            if state.get("curvature_freeze_timestamp")
            else None
        ),
        curvature_freeze_frame_sequence=_optional_integer(
            state.get("curvature_freeze_frame_sequence")
        ),
        decision_margin_mm=_optional_number(state.get("decision_margin_mm")),
        decision_margin_entered=bool(state.get("decision_margin_entered")),
        remaining_distance_to_decision_gate_mm=_optional_number(
            state.get("remaining_distance_to_decision_gate_mm")
        ),
        remaining_distance_to_first_roller_mm=remaining_distance_value,
        remaining_time_to_first_roller_ms=remaining_time_ms,
        result_expiry=expiry,
        deadline_met=deadline_met,
        reason_codes=list(dict.fromkeys(reason for reason in reasons if reason)),
        component_versions={
            "curvature": str(state.get("curvature_algorithm_version") or "unknown"),
            "phase": str(state.get("phase_algorithm_version") or "unknown"),
            "solver": ALGORITHM_VERSION,
        },
        asset_references={"status": "not_recorded"},
    )


def _validation_field(value: object, key: str) -> str | None:
    if not isinstance(value, Mapping):
        return None
    field = value.get(key)
    return str(field) if isinstance(field, str) and field else None


def _validation_status(value: object) -> str | None:
    status = _validation_field(value, "status")
    return (
        status
        if status in {"VALIDATION_PASSED", "VALIDATION_FAILED", "INSUFFICIENT_DATA"}
        else None
    )


def _bow_irrelevance_confidence(fit: StemCurvaturePhaseFitResult) -> float:
    if fit.signed_fit is None or fit.energy_fit is None:
        return 0.0
    if not fit.signed_fit.stable or not fit.energy_fit.stable:
        return 0.0
    return min(fit.signed_fit.inlier_weight_ratio, fit.energy_fit.inlier_weight_ratio)


def _fit_parameters(fit: StemCurvaturePhaseFitResult) -> dict[str, Any]:
    def harmonic(value: HarmonicFit | None, *, model: str) -> dict[str, Any] | None:
        if value is None:
            return None
        return {
            "model": model,
            "offset": value.coefficients[0],
            "cosine": value.coefficients[1],
            "sine": value.coefficients[2],
            "residual_rms": value.residual_rms,
            "residual_ratio": value.residual_ratio,
            "condition_number": value.condition_number,
            "inlier_weight_ratio": value.inlier_weight_ratio,
            "stable": value.stable,
        }

    return {
        "signed_fit": harmonic(
            fit.signed_fit,
            model="B(phi)=b0+b1*cos(phi)+b2*sin(phi)",
        ),
        "energy_fit": harmonic(
            fit.energy_fit,
            model="K(phi)^2=c0+c1*cos(2phi)+c2*sin(2phi)",
        ),
    }


def _physical_rotation_direction(signed_angle: float, positive_rotation: str) -> str:
    if signed_angle > 0:
        return positive_rotation
    return "COUNTER_CLOCKWISE" if positive_rotation == "CLOCKWISE" else "CLOCKWISE"


def _decision_timestamp(state: Mapping[str, Any]) -> str:
    value = state.get("last_timestamp")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _add_seconds(timestamp: str, seconds: float) -> str:
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (parsed + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _optional_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _optional_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _stored_common_coverage(observations: list[dict[str, Any]]) -> float:
    if not observations:
        return 1.0
    intervals = [
        (float(item["segment_x_start_mm"]), float(item["segment_x_end_mm"]))
        for item in observations
        if isinstance(item.get("segment_x_start_mm"), int | float)
        and isinstance(item.get("segment_x_end_mm"), int | float)
        and float(item["segment_x_end_mm"]) > float(item["segment_x_start_mm"])
    ]
    if len(intervals) != len(observations) or len(intervals) < 2:
        return min(float(item.get("common_coverage", 0.0)) for item in observations)
    common_length = max(
        0.0,
        min(end for _start, end in intervals) - max(start for start, _end in intervals),
    )
    return max(
        0.0,
        min(
            1.0,
            min(common_length / max(end - start, 1.0e-9) for start, end in intervals),
        ),
    )


def _payload_from_state(
    state: dict[str, Any],
    *,
    fit: StemCurvaturePhaseFitResult,
    curvature: StemHorizontalCurvaturePayload | None = None,
    phase: StemSurfacePhasePayload | None = None,
    accepted_new: bool = False,
    state_override: str | None = None,
    reasons: list[str] | None = None,
    node: RuntimeNodeSpec | None = None,
) -> StemCurvaturePhaseSolutionPayload:
    payload_state = state_override or fit.state
    publish_target = payload_state in {"SOLVED", "FROZEN"}
    frozen_result = state.get("frozen_result")
    orientation_result = (
        StemOrientationResultV3.model_validate(frozen_result)
        if isinstance(frozen_result, Mapping)
        else None
    )
    diagnostic_graph = _diagnostic_graph(
        state,
        fit,
        reasons=list(dict.fromkeys(reasons or fit.reason_codes)),
    )
    return StemCurvaturePhaseSolutionPayload(
        session_id=state.get("session_id"),
        track_id=state.get("track_id"),
        frame_sequence=state.get("last_frame_sequence"),
        timestamp=state.get("last_timestamp"),
        current_phase_deg=state.get("current_phase_deg"),
        observation_count=fit.observation_count,
        accepted_new_observation=accepted_new,
        observed_phase_span_deg=fit.observed_phase_span_deg,
        curvature_frozen=bool(state.get("curvature_frozen")),
        decision_gate_reached=bool(state.get("decision_gate_reached")),
        session_state=str(state.get("session_state") or "WAITING"),
        phase_settled=bool(state.get("phase_settled")),
        result_frozen=orientation_result is not None,
        orientation_result=orientation_result,
        state=payload_state,
        observed_min_curvature_mm=fit.observed_min_curvature_mm,
        observed_min_phase_deg=fit.observed_min_phase_deg,
        observed_max_curvature_mm=fit.observed_max_curvature_mm,
        observed_max_phase_deg=fit.observed_max_phase_deg,
        observed_min_signed_bow_mm=fit.observed_min_signed_bow_mm,
        observed_max_signed_bow_mm=fit.observed_max_signed_bow_mm,
        fitted_min_curvature_mm=fit.fitted_min_curvature_mm,
        fitted_max_curvature_mm=fit.fitted_max_curvature_mm,
        fitted_min_signed_bow_mm=fit.fitted_min_signed_bow_mm,
        fitted_max_signed_bow_mm=fit.fitted_max_signed_bow_mm,
        curvature_span_mm=fit.curvature_span_mm,
        target_phase_deg=fit.target_phase_deg if publish_target else None,
        maximum_phase_deg=fit.maximum_phase_deg if publish_target else None,
        target_phase_source=fit.target_phase_source if publish_target else "unavailable",
        target_extrapolation_deg=(fit.target_extrapolation_deg if publish_target else None),
        minimum_unique=fit.minimum_unique,
        bow_relevant=fit.bow_relevant,
        s_bow_residual_minimum=fit.s_bow_residual_minimum,
        signed_fit=_fit_payload(fit.signed_fit),
        energy_fit=_fit_payload(fit.energy_fit),
        signed_fit_residual_rms_mm=(
            fit.signed_fit.residual_rms if fit.signed_fit is not None else None
        ),
        energy_fit_residual_rms_mm2=(
            fit.energy_fit.residual_rms if fit.energy_fit is not None else None
        ),
        diagnostic_graph=diagnostic_graph,
        confidence=fit.confidence,
        process_calibration_id=state.get("process_calibration_id"),
        process_calibration_hash=state.get("process_calibration_hash"),
        phase_calibration_id=state.get("phase_calibration_id"),
        phase_calibration_hash=state.get("phase_calibration_hash"),
        reason_codes=list(dict.fromkeys(reasons or fit.reason_codes)),
        reason_code_history=list(state.get("reason_code_history") or []),
        provenance=_provenance(node, curvature, phase),
        metadata={
            "signed_fit_model": "B(phi)=b0+b1*cos(phi)+b2*sin(phi)",
            "energy_fit_model": "K(phi)^2=c0+c1*cos(2phi)+c2*sin(2phi)",
            "phase_period_deg": 180,
            "session_started": bool(state.get("session_started")),
            "phase_calibrated_valid": bool(
                phase is not None and phase.calibrated_phase_valid
            ),
            "phase_locked": bool(phase is not None and phase.phase_locked),
            "phase_tracking_mode": phase.tracking_mode if phase is not None else None,
            "phase_confidence": phase.confidence if phase is not None else None,
            "phase_inlier_ratio": phase.inlier_ratio if phase is not None else None,
            "diameter_mm": _optional_number(state.get("diameter_mm")),
            "current_observation_rejection_reason": next(
                (
                    reason
                    for reason in (reasons or fit.reason_codes)
                    if reason.startswith("SOLVER_")
                    and reason
                    not in {
                        "SOLVER_DECISION_GATE_REACHED",
                        "SOLVER_CURVATURE_FREEZE_GATE_REACHED",
                    }
                ),
                None,
            ),
            "correction_angle_computed": bool(
                orientation_result is not None
                and orientation_result.recommended_rotation_deg_signed is not None
            ),
            "post_freeze_geometry_observations": 0,
            **(
                {"last_error_detail": state["last_error_detail"]}
                if state.get("last_error_detail")
                else {}
            ),
        },
    )


def _diagnostic_graph(
    state: Mapping[str, Any],
    fit: StemCurvaturePhaseFitResult,
    *,
    reasons: list[str],
) -> StemCurvaturePhaseDiagnosticGraph:
    stored_observations = [
        item for item in state.get("observations", []) if isinstance(item, Mapping)
    ]
    retained_frames = {str(item.get("frame_id")) for item in stored_observations}
    fit_weights: dict[str, float] = {}
    if fit.energy_fit is not None:
        fit_weights = {
            str(item.get("frame_id")): float(weight)
            for item, weight in zip(
                stored_observations,
                fit.energy_fit.sample_weights,
                strict=False,
            )
        }
    points: list[StemCurvaturePhaseDiagnosticPoint] = []
    for raw in state.get("diagnostic_observations", []):
        if not isinstance(raw, Mapping):
            continue
        frame_id = str(raw.get("frame_id") or "unknown-frame")
        included = bool(raw.get("included")) and frame_id in retained_frames
        point_reasons = [str(item) for item in raw.get("reason_codes", []) if item]
        if bool(raw.get("included")) and frame_id not in retained_frames:
            point_reasons.append("SOLVER_OBSERVATION_PRUNED_BY_LIMIT")
        base_weight = min(max(float(raw.get("base_weight") or 0.0), 0.0), 1.0)
        fit_weight = min(max(fit_weights.get(frame_id, 0.0), 0.0), 1.0) if included else 0.0
        status = "ignored"
        if included:
            robust_ratio = fit_weight / max(base_weight, 1.0e-9)
            status = (
                "downweighted"
                if fit.energy_fit is not None and robust_ratio < 0.95
                else "weighted"
            )
        points.append(
            StemCurvaturePhaseDiagnosticPoint(
                frame_id=frame_id,
                frame_sequence=_optional_integer(raw.get("frame_sequence")),
                timestamp=str(raw["timestamp"]) if raw.get("timestamp") else None,
                cumulative_phase_deg=_optional_number(raw.get("cumulative_phase_deg")),
                curvature_rms_mm=_optional_number(raw.get("curvature_rms_mm")),
                signed_bow_mm=_optional_number(raw.get("signed_bow_mm")),
                geometry_confidence=min(
                    max(float(raw.get("geometry_confidence") or 0.0), 0.0), 1.0
                ),
                phase_confidence=min(
                    max(float(raw.get("phase_confidence") or 0.0), 0.0), 1.0
                ),
                common_coverage=min(
                    max(float(raw.get("common_coverage") or 0.0), 0.0), 1.0
                ),
                base_weight=base_weight,
                fit_weight=fit_weight,
                status=status,
                included=included,
                reason_codes=list(dict.fromkeys(point_reasons)),
            )
        )

    curve: list[StemCurvaturePhaseDiagnosticCurvePoint] = []
    minimum_phase: float | None = None
    maximum_phase: float | None = None
    if fit.energy_fit is not None:
        offset, cosine, sine = fit.energy_fit.coefficients
        plotted_phases = [
            float(item.get("cumulative_phase_deg"))
            for item in stored_observations
            if isinstance(item.get("cumulative_phase_deg"), int | float)
        ]
        current_phase_value = _optional_number(state.get("current_phase_deg"))
        if current_phase_value is not None:
            plotted_phases.append(current_phase_value)
        if plotted_phases:
            curve_start = math.floor(min(plotted_phases) / 180.0) * 180
            curve_end = math.ceil(max(plotted_phases) / 180.0) * 180
            if curve_end <= curve_start:
                curve_end = curve_start + 180
        else:
            curve_start, curve_end = 0, 180
        if curve_end - curve_start > 720:
            curve_start = curve_end - 720
        step = max(2, math.ceil((curve_end - curve_start) / 360.0))
        for phase_deg in range(curve_start, curve_end + 1, step):
            radians = math.radians(2.0 * phase_deg)
            energy = offset + cosine * math.cos(radians) + sine * math.sin(radians)
            curve.append(
                StemCurvaturePhaseDiagnosticCurvePoint(
                    cumulative_phase_deg=float(phase_deg),
                    fitted_curvature_mm=math.sqrt(max(0.0, energy)),
                )
            )
        if fit.minimum_unique and fit.energy_fit.stable:
            maximum_phase_mod = (
                math.degrees(math.atan2(sine, cosine)) / 2.0
            ) % 180.0
            minimum_phase_mod = (maximum_phase_mod + 90.0) % 180.0
            curve_center = 0.5 * (curve_start + curve_end)
            maximum_phase = maximum_phase_mod + 180.0 * round(
                (curve_center - maximum_phase_mod) / 180.0
            )
            minimum_phase = minimum_phase_mod + 180.0 * round(
                (curve_center - minimum_phase_mod) / 180.0
            )

    current_phase = _optional_number(state.get("current_phase_deg"))
    decision_phase = current_phase if bool(state.get("decision_gate_reached")) else None
    return StemCurvaturePhaseDiagnosticGraph(
        points=points,
        fit_curve=curve,
        observed_phase_span_deg=fit.observed_phase_span_deg,
        minimum_phase_deg=minimum_phase,
        minimum_curvature_mm=(fit.fitted_min_curvature_mm if minimum_phase is not None else None),
        maximum_phase_deg=maximum_phase,
        maximum_curvature_mm=(fit.fitted_max_curvature_mm if maximum_phase is not None else None),
        current_phase_deg=current_phase,
        decision_phase_deg=decision_phase,
        curvature_frozen=bool(state.get("curvature_frozen")),
        fit_available=fit.energy_fit is not None,
        reason_codes=list(dict.fromkeys(reason for reason in reasons if reason)),
    )


def _diagnostic_overlay(
    solution: StemCurvaturePhaseSolutionPayload,
    *,
    active_stem: SegmentPayload | None = None,
) -> OverlayPayload:
    graph = solution.diagnostic_graph
    current_point = next(
        (
            point
            for point in reversed(graph.points)
            if point.curvature_rms_mm is not None and point.cumulative_phase_deg is not None
        ),
        None,
    )
    orientation = solution.orientation_result
    decision = orientation.decision if orientation is not None else None
    correction = (
        orientation.recommended_rotation_deg_signed if orientation is not None else None
    )
    overall_confidence = (
        orientation.overall_confidence if orientation is not None else solution.confidence
    )
    reason_codes = (
        orientation.reason_codes if orientation is not None else solution.reason_codes
    )
    session_started = bool(solution.metadata.get("session_started"))
    phase_calibrated = bool(solution.metadata.get("phase_calibrated_valid"))
    phase_locked = bool(solution.metadata.get("phase_locked"))
    phase_status = (
        "kalibriert + gelockt"
        if phase_calibrated and phase_locked
        else "kalibriert, aber nicht gelockt"
        if phase_calibrated
        else "unkalibriert"
    )
    current_rejection = solution.metadata.get("current_observation_rejection_reason")
    visible_reasons = list(
        dict.fromkeys(
            [
                *([str(current_rejection)] if current_rejection else []),
                *reason_codes,
            ]
        )
    )
    labels = [
        (
            f"Stamm {solution.track_id or 'n/a'} · {solution.session_state}"
            + (f" · {decision}" if decision is not None else ""),
            {"value": solution.track_id or "n/a"},
        ),
        (
            f"Messpunkte {solution.observation_count} · "
            + ("neu akzeptiert" if solution.accepted_new_observation else "kein neuer Punkt")
            + f" · Fit {solution.state}",
            {
                "observation_count": solution.observation_count,
                "accepted_new_observation": solution.accepted_new_observation,
                "solver_state": solution.state,
            },
        ),
        (
            "Gates "
            f"Start {'ja' if session_started else 'offen'} · "
            f"Freeze {'ja' if solution.curvature_frozen else 'offen'} · "
            f"Decision {'ja' if solution.decision_gate_reached else 'offen'}",
            {
                "session_started": session_started,
                "curvature_frozen": solution.curvature_frozen,
                "decision_gate_reached": solution.decision_gate_reached,
            },
        ),
        (
            "Krümmung "
            + (
                f"{current_point.curvature_rms_mm:.2f} mm"
                if current_point is not None and current_point.curvature_rms_mm is not None
                else "n/a"
            ),
            (
                {"value": current_point.curvature_rms_mm}
                if current_point is not None and current_point.curvature_rms_mm is not None
                else {}
            ),
        ),
        (
            f"Mantelphase {_format_optional(solution.current_phase_deg, '°')} · "
            f"Spanne {solution.observed_phase_span_deg:.1f}° · {phase_status}",
            {
                **(
                    {"value": solution.current_phase_deg}
                    if solution.current_phase_deg is not None
                    else {}
                ),
                "observed_phase_span_deg": solution.observed_phase_span_deg,
                "phase_calibrated_valid": phase_calibrated,
                "phase_locked": phase_locked,
                "phase_tracking_mode": solution.metadata.get("phase_tracking_mode"),
                "phase_confidence": solution.metadata.get("phase_confidence"),
                "phase_inlier_ratio": solution.metadata.get("phase_inlier_ratio"),
            },
        ),
        (
            f"Ziel {_format_optional(solution.target_phase_deg, '°')} · "
            f"aktuell {_format_optional(solution.current_phase_deg, '°')}",
            (
                {"value": solution.target_phase_deg}
                if solution.target_phase_deg is not None
                else {}
            ),
        ),
        (
            (
                f"Entscheidung {decision} · " if decision is not None else ""
            )
            + f"Korrektur {_format_optional(correction, '°')}",
            ({"value": correction} if correction is not None else {}),
        ),
        (
            f"Confidence {overall_confidence:.2f} · "
            + (", ".join(visible_reasons[:3]) if visible_reasons else "OK"),
            {
                "value": overall_confidence,
                "reason_codes": visible_reasons,
                "diagnostic_graph": graph.model_dump(mode="json"),
                "decision": decision,
                "diameter_mm": _optional_number(solution.metadata.get("diameter_mm")),
                "signed_correction_deg": correction,
                "recommended_rotation_deg_signed": correction,
            },
        ),
    ]
    primitives = [
            OverlayPrimitive(
                primitive_type="label",
                layer="stem_curvature_phase_diagnostic",
                label=label,
                confidence=overall_confidence,
                metadata={
                    **metadata,
                    "track_id": solution.track_id,
                    "session_id": solution.session_id,
                },
            )
            for label, metadata in labels
        ]
    subject_outline = _diagnostic_subject_outline(solution, active_stem)
    if subject_outline is not None:
        primitives.append(subject_outline)
    return OverlayPayload(primitives=primitives)


def _diagnostic_subject_outline(
    solution: StemCurvaturePhaseSolutionPayload,
    active_stem: SegmentPayload | None,
) -> OverlayPrimitive | None:
    """Outline exactly the visible stem represented by the diagnostic graph."""

    if active_stem is None or not solution.track_id or len(active_stem.segments) != 1:
        return None
    active = _active_stem(active_stem)
    if not active.unique or active.track_id != solution.track_id:
        return None
    polygon = active_stem.segments[0].polygon
    if len(polygon) < 3:
        return None
    source_object = active_stem.objects[0] if len(active_stem.objects) == 1 else None
    return OverlayPrimitive(
        primitive_type="polygon",
        layer="stem_curvature_phase_subject",
        label="Stamm im Live-Diagnose-Diagramm",
        confidence=active.confidence,
        geometry={
            "points": [{"x": float(point.x), "y": float(point.y)} for point in polygon],
            "coordinate_system": "normalized_polygon",
        },
        image_context_id=active_stem.image_context_id,
        object_id=source_object.object_id if source_object is not None else None,
        source_object_id=source_object.object_id if source_object is not None else None,
        style={
            "stroke": "#ff1744",
            "stroke_width": 6.0,
            "fill": "rgba(255,23,68,0.03)",
        },
        metadata={
            "track_id": solution.track_id,
            "session_id": solution.session_id,
            "linked_to": "stem_curvature_phase_diagnostic",
            "diagnostic_subject": True,
        },
    )


def _format_optional(value: float | None, suffix: str = "") -> str:
    return f"{value:.1f}{suffix}" if value is not None else "n/a"


def _fit_payload(fit: HarmonicFit | None) -> StemCurvaturePhaseHarmonicFit | None:
    if fit is None:
        return None
    return StemCurvaturePhaseHarmonicFit(
        offset=fit.coefficients[0],
        cosine=fit.coefficients[1],
        sine=fit.coefficients[2],
        residual_rms=fit.residual_rms,
        residual_ratio=fit.residual_ratio,
        condition_number=fit.condition_number,
        inlier_weight_ratio=fit.inlier_weight_ratio,
        stable=fit.stable,
    )


def _provenance(
    node: RuntimeNodeSpec | None,
    curvature: StemHorizontalCurvaturePayload | None,
    phase: StemSurfacePhasePayload | None,
) -> list[ProvenanceLink]:
    node_key = node.node_key if node is not None else None
    links: list[ProvenanceLink] = []
    if curvature is not None and curvature.process_calibration_hash:
        links.append(
            ProvenanceLink(
                source_kind="operator_config",
                source_id=curvature.process_calibration_hash,
                relationship="uses_stem_curvature_process_calibration",
                node_key=node_key,
            )
        )
    if phase is not None and phase.calibration_hash:
        links.append(
            ProvenanceLink(
                source_kind="operator_config",
                source_id=phase.calibration_hash,
                relationship="uses_stem_surface_phase_calibration",
                node_key=node_key,
            )
        )
    if node is not None:
        links.append(
            ProvenanceLink(
                source_kind="node",
                source_id=node.node_key,
                relationship="fits_curvature_over_surface_phase",
                node_key=node.node_key,
                metadata={"algorithm_version": ALGORITHM_VERSION},
            )
        )
    return links


def _event_types(events: object) -> set[str]:
    if not isinstance(events, EventsPayload):
        return set()
    return {item.event_type.strip().casefold() for item in events.events}


def _event_session_id(events: object) -> str | None:
    if not isinstance(events, EventsPayload):
        return None
    for item in events.events:
        value = item.payload.get("session_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _runtime_session_id(context: RuntimeExecutionContext, *, track_id: str | None = None) -> str:
    root = context.runtime_execution_id or context.correlation_id
    return f"{root}:{track_id}" if track_id else root


def _remember_reasons(state: dict[str, Any], reasons: list[str] | tuple[str, ...]) -> None:
    history = state.setdefault("reason_code_history", [])
    for reason in reasons:
        if reason and (not history or history[-1] != reason):
            history.append(reason)
    if len(history) > 96:
        del history[:-96]


def _frame_sequence(
    curvature: StemHorizontalCurvaturePayload, phase: StemSurfacePhasePayload
) -> int | None:
    return (
        curvature.frame_sequence if curvature.frame_sequence is not None else phase.frame_sequence
    )


def _number(config: dict[str, Any], key: str, default: float) -> float:
    value = config.get(key, default)
    if isinstance(value, bool):
        raise TypeError(f"{key} must be numeric")
    return float(value)


def _integer(config: dict[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    if isinstance(value, bool) or int(value) != float(value):
        raise TypeError(f"{key} must be an integer")
    return int(value)


analysis_stem_curvature_phase_solver = StemCurvaturePhaseSolverHandler()
