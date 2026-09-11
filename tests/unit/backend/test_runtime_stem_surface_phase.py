from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import cv2
import numpy as np
import pytest
from cv_contracts import SegmentationPoint, SegmentationPrediction
from cv_contracts.runtime import (
    RuntimeNodeSpec,
    RuntimeStemCurvatureProcessCalibrationProfile,
)
from cv_runtime import BUILTIN_NODE_TYPE_BY_KEY
from cv_runtime.context import RuntimeExecutionContext
from cv_runtime.high_rate_phase import (
    HighRatePhaseConfig,
    HighRatePhaseFrame,
    HighRateStemPhaseBridge,
    StemPhaseSeed,
)
from cv_runtime.loader import load_runtime_handler
from cv_runtime.nodes.stem_surface_phase import (
    StemSurfacePhaseHandler,
    _metric_cross_section_diameter_mm,
    _metric_translation_component,
)
from cv_runtime.payloads import (
    FramePayload,
    RuntimeImageContext,
    RuntimeImageTransform,
    RuntimeObjectReference,
    SegmentPayload,
    StemGeometryPayload,
    StemGeometryPoint,
    StemGeometryReferenceAxis,
    StemSurfacePhasePayload,
)
from cv_runtime.stem_process_coordinates import calculate_stem_curvature_process_calibration
from cv_runtime.stem_rotation import StemAxisObservation
from cv_runtime.stem_surface_phase import calculate_stem_surface_phase_calibration
from cv_status import RuntimeMode
from tests.unit.backend.test_stem_process_coordinates import process_calibration_request
from tests.unit.backend.test_stem_surface_phase import _request as _phase_request


def _process_profile() -> RuntimeStemCurvatureProcessCalibrationProfile:
    result = calculate_stem_curvature_process_calibration(
        source_channel_id="chn_stem",
        request=process_calibration_request(),
        generated_at="2026-08-04T10:00:00Z",
    )
    assert result.profile is not None
    return result.profile


def _node() -> RuntimeNodeSpec:
    definition = BUILTIN_NODE_TYPE_BY_KEY["analysis.stem_surface_phase"]
    return RuntimeNodeSpec(
        node_key="surface_phase",
        node_type=definition.node_type,
        node_category=definition.node_category,
        node_contract_version=definition.contract_version,
        input_ports=definition.input_ports,
        output_ports=definition.output_ports,
        config={
            **definition.default_config,
            "phase_estimator_version": "LK_KEYFRAME_RELOCK_V1",
            "execution_mode": "SYNCHRONOUS",
            "require_active_stem_motion_for_phase": True,
            "minimum_blur_score": 0,
            "keyframe_minimum_features": 4,
        },
    )


def _context() -> RuntimeExecutionContext:
    return RuntimeExecutionContext(
        correlation_id="corr_surface_phase",
        pipeline_version_id="piv_surface_phase",
        channel_id="chn_stem",
        mode=RuntimeMode.REALTIME,
        state={},
    )


def _high_rate_node() -> RuntimeNodeSpec:
    node = _node()
    return node.model_copy(
        update={
            "config": {
                **node.config,
                "phase_estimator_version": "CYLINDRICAL_SURFACE_V2",
                "execution_mode": "HIGH_RATE_SEEDED",
                "surface_phase_calibration": None,
            }
        }
    )


def _image(*, texture_shift_y: int = 0, blank: bool = False) -> np.ndarray:
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    if not blank:
        cv2.rectangle(image, (100, 160), (540, 320), (55, 55, 55), -1)
        for x in range(120, 530, 28):
            for y in range(180, 310, 28):
                shifted = min(max(y + texture_shift_y, 165), 315)
                cv2.circle(image, (x, shifted), 3, (220, 220, 220), -1)
    return image


def _timestamp(sequence: int) -> str:
    value = datetime(2026, 8, 4, 10, tzinfo=UTC) + timedelta(
        milliseconds=max(sequence, 0) * 20
    )
    return value.isoformat().replace("+00:00", "Z")


