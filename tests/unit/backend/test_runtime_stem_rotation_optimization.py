from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cv_contracts.runtime import RuntimeNodeSpec
from cv_db.runtime_execution_store import _result_type, _stem_shadow_mode_from_outputs
from cv_runtime import BUILTIN_NODE_TYPE_BY_KEY
from cv_runtime.context import RuntimeExecutionContext
from cv_runtime.nodes.stem_rotation_optimizer import StemRotationOptimizerHandler
from cv_runtime.nodes.stem_rotation_priority import StemRotationPriorityAdapterHandler
from cv_runtime.payloads import (
    FramePayload,
    ResultPayload,
    StemProjectionObservationPayload,
    StemRotationOptimizationPayload,
    StemRotationPriorityPayload,
    StemSliceFitDiagnosticsPayload,
    StemSliceGeometryPayload,
    StemSliceModelPayload,
)
from cv_runtime.stem_rotation_priority import resolve_stem_rotation_priority
from cv_runtime.stem_rotation_validation import load_validation_document
from cv_status import RuntimeMode


def _node(node_type: str, **config: object) -> RuntimeNodeSpec:
    definition = BUILTIN_NODE_TYPE_BY_KEY[node_type]
    return RuntimeNodeSpec(
        node_key=node_type.rsplit(".", 1)[-1],
        node_type=node_type,
        node_category=definition.node_category,
        node_contract_version=definition.contract_version,
        input_ports=definition.input_ports,
        output_ports=definition.output_ports,
        config={**definition.default_config, **config},
    )


def _context(*, control: dict[str, object] | None = None) -> RuntimeExecutionContext:
    return RuntimeExecutionContext(
        correlation_id="corr-stem-rotation",
        pipeline_version_id="piv-stem-rotation",
        runtime_execution_id="rex-stem-rotation",
        channel_id="chn-stem",
        mode=RuntimeMode.REALTIME,
        metadata={"stem_rotation_priority_control": control} if control else {},
    )


def _priority(mode: str, *, revision: int = 1) -> StemRotationPriorityPayload:
    return resolve_stem_rotation_priority(
        {
            "schema_version": "runtime.stem-rotation-priority-control.v1",
            "priority_source": "FRONTEND",
            "priority_mode": mode,
            "optimization_enable": True,
            "revision": revision,
        }
    )


def _model(*, a_mm: float = 120, b_mm: float = 80) -> StemSliceModelPayload:
    slices = [
        StemSliceGeometryPayload(
            x_mm=float(index * 50),
            cy_mm=0,
            cz_mm=0,
            a_mm=a_mm,
            b_mm=b_mm,
            phi_rad=0,
            quality=0.95,
            valid=True,
        )
        for index in range(3)
    ]
    return StemSliceModelPayload(
        state="FROZEN",
        valid=True,
        frozen=True,
        session_id="stem-cycle-17",
        track_id="track-17",
        current_pose_phase_deg=0,
        rotation_reference_view="looking_along_positive_x",
        positive_rotation="counter_clockwise_in_y_z_view",
        model_hash=f"model-{a_mm}-{b_mm}",
        model_quality=0.9,
        requested_station_count=3,
        valid_raw_slice_count=3,
        valid_processed_slice_count=3,
        valid_raw_slice_ratio=1,
        raw_slices=slices,
        processed_slices=slices,
    )


def _model_with_invalid_ends() -> StemSliceModelPayload:
    slices: list[StemSliceGeometryPayload] = []
    diagnostics: list[StemSliceFitDiagnosticsPayload] = []
    for index in range(7):
        valid = 2 <= index <= 4
        x_mm = float(index * 50)
        slices.append(
            StemSliceGeometryPayload(
                x_mm=x_mm,
                cy_mm=0 if valid else None,
                cz_mm=0 if valid else None,
                a_mm=120 if valid else None,
                b_mm=80 if valid else None,
                phi_rad=0 if valid else None,
                quality=0.95 if valid else 0,
                valid=valid,
                reason_codes=[] if valid else ["FIT_SUPPORT_LOW"],
            )
        )
        diagnostics.append(
            StemSliceFitDiagnosticsPayload(
                x_mm=x_mm,
                valid=valid,
                observation_count=7 if valid else 2,
                accepted_observation_count=7 if valid else 2,
                unique_phase_count=7 if valid else 2,
                phase_span_deg=120 if valid else 20,
                phase_coverage_ratio=2 / 3 if valid else 1 / 9,
                reason_codes=[] if valid else ["FIT_SUPPORT_LOW"],
            )
        )
    valid_slices = [item for item in slices if item.valid]
    return StemSliceModelPayload(
        state="FROZEN",
        valid=True,
        frozen=True,
        session_id="stem-cycle-with-invalid-ends",
        track_id="track-invalid-ends",
        current_pose_phase_deg=17.5,
        rotation_reference_view="looking_along_positive_x",
        positive_rotation="counter_clockwise_in_y_z_view",
        model_hash="model-with-invalid-ends",
        model_quality=0.9,
        requested_station_count=7,
        valid_raw_slice_count=3,
        valid_processed_slice_count=3,
        valid_raw_slice_ratio=3 / 7,
        raw_slices=valid_slices,
        processed_slices=slices,
        fit_diagnostics=diagnostics,
    )


