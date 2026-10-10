from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any, Literal, cast

from cv_contracts.runtime import RuntimeNodeSpec

from cv_runtime.context import NodeInputs, RuntimeExecutionContext
from cv_runtime.handlers import NodeExecutionResult
from cv_runtime.high_rate_phase import HighRateStemPhaseBridge
from cv_runtime.payloads import (
    EventsPayload,
    ProvenanceLink,
    SegmentPayload,
    StemHorizontalCurvaturePayload,
    StemProjectionObservationPayload,
    StemSliceFitDiagnosticsPayload,
    StemSliceGeometryPayload,
    StemSliceModelPayload,
    StemSurfacePhasePayload,
)
from cv_runtime.stem_slice_measurement import (
    STEM_SLICE_MEASUREMENT_VERSION,
    StemProjectionObservation,
    StemSliceFitConfig,
    StemSliceModelBuilderConfig,
    StemSliceModelBuildResult,
    StemSliceModelStatus,
    build_stem_slice_model,
)
from cv_runtime.stem_slice_preprocessing import StemSlicePreprocessingConfig

_STATE_SCHEMA = "stem.slice-model-session.state.v2"
_RESET_EVENTS = {"pipeline_reset", "runtime_reset", "pipeline_stopped"}
_SESSION_END_EVENTS = {
    "session_end",
    "stem_session_end",
    "track_completed",
    "stem_track_completed",
}
StemSlicePayloadState = Literal[
    "RESET", "COLLECTING", "READY", "DEGRADED", "FROZEN", "INVALID"
]