def _frame(
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
    image: np.ndarray,
    sequence: int,
) -> FramePayload:
    context = RuntimeImageContext(
        context_id="ctx_process_rectified",
        kind="transformed",
        width=640,
        height=480,
        source_image_context_id="ctx_undistorted",
        parent_image_context_id="ctx_undistorted",
        transform=RuntimeImageTransform(
            transform_type="perspective_rectify",
            coordinate_projection="homography",
            parameters={"process_calibration_hash": profile.calibration_hash},
        ),
    )
    return FramePayload.from_cv2_frame(
        image,
        timestamp=_timestamp(sequence),
        image_context=context,
        metadata={
            "sequence": sequence,
            "process_calibration_hash": profile.calibration_hash,
            "intrinsic_calibration_hash": profile.intrinsic_calibration_hash,
            "stem_curvature_process_calibration": profile.model_dump(mode="json"),
            "perspective_rectified": True,
        },
    )


def _segments(
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
    *,
    track_id: str,
    motion_speed_mm_per_frame: float = 10.0,
    process_motion_axis: str = "transfer_y",
) -> SegmentPayload:
    polygon = [
        SegmentationPoint(x=x / 639, y=y / 479)
        for x, y in [(100, 160), (540, 160), (540, 320), (100, 320)]
    ]
    tracked = RuntimeObjectReference(
        object_id=f"obj-{track_id}",
        object_type="tracked_object",
        source_image_context_id="ctx_process_rectified",
        coordinate_system="normalized_polygon",
        geometry={"points": [point.model_dump(mode="json") for point in polygon]},
        label="stem",
        score=0.95,
        track_id=track_id,
        producer_node_key="active_tracker",
    )
    return SegmentPayload(
        segments=[SegmentationPrediction(label="stem", score=0.95, polygon=polygon)],
        image_width=profile.image_width,
        image_height=profile.image_height,
        image_context_id="ctx_process_rectified",
        objects=[tracked],
        metadata={
            "active_stem_tracking": {
                "status": "active",
                "track_id": track_id,
                "movement": {
                    "process_motion_axis": process_motion_axis,
                    "velocity_x_mm_per_frame": 0.0,
                    "velocity_y_mm_per_frame": motion_speed_mm_per_frame,
                },
            }
        },
    )


def _geometry(sequence: int, *, track_id: str) -> StemGeometryPayload:
    return StemGeometryPayload(
        valid=True,
        track_id=track_id,
        source_object_id=f"obj-{track_id}",
        image_context_id="ctx_process_rectified",
        image_width=640,
        image_height=480,
        frame_sequence=sequence,
        timestamp=_timestamp(sequence),
        reference_axis=StemGeometryReferenceAxis(
            start=StemGeometryPoint(x=100, y=240),
            end=StemGeometryPoint(x=540, y=240),
            direction=StemGeometryPoint(x=1, y=0),
            normal=StemGeometryPoint(x=0, y=1),
            length_px=440,
        ),
        median_width_px=160,
        visible_fraction=1,
        valid_cross_section_fraction=1,
        quality_score=1,
        selected_component_area_px=70_000,
        meaningful_component_count=1,
        selected_component_fraction=1,
    )


def test_metric_diameter_projects_cross_section_endpoints() -> None:
    profile = _process_profile().model_copy(
        update={
            "homography_image_to_process": [2.0, 0.0, 0.0, 0.0, 3.0, 0.0, 0.0, 0.0, 1.0],
            "homography_process_to_image": [0.5, 0.0, 0.0, 0.0, 1.0 / 3.0, 0.0, 0.0, 0.0, 1.0],
        }
    )
    geometry = _geometry(1, track_id="track-a").model_copy(
        update={
            "normalized_s": [0.0, 1.0],
            "centerline_image_xy": [
                StemGeometryPoint(x=100, y=240),
                StemGeometryPoint(x=540, y=240),
            ],
            "width_normalized_s": [0.25, 0.75],
            "width_profile_px": [100.0, 120.0],
            "width_valid_mask": [True, True],
        }
    )

    assert _metric_cross_section_diameter_mm(geometry, profile) == pytest.approx(330.0)


