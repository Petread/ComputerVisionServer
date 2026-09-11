from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
from cv_contracts import (
    InferenceTiming,
    ModelArchitectureSummary,
    ModelRouteRef,
    RealtimeTemporalSequenceRequest,
    SegmentationPoint,
    SegmentationPrediction,
    TemporalSequenceInferenceResponse,
)
from cv_contracts.runtime import (
    RuntimeGraphEdge,
    RuntimeLogicalModelRef,
    RuntimeNodeSpec,
    RuntimePipelineGraph,
)
from cv_runtime import BUILTIN_NODE_TYPE_BY_KEY, ExecutablePipelineGraph
from cv_runtime.context import RuntimeExecutionContext, RuntimeModelBinding
from cv_runtime.loader import load_runtime_handler
from cv_runtime.nodes import stem_temporal as runtime_module
from cv_runtime.nodes.stem_temporal import (
    StemSurfaceRotationHandler,
    StemTemporalOrientationHandler,
)
from cv_runtime.nodes.temporal_inference import (
    StemOrientationModelDecisionHandler,
    TemporalSequenceInferenceHandler,
)
from cv_runtime.payloads import (
    FramePayload,
    OverlayPayload,
    ResultPayload,
    RuntimeImageContext,
    RuntimeImageTransform,
    RuntimeObjectReference,
    SegmentPayload,
    StemGeometryPayload,
    StemGeometryPoint,
    StemGeometryReferenceAxis,
    StemRotationFeatureQuality,
    StemRotationPayload,
    TemporalFeatureFrame,
    TemporalPredictionPayload,
    TemporalSequencePayload,
)
from cv_runtime.stem_rotation import (
    ROTATION_ALGORITHM_VERSION,
    StemRotationCalibrationMethod,
    StemRotationCalibrationObservation,
    SurfaceFeatureMatch,
    calibrate_stem_rotation,
)
from cv_status import DeploymentReleaseStatus, RuntimeMode, TargetPath, TaskType

_BASE_TIMESTAMP = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
_INTRINSIC_HASH = "intrinsic-sha256"
_VIEWPOINT = "viewed_from_configured_log_end_a"
_POSITIVE_DIRECTION = "clockwise_viewed_from_configured_log_end"


def _node(
    node_key: str,
    node_type: str,
    config: dict[str, object] | None = None,
) -> RuntimeNodeSpec:
    definition = BUILTIN_NODE_TYPE_BY_KEY[node_type]
    return RuntimeNodeSpec(
        node_key=node_key,
        node_type=node_type,
        node_category=definition.node_category,
        node_contract_version=definition.contract_version,
        input_ports=definition.input_ports,
        output_ports=definition.output_ports,
        config={**definition.default_config, **(config or {})},
        logical_model_ref=(
            RuntimeLogicalModelRef(
                model_family_slug="stem-segmentation",
                target_path=TargetPath.REALTIME,
                selection_policy="active_production",
                required_task_type=TaskType.SEGMENTATION,
            )
            if node_type == "inference.segmentation"
            else RuntimeLogicalModelRef(
                model_family_slug="stem-temporal-angle",
                target_path=TargetPath.REALTIME,
                selection_policy="staged_test",
                required_task_type=TaskType.TEMPORAL_SEQUENCE,
            )
            if node_type == "inference.temporal_sequence"
            else None
        ),
    )


def _context(
    *,
    runtime_execution_id: str = "runtime-stem-01",
) -> RuntimeExecutionContext:
    return RuntimeExecutionContext(
        correlation_id="corr-stem-temporal",
        pipeline_version_id="pipeline-stem-temporal-v1",
        runtime_execution_id=runtime_execution_id,
        channel_id="channel-stem",
        mode=RuntimeMode.REALTIME,
        state={},
        metadata={},
    )


def _geometry(
    sequence: int,
    center_x: float,
    bow: float,
    *,
    track_id: str = "track-a",
    shape_class: str = "c_bow",
) -> StemGeometryPayload:
    center_x_px = center_x * 1000.0
    timestamp = (_BASE_TIMESTAMP + timedelta(milliseconds=100 * sequence)).isoformat()
    return StemGeometryPayload(
        valid=True,
        track_id=track_id,
        source_object_id=f"segment-{track_id}",
        source_label="stem",
        segmentation_score=0.96,
        image_context_id="ctx-undistorted",
        image_width=1000,
        image_height=600,
        frame_sequence=sequence,
        timestamp=timestamp,
        reference_axis=StemGeometryReferenceAxis(
            start=StemGeometryPoint(x=center_x_px - 200.0, y=300.0),
            end=StemGeometryPoint(x=center_x_px + 200.0, y=300.0),
            direction=StemGeometryPoint(x=1.0, y=0.0),
            normal=StemGeometryPoint(x=0.0, y=1.0),
            length_px=400.0,
        ),
        visible_endpoints=(
            StemGeometryPoint(x=center_x_px - 200.0, y=300.0),
            StemGeometryPoint(x=center_x_px + 200.0, y=300.0),
        ),
        normalized_s=[0.0, 1.0],
        centerline_image_xy=[
            StemGeometryPoint(x=center_x_px - 200.0, y=300.0),
            StemGeometryPoint(x=center_x_px + 200.0, y=300.0),
        ],
        residual_profile_px=[0.0, 0.0],
        residual_profile_normalized=[bow, bow],
        residual_valid_mask=[True, True],
        width_normalized_s=[0.0, 1.0],
        width_profile_px=[80.0, 80.0],
        width_valid_mask=[True, True],
        signed_curvature=bow,
        signed_bow_px=bow * 80.0,
        signed_bow_normalized=bow,
        rms_residual_px=abs(bow) * 40.0,
        max_abs_residual_px=abs(bow) * 80.0,
        peak_to_peak_px=abs(bow) * 80.0,
        median_width_px=80.0,
        rms_residual_normalized=abs(bow) * 0.5,
        visible_fraction=0.85,
        valid_cross_section_fraction=0.9,
        shape_class=shape_class,  # type: ignore[arg-type]
        quality_score=0.9,
        selected_component_area_px=30_000,
        meaningful_component_count=1,
        selected_component_fraction=1.0,
        segmentation_route={"model_version_id": "mvr-stem-seg-v1"},
    )