class StemSliceModelHandler:
    node_type = "analysis.stem_slice_model"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        state = _node_state(context, node)
        event_types = _event_types(inputs.get("events"))
        if event_types & _RESET_EVENTS:
            _reset_state(state)
            return NodeExecutionResult(
                outputs={"slice_model": _payload_from_state(state, reasons=["PIPELINE_RESET"])}
            )
        if event_types & _SESSION_END_EVENTS:
            _reset_state(state)
            return NodeExecutionResult(
                outputs={"slice_model": _payload_from_state(state, reasons=["SESSION_ENDED"])}
            )

        curvature = inputs.get("curvature")
        phase = inputs.get("surface_phase")
        bridge = context.metadata.get("high_rate_stem_phase_bridge")
        if (isinstance(phase, StemSurfacePhasePayload)
                and phase.execution_mode == "HIGH_RATE_SEEDED"
                and isinstance(bridge, HighRateStemPhaseBridge)
                and phase.trace_generation != bridge.generation):
            identity = state.get("identity")
            if (isinstance(identity, Mapping)
                    and identity.get("trace_generation") != bridge.generation):
                _reset_state(state)
            return NodeExecutionResult(outputs={"slice_model": _payload_from_state(
                state, reasons=["PHASE_GENERATION_MISMATCH"], state_override="INVALID",
            )})
        active_stem = inputs.get("active_stem")
        active = _active_tracking(active_stem)
        try:
            runtime_config = _runtime_config(node.config or {})
            builder_config = _builder_config(node.config or {})
        except (TypeError, ValueError) as exc:
            state["last_error_detail"] = str(exc)[:200]
            return NodeExecutionResult(
                outputs={
                    "slice_model": _payload_from_state(
                        state,
                        reasons=["CONFIGURATION_INVALID"],
                        state_override="INVALID",
                    )
                }
            )

        identity = _input_identity(curvature, phase, active)
        previous_identity = state.get("identity")
        if previous_identity is not None and identity is not None and identity != previous_identity:
            _reset_state(state)
            state["rejected_observation_frame_count"] = 1
            state["reason_code_history"].append("INPUT_BINDING_CHANGED_RESET")
        if identity is not None:
            state["identity"] = identity
            _remember_identity(state, identity, context)

        if isinstance(state.get("frozen_payload"), Mapping):
            return NodeExecutionResult(
                outputs={
                    "slice_model": StemSliceModelPayload.model_validate(state["frozen_payload"])
                }
            )

        movement = active.get("movement")
        process_zone = (
            str(movement.get("process_zone") or active.get("process_zone") or "")
            if isinstance(movement, Mapping)
            else str(active.get("process_zone") or "")
        ).casefold()
        process_stage = int(active.get("process_stage") or 0)
        longitudinal_transport = (
            process_stage >= 1 or process_zone == "longitudinal_conveyor"
        )
        freeze_gate = bool(active.get("curvature_freeze_gate_reached"))
        decision_gate = bool(active.get("decision_gate_reached"))
        if longitudinal_transport:
            state["measurement_frozen"] = True
            state["freeze_reason"] = "LONGITUDINAL_TRANSPORT_STARTED"
        elif freeze_gate:
            state["measurement_frozen"] = True
            state["freeze_reason"] = "CURVATURE_FREEZE_GATE_REACHED"
        freeze_reason = state.get("freeze_reason")
        if isinstance(freeze_reason, str):
            history = state.setdefault("reason_code_history", [])
            if not history or history[-1] != freeze_reason:
                history.append(freeze_reason)
        if decision_gate:
            state["decision_gate_reached"] = True

        rejection = _observation_rejection_reason(
            curvature,
            phase,
            active,
            state=state,
            config=runtime_config,
        )
        if rejection is None:
            assert isinstance(curvature, StemHorizontalCurvaturePayload)
            assert isinstance(phase, StemSurfacePhasePayload)
            accepted = _append_profile_observations(
                state,
                curvature,
                phase,
                active,
                config=runtime_config,
                builder_config=builder_config,
            )
            if accepted:
                if (phase.execution_mode == "HIGH_RATE_SEEDED"
                        and isinstance(bridge, HighRateStemPhaseBridge)):
                    assert phase.track_id is not None and phase.frame_sequence is not None
                    bridge.record_profile_pair(
                        phase.track_id, phase.frame_sequence, phase.trace_generation
                    )
                state["last_frame_sequence"] = curvature.frame_sequence
                state["last_timestamp"] = curvature.timestamp or phase.timestamp
                state["last_phase_deg"] = phase.cumulative_phase_deg
                state["observation_frame_count"] = int(state["observation_frame_count"]) + 1
                state["session_started"] = True
            else:
                rejection = "PROFILE_HAS_NO_COMMON_VALID_STATIONS"
        elif rejection not in {"MEASUREMENT_FROZEN", "CURVATURE_FROZEN"}:
            state["rejected_observation_frame_count"] = (
                int(state["rejected_observation_frame_count"]) + 1
            )
        if rejection:
            history = state.setdefault("reason_code_history", [])
            if not history or history[-1] != rejection:
                history.append(rejection)
            if len(history) > 96:
                del history[:-96]

        if (
            isinstance(phase, StemSurfacePhasePayload)
            and phase.calibrated_phase_valid
            and phase.phase_locked
            and phase.cumulative_phase_deg is not None
            and identity is not None
        ):
            state["last_phase_deg"] = phase.cumulative_phase_deg
            state["last_frame_sequence"] = (
                phase.frame_sequence
                if phase.frame_sequence is not None
                else state.get("last_frame_sequence")
            )
            state["last_timestamp"] = phase.timestamp or state.get("last_timestamp")

        reference_phase = _optional_float(state.get("last_phase_deg"))
        build = (
            _build_from_state(state, reference_phase, builder_config)
            if reference_phase is not None and state.get("observations")
            else None
        )
        reasons = [rejection] if rejection else []
        if build is not None:
            reasons.extend(build.reason_codes)
        should_freeze = bool(
            state.get("measurement_frozen")
            and build is not None
            and _build_is_usable(build)
        )
        payload = _payload_from_state(
            state,
            build=build,
            reasons=reasons,
            frozen=should_freeze,
            include_raw_observations=runtime_config["include_raw_observations"],
        )
        if should_freeze:
            state["frozen_payload"] = payload.model_dump(mode="json")
        return NodeExecutionResult(outputs={"slice_model": payload})