def test_surface_translation_is_projected_to_signed_process_x() -> None:
    profile = _process_profile().model_copy(
        update={
            "homography_image_to_process": [2.0, 0.0, 0.0, 0.0, 3.0, 0.0, 0.0, 0.0, 1.0],
            "homography_process_to_image": [0.5, 0.0, 0.0, 0.0, 1.0 / 3.0, 0.0, 0.0, 0.0, 1.0],
        }
    )

    component = _metric_translation_component(
        geometry=_geometry(1, track_id="track-a"),
        process_profile=profile,
        step_x_px=5.0,
        step_y_px=-2.0,
        cumulative_x_px=10.0,
        cumulative_y_px=4.0,
    )

    assert component.x_mm == pytest.approx(10.0)
    assert component.y_mm == pytest.approx(-6.0)
    assert component.cumulative_x_mm == pytest.approx(20.0)
    assert component.cumulative_y_mm == pytest.approx(12.0)


def _execute(
    handler: StemSurfacePhaseHandler,
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
    image: np.ndarray,
    sequence: int,
    track_id: str = "track-a",
    motion_speed_mm_per_frame: float = 10.0,
    process_motion_axis: str = "transfer_y",
) -> StemSurfacePhasePayload:
    return handler.execute(
        context,
        node,
        {
            "frame": _frame(profile, image, sequence),
            "segments": _segments(
                profile,
                track_id=track_id,
                motion_speed_mm_per_frame=motion_speed_mm_per_frame,
                process_motion_axis=process_motion_axis,
            ),
            "stem_geometry": _geometry(sequence, track_id=track_id),
        },
    ).outputs["surface_phase"]


def test_registry_loader_and_stateful_executor_contract() -> None:
    from realtime_executor.live import _STATEFUL_PIPELINE_NODE_TYPES

    definition = BUILTIN_NODE_TYPE_BY_KEY["analysis.stem_surface_phase"]

    assert definition.compatibility_constraints["stateful"] is True
    assert definition.compatibility_constraints["raw_phase_without_angle_calibration"] is True
    assert definition.compatibility_constraints["calibrated_degrees_fail_closed"] is True
    assert any(
        field.field_type == "stem_surface_phase_calibration" for field in definition.property_schema
    )
    fields = {field.key: field for field in definition.property_schema}
    assert fields["phase_estimator_version"].field_type == "select"
    assert fields["execution_mode"].field_type == "select"
    assert fields["require_active_stem_motion_for_phase"].field_type == "boolean"
    assert fields["minimum_phase_motion_mm_per_frame"].field_type == "number"
    assert definition.default_config["phase_estimator_version"] == "LK_KEYFRAME_RELOCK_V1"
    assert definition.default_config["execution_mode"] == "SYNCHRONOUS"
    assert definition.default_config["require_active_stem_motion_for_phase"] is False
    assert definition.default_config["minimum_phase_motion_mm_per_frame"] == pytest.approx(3.0)
    assert (
        load_runtime_handler(
            definition.runtime_handler_ref, expected_node_type=definition.node_type
        ).node_type
        == definition.node_type
    )
    assert "analysis.stem_surface_phase" in _STATEFUL_PIPELINE_NODE_TYPES


def test_high_rate_node_publishes_only_the_active_track_as_seed() -> None:
    profile = _process_profile()
    bridge = HighRateStemPhaseBridge(
        HighRatePhaseConfig(minimum_blur_score=0, minimum_matched_features=4)
    )
    context = _context()
    context.metadata["high_rate_stem_phase_bridge"] = bridge

    payload = _execute(
        StemSurfacePhaseHandler(),
        context,
        _high_rate_node(),
        profile,
        _image(),
        1,
        track_id="track-fast",
    )
    snapshot = bridge.snapshot("track-fast")

    assert payload.execution_mode == "HIGH_RATE_SEEDED"
    assert payload.phase_estimator_version == "CYLINDRICAL_SURFACE_V2"
    assert payload.track_id == "track-fast"
    assert snapshot.track_id == "track-fast"
    assert snapshot.seed_status == "WAITING"
    assert "PHASE_CALIBRATION_MISSING" in payload.reason_codes


