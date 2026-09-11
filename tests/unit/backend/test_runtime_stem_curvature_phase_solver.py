from __future__ import annotations

import math
from pathlib import Path

import pytest
from cv_contracts import SegmentationPoint, SegmentationPrediction
from cv_contracts.runtime import RuntimeNodeSpec
from cv_runtime import BUILTIN_NODE_TYPE_BY_KEY
from cv_runtime.context import RuntimeExecutionContext
from cv_runtime.loader import load_runtime_handler
from cv_runtime.nodes.stem_curvature_phase_solver import (
    StemCurvaturePhaseSolverHandler,
    _diagnostic_overlay,
)
from cv_runtime.payloads import (
    EventPayload,
    EventsPayload,
    ResultPayload,
    SegmentPayload,
    StemCurvaturePhaseSolutionPayload,
    StemHorizontalCommonSegmentDescriptor,
    StemHorizontalCurvaturePayload,
    StemSurfacePhasePayload,
)
from cv_status import RuntimeMode


def _node() -> RuntimeNodeSpec:
    definition = BUILTIN_NODE_TYPE_BY_KEY["analysis.stem_curvature_phase_solver"]
    return RuntimeNodeSpec(
        node_key="curvature_phase_solver",
        node_type=definition.node_type,
        node_category=definition.node_category,
        node_contract_version=definition.contract_version,
        input_ports=definition.input_ports,
        output_ports=definition.output_ports,
        config={
            **definition.default_config,
            "minimum_observations": 7,
            "minimum_phase_span_deg": 80,
        },
    )


def _context() -> RuntimeExecutionContext:
    return RuntimeExecutionContext(
        correlation_id="corr-curvature-phase",
        pipeline_version_id="piv-curvature-phase",
        runtime_execution_id="rex-curvature-phase",
        channel_id="chn-stem",
        mode=RuntimeMode.REALTIME,
        state={},
    )


def _curvature(
    sequence: int,
    phase_deg: float,
    *,
    track_id: str = "track-a",
    frozen: bool = False,
    straight: bool = False,
    x_start_mm: float = 0,
    x_end_mm: float = 1000,
    canonical_view_corridor: bool = False,
) -> StemHorizontalCurvaturePayload:
    radians = math.radians(phase_deg)
    signed_bow = (
        0.2 * math.cos(radians - math.radians(40))
        if straight
        else 2 + 8 * math.cos(radians - math.radians(40))
    )
    energy = (
        0.3 + 0.02 * math.cos(2 * (radians - math.radians(20)))
        if straight
        else 25 + 16 * math.cos(2 * (radians - math.radians(20)))
    )
    return StemHorizontalCurvaturePayload(
        valid=not frozen,
        observation_state="curvature_frozen" if frozen else "accepted",
        track_id=track_id,
        source_object_id=f"obj-{track_id}",
        image_context_id="ctx-process",
        frame_sequence=sequence,
        timestamp=f"2026-08-04T12:00:{sequence:02d}Z",
        calibration_id="spc-test-v1",
        process_calibration_hash="process-hash",
        signed_bow_mm=None if frozen else signed_bow,
        curvature_rms_mm=None if frozen else math.sqrt(energy),
        curvature_maxabs_mm=None if frozen else abs(signed_bow),
        curvature_peak_to_peak_mm=None if frozen else 2 * abs(signed_bow),
        common_segment_descriptor=StemHorizontalCommonSegmentDescriptor(
            x_start_mm=x_start_mm,
            x_end_mm=x_end_mm,
            length_mm=x_end_mm - x_start_mm,
            common_segment_ratio=0.85,
            minimum_common_segment_ratio=0.4,
            comparable=not frozen,
        ),
        geometry_confidence=0 if frozen else 0.94,
        bow_shape_hint="uncertain" if frozen else "positive_c_bow",
        reason_codes=["curvature_freeze_gate_reached"] if frozen else [],
        metadata=(
            {"measurement_geometry_mode": "CANONICAL_VIEW_CORRIDOR_V1"}
            if canonical_view_corridor
            else {}
        ),
    )