def _runtime_config(config: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "minimum_geometry_confidence": _number(config, "minimum_geometry_confidence", 0.4),
        "minimum_phase_confidence": _number(config, "minimum_phase_confidence", 0.45),
        "minimum_tracking_confidence": _number(config, "minimum_tracking_confidence", 0.35),
        "maximum_frame_sequence_gap": _integer(config, "maximum_frame_sequence_gap", 2),
        "maximum_material_x_step_mm": _number(
            config, "maximum_material_x_step_mm", 75.0
        ),
        "maximum_material_x_excursion_mm": _number(
            config, "maximum_material_x_excursion_mm", 150.0
        ),
        "maximum_observation_frames": _integer(config, "maximum_observation_frames", 180),
        "include_raw_observations": bool(config.get("include_raw_observations", False)),
    }
    for key in (
        "minimum_geometry_confidence",
        "minimum_phase_confidence",
        "minimum_tracking_confidence",
    ):
        if not 0 <= result[key] <= 1:
            raise ValueError(f"{key} must be between zero and one")
    for key in (
        "maximum_material_x_step_mm",
        "maximum_material_x_excursion_mm",
    ):
        if result[key] < 0:
            raise ValueError(f"{key} must not be negative")
    if result["maximum_frame_sequence_gap"] < 0:
        raise ValueError("maximum_frame_sequence_gap must not be negative")
    if not 3 <= result["maximum_observation_frames"] <= 2000:
        raise ValueError("maximum_observation_frames must be in [3, 2000]")
    return result


def _builder_config(config: Mapping[str, Any]) -> StemSliceModelBuilderConfig:
    spacing = _number(config, "slice_spacing_mm", 50.0)
    maximum_gap = _number(config, "maximum_interpolation_gap_mm", 100.0)
    fit = StemSliceFitConfig(
        minimum_observations=_integer(config, "minimum_observations_per_slice", 7),
        minimum_unique_phases=_integer(config, "minimum_unique_phases", 6),
        minimum_phase_span_deg=_number(config, "minimum_phase_span_deg", 90.0),
        phase_separation_deg=_number(config, "phase_separation_deg", 8.0),
        minimum_observation_quality=_number(config, "minimum_observation_quality", 0.1),
        maximum_condition_number=_number(config, "maximum_fit_condition_number", 100_000),
        maximum_center_residual_rms_mm=_number(
            config, "maximum_center_residual_rms_mm", 5.0
        ),
        maximum_width_residual_rms_mm=_number(config, "maximum_width_residual_rms_mm", 5.0),
        minimum_slice_quality=_number(config, "minimum_slice_quality", 0.25),
    )
    preprocessing = StemSlicePreprocessingConfig(
        slice_spacing_mm=spacing,
        max_interpolation_gap_mm=maximum_gap,
        smoothing_window_slices=_integer(config, "smoothing_window_slices", 3),
        smoothing_strength=_number(config, "smoothing_strength", 0.25),
        minimum_slice_quality=_number(config, "minimum_slice_quality", 0.25),
    )
    return StemSliceModelBuilderConfig(
        slice_spacing_mm=spacing,
        minimum_valid_slice_count=_integer(config, "minimum_valid_slice_count", 3),
        minimum_valid_slice_ratio=_number(config, "minimum_valid_slice_ratio", 0.6),
        maximum_invalid_gap_mm=maximum_gap,
        minimum_model_quality=_number(config, "minimum_model_quality", 0.25),
        degraded_model_quality=_number(config, "degraded_model_quality", 0.6),
        fit=fit,
        preprocessing=preprocessing,
    ).validate()


