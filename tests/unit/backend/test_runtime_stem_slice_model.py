from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from cv_contracts import SegmentationPoint, SegmentationPrediction
from cv_contracts.runtime import RuntimeNodeSpec
from cv_runtime import BUILTIN_NODE_TYPE_BY_KEY
from cv_runtime.context import RuntimeExecutionContext
from cv_runtime.loader import load_runtime_handler
from cv_runtime.nodes.stem_slice_model import StemSliceModelHandler
from cv_runtime.payloads import (
    SegmentPayload,
    StemHorizontalCommonSegmentDescriptor,
    StemHorizontalCurvaturePayload,
    StemHorizontalCurvatureProfilePoint,
    StemHorizontalWidthPoint,
    StemRotationTranslationComponent,
    StemSliceModelPayload,
    StemSurfacePhasePayload,
)
from cv_runtime.stem_slice_measurement import replay_projection_observation_document
from cv_status import RuntimeMode


def _node(**config: object) -> RuntimeNodeSpec:
    definition = BUILTIN_NODE_TYPE_BY_KEY["analysis.stem_slice_model"]
    return RuntimeNodeSpec(
        node_key="slice_model",
        node_type=definition.node_type,
        node_category=definition.node_category,
        node_contract_version=definition.contract_version,
        input_ports=definition.input_ports,
        output_ports=definition.output_ports,
        config={
            **definition.default_config,
            "minimum_observations_per_slice": 7,
            "minimum_unique_phases": 6,
            "minimum_phase_span_deg": 90,
            "minimum_valid_slice_count": 3,
            "smoothing_window_slices": 1,
            **config,
        },
    )


def _context() -> RuntimeExecutionContext:
    return RuntimeExecutionContext(
        correlation_id="corr-slice-model",
        pipeline_version_id="piv-slice-model",
        runtime_execution_id="rex-slice-model",
        channel_id="chn-stem",
        mode=RuntimeMode.REALTIME,
        state={},
    )


def _active_stem(
    *,
    decision: bool = False,
    freeze: bool = False,
    track_id: str = "track-a",
    motion_axis: str = "stationary",
    delta_x_mm: float = 0,
    delta_y_mm: float = 0,
    process_zone: str = "transfer",
    process_stage: int = 0,
    longitudinal_motion_streak: int = 0,
    longitudinal_transition_pending: bool | None = None,
) -> SegmentPayload:
    movement: dict[str, object] = {
        "process_motion_axis": motion_axis,
        "delta_x_mm": delta_x_mm,
        "delta_y_mm": delta_y_mm,
        "process_zone": process_zone,
        "longitudinal_motion_streak": longitudinal_motion_streak,
    }
    if longitudinal_transition_pending is not None:
        movement["longitudinal_transition_pending"] = longitudinal_transition_pending
    return SegmentPayload(
        segments=[
            SegmentationPrediction(
                label="stem",
                score=0.96,
                polygon=[
                    SegmentationPoint(x=0.1, y=0.2),
                    SegmentationPoint(x=0.8, y=0.2),
                    SegmentationPoint(x=0.8, y=0.7),
                    SegmentationPoint(x=0.1, y=0.7),
                ],
            )
        ],
        metadata={
            "active_stem_tracking": {
                "status": "active",
                "track_id": track_id,
                "selection_confidence": 0.95,
                "fail_closed": False,
                "session_start_gate_reached": True,
                "curvature_freeze_gate_reached": freeze,
                "decision_gate_reached": decision,
                "process_stage": process_stage,
                "process_zone": process_zone,
                "movement": movement,
            }
        },
    )


def _projected(
    phase_deg: float,
    *,
    cy_mm: float,
    cz_mm: float,
    a_mm: float,
    b_mm: float,
    phi_deg: float,
) -> tuple[float, float]:
    theta = math.radians(phase_deg)
    phi = math.radians(phi_deg)
    cosine_phi = math.cos(phi)
    sine_phi = math.sin(phi)
    s_yy = a_mm**2 * cosine_phi**2 + b_mm**2 * sine_phi**2
    s_yz = (a_mm**2 - b_mm**2) * cosine_phi * sine_phi
    s_zz = a_mm**2 * sine_phi**2 + b_mm**2 * cosine_phi**2
    vy = math.cos(theta)
    vz = -math.sin(theta)
    center = cy_mm * vy + cz_mm * vz
    width = 2 * math.sqrt(vy * (s_yy * vy + s_yz * vz) + vz * (s_yz * vy + s_zz * vz))
    return center, width