def _phase(
    sequence: int,
    phase_deg: float,
    *,
    track_id: str = "track-a",
    calibrated: bool = True,
    trace_generation: int = 0,
    relock_status: str = "NOT_REQUIRED",
) -> StemSurfacePhasePayload:
    return StemSurfacePhasePayload(
        raw_phase_valid=True,
        calibrated_phase_valid=calibrated,
        phase_locked=True,
        track_id=track_id,
        source_object_id=f"obj-{track_id}",
        image_context_id="ctx-process",
        frame_sequence=sequence,
        timestamp=f"2026-08-04T12:00:{sequence:02d}Z",
        delta_raw_phase=0.01,
        cumulative_raw_phase=phase_deg / 360,
        last_safe_raw_phase=phase_deg / 360,
        delta_phase_deg=3.6 if calibrated else None,
        cumulative_phase_deg=phase_deg if calibrated else None,
        direction="positive",
        tracking_mode="INCREMENTAL",
        relock_status=relock_status,
        inlier_ratio=0.88,
        confidence=0.93,
        trace_generation=trace_generation,
        calibration_id="phase-test-v1" if calibrated else None,
        calibration_hash="phase-hash" if calibrated else None,
        process_calibration_hash="process-hash",
        rotation_reference_view="LOOKING_ALONG_POSITIVE_X",
        positive_rotation="CLOCKWISE",
    )


def _active_stem(
    track_id: str,
    *,
    start_reached: bool = True,
    freeze_reached: bool = False,
    decision_reached: bool = False,
    confidence: float = 0.95,
) -> SegmentPayload:
    return SegmentPayload(
        segments=[
            SegmentationPrediction(
                label="stem",
                score=confidence,
                polygon=[
                    SegmentationPoint(x=0.20, y=0.30),
                    SegmentationPoint(x=0.75, y=0.30),
                    SegmentationPoint(x=0.75, y=0.55),
                    SegmentationPoint(x=0.20, y=0.55),
                ],
            )
        ],
        image_context_id="ctx-process",
        metadata={
            "active_stem_tracking": {
                "status": "active",
                "track_id": track_id,
                "selection_confidence": confidence,
                "fail_closed": False,
                "session_start_gate_reached": start_reached,
                "curvature_freeze_gate_reached": freeze_reached,
                "decision_gate_reached": decision_reached,
                "remaining_distance_to_decision_gate_mm": (0.0 if decision_reached else 250.0),
                "decision_margin_mm": 100.0,
                "decision_margin_entered": decision_reached,
                "rotation_reference_view": "LOOKING_IN_CONVEYOR_DIRECTION",
                "positive_rotation": "CLOCKWISE",
            }
        },
    )


def _execute(
    handler: StemCurvaturePhaseSolverHandler,
    context: RuntimeExecutionContext,
    sequence: int,
    phase_deg: float,
    *,
    frozen: bool = False,
    events: EventsPayload | None = None,
    track_id: str = "track-a",
    calibrated: bool = True,
    trace_generation: int = 0,
    x_start_mm: float = 0,
    x_end_mm: float = 1000,
    include_active_stem: bool = False,
    start_reached: bool = True,
    decision_reached: bool = False,
    relock_status: str = "NOT_REQUIRED",
    straight: bool = False,
    canonical_view_corridor: bool = False,
) -> StemCurvaturePhaseSolutionPayload:
    inputs = {
        "curvature": _curvature(
            sequence,
            phase_deg,
            track_id=track_id,
            frozen=frozen,
            straight=straight,
            x_start_mm=x_start_mm,
            x_end_mm=x_end_mm,
            canonical_view_corridor=canonical_view_corridor,
        ),
        "surface_phase": _phase(
            sequence,
            phase_deg,
            track_id=track_id,
            calibrated=calibrated,
            trace_generation=trace_generation,
            relock_status=relock_status,
        ),
    }
    if include_active_stem:
        inputs["active_stem"] = _active_stem(
            track_id,
            start_reached=start_reached,
            freeze_reached=frozen,
            decision_reached=decision_reached,
        )
    if events is not None:
        inputs["events"] = events
    return handler.execute(context, _node(), inputs).outputs["solution"]