def _optimize(
    model: StemSliceModelPayload,
    priority: StemRotationPriorityPayload,
    **config: object,
) -> StemRotationOptimizationPayload:
    output = StemRotationOptimizerHandler().execute(
        _context(),
        _node(
            "analysis.stem_rotation_optimizer",
            min_gain_mm=0.1,
            min_rotation_deg=0.1,
            fine_angle_step_deg=0.25,
            **config,
        ),
        {"slice_model": model, "priority": priority},
    ).outputs["result"]
    assert isinstance(output, ResultPayload)
    return StemRotationOptimizationPayload.model_validate(output.result)


def test_default_priority_source_is_frontend_and_none_is_fail_closed() -> None:
    priority = resolve_stem_rotation_priority(None)

    assert priority.priority_source == "FRONTEND"
    assert priority.priority_mode == "NONE"
    assert priority.valid is False
    assert priority.reason_code == "INVALID_PRIORITY_MODE"


def test_frontend_priority_adapter_maps_modes_to_canonical_axes() -> None:
    handler = StemRotationPriorityAdapterHandler()
    node = _node("analysis.stem_rotation_priority_adapter")
    frame = FramePayload(image_base64="")

    vertical = handler.execute(
        _context(
            control={
                "priority_source": "FRONTEND",
                "priority_mode": "VORSCHNITT_KRITISCH",
                "optimization_enable": True,
                "revision": 2,
            }
        ),
        node,
        {"frame": frame},
    ).outputs["priority"]
    horizontal = handler.execute(
        _context(
            control={
                "priority_source": "FRONTEND",
                "priority_mode": "NACHSCHNITT_KRITISCH",
                "optimization_enable": True,
                "revision": 3,
            }
        ),
        node,
        {"frame": frame},
    ).outputs["priority"]

    assert isinstance(vertical, StemRotationPriorityPayload)
    assert vertical.priority_axis == "VERTICAL_Z"
    assert vertical.valid is True
    assert isinstance(horizontal, StemRotationPriorityPayload)
    assert horizontal.priority_axis == "HORIZONTAL_Y"
    assert horizontal.valid is True


def test_plc_source_is_unambiguous_and_frontend_mode_cannot_override_it() -> None:
    priority = resolve_stem_rotation_priority(
        {
            "priority_source": "PLC",
            "priority_mode": "VORSCHNITT_KRITISCH",
            "optimization_enable": True,
            "revision": 7,
        }
    )

    assert priority.priority_source == "PLC"
    assert priority.priority_mode == "NONE"
    assert priority.valid is False
    assert priority.reason_code == "PLC_PRIORITY_PROVIDER_NOT_CONNECTED"


def test_mode_switch_reoptimizes_the_same_frozen_model_without_measurement_state() -> None:
    model = _model()

    vertical = _optimize(model, _priority("VORSCHNITT_KRITISCH", revision=10))
    horizontal = _optimize(model, _priority("NACHSCHNITT_KRITISCH", revision=11))

    assert vertical.result_ready is True
    assert vertical.result_valid is True
    assert vertical.should_rotate is True
    assert 80 <= abs(vertical.rotation_delta_deg or 0) <= 90
    assert vertical.gain_mm is not None and vertical.gain_mm > 79
    assert horizontal.result_ready is True
    assert horizontal.result_valid is True
    assert horizontal.should_rotate is False
    assert horizontal.rotation_delta_deg == 0
    assert vertical.model_hash == horizontal.model_hash == model.model_hash
    assert vertical.priority_revision == 10
    assert horizontal.priority_revision == 11