def _rotation(
    sequence: int,
    cumulative_rotation_deg: float,
    *,
    track_id: str = "track-a",
    calibration_valid: bool = True,
    trace_discontinuity: bool = False,
) -> StemRotationPayload:
    return StemRotationPayload(
        valid=not trace_discontinuity,
        track_id=track_id,
        source_object_id=f"segment-{track_id}",
        image_context_id="ctx-undistorted",
        frame_sequence=sequence,
        timestamp=(_BASE_TIMESTAMP + timedelta(milliseconds=100 * sequence)).isoformat(),
        delta_rotation_deg=0.0 if sequence == 1 else 20.0,
        cumulative_rotation_deg_unwrapped=cumulative_rotation_deg,
        rotation_direction="stationary" if sequence == 1 else "positive",
        rotation_confidence=0.92,
        feature_quality=StemRotationFeatureQuality(
            detected_feature_count=24,
            matched_feature_count=22,
            inlier_feature_count=20,
            inlier_ratio=0.9,
            quality_score=0.9,
        ),
        trace_discontinuity=trace_discontinuity,
        trace_generation=1 if trace_discontinuity else 0,
        calibration_valid=calibration_valid,
        calibration_id="stem-rotation-cal-01",
        calibration_version="1.0.0",
        calibration_method="known_angle_positions",
        calibration_hash="rotation-calibration-hash",
        intrinsic_calibration_hash=_INTRINSIC_HASH,
        rotation_viewpoint_convention=_VIEWPOINT,
        positive_rotation_direction=_POSITIVE_DIRECTION,
        algorithm_version=ROTATION_ALGORITHM_VERSION,
    )


def _temporal_config() -> dict[str, object]:
    return {
        "observation_entry_roi": [{"x": 0.05, "y": 0.2, "width": 0.15, "height": 0.6}],
        "throw_in_roi": [{"x": 0.2, "y": 0.2, "width": 0.48, "height": 0.6}],
        "decision_roi": [{"x": 0.68, "y": 0.2, "width": 0.12, "height": 0.6}],
        "first_roller_roi": [{"x": 0.82, "y": 0.2, "width": 0.15, "height": 0.6}],
        "process_zone_calibration_id": "process-zones-01",
        "process_zone_calibration_version": "1.0.0",
        "process_zone_intrinsic_hash": _INTRINSIC_HASH,
        "rotation_viewpoint_convention": _VIEWPOINT,
        "positive_rotation_direction": _POSITIVE_DIRECTION,
    }


def _calibration_config() -> dict[str, object]:
    profile = calibrate_stem_rotation(
        [
            StemRotationCalibrationObservation(
                observed_normalized_displacement=displacement,
                known_rotation_deg=180.0 * displacement,
                stem_width_fraction=width,
                image_center_x=center_x,
                image_center_y=0.5,
            )
            for displacement, width, center_x in (
                (-0.25, 0.06, 0.25),
                (-0.125, 0.07, 0.4),
                (0.125, 0.075, 0.6),
                (0.25, 0.08, 0.75),
            )
        ],
        calibration_id="stem-rotation-cal-01",
        version="1.0.0",
        method=StemRotationCalibrationMethod.KNOWN_ANGLE_POSITIONS,
        source_intrinsic_calibration_hash=_INTRINSIC_HASH,
        rotation_viewpoint_convention=_VIEWPOINT,
        positive_rotation_direction=_POSITIVE_DIRECTION,
    )
    return profile.runtime_config()


def _segment(track_id: str = "track-a") -> SegmentPayload:
    polygon = [
        SegmentationPoint(x=0.25, y=0.42),
        SegmentationPoint(x=0.75, y=0.42),
        SegmentationPoint(x=0.75, y=0.58),
        SegmentationPoint(x=0.25, y=0.58),
    ]
    return SegmentPayload(
        segments=[SegmentationPrediction(label="stem", score=0.96, polygon=polygon)],
        image_width=1000,
        image_height=600,
        image_context_id="ctx-undistorted",
        objects=[
            RuntimeObjectReference(
                object_id=f"segment-{track_id}",
                object_type="tracked_object",
                source_image_context_id="ctx-undistorted",
                geometry={"points": [point.model_dump(mode="json") for point in polygon]},
                coordinate_system="normalized_polygon",
                label="stem",
                score=0.96,
                track_id=track_id,
                producer_node_key="active-stem",
            )
        ],
    )