def test_registry_loader_stateful_executor_and_result_contract() -> None:
    definition = BUILTIN_NODE_TYPE_BY_KEY["analysis.stem_curvature_phase_solver"]

    assert [(port.key, port.data_type) for port in definition.input_ports] == [
        ("curvature", "stem_horizontal_curvature"),
        ("surface_phase", "stem_surface_phase"),
        ("active_stem", "segments"),
        ("events", "events"),
    ]
    assert [(port.key, port.data_type) for port in definition.output_ports] == [
        ("solution", "stem_curvature_phase_solution"),
        ("overlay", "overlay"),
    ]
    assert definition.compatibility_constraints["stateful"] is True
    assert definition.compatibility_constraints["correction_angle_output"] is True
    assert definition.compatibility_constraints["result_contract"] == "stem.orientation.v3"
    assert definition.default_config["minimum_principle_validation"] is None
    assert definition.default_config["shadow_mode_enabled"] is False
    assert definition.default_config["shadow_evaluation_approved"] is False
    objective_field = next(
        field
        for field in definition.property_schema
        if field.key == "minimum_principle_validation"
    )
    assert objective_field.field_type == "stem_minimum_principle_validation"
    assert objective_field.required is False
    live_source = Path(
        "backend/services/realtime-executor/src/realtime_executor/live.py"
    ).read_text(encoding="utf-8")
    assert '"analysis.stem_curvature_phase_solver"' in live_source
    assert (
        load_runtime_handler(
            definition.runtime_handler_ref, expected_node_type=definition.node_type
        ).node_type
        == definition.node_type
    )


def test_overlay_exposes_a_diagnostic_point_before_fit_is_available() -> None:
    handler = StemCurvaturePhaseSolverHandler()
    execution = handler.execute(
        _context(),
        _node(),
        {
            "curvature": _curvature(1, 10),
            "surface_phase": _phase(1, 10),
        },
    )

    solution = execution.outputs["solution"]
    overlay = execution.outputs["overlay"]
    assert solution.diagnostic_graph.fit_available is False
    assert len(solution.diagnostic_graph.points) == 1
    assert overlay.payload_type == "overlay"
    graph = overlay.primitives[-1].metadata["diagnostic_graph"]
    assert graph["schema_version"] == "stem.curvature-phase-diagnostic.v1"
    assert graph["points"][0]["curvature_rms_mm"] is not None


def test_sequence_solves_then_freezes_geometry_while_phase_keeps_updating() -> None:
    handler = StemCurvaturePhaseSolverHandler()
    context = _context()
    result = None
    for sequence, phase_deg in enumerate(range(0, 121, 10)):
        result = _execute(handler, context, sequence, phase_deg)

    assert result is not None
    assert result.state == "SOLVED"
    assert result.target_phase_deg is not None
    assert result.signed_fit_residual_rms_mm is not None
    assert result.energy_fit_residual_rms_mm2 is not None
    assert result.diagnostic_graph.fit_available
    assert result.diagnostic_graph.fit_curve
    assert len(result.diagnostic_graph.points) == result.observation_count
    assert all(
        point.status in {"weighted", "downweighted"}
        for point in result.diagnostic_graph.points
    )
    solver_state = context.state["curvature_phase_solver"]
    assert solver_state["solver_state"] == "SOLVED"
    assert solver_state["target_phase_deg"] == result.target_phase_deg
    assert solver_state["signed_fit"] is not None
    accepted_count = result.observation_count

    missing_phase = handler.execute(
        context,
        _node(),
        {"curvature": _curvature(19, 125)},
    ).outputs["solution"]
    assert missing_phase.state == "INVALID"
    assert missing_phase.target_phase_deg is None

    frozen = _execute(handler, context, 20, 130, frozen=True)
    after_freeze = _execute(handler, context, 21, 140)

    assert frozen.state == "FROZEN"
    assert frozen.curvature_frozen
    assert frozen.observation_count == accepted_count
    assert frozen.current_phase_deg == 130
    assert after_freeze.state == "FROZEN"
    assert after_freeze.observation_count == accepted_count
    assert after_freeze.current_phase_deg == 140
    assert after_freeze.metadata["post_freeze_geometry_observations"] == 0
    assert after_freeze.diagnostic_graph.points[-1].status == "ignored"
    assert "SOLVER_CURVATURE_ALREADY_FROZEN" in (
        after_freeze.diagnostic_graph.points[-1].reason_codes
    )