def _observation_rejection_reason(
    curvature: object,
    phase: object,
    active: Mapping[str, Any],
    *,
    state: dict[str, Any],
    config: Mapping[str, Any],
) -> str | None:
    if not active or active.get("status") != "active" or bool(active.get("fail_closed")):
        return "ACTIVE_STEM_NOT_UNIQUE"
    if not bool(active.get("session_start_gate_reached")):
        return "MEASUREMENT_SESSION_NOT_STARTED"
    if bool(state.get("measurement_frozen")):
        return "MEASUREMENT_FROZEN"
    if not isinstance(curvature, StemHorizontalCurvaturePayload):
        return "CURVATURE_INPUT_MISSING"
    if not isinstance(phase, StemSurfacePhasePayload):
        return "SURFACE_PHASE_INPUT_MISSING"
    if not curvature.valid or curvature.observation_state != "accepted":
        return (
            "CURVATURE_FROZEN"
            if curvature.observation_state == "curvature_frozen"
            else "CURVATURE_INVALID"
        )
    if not curvature.common_segment_descriptor.comparable:
        return "CURVATURE_PROFILE_NOT_COMPARABLE"
    if curvature.geometry_confidence < float(config["minimum_geometry_confidence"]):
        return "GEOMETRY_CONFIDENCE_LOW"
    if (
        not phase.calibrated_phase_valid
        or not phase.phase_locked
        or phase.cumulative_phase_deg is None
        or phase.rotation_reference_view is None
        or phase.positive_rotation is None
    ):
        return "CALIBRATED_PHASE_INVALID"
    if phase.confidence < float(config["minimum_phase_confidence"]):
        return "PHASE_CONFIDENCE_LOW"
    track_id = active.get("track_id")
    if (
        not isinstance(track_id, str)
        or track_id != curvature.track_id
        or track_id != phase.track_id
    ):
        return "TRACK_BINDING_MISMATCH"
    if curvature.process_calibration_hash != phase.process_calibration_hash:
        return "PROCESS_CALIBRATION_MISMATCH"
    if not curvature.process_calibration_hash or not phase.calibration_hash:
        return "CALIBRATION_BINDING_MISSING"
    if phase.execution_mode == "HIGH_RATE_SEEDED" and (
        curvature.frame_sequence is None or phase.frame_sequence is None
        or curvature.frame_sequence != phase.frame_sequence
    ):
        return "PHASE_EXACT_FRAME_REQUIRED"
    if curvature.frame_sequence is not None and phase.frame_sequence is not None:
        if abs(curvature.frame_sequence - phase.frame_sequence) > int(
            config["maximum_frame_sequence_gap"]
        ):
            return "FRAME_SEQUENCE_GAP_TOO_LARGE"
    tracking_confidence = _optional_float(active.get("selection_confidence")) or 0.0
    if tracking_confidence < float(config["minimum_tracking_confidence"]):
        return "TRACKING_CONFIDENCE_LOW"
    movement = active.get("movement")
    if not isinstance(movement, Mapping):
        return "MOTION_DIAGNOSTICS_MISSING"
    process_zone = str(
        movement.get("process_zone") or active.get("process_zone") or ""
    ).casefold()
    if int(active.get("process_stage") or 0) >= 1 or process_zone == "longitudinal_conveyor":
        return "LONGITUDINAL_TRANSPORT_STARTED"
    if process_zone != "transfer":
        return "TRANSFER_PHASE_UNCONFIRMED"
    transition_pending = movement.get("longitudinal_transition_pending")
    if transition_pending is True or (
        transition_pending is None and int(movement.get("longitudinal_motion_streak") or 0) > 0
    ):
        return "LONGITUDINAL_TRANSITION_PENDING"
    cumulative_x_mm = phase.translation_component.cumulative_x_mm
    if cumulative_x_mm is None:
        return "MATERIAL_X_REGISTRATION_UNAVAILABLE"
    registration_origin = _optional_float(state.get("registration_origin_x_mm"))
    registered_shift = (
        0.0 if registration_origin is None else cumulative_x_mm - registration_origin
    )
    previous_shift = _optional_float(state.get("last_material_x_shift_mm"))
    if (
        previous_shift is not None
        and abs(registered_shift - previous_shift)
        > float(config["maximum_material_x_step_mm"])
    ):
        return "MATERIAL_X_REGISTRATION_STEP_IMPLAUSIBLE"
    if abs(registered_shift) > float(config["maximum_material_x_excursion_mm"]):
        return "MATERIAL_X_EXCURSION_TOO_LARGE"
    return None