def test_high_rate_node_keeps_bound_degrees_during_short_geometry_dropout() -> None:
    profile = _process_profile()
    calibration_result = calculate_stem_surface_phase_calibration(
        source_channel_id="chn_stem",
        request=_phase_request().model_copy(
            update={
                "image_width": 640,
                "image_height": 480,
                "intrinsic_calibration_hash": profile.intrinsic_calibration_hash,
                "process_calibration_hash": profile.calibration_hash,
                "rotation_reference_view": profile.rotation_reference_view,
                "positive_rotation": profile.positive_rotation,
                "phase_estimator_version": "CYLINDRICAL_SURFACE_V2",
                "valid_diameter_min_mm": 1.0,
                "valid_diameter_max_mm": 10_000.0,
            }
        ),
    )
    assert calibration_result.profile is not None
    node = _high_rate_node().model_copy(
        update={
            "config": {
                **_high_rate_node().config,
                "surface_phase_calibration": calibration_result.profile.model_dump(
                    mode="json"
                ),
            }
        }
    )
    bridge = HighRateStemPhaseBridge(
        HighRatePhaseConfig(minimum_blur_score=0, minimum_matched_features=4)
    )
    context = _context()
    context.metadata["high_rate_stem_phase_bridge"] = bridge
    handler = StemSurfacePhaseHandler()
    image = _image()

    _execute(handler, context, node, profile, image, 1, track_id="track-fast")
    bridge.process_frame(
        HighRatePhaseFrame(image, 1, _timestamp(1), time.perf_counter())
    )
    geometry = _geometry(2, track_id="track-fast").model_copy(update={"valid": False})
    payload = handler.execute(
        context,
        node,
        {
            "frame": _frame(profile, image, 2),
            "segments": _segments(profile, track_id="track-fast"),
            "stem_geometry": geometry,
        },
    ).outputs["surface_phase"]

    assert payload.phase_locked
    assert payload.calibrated_phase_valid
    assert payload.cumulative_phase_deg == pytest.approx(0.0)
    assert "PHASE_SEED_NOT_REFRESHED" in payload.reason_codes


def test_high_rate_bridge_applies_seed_and_fails_closed_when_it_becomes_stale() -> None:
    image = _image()
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    mask = np.zeros(gray.shape, dtype=np.uint8)
    cv2.rectangle(mask, (100, 160), (540, 320), 255, -1)
    bridge = HighRateStemPhaseBridge(
        HighRatePhaseConfig(
            minimum_blur_score=0,
            minimum_matched_features=4,
            maximum_seed_age_ms=300,
        )
    )
    bridge.publish_seed(
        StemPhaseSeed(
            track_id="track-fast",
            session_id="session-fast",
            frame_sequence=1,
            timestamp=_timestamp(1),
            gray=gray,
            stem_mask=mask,
            axis=_axis_observation_for_test(),
            confidence=0.9,
            image_context_id="ctx_process_rectified",
            intrinsic_calibration_hash="a" * 64,
            process_calibration_hash="b" * 64,
        )
    )

    initialized = bridge.process_frame(
        HighRatePhaseFrame(image, 1, _timestamp(1), time.perf_counter())
    )
    stale_timestamp = (
        datetime(2026, 8, 4, 10, tzinfo=UTC) + timedelta(seconds=1)
    ).isoformat().replace("+00:00", "Z")
    stale = bridge.process_frame(
        HighRatePhaseFrame(image, 50, stale_timestamp, time.perf_counter())
    )

    assert initialized.track_id == "track-fast"
    assert initialized.seed_status == "APPLIED"
    assert initialized.cumulative_raw_phase == pytest.approx(0.0)
    assert stale.tracking_mode == "LOST"
    assert not stale.phase_locked
    assert "PHASE_SEED_STALE" in stale.reason_codes