def test_decision_gate_is_latched_without_computing_a_correction_angle() -> None:
    handler = StemCurvaturePhaseSolverHandler()
    context = _context()
    for sequence, phase_deg in enumerate(range(0, 121, 10)):
        _execute(handler, context, sequence, phase_deg)
    _execute(handler, context, 20, 130, frozen=True)
    decision = _execute(
        handler,
        context,
        21,
        145,
        events=EventsPayload(
            events=[
                EventPayload(
                    event_type="stem_decision_gate_reached",
                    key="track-a",
                    payload={"session_id": "session-a"},
                )
            ]
        ),
    )

    assert decision.decision_gate_reached
    assert decision.current_phase_deg == 145
    assert decision.target_phase_deg is not None
    assert decision.metadata["correction_angle_computed"] is False
    assert "SOLVER_DECISION_GATE_REACHED" in decision.reason_code_history

    result_definition = BUILTIN_NODE_TYPE_BY_KEY["output.result"]
    result_node = RuntimeNodeSpec(
        node_key="result",
        node_type=result_definition.node_type,
        node_category=result_definition.node_category,
        node_contract_version=result_definition.contract_version,
        input_ports=result_definition.input_ports,
        output_ports=result_definition.output_ports,
        config={"include_runtime_metadata": False},
    )
    projected = (
        load_runtime_handler(
            result_definition.runtime_handler_ref,
            expected_node_type=result_definition.node_type,
        )
        .execute(context, result_node, {"result": decision})
        .outputs["result"]
    )

    assert isinstance(projected, ResultPayload)
    assert projected.decision is True
    assert projected.result["target_phase_deg"] == decision.target_phase_deg
    assert projected.result["stem_curvature_phase_solution"]["track_id"] == "track-a"


def test_uncalibrated_phase_and_track_switch_fail_closed_and_reset() -> None:
    handler = StemCurvaturePhaseSolverHandler()
    context = _context()
    uncalibrated = _execute(handler, context, 1, 10, calibrated=False)
    first = _execute(handler, context, 2, 20)
    switched = _execute(handler, context, 3, 30, track_id="track-b")

    assert uncalibrated.observation_count == 0
    assert uncalibrated.target_phase_deg is None
    assert "SOLVER_CALIBRATED_PHASE_REQUIRED" in uncalibrated.reason_codes
    overlay = _diagnostic_overlay(uncalibrated)
    labels = [primitive.label or "" for primitive in overlay.primitives]
    assert any("Messpunkte 0" in label and "kein neuer Punkt" in label for label in labels)
    assert any("Gates Start ja" in label and "Freeze offen" in label for label in labels)
    assert any("Mantelphase n/a" in label and "unkalibriert" in label for label in labels)
    assert any("SOLVER_CALIBRATED_PHASE_REQUIRED" in label for label in labels)
    assert first.observation_count == 1
    assert switched.track_id == "track-b"
    assert switched.observation_count == 1
    assert "SOLVER_TRACK_CHANGED_RESET" in switched.reason_codes


def test_phase_trace_change_resets_fit_and_low_shared_coverage_is_rejected() -> None:
    handler = StemCurvaturePhaseSolverHandler()
    context = _context()
    first = _execute(handler, context, 1, 0, x_start_mm=0, x_end_mm=1000)
    low_overlap = _execute(
        handler,
        context,
        2,
        10,
        x_start_mm=800,
        x_end_mm=1000,
    )
    trace_reset = _execute(
        handler,
        context,
        3,
        20,
        trace_generation=1,
        x_start_mm=0,
        x_end_mm=1000,
    )

    assert first.observation_count == 1
    assert low_overlap.observation_count == 1
    assert "SOLVER_COMMON_SEGMENT_OVERLAP_LOW" in low_overlap.reason_codes
    assert trace_reset.observation_count == 1
    assert "SOLVER_PHASE_TRACE_CHANGED_RESET" in trace_reset.reason_codes