def test_result_is_replayable_and_carries_source_mode_scores_and_limiting_x() -> None:
    model = _model().model_copy(
        update={
            "current_pose_phase_deg": 37.5,
            "trace_generation": 4,
            "process_calibration_hash": "process-calibration-hash",
            "phase_calibration_id": "phase-calibration-id",
            "phase_calibration_hash": "phase-calibration-hash",
            "raw_observations": [
                StemProjectionObservationPayload(
                    x_mm=0.0,
                    phase_deg=37.5,
                    projected_center_mm=2.5,
                    visible_width_mm=210.0,
                    quality=0.92,
                    frame_sequence=17,
                )
            ],
        }
    )
    result = _optimize(model, _priority("VORSCHNITT_KRITISCH", revision=4))

    assert result.schema_version == "stem.rotation-optimization.v1"
    assert result.priority_source == "FRONTEND"
    assert result.priority_mode == "VORSCHNITT_KRITISCH"
    assert result.score_current_mm is not None
    assert result.score_optimal_mm is not None
    assert result.limiting_x_current_mm == 0
    assert result.limiting_x_optimal_mm == 0
    assert result.replay.model_hash == result.model_hash
    assert len(result.replay.raw_slices) == 3
    assert len(result.replay.raw_observations) == 1
    assert len(result.replay.processed_slices) == 3
    assert result.replay.current_pose_phase_deg == 37.5
    assert result.replay.rotation_reference_view == "looking_along_positive_x"
    assert result.replay.positive_rotation == "counter_clockwise_in_y_z_view"
    assert result.replay.trace_generation == 4
    assert result.replay.process_calibration_hash == "process-calibration-hash"
    assert result.replay.phase_calibration_id == "phase-calibration-id"
    assert result.replay.phase_calibration_hash == "phase-calibration-hash"
    assert result.replay.priority_control["priority_source"] == "FRONTEND"
    assert result.score_curve
    assert result.shadow_mode is True
    assert result.plc_writes_suppressed is True

    replay_source = load_validation_document(result.model_dump(mode="json"))
    assert replay_source.model_payload.current_pose_phase_deg == 37.5
    assert replay_source.model_payload.raw_observations == model.raw_observations
    assert replay_source.model_payload.raw_slices == model.raw_slices
    assert replay_source.model_payload.phase_calibration_hash == "phase-calibration-hash"


def test_result_contains_backend_prepared_read_only_hmi_geometry() -> None:
    result = _optimize(_model(), _priority("VORSCHNITT_KRITISCH", revision=6))

    view = result.view_model
    assert view is not None
    assert view.schema_version == "stem.rotation-hmi.v1"
    assert view.timestamp == result.timestamp
    assert view.priority_axis == "VERTICAL_Z"
    assert view.priority_revision == 6
    assert view.optimization_enable is True
    assert view.rotation_direction in {"POSITIVE", "NEGATIVE"}
    assert view.comparison_angle_deg == result.optimal_candidate_angle_deg
    assert len(view.stem_longitudinal_geometry) == 3
    assert len(view.delta_geometry_by_x) == 3
    assert all(point.delta_geometry_mm is not None for point in view.delta_geometry_by_x)
    assert view.delta_geometry_by_x[0].delta_geometry_mm == result.gain_mm
    assert view.delta_geometry_by_x[0].limiting_current is True
    assert view.delta_geometry_by_x[0].limiting_optimal is True

    cross_section = view.current_critical_cross_section
    assert cross_section is not None
    assert len(cross_section.current_ellipse) == 64
    assert len(cross_section.optimal_ellipse) == 64
    assert cross_section.current_symmetric_chord.span_mm == result.score_current_mm
    assert cross_section.optimal_symmetric_chord is not None
    assert (
        cross_section.optimal_symmetric_chord.span_mm
        == result.score_optimal_mm
    )
    assert view.diagnostics.score_current_mm == result.score_current_mm
    assert view.diagnostics.score_optimal_mm == result.score_optimal_mm
    assert any(point.current for point in view.diagnostics.angle_score_curve)
    assert any(point.selected for point in view.diagnostics.angle_score_curve)