def _append_profile_observations(
    state: dict[str, Any],
    curvature: StemHorizontalCurvaturePayload,
    phase: StemSurfacePhasePayload,
    active: Mapping[str, Any],
    *,
    config: Mapping[str, Any],
    builder_config: StemSliceModelBuilderConfig,
) -> int:
    if phase.cumulative_phase_deg is None:
        return 0
    descriptor = curvature.common_segment_descriptor
    if descriptor.x_start_mm is None or descriptor.x_end_mm is None:
        return 0
    start = descriptor.x_start_mm
    end = descriptor.x_end_mm
    if end <= start:
        return 0
    spacing = builder_config.slice_spacing_mm
    cumulative_x_mm = phase.translation_component.cumulative_x_mm
    if cumulative_x_mm is None:
        return 0
    registration_origin = _optional_float(state.get("registration_origin_x_mm"))
    if registration_origin is None:
        registration_origin = cumulative_x_mm
        state["registration_origin_x_mm"] = registration_origin
    material_shift = cumulative_x_mm - registration_origin
    machine_origin = _optional_float(state.get("material_x_origin_machine_mm"))
    if machine_origin is None:
        machine_origin = math.ceil((start - 1.0e-9) / spacing) * spacing
        state["material_x_origin_machine_mm"] = machine_origin
    current_material_start = start - machine_origin - material_shift
    current_material_end = end - machine_origin - material_shift
    common_start = max(
        float(state.get("common_material_x_start_mm", current_material_start)),
        current_material_start,
    )
    common_end = min(
        float(state.get("common_material_x_end_mm", current_material_end)),
        current_material_end,
    )
    if common_end <= common_start:
        return 0
    state["common_material_x_start_mm"] = common_start
    state["common_material_x_end_mm"] = common_end
    first_index = math.ceil((common_start - 1.0e-9) / spacing)
    last_index = math.floor((common_end + 1.0e-9) / spacing)
    material_stations = [index * spacing for index in range(first_index, last_index + 1)]
    if not material_stations:
        return 0
    state["last_material_x_shift_mm"] = material_shift
    state["minimum_material_x_shift_mm"] = min(
        float(state.get("minimum_material_x_shift_mm", material_shift)),
        material_shift,
    )
    state["maximum_material_x_shift_mm"] = max(
        float(state.get("maximum_material_x_shift_mm", material_shift)),
        material_shift,
    )
    center_points = sorted(curvature.centerline_profile, key=lambda item: item.x_mm)
    width_points = sorted(
        [item for item in curvature.width_profile if item.valid and item.width_mm is not None],
        key=lambda item: item.x_mm,
    )
    tracking_quality = _bounded_quality(active.get("selection_confidence"))
    segmentation_quality = _bounded_quality(
        curvature.metadata.get("segmentation_score", curvature.selected_component_fraction)
    )
    added: list[dict[str, Any]] = []
    for material_x in material_stations:
        machine_x = machine_origin + material_x + material_shift
        center = _interpolate_center(center_points, machine_x)
        width = _interpolate_width(width_points, machine_x)
        if center is None or width is None or width <= 0:
            continue
        center_mm, center_weight = center
        quality = min(
            curvature.geometry_confidence,
            phase.confidence,
            tracking_quality,
            segmentation_quality,
            center_weight,
        )
        observation = StemProjectionObservation(
            x_mm=float(material_x),
            phase_deg=float(phase.cumulative_phase_deg),
            projected_center_mm=center_mm,
            visible_width_mm=width,
            quality=quality,
            frame_sequence=curvature.frame_sequence,
            timestamp=curvature.timestamp or phase.timestamp,
            geometry_quality=curvature.geometry_confidence,
            phase_quality=phase.confidence,
            segmentation_quality=segmentation_quality,
            tracking_quality=tracking_quality,
        ).validate()
        added.append(
            {
                **asdict(observation),
                "machine_x_mm": machine_x,
                "material_x_shift_mm": material_shift,
            }
        )
    if not added:
        return 0
    stored = state.setdefault("observations", [])
    stored.extend(added)
    frame_sequences = state.setdefault("observation_frame_sequences", [])
    if curvature.frame_sequence not in frame_sequences:
        frame_sequences.append(curvature.frame_sequence)
    maximum_frames = int(config["maximum_observation_frames"])
    if len(frame_sequences) > maximum_frames:
        drop = set(frame_sequences[: len(frame_sequences) - maximum_frames])
        del frame_sequences[: len(frame_sequences) - maximum_frames]
        state["observations"] = [
            item for item in stored if item.get("frame_sequence") not in drop
        ]
    return len(added)