def test_productive_session_freezes_v3_angle_once_at_decision_gate() -> None:
    handler = StemCurvaturePhaseSolverHandler()
    context = _context()
    for sequence, phase_deg in enumerate(range(0, 121, 10)):
        observing = _execute(
            handler,
            context,
            sequence,
            phase_deg,
            include_active_stem=True,
        )
        assert observing.session_state == "OBSERVING_CURVATURE"

    frozen = _execute(
        handler,
        context,
        20,
        130,
        frozen=True,
        include_active_stem=True,
    )
    assert frozen.session_state == "READY_TO_DECIDE"

    decided = _execute(
        handler,
        context,
        21,
        145,
        include_active_stem=True,
        decision_reached=True,
    )
    result = decided.orientation_result
    assert result is not None
    assert decided.session_state == "RESULT_READY"
    assert decided.result_frozen
    assert result.schema_version == "stem.orientation.v3"
    assert result.algorithm == "CURVATURE_PHASE_MINIMUM"
    assert result.decision == "ROTATE"
    assert result.direction == "COUNTER_CLOCKWISE"
    assert result.signed_correction_deg == result.recommended_rotation_deg_signed
    diagnostic_overlay = _diagnostic_overlay(
        decided,
        active_stem=_active_stem("track-a", decision_reached=True),
    )
    graph_primitive = next(
        primitive
        for primitive in diagnostic_overlay.primitives
        if primitive.metadata.get("diagnostic_graph") is not None
    )
    assert graph_primitive.metadata["decision"] == "ROTATE"
    assert graph_primitive.metadata["signed_correction_deg"] == pytest.approx(
        result.signed_correction_deg
    )
    assert graph_primitive.metadata["recommended_rotation_deg_signed"] == pytest.approx(
        result.recommended_rotation_deg_signed
    )
    subject_outline = next(
        primitive
        for primitive in diagnostic_overlay.primitives
        if primitive.layer == "stem_curvature_phase_subject"
    )
    assert subject_outline.primitive_type == "polygon"
    assert subject_outline.style["stroke"] == "#ff1744"
    assert subject_outline.style["stroke_width"] == 6.0
    assert subject_outline.metadata["track_id"] == "track-a"
    assert subject_outline.metadata["linked_to"] == "stem_curvature_phase_diagnostic"
    mismatched_overlay = _diagnostic_overlay(
        decided,
        active_stem=_active_stem("track-b", decision_reached=True),
    )
    assert not any(
        primitive.layer == "stem_curvature_phase_subject"
        for primitive in mismatched_overlay.primitives
    )
    assert result.recommended_rotation_deg_signed == pytest.approx(
        (result.target_phase_deg - result.current_phase_deg + 90) % 180 - 90
    )
    assert result.phase_distance_to_target_deg_signed == pytest.approx(
        result.recommended_rotation_deg_signed
    )
    assert result.curvature_min_normalized is not None
    assert result.curvature_max_normalized is not None
    assert result.common_segment_coverage == pytest.approx(0.85)
    assert result.used_frame_sequences == list(range(13))
    assert result.used_phase_points_deg == list(range(0, 121, 10))
    assert result.fit_parameters["signed_fit"]["stable"] is True
    assert result.asset_references == {"status": "not_recorded"}
    assert result.deadline_met
    assert result.decision_timestamp == "2026-08-04T12:00:21Z"
    assert result.result_expiry == "2026-08-04T12:00:26Z"
    assert result.decision_margin_mm == 100
    assert result.decision_margin_entered
    assert result.remaining_distance_to_decision_gate_mm == 0
    result_definition = BUILTIN_NODE_TYPE_BY_KEY["output.result"]
    projected = (
        load_runtime_handler(
            result_definition.runtime_handler_ref,
            expected_node_type=result_definition.node_type,
        )
        .execute(
            context,
            RuntimeNodeSpec(
                node_key="result_v3",
                node_type=result_definition.node_type,
                node_category=result_definition.node_category,
                node_contract_version=result_definition.contract_version,
                input_ports=result_definition.input_ports,
                output_ports=result_definition.output_ports,
                config={"include_runtime_metadata": False},
            ),
            {"result": decided},
        )
        .outputs["result"]
    )
    assert isinstance(projected, ResultPayload)
    assert projected.decision is True
    assert projected.result["schema_version"] == "stem.orientation.v3"
    assert projected.result["algorithm"] == "CURVATURE_PHASE_MINIMUM"

    after_gate = _execute(
        handler,
        context,
        22,
        160,
        include_active_stem=True,
        decision_reached=True,
    )
    assert after_gate.orientation_result == result
    assert after_gate.observation_count == decided.observation_count
    assert after_gate.current_phase_deg == 160
    assert "PHASE_TRACKING_CONTINUED_AFTER_DECISION" in after_gate.reason_codes


def test_canonical_view_exit_freezes_and_decides_without_a_second_manual_gate() -> None:
    handler = StemCurvaturePhaseSolverHandler()
    context = _context()
    for sequence, phase_deg in enumerate(range(0, 121, 10)):
        _execute(
            handler,
            context,
            sequence,
            phase_deg,
            include_active_stem=True,
            canonical_view_corridor=True,
        )

    exited = _execute(
        handler,
        context,
        20,
        130,
        frozen=True,
        include_active_stem=True,
        canonical_view_corridor=True,
    )

    assert exited.session_state == "RESULT_READY"
    assert exited.orientation_result is not None
    assert exited.orientation_result.deadline_met
    assert exited.orientation_result.remaining_distance_to_decision_gate_mm == 0
    assert "SESSION_CANONICAL_VIEW_EXIT_REACHED" in exited.reason_codes