def test_high_rate_bridge_replays_buffered_frames_after_a_late_seed() -> None:
    image = _image()
    bridge = HighRateStemPhaseBridge(
        HighRatePhaseConfig(
            minimum_blur_score=0,
            minimum_matched_features=4,
            maximum_seed_age_ms=500,
        )
    )
    for sequence in range(2, 5):
        bridge.process_frame(
            HighRatePhaseFrame(image, sequence, _timestamp(sequence), time.perf_counter())
        )

    bridge.publish_seed(_phase_seed(image, sequence=1))
    replayed = bridge.process_frame(
        HighRatePhaseFrame(image, 5, _timestamp(5), time.perf_counter())
    )

    assert replayed.frame_sequence == 5
    assert replayed.track_id == "track-fast"
    assert replayed.seed_status == "PROPAGATED"
    assert replayed.cumulative_raw_phase == pytest.approx(0.0, abs=0.02)


def test_high_rate_bridge_never_returns_another_track_and_resets_for_new_session() -> None:
    image = _image()
    bridge = HighRateStemPhaseBridge(
        HighRatePhaseConfig(minimum_blur_score=0, minimum_matched_features=4)
    )
    bridge.publish_seed(_phase_seed(image, sequence=1))
    first = bridge.process_frame(
        HighRatePhaseFrame(image, 1, _timestamp(1), time.perf_counter())
    )

    mismatch = bridge.snapshot("waiting-stem", 1)
    bridge.publish_seed(_phase_seed(image, sequence=2, session_id="session-next"))
    next_session = bridge.process_frame(
        HighRatePhaseFrame(image, 2, _timestamp(2), time.perf_counter())
    )

    assert mismatch.track_id == "waiting-stem"
    assert mismatch.tracking_mode == "LOST"
    assert "PHASE_FAST_PATH_TRACK_MISMATCH" in mismatch.reason_codes
    assert next_session.session_id == "session-next"
    assert next_session.cumulative_raw_phase == pytest.approx(0.0)
    assert next_session.trace_generation == first.trace_generation + 1
    assert next_session.seed_status == "TRACK_CHANGED"


def test_high_rate_bridge_holds_phase_on_blurred_frame_without_new_baseline() -> None:
    image = _image()
    bridge = HighRateStemPhaseBridge(
        HighRatePhaseConfig(minimum_blur_score=5, minimum_matched_features=4)
    )
    bridge.publish_seed(_phase_seed(image, sequence=1))
    initialized = bridge.process_frame(
        HighRatePhaseFrame(image, 1, _timestamp(1), time.perf_counter())
    )
    blurred = bridge.process_frame(
        HighRatePhaseFrame(
            np.full_like(image, 80),
            2,
            _timestamp(2),
            time.perf_counter(),
        )
    )

    assert initialized.phase_locked
    assert blurred.tracking_mode == "HOLDING"
    assert not blurred.phase_locked
    assert blurred.cumulative_raw_phase == initialized.cumulative_raw_phase
    assert "PHASE_FRAME_BLURRED" in blurred.reason_codes


def _phase_seed(
    image: np.ndarray,
    *,
    sequence: int,
    session_id: str = "session-fast",
) -> StemPhaseSeed:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    mask = np.zeros(gray.shape, dtype=np.uint8)
    cv2.rectangle(mask, (100, 160), (540, 320), 255, -1)
    return StemPhaseSeed(
        track_id="track-fast",
        session_id=session_id,
        frame_sequence=sequence,
        timestamp=_timestamp(sequence),
        gray=gray,
        stem_mask=mask,
        axis=_axis_observation_for_test(),
        confidence=0.9,
        image_context_id="ctx_process_rectified",
        intrinsic_calibration_hash="a" * 64,
        process_calibration_hash="b" * 64,
    )