def _frame(sequence: int) -> FramePayload:
    return FramePayload(
        image_base64="",
        width=1000,
        height=600,
        timestamp=(_BASE_TIMESTAMP + timedelta(milliseconds=100 * sequence)).isoformat(),
        image_context=RuntimeImageContext(
            context_id="ctx-undistorted",
            kind="processed",
            width=1000,
            height=600,
            source_node_key="undistort",
            transform=RuntimeImageTransform(
                transform_type="undistort",
                coordinate_projection="not_projectable",
                parameters={"calibration_hash": _INTRINSIC_HASH},
            ),
        ),
        metadata={"sequence": sequence, "calibration_hash": _INTRINSIC_HASH},
    )


def test_registry_loader_payload_and_standard_designer_fields() -> None:
    surface = BUILTIN_NODE_TYPE_BY_KEY["analysis.stem_surface_rotation"]
    temporal = BUILTIN_NODE_TYPE_BY_KEY["analysis.stem_temporal_orientation"]
    temporal_inference = BUILTIN_NODE_TYPE_BY_KEY["inference.temporal_sequence"]
    model_decision = BUILTIN_NODE_TYPE_BY_KEY["analysis.stem_orientation_model_decision"]

    assert [(item.key, item.data_type) for item in surface.input_ports] == [
        ("frame", "frame"),
        ("segments", "segments"),
        ("stem_geometry", "stem_geometry"),
    ]
    assert [(item.key, item.data_type) for item in temporal.input_ports] == [
        ("stem_geometry", "stem_geometry"),
        ("stem_rotation", "stem_rotation"),
        ("active_stem", "segments"),
        ("events", "events"),
    ]
    assert [(item.key, item.data_type) for item in temporal.output_ports] == [
        ("result", "result"),
        ("sequence", "temporal_sequence"),
        ("overlay", "overlay"),
    ]
    assert surface.compatibility_constraints["stateful"] is True
    assert temporal.compatibility_constraints["stateful"] is True
    assert temporal.compatibility_constraints["shadow_mode_only"] is True
    assert temporal_inference.compatibility_constraints["causal"] is True
    assert model_decision.compatibility_constraints["stateful"] is True
    assert model_decision.compatibility_constraints["shadow_mode_only"] is True
    allowed_standard_fields = {"boolean", "integer", "number", "roi", "select", "text"}
    assert {field.field_type for field in surface.property_schema} <= allowed_standard_fields
    assert {field.field_type for field in temporal.property_schema} <= allowed_standard_fields
    assert {field.field_type for field in model_decision.property_schema} <= allowed_standard_fields
    assert isinstance(
        load_runtime_handler(
            surface.runtime_handler_ref,
            expected_node_type=surface.node_type,
        ),
        StemSurfaceRotationHandler,
    )
    assert isinstance(
        load_runtime_handler(
            temporal.runtime_handler_ref,
            expected_node_type=temporal.node_type,
        ),
        StemTemporalOrientationHandler,
    )
    assert isinstance(
        load_runtime_handler(
            temporal_inference.runtime_handler_ref,
            expected_node_type=temporal_inference.node_type,
        ),
        TemporalSequenceInferenceHandler,
    )
    assert isinstance(
        load_runtime_handler(
            model_decision.runtime_handler_ref,
            expected_node_type=model_decision.node_type,
        ),
        StemOrientationModelDecisionHandler,
    )

    payload = _rotation(2, 20.0)
    assert StemRotationPayload.model_validate_json(payload.model_dump_json()) == payload


def test_pipeline_with_both_new_nodes_is_validatable() -> None:
    node_types = [
        ("source", "source.file_test"),
        ("undistort", "preprocessing.camera_undistort"),
        ("segment", "inference.segmentation"),
        ("active", "analysis.active_stem_tracker"),
        ("geometry", "analysis.stem_mask_geometry"),
        ("rotation", "analysis.stem_surface_rotation"),
        ("temporal", "analysis.stem_temporal_orientation"),
        ("temporal_model", "inference.temporal_sequence"),
        ("model_decision", "analysis.stem_orientation_model_decision"),
        ("result", "output.result"),
        ("overlay", "output.overlay"),
    ]
    nodes = [_node(key, node_type) for key, node_type in node_types]
    edges = [
        ("source", "frame", "undistort", "frame"),
        ("undistort", "frame", "segment", "frame"),
        ("segment", "segments", "active", "segments"),
        ("undistort", "frame", "active", "frame"),
        ("active", "segments", "geometry", "segments"),
        ("undistort", "frame", "geometry", "frame"),
        ("undistort", "frame", "rotation", "frame"),
        ("active", "segments", "rotation", "segments"),
        ("geometry", "stem_geometry", "rotation", "stem_geometry"),
        ("geometry", "stem_geometry", "temporal", "stem_geometry"),
        ("rotation", "stem_rotation", "temporal", "stem_rotation"),
        ("active", "segments", "temporal", "active_stem"),
        ("temporal", "sequence", "temporal_model", "sequence"),
        ("temporal", "result", "model_decision", "baseline"),
        ("temporal_model", "prediction", "model_decision", "prediction"),
        ("model_decision", "result", "result", "result"),
        ("model_decision", "overlay", "overlay", "result"),
    ]
    graph = RuntimePipelineGraph(
        nodes=nodes,
        edges=[
            RuntimeGraphEdge(
                from_node_key=source_key,
                from_port=source_port,
                to_node_key=target_key,
                to_port=target_port,
            )
            for source_key, source_port, target_key, target_port in edges
        ],
    )

    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="pipeline-stem-temporal-v1",
        graph=graph,
        handler_refs={
            node.node_type: BUILTIN_NODE_TYPE_BY_KEY[node.node_type].runtime_handler_ref
            for node in nodes
        },
    )

    assert executable.pipeline_version_id == "pipeline-stem-temporal-v1"