def test_persisted_result_can_be_replayed_by_the_offline_tool(tmp_path: Path) -> None:
    result = _optimize(_model(), _priority("VORSCHNITT_KRITISCH", revision=8))
    input_path = tmp_path / "stem-rotation-result.json"
    input_path.write_text(result.model_dump_json(), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/dev/replay_stem_rotation_optimization.py",
            str(input_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    replayed = json.loads(completed.stdout)

    assert replayed["matches_persisted_decision"] is True
    assert replayed["priority_source"] == "FRONTEND"
    assert replayed["priority_mode"] == "VORSCHNITT_KRITISCH"
    assert replayed["priority_revision"] == 8


def test_low_gain_and_invalid_new_model_never_expose_a_rotation() -> None:
    circle = _optimize(_model(a_mm=100, b_mm=100), _priority("VORSCHNITT_KRITISCH"))
    invalid_model = StemSliceModelPayload(state="INVALID", reason_codes=["FIT_SUPPORT_LOW"])
    invalid = _optimize(invalid_model, _priority("VORSCHNITT_KRITISCH"))

    assert circle.result_valid is True
    assert circle.should_rotate is False
    assert circle.rotation_delta_deg == 0
    assert circle.reason_code == "LOW_GAIN"
    assert invalid.state == "INVALID"
    assert invalid.result_ready is True
    assert invalid.result_valid is False
    assert invalid.should_rotate is False
    assert invalid.rotation_delta_deg is None


def test_mechanical_limits_bound_the_runtime_recommendation() -> None:
    result = _optimize(
        _model(),
        _priority("VORSCHNITT_KRITISCH"),
        rotation_negative_limit_deg=-25,
        rotation_positive_limit_deg=20,
    )

    assert result.result_valid is True
    assert result.rotation_delta_deg is not None
    assert -25 <= result.rotation_delta_deg <= 20


def test_conflicting_mechanical_limits_fail_closed_without_a_rotation() -> None:
    result = _optimize(
        _model(),
        _priority("VORSCHNITT_KRITISCH"),
        rotation_negative_limit_deg=5,
        rotation_positive_limit_deg=20,
    )

    assert result.state == "INVALID"
    assert result.result_valid is False
    assert result.should_rotate is False
    assert result.rotation_delta_deg is None
    assert result.reason_code == "ROTATION_CONSTRAINT_CONFLICT"


def test_valid_slice_ratio_is_checked_inside_the_configured_evaluation_range() -> None:
    model = _model_with_invalid_ends()

    valid_center = _optimize(
        model,
        _priority("VORSCHNITT_KRITISCH"),
        x_eval_start_mm=100,
        x_eval_end_mm=200,
    )
    invalid_whole_stem = _optimize(
        model,
        _priority("VORSCHNITT_KRITISCH"),
        x_eval_start_mm=0,
        x_eval_end_mm=300,
    )

    assert valid_center.result_valid is True
    assert valid_center.valid_slice_count == 3
    assert valid_center.evaluated_slice_count == 3
    assert invalid_whole_stem.result_valid is False
    assert invalid_whole_stem.reason_code == "VALID_SLICE_RATIO_LOW"
    assert invalid_whole_stem.valid_slice_count == 3
    assert invalid_whole_stem.evaluated_slice_count == 7


def test_optimizer_nodes_are_registered_and_only_measurement_is_stateful() -> None:
    priority = BUILTIN_NODE_TYPE_BY_KEY["analysis.stem_rotation_priority_adapter"]
    optimizer = BUILTIN_NODE_TYPE_BY_KEY["analysis.stem_rotation_optimizer"]

    assert priority.output_ports[0].data_type == "stem_rotation_priority"
    assert optimizer.output_ports[0].data_type == "result"
    assert optimizer.compatibility_constraints["stateful"] is False
    assert optimizer.compatibility_constraints["plc_writes_allowed"] is False


def test_ready_rotation_result_gets_a_dedicated_history_type() -> None:
    history_outputs = [
        {
            "payload": {
                "result": {
                    "schema_version": "stem.rotation-optimization.v1",
                    "result_ready": True,
                    "shadow_mode": True,
                    "priority_source": "FRONTEND",
                    "priority_mode": "VORSCHNITT_KRITISCH",
                }
            }
        }
    ]
    result_type = _result_type(
        mode=RuntimeMode.REALTIME,
        status="completed",
        trigger_endpoint_id=None,
        history_outputs=history_outputs,
    )

    assert result_type == "stem_rotation_optimization"
    assert _stem_shadow_mode_from_outputs(history_outputs) is True