def _build_from_state(
    state: Mapping[str, Any],
    reference_phase_deg: float,
    config: StemSliceModelBuilderConfig,
) -> StemSliceModelBuildResult:
    common_start = _optional_float(state.get("common_material_x_start_mm"))
    common_end = _optional_float(state.get("common_material_x_end_mm"))
    stored = state.get("observations")
    observations: list[StemProjectionObservation] = []
    if isinstance(stored, list):
        for item in stored:
            if not isinstance(item, Mapping):
                continue
            material_x = _optional_float(item.get("x_mm"))
            if material_x is None:
                continue
            if common_start is not None and material_x < common_start - 1.0e-9:
                continue
            if common_end is not None and material_x > common_end + 1.0e-9:
                continue
            observations.append(
                StemProjectionObservation(
                    **{key: item[key] for key in StemProjectionObservation.__dataclass_fields__}
                )
            )
    return build_stem_slice_model(
        observations,
        reference_phase_deg=reference_phase_deg,
        config=config,
    )


def _payload_from_state(
    state: Mapping[str, Any],
    *,
    build: StemSliceModelBuildResult | None = None,
    reasons: Sequence[str] = (),
    frozen: bool = False,
    state_override: StemSlicePayloadState | None = None,
    include_raw_observations: bool = False,
) -> StemSliceModelPayload:
    stored_identity = state.get("identity")
    identity: Mapping[str, Any] = (
        stored_identity if isinstance(stored_identity, Mapping) else {}
    )
    valid = build is not None and _build_is_usable(build)
    if state_override is not None:
        payload_state: StemSlicePayloadState = state_override
    elif frozen and valid:
        payload_state = "FROZEN"
    elif build is None:
        payload_state = "COLLECTING" if state.get("session_started") else "RESET"
    elif build.status is StemSliceModelStatus.VALID:
        payload_state = "READY"
    elif build.status is StemSliceModelStatus.DEGRADED:
        payload_state = "DEGRADED"
    else:
        payload_state = "COLLECTING" if not state.get("measurement_frozen") else "INVALID"
    all_reasons = list(
        dict.fromkeys(
            [
                *(str(item) for item in state.get("reason_code_history", []) if item),
                *(str(item) for item in reasons if item),
            ]
        )
    )[-96:]
    raw_slices = _geometry_payloads(build.raw_model.slices) if build is not None else []
    processed_slices = (
        _geometry_payloads(build.processed_model.slices, mask_invalid=True)
        if build is not None
        else []
    )
    diagnostics = (
        [StemSliceFitDiagnosticsPayload(**asdict(item.diagnostics)) for item in build.station_fits]
        if build is not None
        else []
    )
    raw_observations: list[StemProjectionObservationPayload] = []
    if include_raw_observations:
        for item in state.get("observations", []):
            if not isinstance(item, Mapping):
                continue
            raw_observations.append(
                StemProjectionObservationPayload(
                    x_mm=float(item["x_mm"]),
                    phase_deg=float(item["phase_deg"]),
                    projected_center_mm=float(item["projected_center_mm"]),
                    visible_width_mm=float(item["visible_width_mm"]),
                    quality=float(item["quality"]),
                    frame_sequence=item.get("frame_sequence"),
                    timestamp=item.get("timestamp"),
                )
            )
    model_hash = _model_hash(build, identity) if valid and build is not None else None
    return StemSliceModelPayload(
        state=payload_state,
        valid=valid,
        frozen=frozen and valid,
        session_id=_optional_string(state.get("session_id")),
        track_id=_optional_string(identity.get("track_id")),
        frame_sequence=_optional_int(state.get("last_frame_sequence")),
        timestamp=_optional_string(state.get("last_timestamp")),
        current_pose_phase_deg=(
            _optional_float(state.get("last_phase_deg")) if valid else None
        ),
        rotation_reference_view=(
            _optional_string(identity.get("rotation_reference_view")) if valid else None
        ),
        positive_rotation=_optional_string(identity.get("positive_rotation")) if valid else None,
        trace_generation=_optional_int(identity.get("trace_generation")),
        process_calibration_hash=_optional_string(identity.get("process_calibration_hash")),
        phase_calibration_id=_optional_string(identity.get("phase_calibration_id")),
        phase_calibration_hash=_optional_string(identity.get("phase_calibration_hash")),
        model_hash=model_hash,
        model_quality=build.model_quality if build is not None else 0,
        requested_station_count=build.requested_station_count if build is not None else 0,
        valid_raw_slice_count=build.valid_raw_slice_count if build is not None else 0,
        valid_processed_slice_count=(build.valid_processed_slice_count if build is not None else 0),
        valid_raw_slice_ratio=build.valid_raw_slice_ratio if build is not None else 0,
        maximum_invalid_gap_mm=build.maximum_invalid_gap_mm if build is not None else 0,
        observation_frame_count=int(state.get("observation_frame_count") or 0),
        accepted_observation_count=len(state.get("observations", [])),
        rejected_observation_frame_count=int(state.get("rejected_observation_frame_count") or 0),
        raw_slices=raw_slices,
        processed_slices=processed_slices,
        fit_diagnostics=diagnostics,
        raw_observations=raw_observations,
        reason_codes=all_reasons,
        provenance=_provenance(identity),
        metadata={
            "measurement_pose_mode": "NATURAL_ROLL_TRANSFER_ONLY",
            "material_registration_mode": "TRACKED_SIGNED_MATERIAL_X_V1",
            "material_x_origin_machine_mm": state.get("material_x_origin_machine_mm"),
            "common_material_x_start_mm": state.get("common_material_x_start_mm"),
            "common_material_x_end_mm": state.get("common_material_x_end_mm"),
            "current_material_x_shift_mm": state.get("last_material_x_shift_mm"),
            "minimum_material_x_shift_mm": state.get("minimum_material_x_shift_mm"),
            "maximum_material_x_shift_mm": state.get("maximum_material_x_shift_mm"),
            "measurement_frozen": bool(state.get("measurement_frozen")),
            "freeze_reason": state.get("freeze_reason"),
            "last_error_detail": state.get("last_error_detail"),
        },
    )