def test_surface_rotation_rejects_a_raw_non_undistorted_frame() -> None:
    node = _node("surface", "analysis.stem_surface_rotation", _calibration_config())
    raw_frame = _frame(1).model_copy(update={"image_context": None})

    result = (
        StemSurfaceRotationHandler()
        .execute(
            _context(),
            node,
            {
                "frame": raw_frame,
                "segments": _segment(),
                "stem_geometry": _geometry(1, 0.5, 0.4),
            },
        )
        .outputs["stem_rotation"]
    )

    assert isinstance(result, StemRotationPayload)
    assert result.valid is False
    assert result.delta_rotation_deg is None
    assert result.trace_discontinuity is True
    assert "rotation_undistorted_frame_required" in result.reason_codes


def test_surface_trace_handles_feature_loss_reappearance_and_track_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = _node("surface", "analysis.stem_surface_rotation", _calibration_config())
    context = _context()
    handler = StemSurfaceRotationHandler()
    mask = np.full((600, 1000), 255, dtype=np.uint8)
    matches = tuple(
        SurfaceFeatureMatch(
            previous_x_px=320.0 + 20.0 * index,
            previous_y_px=280.0,
            current_x_px=320.0 + 20.0 * index,
            current_y_px=280.0 + 80.0 * 10.0 / 180.0,
            tracking_error_px=0.2,
        )
        for index in range(16)
    )
    tracked = runtime_module._FeatureTrackingResult(16, matches, ())
    lost = runtime_module._FeatureTrackingResult(16, (), ())
    tracking_results = iter((tracked, lost, tracked))

    monkeypatch.setattr(runtime_module, "_opencv", lambda: SimpleNamespace())
    monkeypatch.setattr(
        runtime_module,
        "_frame_gray",
        lambda frame, cv2: np.zeros((600, 1000), dtype=np.uint8),
    )
    monkeypatch.setattr(runtime_module, "_segment_mask", lambda *args, **kwargs: mask)
    monkeypatch.setattr(runtime_module, "_eroded_stem_mask", lambda *args, **kwargs: mask)
    monkeypatch.setattr(runtime_module, "_blur_score", lambda *args, **kwargs: 100.0)
    monkeypatch.setattr(runtime_module, "_mask_fraction", lambda *args, **kwargs: 0.75)
    monkeypatch.setattr(
        runtime_module,
        "_track_surface_features",
        lambda *args, **kwargs: next(tracking_results),
    )

    def execute(sequence: int, track_id: str = "track-a") -> StemRotationPayload:
        context.metadata["live_frame_sequence"] = sequence
        result = handler.execute(
            context,
            node,
            {
                "frame": _frame(sequence),
                "segments": _segment(track_id),
                "stem_geometry": _geometry(sequence, 0.5, 0.4, track_id=track_id),
            },
        )
        payload = result.outputs["stem_rotation"]
        assert isinstance(payload, StemRotationPayload)
        return payload

    anchor = execute(1)
    valid = execute(2)
    feature_loss = execute(3)
    recovered = execute(4)
    switched = execute(5, track_id="track-b")

    assert anchor.valid is True
    assert anchor.delta_rotation_deg is None
    assert anchor.cumulative_rotation_deg_unwrapped == pytest.approx(0.0)
    assert "rotation_baseline_initialized" in anchor.reason_codes
    assert valid.delta_rotation_deg == pytest.approx(10.0, abs=0.1)
    assert valid.detected_feature_count == 16
    assert valid.matched_feature_count == 16
    assert valid.inlier_feature_count == 16
    assert valid.inlier_ratio == pytest.approx(1.0)
    assert feature_loss.valid is False
    assert feature_loss.delta_rotation_deg is None
    assert feature_loss.trace_discontinuity is True
    assert recovered.valid is True
    assert recovered.cumulative_rotation_deg_unwrapped == pytest.approx(10.0, abs=0.1)
    assert recovered.trace_generation == 1
    assert switched.valid is True
    assert switched.delta_rotation_deg is None
    assert switched.cumulative_rotation_deg_unwrapped == pytest.approx(0.0)
    assert switched.track_id == "track-b"
    assert switched.trace_discontinuity is False