def test_productive_session_emits_uncertain_for_insufficient_rotation() -> None:
    handler = StemCurvaturePhaseSolverHandler()
    context = _context()
    for sequence, phase_deg in enumerate((0, 10, 20)):
        _execute(
            handler,
            context,
            sequence,
            phase_deg,
            include_active_stem=True,
        )
    _execute(
        handler,
        context,
        10,
        25,
        frozen=True,
        include_active_stem=True,
    )
    decided = _execute(
        handler,
        context,
        11,
        25,
        include_active_stem=True,
        decision_reached=True,
    )

    assert decided.orientation_result is not None
    assert decided.orientation_result.decision == "UNCERTAIN"
    assert decided.orientation_result.recommended_rotation_deg_signed is None
    assert "DECISION_CURVATURE_MINIMUM_UNAVAILABLE" in (decided.orientation_result.reason_codes)


def test_productive_session_reports_no_rotation_for_irrelevant_bow() -> None:
    handler = StemCurvaturePhaseSolverHandler()
    context = _context()
    for sequence, phase_deg in enumerate(range(0, 121, 10)):
        _execute(
            handler,
            context,
            sequence,
            phase_deg,
            include_active_stem=True,
            straight=True,
        )
    _execute(
        handler,
        context,
        20,
        130,
        frozen=True,
        include_active_stem=True,
        straight=True,
    )
    decided = _execute(
        handler,
        context,
        21,
        130,
        include_active_stem=True,
        decision_reached=True,
        straight=True,
    )

    assert decided.orientation_result is not None
    assert decided.orientation_result.decision == "NO_ROTATION"
    assert decided.orientation_result.recommended_rotation_deg_signed == 0
    assert decided.orientation_result.direction == "NONE"


def test_relock_track_change_reset_and_two_consecutive_stems_do_not_leak_state() -> None:
    handler = StemCurvaturePhaseSolverHandler()
    context = _context()
    for sequence, phase_deg in enumerate(range(0, 121, 10)):
        _execute(
            handler,
            context,
            sequence,
            phase_deg,
            include_active_stem=True,
        )
    _execute(
        handler,
        context,
        20,
        130,
        frozen=True,
        include_active_stem=True,
    )
    first = _execute(
        handler,
        context,
        21,
        145,
        include_active_stem=True,
        decision_reached=True,
        relock_status="RELOCKED",
    )
    assert first.orientation_result is not None
    assert first.orientation_result.phase_relocked_after_impact

    second = _execute(
        handler,
        context,
        22,
        0,
        include_active_stem=True,
        track_id="track-b",
    )
    assert second.track_id == "track-b"
    assert second.orientation_result is None
    assert second.observation_count == 1
    assert second.session_state == "OBSERVING_CURVATURE"

    reset = handler.execute(
        context,
        _node(),
        {
            "events": EventsPayload(
                events=[EventPayload(event_type="pipeline_reset", key="track-b")]
            )
        },
    ).outputs["solution"]
    assert reset.session_state == "WAITING"
    assert reset.track_id is None
    assert reset.observation_count == 0


def test_track_switch_before_gate_closes_old_session_uncertain_then_starts_new() -> None:
    handler = StemCurvaturePhaseSolverHandler()
    context = _context()
    _execute(handler, context, 1, 0, include_active_stem=True)

    closed = _execute(
        handler,
        context,
        2,
        10,
        include_active_stem=True,
        track_id="track-b",
    )
    assert closed.track_id == "track-a"
    assert closed.orientation_result is not None
    assert closed.orientation_result.decision == "UNCERTAIN"
    assert closed.orientation_result.deadline_met is False
    assert "TRACK_CHANGED_BEFORE_DECISION" in closed.orientation_result.reason_codes

    started = _execute(
        handler,
        context,
        3,
        20,
        include_active_stem=True,
        track_id="track-b",
    )
    assert started.track_id == "track-b"
    assert started.orientation_result is None
    assert started.observation_count == 1


def test_session_end_transitions_to_done_with_fail_closed_result() -> None:
    handler = StemCurvaturePhaseSolverHandler()
    context = _context()
    _execute(handler, context, 1, 0, include_active_stem=True)

    done = handler.execute(
        context,
        _node(),
        {
            "events": EventsPayload(
                events=[EventPayload(event_type="stem_session_end", key="track-a")]
            )
        },
    ).outputs["solution"]

    assert done.session_state == "DONE"
    assert done.orientation_result is not None
    assert done.orientation_result.decision == "UNCERTAIN"
    assert done.result_frozen