def _curvature(
    sequence: int,
    phase_deg: float,
    *,
    valid: bool = True,
    track_id: str = "track-a",
    process_hash: str = "process-hash",
    material_x_shift_mm: float = 0.0,
) -> StemHorizontalCurvaturePayload:
    centers: list[StemHorizontalCurvatureProfilePoint] = []
    widths: list[StemHorizontalWidthPoint] = []
    geometries = (
        (0.0, 10.0, -5.0, 120.0, 80.0, 20.0),
        (50.0, 8.0, 3.0, 110.0, 75.0, -15.0),
        (100.0, -4.0, 7.0, 100.0, 70.0, 35.0),
    )
    for index, (material_x_mm, cy_mm, cz_mm, a_mm, b_mm, phi_deg) in enumerate(
        geometries
    ):
        x_mm = material_x_mm + material_x_shift_mm
        center, width = _projected(
            phase_deg,
            cy_mm=cy_mm,
            cz_mm=cz_mm,
            a_mm=a_mm,
            b_mm=b_mm,
            phi_deg=phi_deg,
        )
        centers.append(
            StemHorizontalCurvatureProfilePoint(
                x_mm=x_mm,
                x_normalized=index / 2,
                y_center_mm=center,
                y_reference_mm=0,
                residual_mm=center,
                residual_normalized_length=center / 100,
                weight=0.97,
            )
        )
        widths.append(
            StemHorizontalWidthPoint(
                x_mm=x_mm,
                x_normalized=index / 2,
                width_mm=width,
                width_normalized=width / 100,
                valid=True,
            )
        )
    return StemHorizontalCurvaturePayload(
        valid=valid,
        observation_state="accepted" if valid else "curvature_frozen",
        track_id=track_id,
        source_object_id=f"obj-{track_id}",
        image_context_id="ctx-process",
        frame_sequence=sequence,
        timestamp=f"2026-08-20T12:00:{sequence:02d}Z",
        calibration_id="process-calibration-v1",
        process_calibration_hash=process_hash,
        centerline_profile=centers if valid else [],
        width_profile=widths if valid else [],
        common_segment_descriptor=StemHorizontalCommonSegmentDescriptor(
            x_start_mm=material_x_shift_mm,
            x_end_mm=100 + material_x_shift_mm,
            length_mm=100,
            visible_x_start_mm=material_x_shift_mm,
            visible_x_end_mm=100 + material_x_shift_mm,
            visible_length_mm=100,
            common_segment_ratio=1,
            minimum_common_segment_ratio=0.4,
            comparable=valid,
        ),
        valid_station_ratio=1 if valid else 0,
        geometry_confidence=0.95 if valid else 0,
        selected_component_fraction=0.96,
        metadata={
            "segmentation_score": 0.96,
            "measurement_geometry_mode": "PERSPECTIVE_PRESERVED_MODEL_VIEW_V1",
            "geometry_observation_performed": valid,
            "coordinate_unit": "mm",
        },
    )


def _phase(
    sequence: int,
    phase_deg: float,
    *,
    track_id: str = "track-a",
    trace_generation: int = 0,
    process_hash: str = "process-hash",
    material_x_shift_mm: float = 0.0,
) -> StemSurfacePhasePayload:
    return StemSurfacePhasePayload(
        raw_phase_valid=True,
        calibrated_phase_valid=True,
        phase_locked=True,
        track_id=track_id,
        source_object_id=f"obj-{track_id}",
        image_context_id="ctx-process",
        frame_sequence=sequence,
        timestamp=f"2026-08-20T12:00:{sequence:02d}Z",
        cumulative_raw_phase=phase_deg / 360,
        last_safe_raw_phase=phase_deg / 360,
        cumulative_phase_deg=phase_deg,
        tracking_mode="INCREMENTAL",
        relock_status="NOT_REQUIRED",
        confidence=0.94,
        trace_generation=trace_generation,
        calibration_id="phase-calibration-v1",
        calibration_hash="phase-hash",
        process_calibration_hash=process_hash,
        rotation_reference_view="LOOKING_ALONG_POSITIVE_X",
        positive_rotation="CLOCKWISE",
        translation_component=StemRotationTranslationComponent(
            cumulative_x_mm=material_x_shift_mm,
            cumulative_y_mm=0.0,
            cumulative_magnitude_mm=abs(material_x_shift_mm),
        ),
    )