def test_temporal_result_is_frozen_at_decision_before_first_roller() -> None:
    handler = StemTemporalOrientationHandler()
    context = _context()
    node = _node("temporal", "analysis.stem_temporal_orientation", _temporal_config())
    observations = [
        (1, 0.15, 0.0, 0.8),
        (2, 0.30, 20.0, 0.4),
        (3, 0.45, 40.0, 0.1),
        (4, 0.60, 60.0, -0.1),
    ]
    for sequence, center_x, rotation_deg, bow in observations:
        output = handler.execute(
            context,
            node,
            {
                "stem_geometry": _geometry(sequence, center_x, bow),
                "stem_rotation": _rotation(sequence, rotation_deg),
            },
        ).outputs["result"]
        assert isinstance(output, ResultPayload)
        assert output.result["decision_ready"] is False

    decision_output = handler.execute(
        context,
        node,
        {
            "stem_geometry": _geometry(5, 0.72, -0.3),
            "stem_rotation": _rotation(5, 75.0),
        },
    )
    decision = decision_output.outputs["result"]
    overlay = decision_output.outputs["overlay"]
    sequence = decision_output.outputs["sequence"]
    assert isinstance(decision, ResultPayload)
    assert isinstance(overlay, OverlayPayload)
    assert isinstance(sequence, TemporalSequencePayload)
    assert sequence.decision_ready is True
    assert sequence.current_frame_index == 4
    assert sequence.decision_frame_index == 4
    assert sequence.feature_schema_version == "temporal.frame-features.v1"
    assert sequence.rotation_schema_version == "stem.rotation.v1"
    assert [item.frame_index for item in sequence.frames] == [1, 2, 3, 4]
    assert sequence.post_decision_frames_included is False
    result = decision.result
    assert result["decision_ready"] is True
    assert result["operating_mode"] == "shadow"
    assert result["estimate_method"] == "projected_baseline"
    assert result["recommended_rotation_deg_signed"] == pytest.approx(-10.0)
    assert result["recommended_rotation_deg_abs"] == pytest.approx(10.0)
    assert result["recommended_direction"] == ("counter_clockwise_viewed_from_configured_log_end")
    assert result["decision_frame"]["frame_sequence"] == 4
    assert result["current_observation"]["frame_sequence"] == 4
    assert result["target_frame_or_interval_candidate"]["frame_start"] == 3
    assert result["target_frame_or_interval_candidate"]["frame_end"] == 4
    assert result["decision"]["before_first_roller"] is True
    assert result["versions"]["temporal_model_version_id"] is None
    assert result["channel_id"] == "channel-stem"
    assert result["observation_started_at"] == (
        _BASE_TIMESTAMP + timedelta(milliseconds=100)
    ).isoformat()
    assert result["compact_curvature_trace"]["source_observation_count"] == 4
    assert len(result["compact_curvature_trace"]["samples"]) == 4
    assert len(result["compact_rotation_trace"]["samples"]) == 4
    assert result["asset_references"]["status"] == "not_recorded"
    assert result["asset_references"]["selected_frames"][0]["role"] == "decision"
    decision_labels = [
        item.label
        for item in overlay.primitives
        if item.layer == "stem_pre_entry_decision"
    ]
    expected_confidence = f"{float(result['confidence']):.2f}".replace(".", ",")
    assert any(
        "Drehe 10,0° gegen den Uhrzeigersinn" in str(label)
        and f"Konfidenz {expected_confidence}" in str(label)
        for label in decision_labels
    )
    assert {item.label for item in overlay.primitives if item.primitive_type == "zone"} == {
        "Observation entry",
        "Throw-in / roll",
        "Decision deadline",
        "First roller (diagnostic only)",
    }

    frozen_output = handler.execute(
        context,
        node,
        {
            "stem_geometry": _geometry(6, 0.88, 0.9),
            "stem_rotation": _rotation(6, 100.0),
        },
    )
    frozen = frozen_output.outputs["result"]
    frozen_sequence = frozen_output.outputs["sequence"]
    assert isinstance(frozen, ResultPayload)
    assert isinstance(frozen_sequence, TemporalSequencePayload)
    assert frozen.result == result
    assert frozen_sequence == sequence


def test_temporal_fail_closed_track_switch_and_runtime_restart() -> None:
    handler = StemTemporalOrientationHandler()
    node = _node("temporal", "analysis.stem_temporal_orientation", _temporal_config())
    context = _context()

    first = handler.execute(
        context,
        node,
        {
            "stem_geometry": _geometry(1, 0.15, 0.8, track_id="track-a"),
            "stem_rotation": _rotation(1, 0.0, track_id="track-a"),
        },
    ).outputs["result"]
    switched = handler.execute(
        context,
        node,
        {
            "stem_geometry": _geometry(2, 0.15, 0.7, track_id="track-b"),
            "stem_rotation": _rotation(2, 0.0, track_id="track-b"),
        },
    ).outputs["result"]
    assert isinstance(first, ResultPayload)
    assert isinstance(switched, ResultPayload)
    assert switched.result["session_id"] != first.result["session_id"]
    assert "stem_temporal_track_changed_reset" in switched.result["reason_codes"]
    state = context.state["temporal"]
    assert state["track_id"] == "track-b"
    assert len(state["bounded_observation_buffer"]) == 1
    assert len(state["bounded_feature_buffer"]) == 1
    assert state["rotation_trace_summary"]["observation_count"] == 1
    assert state["lost_counter"] == 0
    assert "target_candidates" in state
    assert "post_rotation_verification" in state

    discontinuous = handler.execute(
        context,
        node,
        {
            "stem_geometry": _geometry(3, 0.30, 0.4, track_id="track-b"),
            "stem_rotation": _rotation(
                3,
                20.0,
                track_id="track-b",
                trace_discontinuity=True,
            ),
        },
    ).outputs["result"]
    assert isinstance(discontinuous, ResultPayload)
    assert discontinuous.result["recommended_rotation_deg_signed"] is None

    restarted = (
        StemTemporalOrientationHandler()
        .execute(
            _context(runtime_execution_id="runtime-stem-restarted"),
            node,
            {
                "stem_geometry": _geometry(4, 0.72, -0.1, track_id="track-b"),
                "stem_rotation": _rotation(4, 60.0, track_id="track-b"),
            },
        )
        .outputs["result"]
    )
    assert isinstance(restarted, ResultPayload)
    assert restarted.result["session_state"] == "WAITING_FOR_LOG"
    assert restarted.result["recommended_rotation_deg_signed"] is None