def _axis_observation_for_test() -> StemAxisObservation:
    return StemAxisObservation(
        center_x_px=320,
        center_y_px=240,
        direction_x=1,
        direction_y=0,
        length_px=440,
        median_width_px=160,
        image_width=640,
        image_height=480,
    )


def test_overlay_contains_the_calibrated_surface_tracking_roi() -> None:
    profile = _process_profile()
    execution = StemSurfacePhaseHandler().execute(
        _context(),
        _node(),
        {
            "frame": _frame(profile, _image(), 1),
            "segments": _segments(profile, track_id="track-a"),
            "stem_geometry": _geometry(1, track_id="track-a"),
        },
    )

    overlay = execution.outputs["overlay"]
    surface_roi = next(
        primitive
        for primitive in overlay.primitives
        if primitive.layer == "stem_surface_tracking_roi"
    )
    assert surface_roi.primitive_type == "polygon"
    assert surface_roi.label == "Surface Tracking ROI"
    assert len(surface_roi.geometry["points"]) >= 3
    phase_label = next(
        primitive for primitive in overlay.primitives if primitive.layer == "stem_surface_phase"
    )
    assert phase_label.metadata["eligible"] is True
    assert phase_label.metadata["reference_eligible"] is True
    assert phase_label.metadata["measurement_eligible"] is False
    assert phase_label.metadata["phase_locked"] is True
    assert phase_label.metadata["track_id"] == "track-a"
    assert phase_label.metadata["frame_sequence"] == 1
    assert phase_label.metadata["raw_phase"] == pytest.approx(0.0)
    assert phase_label.metadata["tracking_mode"] == "INITIALIZING"
    assert phase_label.metadata["trace_generation"] == 0
    assert phase_label.metadata["failure_count"] == 0
    assert phase_label.metadata["detected_feature_count"] >= 0
    assert phase_label.metadata["diameter_mm"] > 0
    assert "diameter=" in phase_label.label
    assert "mm" in phase_label.label
    assert phase_label.metadata["image_center_x_px"] > 0
    assert phase_label.metadata["image_center_y_px"] > 0


def test_incremental_phase_survives_short_gap_without_inventing_degrees() -> None:
    profile = _process_profile()
    handler = StemSurfacePhaseHandler()
    context = _context()
    node = _node()

    baseline = _execute(handler, context, node, profile, _image(), 1)
    moved = _execute(handler, context, node, profile, _image(texture_shift_y=8), 3)

    assert isinstance(baseline, StemSurfacePhasePayload)
    assert baseline.tracking_mode == "INITIALIZING"
    assert moved.raw_phase_valid and moved.phase_locked
    assert moved.tracking_mode == "INCREMENTAL"
    assert moved.metadata["phase_calibration_capture"]["measurement_eligible"] is True
    assert moved.cumulative_raw_phase is not None and moved.cumulative_raw_phase > 0
    assert not moved.calibrated_phase_valid
    assert moved.delta_phase_deg is None and moved.cumulative_phase_deg is None
    assert "PHASE_CALIBRATION_MISSING" in moved.reason_codes

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
        .execute(context, result_node, {"result": moved})
        .outputs["result"]
    )
    assert projected.result["stem_surface_phase"]["tracking_mode"] == "INCREMENTAL"
    assert projected.result["cumulative_phase_deg"] is None
    assert projected.decision is False