def _execute(
    handler: StemSliceModelHandler,
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    sequence: int,
    phase_deg: float,
    *,
    decision: bool = False,
    freeze: bool = False,
    track_id: str = "track-a",
    motion_axis: str = "stationary",
    delta_x_mm: float = 0,
    material_x_shift_mm: float = 0,
    process_zone: str = "transfer",
    process_stage: int = 0,
    longitudinal_motion_streak: int = 0,
    longitudinal_transition_pending: bool | None = None,
) -> StemSliceModelPayload:
    return handler.execute(
        context,
        node,
        {
            "curvature": _curvature(
                sequence,
                phase_deg,
                valid=not freeze,
                track_id=track_id,
                material_x_shift_mm=material_x_shift_mm,
            ),
            "surface_phase": _phase(
                sequence,
                phase_deg,
                track_id=track_id,
                material_x_shift_mm=material_x_shift_mm,
            ),
            "active_stem": _active_stem(
                decision=decision,
                freeze=freeze,
                track_id=track_id,
                motion_axis=motion_axis,
                delta_x_mm=delta_x_mm,
                process_zone=process_zone,
                process_stage=process_stage,
                longitudinal_motion_streak=longitudinal_motion_streak,
                longitudinal_transition_pending=longitudinal_transition_pending,
            ),
        },
    ).outputs["slice_model"]


def test_registry_loader_and_stateful_executor_contract() -> None:
    definition = BUILTIN_NODE_TYPE_BY_KEY["analysis.stem_slice_model"]

    assert [(port.key, port.data_type) for port in definition.input_ports] == [
        ("curvature", "stem_horizontal_curvature"),
        ("surface_phase", "stem_surface_phase"),
        ("active_stem", "segments"),
        ("events", "events"),
    ]
    assert [(port.key, port.data_type) for port in definition.output_ports] == [
        ("slice_model", "stem_slice_model")
    ]
    assert definition.compatibility_constraints["stateful"] is True
    assert definition.compatibility_constraints["material_registration_mode"] == (
        "TRACKED_SIGNED_MATERIAL_X_V1"
    )
    assert definition.compatibility_constraints["measurement_phase"] == (
        "NATURAL_ROLL_TRANSFER_ONLY"
    )
    assert load_runtime_handler(
        definition.runtime_handler_ref,
        expected_node_type=definition.node_type,
    )

    live_source = Path(
        "backend/services/realtime-executor/src/realtime_executor/live.py"
    ).read_text(encoding="utf-8")
    assert '"analysis.stem_slice_model"' in live_source


def test_runtime_collects_metric_profiles_and_freezes_current_pose_model() -> None:
    handler = StemSliceModelHandler()
    context = _context()
    node = _node()
    payload = None
    # Normal production rolling is not commanded at fixed angle increments.
    for sequence, phase_deg in enumerate((0, 19, 51, 88, 117, 151, 180)):
        payload = _execute(handler, context, node, sequence, phase_deg)
    assert payload is not None and payload.valid is True
    assert payload.coordinate_unit == "mm"
    assert payload.process_calibration_hash == "process-hash"
    assert payload.observation_frame_count == 7

    frozen = _execute(
        handler,
        context,
        node,
        sequence=7,
        phase_deg=180,
        decision=True,
        freeze=True,
    )

    assert frozen.valid is True
    assert frozen.frozen is True
    assert frozen.state == "FROZEN"
    assert frozen.model_hash
    assert frozen.current_pose_phase_deg == 180
    assert len(frozen.processed_slices) == 3
    first = frozen.processed_slices[0]
    assert first.cy_mm == pytest.approx(-10, abs=1.0e-6)
    assert first.cz_mm == pytest.approx(5, abs=1.0e-6)
    assert first.a_mm == pytest.approx(120, abs=1.0e-6)
    assert first.b_mm == pytest.approx(80, abs=1.0e-6)
    assert first.phi_rad is not None
    assert abs(math.degrees(first.phi_rad) - 20) == pytest.approx(0, abs=1.0e-6)


def test_transfer_drift_is_registered_and_longitudinal_transport_freezes_model() -> None:
    handler = StemSliceModelHandler()
    context = _context()
    node = _node()
    shifts = (0.0, 20.0, 45.0, 75.0, 40.0, 5.0, -20.0)
    for sequence, (phase_deg, shift_mm) in enumerate(
        zip(range(0, 181, 30), shifts, strict=True)
    ):
        payload = _execute(
            handler,
            context,
            node,
            sequence=sequence,
            phase_deg=phase_deg,
            material_x_shift_mm=shift_mm,
            motion_axis="transfer_y",
        )

    assert payload.valid is True
    assert payload.observation_frame_count == 7
    assert payload.metadata["current_material_x_shift_mm"] == pytest.approx(-20)
    assert payload.metadata["minimum_material_x_shift_mm"] == pytest.approx(-20)
    assert payload.metadata["maximum_material_x_shift_mm"] == pytest.approx(75)

    frozen = _execute(
        handler,
        context,
        node,
        sequence=7,
        phase_deg=180,
        material_x_shift_mm=-20,
        motion_axis="longitudinal_x",
        process_zone="longitudinal_conveyor",
        process_stage=1,
        decision=False,
    )

    assert frozen.valid is True
    assert frozen.frozen is True
    assert frozen.observation_frame_count == 7
    assert frozen.metadata["freeze_reason"] == "LONGITUDINAL_TRANSPORT_STARTED"
    assert "LONGITUDINAL_TRANSPORT_STARTED" in frozen.reason_codes