def test_temporal_model_result_uses_normal_binding_and_preserves_frozen_baseline() -> None:
    sequence = TemporalSequencePayload(
        sequence_id="session-track-a",
        group_id="session-track-a",
        track_id="track-a",
        feature_set_id="stem_orientation.frame_features.v1",
        feature_schema_version="temporal.frame-features.v1",
        rotation_schema_version="stem.rotation.v1",
        calibration_schema_version="1.0.0",
        rotation_calibration_id="stem-rotation-cal-01",
        rotation_calibration_hash="rotation-calibration-hash",
        rotation_viewpoint_convention=_VIEWPOINT,
        positive_rotation_direction=_POSITIVE_DIRECTION,
        frames=[
            TemporalFeatureFrame(
                frame_index=index,
                timestamp_seconds=(index - 1) * 0.1,
                values={"geometry": {}, "rotation": {}},
            )
            for index in range(1, 5)
        ],
        current_frame_index=4,
        decision_frame_index=4,
        decision_ready=True,
    )
    assert TemporalSequencePayload.model_validate_json(sequence.model_dump_json()) == sequence

    class FakeInferenceClient:
        def temporal_sequence(
            self, payload: RealtimeTemporalSequenceRequest
        ) -> TemporalSequenceInferenceResponse:
            assert payload.current_frame_index == 4
            assert payload.decision_frame_index == 4
            assert [item.frame_index for item in payload.frames] == [1, 2, 3, 4]
            return TemporalSequenceInferenceResponse(
                request_id="req-temporal",
                deployment_target="realtime",
                route=ModelRouteRef(
                    deployment_release_id="rel-temporal-staged",
                    target_path=TargetPath.REALTIME,
                    release_status=DeploymentReleaseStatus.ACTIVATED,
                    model_family_slug="stem-temporal-angle",
                    model_version_id="mvr-temporal-v1",
                    task_type=TaskType.TEMPORAL_SEQUENCE,
                    triton_model_name="rt__stem_temporal_v1",
                    triton_model_version=1,
                    serving_artifact_format="tensorrt_plan",
                    contract_major=1,
                ),
                architecture=ModelArchitectureSummary(
                    task_type=TaskType.TEMPORAL_SEQUENCE,
                    family="tcn",
                    version="v1",
                    size="small",
                    display_name="Causal TCN Small",
                    internal_key="temporal_sequence:tcn:v1:small",
                ),
                warm_model=True,
                timing=InferenceTiming(
                    received_at=_BASE_TIMESTAMP.isoformat(),
                    validation_ms=0.1,
                    routing_ms=0.1,
                    preprocessing_ms=0.2,
                    inference_ms=0.3,
                    postprocessing_ms=0.1,
                    total_ms=0.8,
                ),
                sequence_id="session-track-a",
                track_id="track-a",
                sequence_length=4,
                last_frame_index=4,
                decision_frame_index=4,
                feature_set_id="stem_orientation.frame_features.v1",
                feature_schema_digest="feature-schema-digest",
                scaler_sha256="scaler-sha256",
                snapshot_id="snapshot-temporal-v1",
                feature_schema_version="temporal.frame-features.v1",
                rotation_schema_version="stem.rotation.v1",
                calibration_schema_version="1.0.0",
                rotation_calibration_id="stem-rotation-cal-01",
                rotation_calibration_hash="rotation-calibration-hash",
                rotation_viewpoint_convention=_VIEWPOINT,
                positive_rotation_direction=_POSITIVE_DIRECTION,
                bow_relevance_probability=0.91,
                target_observed_probability=0.88,
                angle_sin_2e=-0.406736,
                angle_cos_2e=0.913545,
                signed_correction_deg=-12.0,
                raw_angle_confidence=0.82,
                angle_confidence=0.84,
                confidence_calibration_valid=True,
                final_quality_probabilities={"bad": 0.1, "acceptable": 0.3, "good": 0.6},
                deadband_deg=5.0,
                estimate_method="tcn",
            )

    context = _context()
    context.inference_client = FakeInferenceClient()  # type: ignore[assignment]
    context.model_resolver = lambda _ref: RuntimeModelBinding(
        deployment_release_id="rel-temporal-staged",
        deployment_target="realtime",
        target_path=TargetPath.REALTIME,
        model_family_slug="stem-temporal-angle",
        model_version_id="mvr-temporal-v1",
        task_type=TaskType.TEMPORAL_SEQUENCE,
        contract_major=1,
    )
    inference_result = TemporalSequenceInferenceHandler().execute(
        context,
        _node("temporal_model", "inference.temporal_sequence"),
        {"sequence": sequence},
    )
    prediction = inference_result.outputs["prediction"]
    assert isinstance(prediction, TemporalPredictionPayload)
    assert prediction.valid is True

    baseline_payload = ResultPayload(
        result={
            "schema_version": "stem.orientation.v2",
            "session_id": "session-track-a",
            "track_id": "track-a",
            "decision_ready": True,
            "decision_frame": {
                "frame_sequence": 4,
                "timestamp": (_BASE_TIMESTAMP + timedelta(milliseconds=400)).isoformat(),
            },
            "decision": {
                "timestamp": (_BASE_TIMESTAMP + timedelta(milliseconds=400)).isoformat(),
                "before_first_roller": True,
            },
            "recommended_rotation_deg_signed": -10.0,
            "recommended_rotation_deg_abs": 10.0,
            "recommended_direction": "counter_clockwise_viewed_from_configured_log_end",
            "estimate_method": "projected_baseline",
            "reason_codes": [],
            "direction_provenance": {
                "rotation_viewpoint_convention": _VIEWPOINT,
                "positive_rotation_direction": _POSITIVE_DIRECTION,
            },
            "versions": {
                "feature_schema_version": "stem.geometry.v1",
                "temporal_feature_schema_version": "temporal.frame-features.v1",
                "rotation_schema_version": "stem.rotation.v1",
                "rotation_calibration_version": "1.0.0",
                "rotation_calibration_id": "stem-rotation-cal-01",
                "rotation_calibration_hash": "rotation-calibration-hash",
            },
            "post_rotation_verification": {"status": "not_observed"},
        }
    )
    decision_output = StemOrientationModelDecisionHandler().execute(
        context,
        _node("model_decision", "analysis.stem_orientation_model_decision"),
        {"baseline": baseline_payload, "prediction": prediction},
    )
    result = decision_output.outputs["result"]
    assert isinstance(result, ResultPayload)
    assert result.result["decision_ready"] is True
    assert result.result["decision_frame_index"] == 4
    assert result.result["recommended_rotation_deg_signed"] == pytest.approx(-12.0)
    assert result.result["recommended_rotation_deg_abs"] == pytest.approx(12.0)
    assert result.result["recommended_direction"] == (
        "counter_clockwise_viewed_from_configured_log_end"
    )
    assert result.result["estimate_method"] == "tcn"
    assert result.result["angle_confidence"] == pytest.approx(0.84)
    assert result.result["deterministic_baseline"] == baseline_payload.result
    assert result.result["post_rotation_verification"] == {"status": "not_observed"}
    assert result.result["versions"]["temporal_snapshot_id"] == "snapshot-temporal-v1"
    assert result.result["valid_until"] is not None

    dead_prediction = TemporalPredictionPayload(
        valid=False,
        sequence_id="session-track-a",
        track_id="track-a",
        reason_codes=["gateway_temporarily_unavailable"],
    )
    frozen_result = StemOrientationModelDecisionHandler().execute(
        context,
        _node("model_decision", "analysis.stem_orientation_model_decision"),
        {
            "baseline": baseline_payload,
            "prediction": dead_prediction,
        },
    ).outputs["result"]
    assert isinstance(frozen_result, ResultPayload)
    assert frozen_result.result["recommended_rotation_deg_signed"] == pytest.approx(-12.0)
    assert frozen_result.result["model_prediction"] == prediction.model_dump(mode="json")

    verification_baseline = ResultPayload(
        result={
            **baseline_payload.result,
            "post_rotation_verification": {
                "status": "observed",
                "residual_signed_bow_normalized": 0.03,
            },
        }
    )
    verified_result = StemOrientationModelDecisionHandler().execute(
        context,
        _node("model_decision", "analysis.stem_orientation_model_decision"),
        {"baseline": verification_baseline, "prediction": dead_prediction},
    ).outputs["result"]
    assert isinstance(verified_result, ResultPayload)
    assert verified_result.result["recommended_rotation_deg_signed"] == pytest.approx(-12.0)
    assert verified_result.result["post_rotation_verification"] == {
        "status": "observed",
        "residual_signed_bow_normalized": 0.03,
    }

    switched_baseline = ResultPayload(
        result={
            **baseline_payload.result,
            "session_id": "session-track-b",
            "track_id": "track-b",
            "recommended_rotation_deg_signed": 0.0,
        }
    )
    switched_prediction = prediction.model_copy(
        update={
            "sequence_id": "session-track-b",
            "track_id": "track-b",
            "signed_correction_deg": 3.0,
            "angle_sin_2e": 0.104528,
            "angle_cos_2e": 0.994522,
        }
    )
    switched_result = StemOrientationModelDecisionHandler().execute(
        context,
        _node("model_decision", "analysis.stem_orientation_model_decision"),
        {"baseline": switched_baseline, "prediction": switched_prediction},
    ).outputs["result"]
    assert isinstance(switched_result, ResultPayload)
    assert switched_result.result["track_id"] == "track-b"
    assert switched_result.result["recommended_rotation_deg_signed"] == 0.0

    restarted_result = StemOrientationModelDecisionHandler().execute(
        _context(),
        _node("model_decision", "analysis.stem_orientation_model_decision"),
        {"baseline": baseline_payload, "prediction": dead_prediction},
    ).outputs["result"]
    assert isinstance(restarted_result, ResultPayload)
    assert restarted_result.result["recommended_rotation_deg_signed"] is None
    assert restarted_result.result["recommended_direction"] == "manual_required"

    deadband_prediction = prediction.model_copy(
        update={
            "signed_correction_deg": 3.0,
            "angle_sin_2e": 0.104528,
            "angle_cos_2e": 0.994522,
        }
    )
    deadband_result = StemOrientationModelDecisionHandler().execute(
        _context(),
        _node("deadband_decision", "analysis.stem_orientation_model_decision"),
        {"baseline": baseline_payload, "prediction": deadband_prediction},
    ).outputs["result"]
    assert isinstance(deadband_result, ResultPayload)
    assert deadband_result.result["recommended_rotation_deg_signed"] == 0.0
    assert deadband_result.result["recommended_direction"] == "no_rotation"

    disagreement_prediction = prediction.model_copy(
        update={
            "signed_correction_deg": 45.0,
            "angle_sin_2e": 1.0,
            "angle_cos_2e": 0.0,
        }
    )
    disagreement_result = StemOrientationModelDecisionHandler().execute(
        _context(),
        _node(
            "disagreement_decision",
            "analysis.stem_orientation_model_decision",
            {"maximum_baseline_disagreement_deg": 10.0},
        ),
        {"baseline": baseline_payload, "prediction": disagreement_prediction},
    ).outputs["result"]
    assert isinstance(disagreement_result, ResultPayload)
    assert disagreement_result.result["recommended_rotation_deg_signed"] is None
    assert disagreement_result.result["recommended_direction"] == "manual_required"
    assert (
        "temporal_model_baseline_disagreement"
        in disagreement_result.result["reason_codes"]
    )

    late_baseline = ResultPayload(
        result={
            **baseline_payload.result,
            "reason_codes": ["decision_line_missed_first_roller_reached"],
            "decision": {
                **baseline_payload.result["decision"],
                "before_first_roller": False,
            },
        }
    )
    late_result = StemOrientationModelDecisionHandler().execute(
        _context(),
        _node("late_decision", "analysis.stem_orientation_model_decision"),
        {"baseline": late_baseline, "prediction": prediction},
    ).outputs["result"]
    assert isinstance(late_result, ResultPayload)
    assert late_result.result["recommended_rotation_deg_signed"] is None
    assert late_result.result["recommended_direction"] == "manual_required"