def test_stationary_active_stem_holds_phase_and_refreshes_baseline() -> None:
    profile = _process_profile()
    handler = StemSurfacePhaseHandler()
    context = _context()
    node = _node()

    baseline = _execute(
        handler,
        context,
        node,
        profile,
        _image(),
        1,
        motion_speed_mm_per_frame=0.0,
        process_motion_axis="stationary",
    )
    held = _execute(
        handler,
        context,
        node,
        profile,
        _image(texture_shift_y=8),
        2,
        motion_speed_mm_per_frame=0.0,
        process_motion_axis="stationary",
    )

    assert baseline.cumulative_raw_phase == pytest.approx(0.0)
    assert held.raw_phase_valid and held.phase_locked
    assert held.tracking_mode == "INCREMENTAL"
    assert held.delta_raw_phase == pytest.approx(0.0)
    assert held.cumulative_raw_phase == pytest.approx(0.0)
    assert held.direction == "stationary"
    assert "PHASE_STATIONARY_HOLD" in held.reason_codes
    assert held.metadata["phase_calibration_capture"]["measurement_eligible"] is False
    assert held.metadata["phase_calibration_capture"]["motion_gate_status"] == (
        "stationary_hold"
    )


def test_camera_sequence_jump_with_short_elapsed_time_remains_incremental() -> None:
    profile = _process_profile()
    handler = StemSurfacePhaseHandler()
    context = _context()
    node = _node()

    baseline = _execute(handler, context, node, profile, _image(), 1)
    moved = _execute(handler, context, node, profile, _image(texture_shift_y=8), 77)

    assert baseline.trace_generation == 0
    assert moved.tracking_mode == "INCREMENTAL"
    assert moved.trace_generation == 0
    assert moved.cumulative_raw_phase is not None and moved.cumulative_raw_phase > 0


def test_immediate_keyframe_relock_does_not_create_a_new_trace() -> None:
    profile = _process_profile()
    handler = StemSurfacePhaseHandler()
    context = _context()
    node = _node()

    baseline = _execute(handler, context, node, profile, _image(), 1)
    relocked = _execute(handler, context, node, profile, _image(), 200)

    assert baseline.trace_generation == 0
    assert relocked.tracking_mode == "KEYFRAME_RELOCK"
    assert relocked.trace_generation == 0
    capture = relocked.metadata["phase_calibration_capture"]
    assert capture["matched_feature_count"] >= 4
    assert capture["detected_feature_count"] >= 4


def test_impact_feature_loss_is_explicit_and_track_switch_resets_state() -> None:
    profile = _process_profile()
    handler = StemSurfacePhaseHandler()
    context = _context()
    node = _node()

    _execute(handler, context, node, profile, _image(), 1)
    lost = _execute(handler, context, node, profile, _image(blank=True), 10)
    switched = _execute(handler, context, node, profile, _image(), 11, track_id="track-b")

    assert not lost.phase_locked
    assert lost.tracking_mode == "LOST"
    assert lost.cumulative_raw_phase is None
    assert "PHASE_TRACK_LOST" in lost.reason_codes
    assert switched.track_id == "track-b"
    assert switched.tracking_mode == "INITIALIZING"
    assert switched.cumulative_raw_phase == 0
    assert "PHASE_TRACK_CHANGED_RESET" in switched.reason_codes


def test_valid_bound_calibration_is_the_only_path_to_degree_values() -> None:
    process_profile = _process_profile()
    computation = calculate_stem_surface_phase_calibration(
        source_channel_id="chn_stem",
        request=_phase_request().model_copy(
            update={
                "intrinsic_calibration_hash": process_profile.intrinsic_calibration_hash,
                "process_calibration_hash": process_profile.calibration_hash,
            }
        ),
        generated_at="2026-08-04T12:00:00Z",
    )
    assert computation.profile is not None
    phase_profile = computation.profile
    handler = StemSurfacePhaseHandler()
    context = _context()
    node = _node().model_copy(
        update={
            "config": {
                **_node().config,
                "surface_phase_calibration": phase_profile.model_dump(mode="json"),
            }
        }
    )

    _execute(handler, context, node, process_profile, _image(), 1)
    moved = _execute(handler, context, node, process_profile, _image(texture_shift_y=8), 2)

    assert moved.raw_phase_valid and moved.calibrated_phase_valid
    assert moved.delta_raw_phase is not None
    assert moved.delta_phase_deg == pytest.approx(moved.delta_raw_phase * 360, rel=1e-6)
    assert moved.cumulative_phase_deg is not None