def _geometry_payloads(
    slices: Sequence[Any], *, mask_invalid: bool = False
) -> list[StemSliceGeometryPayload]:
    output: list[StemSliceGeometryPayload] = []
    for item in slices:
        if mask_invalid and not item.valid:
            output.append(
                StemSliceGeometryPayload(
                    x_mm=item.x_mm,
                    quality=0,
                    valid=False,
                    interpolated=False,
                    reason_codes=list(item.reason_codes),
                )
            )
            continue
        output.append(
            StemSliceGeometryPayload(
                x_mm=item.x_mm,
                cy_mm=item.cy_mm,
                cz_mm=item.cz_mm,
                a_mm=item.a_mm,
                b_mm=item.b_mm,
                phi_rad=item.phi_rad,
                quality=item.quality,
                valid=item.valid,
                interpolated=item.interpolated,
                reason_codes=list(item.reason_codes),
            )
        )
    return output


def _build_is_usable(build: StemSliceModelBuildResult) -> bool:
    return (
        build.status is not StemSliceModelStatus.INVALID
        and build.valid_processed_slice_count > 0
    )


def _model_hash(build: StemSliceModelBuildResult, identity: Mapping[str, Any]) -> str:
    serialized = {
        "algorithm_version": STEM_SLICE_MEASUREMENT_VERSION,
        "reference_phase_deg": build.reference_phase_deg,
        "slices": [asdict(item) for item in build.processed_model.slices if item.valid],
        "identity": dict(identity),
    }
    return hashlib.sha256(
        json.dumps(serialized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _input_identity(
    curvature: object,
    phase: object,
    active: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(curvature, StemHorizontalCurvaturePayload):
        return None
    if not isinstance(phase, StemSurfacePhasePayload):
        return None
    track_id = active.get("track_id")
    if not isinstance(track_id, str) or not track_id:
        return None
    return {
        "track_id": track_id,
        "trace_generation": phase.trace_generation,
        "process_calibration_hash": curvature.process_calibration_hash,
        "phase_calibration_id": phase.calibration_id,
        "phase_calibration_hash": phase.calibration_hash,
        "rotation_reference_view": phase.rotation_reference_view,
        "positive_rotation": phase.positive_rotation,
    }


def _remember_identity(
    state: dict[str, Any], identity: Mapping[str, Any], context: RuntimeExecutionContext
) -> None:
    state["session_id"] = state.get("session_id") or (
        f"{context.runtime_execution_id or context.correlation_id}:{identity['track_id']}"
    )


def _node_state(context: RuntimeExecutionContext, node: RuntimeNodeSpec) -> dict[str, Any]:
    value = context.state.setdefault(node.node_key, {})
    if not isinstance(value, dict) or value.get("schema_version") not in {None, _STATE_SCHEMA}:
        value = {}
        context.state[node.node_key] = value
    value.setdefault("schema_version", _STATE_SCHEMA)
    value.setdefault("observations", [])
    value.setdefault("observation_frame_sequences", [])
    value.setdefault("observation_frame_count", 0)
    value.setdefault("rejected_observation_frame_count", 0)
    value.setdefault("measurement_frozen", False)
    value.setdefault("decision_gate_reached", False)
    value.setdefault("session_started", False)
    value.setdefault("reason_code_history", [])
    value.setdefault("frozen_payload", None)
    return cast(dict[str, Any], value)


def _reset_state(state: dict[str, Any]) -> None:
    state.clear()
    state.update(
        {
            "schema_version": _STATE_SCHEMA,
            "observations": [],
            "observation_frame_sequences": [],
            "observation_frame_count": 0,
            "rejected_observation_frame_count": 0,
            "measurement_frozen": False,
            "decision_gate_reached": False,
            "session_started": False,
            "reason_code_history": [],
            "frozen_payload": None,
        }
    )


def _active_tracking(value: object) -> Mapping[str, Any]:
    if not isinstance(value, SegmentPayload):
        return {}
    tracking = value.metadata.get("active_stem_tracking")
    return tracking if isinstance(tracking, Mapping) else {}


def _event_types(value: object) -> set[str]:
    if not isinstance(value, EventsPayload):
        return set()
    return {item.event_type.strip().casefold() for item in value.events}


def _interpolate_center(points: Sequence[Any], x_mm: float) -> tuple[float, float] | None:
    pair = _bracket(points, x_mm)
    if pair is None:
        return None
    left, right, fraction = pair
    return (
        float(left.y_center_mm + fraction * (right.y_center_mm - left.y_center_mm)),
        float(min(left.weight, right.weight)),
    )


def _interpolate_width(points: Sequence[Any], x_mm: float) -> float | None:
    pair = _bracket(points, x_mm)
    if pair is None:
        return None
    left, right, fraction = pair
    assert left.width_mm is not None and right.width_mm is not None
    return float(left.width_mm + fraction * (right.width_mm - left.width_mm))


def _bracket(points: Sequence[Any], x_mm: float) -> tuple[Any, Any, float] | None:
    if not points or x_mm < points[0].x_mm - 1.0e-9 or x_mm > points[-1].x_mm + 1.0e-9:
        return None
    for item in points:
        if abs(item.x_mm - x_mm) <= 1.0e-9:
            return item, item, 0.0
    for left, right in zip(points, points[1:], strict=False):
        if left.x_mm < x_mm < right.x_mm:
            fraction = (x_mm - left.x_mm) / (right.x_mm - left.x_mm)
            return left, right, fraction
    return None


def _provenance(identity: Mapping[str, Any]) -> list[ProvenanceLink]:
    links: list[ProvenanceLink] = []
    for source_id, relationship in (
        (identity.get("process_calibration_hash"), "uses_process_calibration"),
        (identity.get("phase_calibration_hash"), "uses_surface_phase_calibration"),
    ):
        if isinstance(source_id, str) and source_id:
            links.append(
                ProvenanceLink(
                    source_kind="operator_config",
                    source_id=source_id,
                    relationship=relationship,
                )
            )
    return links


def _number(config: Mapping[str, Any], key: str, default: float) -> float:
    value = config.get(key, default)
    if isinstance(value, bool):
        raise TypeError(f"{key} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{key} must be finite")
    return result


def _integer(config: Mapping[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    if isinstance(value, bool) or int(value) != float(value):
        raise TypeError(f"{key} must be an integer")
    return int(value)


def _bounded_quality(value: object) -> float:
    result = _optional_float(value)
    return max(0.0, min(1.0, result if result is not None else 0.0))


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


analysis_stem_slice_model = StemSliceModelHandler()