def test_temporal_model_fail_closed_on_disagreement_and_invalid_prediction() -> None:
    context = _context()
    node = _node(
        "model_decision",
        "analysis.stem_orientation_model_decision",
        {"maximum_baseline_disagreement_deg": 10.0},
    )
    baseline = ResultPayload(
        result={
            "schema_version": "stem.orientation.v2",
            "session_id": "session-a",
            "track_id": "track-a",
            "decision_ready": True,
            "decision_frame": {"frame_sequence": 4},
            "decision": {},
            "recommended_rotation_deg_signed": -10.0,
            "direction_provenance": {
                "rotation_viewpoint_convention": _VIEWPOINT,
                "positive_rotation_direction": _POSITIVE_DIRECTION,
            },
            "versions": {
                "feature_schema_version": "stem.geometry.v1",
                "temporal_feature_schema_version": "temporal.frame-features.v1",
                "rotation_schema_version": "stem.rotation.v1",
                "rotation_calibration_version": "1.0.0",
                "rotation_calibration_id": "cal-1",
                "rotation_calibration_hash": "hash-1",
            },
            "reason_codes": [],
        }
    )
    invalid = TemporalPredictionPayload(
        valid=False,
        sequence_id="session-a",
        track_id="track-a",
        reason_codes=["temporal_trace_discontinuous"],
    )
    output = StemOrientationModelDecisionHandler().execute(
        context,
        node,
        {"baseline": baseline, "prediction": invalid},
    ).outputs["result"]
    assert isinstance(output, ResultPayload)
    assert output.result["recommended_rotation_deg_signed"] is None
    assert output.result["recommended_direction"] == "manual_required"
    assert "temporal_model_prediction_invalid" in output.result["reason_codes"]


def test_realtime_marks_both_nodes_stateful_and_serializes_the_pipeline() -> None:
    from realtime_executor.live import _pipeline_max_inflight, _stateful_pipeline_nodes

    stateful_nodes = _stateful_pipeline_nodes(
        [
            {"node_key": "surface", "node_type": "analysis.stem_surface_rotation"},
            {"node_key": "temporal", "node_type": "analysis.stem_temporal_orientation"},
            {
                "node_key": "model_decision",
                "node_type": "analysis.stem_orientation_model_decision",
            },
        ]
    )
    settings = SimpleNamespace(
        runtime=SimpleNamespace(realtime_pipeline_max_inflight=4),
        inference_limits=SimpleNamespace(
            realtime_max_concurrency=4,
            realtime_per_release_max_concurrency=4,
        ),
    )

    assert stateful_nodes == ["surface", "temporal", "model_decision"]
    assert (
        _pipeline_max_inflight(
            settings,
            {},
            stateful_nodes=stateful_nodes,
            inference_nodes=[],
        )
        == 1
    )