def test_longitudinal_transition_confirmation_frames_are_not_collected() -> None:
    handler = StemSliceModelHandler()
    context = _context()
    payload = _execute(
        handler,
        context,
        _node(),
        sequence=1,
        phase_deg=20,
        material_x_shift_mm=10,
        motion_axis="longitudinal_x",
        longitudinal_motion_streak=1,
    )

    assert payload.observation_frame_count == 0
    assert "LONGITUDINAL_TRANSITION_PENDING" in payload.reason_codes


def test_unconfirmed_longitudinal_sample_is_still_collected() -> None:
    handler = StemSliceModelHandler()
    context = _context()
    payload = _execute(
        handler,
        context,
        _node(),
        sequence=1,
        phase_deg=20,
        material_x_shift_mm=10,
        motion_axis="longitudinal_x",
        longitudinal_motion_streak=1,
        longitudinal_transition_pending=False,
    )

    assert payload.observation_frame_count == 1
    assert "LONGITUDINAL_TRANSITION_PENDING" not in payload.reason_codes


def test_missing_metric_material_registration_fails_closed() -> None:
    handler = StemSliceModelHandler()
    context = _context()
    phase = _phase(1, 20).model_copy(
        update={"translation_component": StemRotationTranslationComponent()}
    )

    payload = handler.execute(
        context,
        _node(),
        {
            "curvature": _curvature(1, 20),
            "surface_phase": phase,
            "active_stem": _active_stem(),
        },
    ).outputs["slice_model"]

    assert payload.observation_frame_count == 0
    assert "MATERIAL_X_REGISTRATION_UNAVAILABLE" in payload.reason_codes


def test_implausible_material_x_excursion_is_rejected_without_corrupting_model() -> None:
    handler = StemSliceModelHandler()
    context = _context()
    node = _node(maximum_material_x_step_mm=250, maximum_material_x_excursion_mm=150)
    accepted = _execute(handler, context, node, sequence=1, phase_deg=0)
    rejected = _execute(
        handler,
        context,
        node,
        sequence=2,
        phase_deg=30,
        material_x_shift_mm=175,
    )

    assert accepted.observation_frame_count == 1
    assert rejected.observation_frame_count == 1
    assert rejected.rejected_observation_frame_count == 1
    assert "MATERIAL_X_EXCURSION_TOO_LARGE" in rejected.reason_codes


def test_track_change_resets_observations_and_never_reuses_old_model() -> None:
    handler = StemSliceModelHandler()
    context = _context()
    node = _node()
    for sequence, phase_deg in enumerate((0, 30, 60)):
        _execute(handler, context, node, sequence, phase_deg)

    changed = _execute(
        handler,
        context,
        node,
        sequence=4,
        phase_deg=90,
        track_id="track-b",
    )

    assert changed.track_id == "track-b"
    assert changed.observation_frame_count == 1
    assert changed.valid is False
    assert "INPUT_BINDING_CHANGED_RESET" in changed.reason_codes


def test_raw_diagnostics_are_optional_and_bounded_to_explicit_config() -> None:
    handler = StemSliceModelHandler()
    context = _context()
    payload = _execute(handler, context, _node(include_raw_observations=True), 0, 0)

    assert len(payload.raw_observations) == 3
    assert payload.raw_observations[0].visible_width_mm > 0


def test_checked_in_replay_fixture_produces_a_valid_three_slice_model() -> None:
    document = json.loads(
        Path("tests/fixtures/synthetic/stem-projection-observations.v1.json").read_text(
            encoding="utf-8"
        )
    )

    replay = replay_projection_observation_document(document)

    assert replay.status.value == "VALID"
    assert replay.valid_processed_slice_count == 3
    assert replay.raw_model.slices[0].a_mm == pytest.approx(100, abs=1.0e-4)
    assert replay.processed_model.slices[0].a_mm > replay.processed_model.slices[-1].a_mm
