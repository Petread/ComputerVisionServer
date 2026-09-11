from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cv_contracts import SegmentationPoint, SegmentationPrediction
from cv_contracts.runtime import RuntimeNodeSpec
from cv_runtime import BUILTIN_NODE_TYPE_BY_KEY
from cv_runtime.context import RuntimeExecutionContext
from cv_runtime.engine import _inference_timings
from cv_runtime.loader import load_runtime_handler
from cv_runtime.nodes.stem_orientation import (
    ActiveStemTrackerHandler,
    _tracker_config,
    _tracker_process_metadata,
    _tracker_process_motion_axis,
    _tracker_session_start_gate_status,
)
from cv_runtime.payloads import (
    FramePayload,
    OverlayPayload,
    RuntimeImageContext,
    RuntimeImageTransform,
    RuntimeObjectReference,
    SegmentPayload,
)
from cv_runtime.stem_process_coordinates import (
    calculate_stem_curvature_process_calibration,
    process_points_to_image,
)
from cv_status import RuntimeMode
from tests.unit.backend.test_stem_process_coordinates import process_calibration_request

_BASE_TIMESTAMP = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)


def _rectangle(
    center_x: float,
    *,
    center_y: float = 0.5,
    length: float = 0.3,
    width: float = 0.08,
) -> list[SegmentationPoint]:
    half_length = length * 0.5
    half_width = width * 0.5
    return [
        SegmentationPoint(x=center_x - half_length, y=center_y - half_width),
        SegmentationPoint(x=center_x + half_length, y=center_y - half_width),
        SegmentationPoint(x=center_x + half_length, y=center_y + half_width),
        SegmentationPoint(x=center_x - half_length, y=center_y + half_width),
    ]


def _segments_payload(
    centers: list[float],
    *,
    sequence: int,
    scores: list[float] | None = None,
    lengths: list[float] | None = None,
    center_ys: list[float] | None = None,
) -> SegmentPayload:
    resolved_scores = scores or [0.9] * len(centers)
    resolved_lengths = lengths or [0.3] * len(centers)
    resolved_center_ys = center_ys or [0.5] * len(centers)
    polygons = [
        _rectangle(center, center_y=center_y, length=length)
        for center, center_y, length in zip(
            centers,
            resolved_center_ys,
            resolved_lengths,
            strict=True,
        )
    ]
    segments = [
        SegmentationPrediction(label="stem", score=score, polygon=polygon)
        for score, polygon in zip(resolved_scores, polygons, strict=True)
    ]
    objects = [
        RuntimeObjectReference(
            object_id=f"segment_{sequence}_{index}",
            object_type="segment",
            source_image_context_id=f"ctx_{sequence}",
            geometry={
                "points": [point.model_dump(mode="json") for point in polygon],
            },
            coordinate_system="normalized_polygon",
            label="stem",
            score=score,
            producer_node_key="segment",
        )
        for index, (score, polygon) in enumerate(
            zip(resolved_scores, polygons, strict=True),
            start=1,
        )
    ]
    return SegmentPayload(
        segments=segments,
        image_width=1000,
        image_height=600,
        image_context_id=f"ctx_{sequence}",
        objects=objects,
        route={"model_version_id": "mvr_stem_segmentation"},
    )


def _node(*, config: dict[str, object] | None = None) -> RuntimeNodeSpec:
    definition = BUILTIN_NODE_TYPE_BY_KEY["analysis.active_stem_tracker"]
    return RuntimeNodeSpec(
        node_key="active_stem",
        node_type=definition.node_type,
        node_category=definition.node_category,
        node_contract_version=definition.contract_version,
        input_ports=definition.input_ports,
        output_ports=definition.output_ports,
        config={
            **definition.default_config,
            "transfer_rois": [
                {"label": "transfer", "x": 0.0, "y": 0.0, "width": 0.55, "height": 1.0}
            ],
            "longitudinal_conveyor_rois": [
                {
                    "label": "conveyor",
                    "x": 0.45,
                    "y": 0.0,
                    "width": 0.55,
                    "height": 1.0,
                }
            ],
            "class_filter": "stem",
            "handover_reference_mode": "manual_point",
            "expected_handover_x": 0.25,
            "expected_handover_y": 0.5,
            "expected_motion_direction": "right",
            **(config or {}),
        },
    )


def _context() -> RuntimeExecutionContext:
    return RuntimeExecutionContext(
        correlation_id="corr_active_stem",
        pipeline_version_id="piv_active_stem",
        runtime_execution_id="rex_active_stem",
        channel_id="channel_stem",
        mode=RuntimeMode.REALTIME,
        state={},
        metadata={"live_frame_sequence": 0},
    )


def _execute(
    handler: ActiveStemTrackerHandler,
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    centers: list[float],
    *,
    sequence: int,
    lengths: list[float] | None = None,
    center_ys: list[float] | None = None,
) -> SegmentPayload:
    context.metadata["live_frame_sequence"] = sequence
    frame = FramePayload(
        image_base64="",
        width=1000,
        height=600,
        timestamp=(_BASE_TIMESTAMP + timedelta(milliseconds=100 * sequence)).isoformat(),
        metadata={"sequence": sequence},
    )
    result = handler.execute(
        context,
        node,
        {
            "segments": _segments_payload(
                centers,
                sequence=sequence,
                lengths=lengths,
                center_ys=center_ys,
            ),
            "frame": frame,
        },
    )
    assert isinstance(result.outputs["segments"], SegmentPayload)
    if node.config.get("debug_overlay"):
        overlay = result.outputs["overlay"]
        assert isinstance(overlay, OverlayPayload)
        assert "point" in {primitive.primitive_type for primitive in overlay.primitives}
        assert {primitive.primitive_type for primitive in overlay.primitives} & {
            "polygon",
            "zone",
        }
        if centers:
            assert any(primitive.primitive_type == "polygon" for primitive in overlay.primitives)
    return result.outputs["segments"]


def _tracking(payload: SegmentPayload) -> dict[str, object]:
    value = payload.metadata["active_stem_tracking"]
    assert isinstance(value, dict)
    return value


def _execute_calibrated(
    handler: ActiveStemTrackerHandler,
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    profile: object,
    center: tuple[float, float] | None,
    *,
    sequence: int,
) -> tuple[SegmentPayload, OverlayPayload | None]:
    context.metadata["live_frame_sequence"] = sequence
    centers = [center[0]] if center is not None else []
    center_ys = [center[1]] if center is not None else []
    source = _segments_payload(
        centers,
        center_ys=center_ys,
        sequence=sequence,
        lengths=[0.08] * len(centers),
    ).model_copy(
        update={
            "image_width": profile.image_width,
            "image_height": profile.image_height,
        }
    )
    frame = FramePayload(
        image_base64="",
        width=profile.image_width,
        height=profile.image_height,
        timestamp=(_BASE_TIMESTAMP + timedelta(milliseconds=100 * sequence)).isoformat(),
        image_context=RuntimeImageContext(
            context_id=f"ctx_{sequence}",
            width=profile.image_width,
            height=profile.image_height,
            transform=RuntimeImageTransform(
                transform_type="perspective_rectify",
                coordinate_projection="homography",
                parameters={"process_calibration_hash": profile.calibration_hash},
            ),
        ),
        metadata={
            "sequence": sequence,
            "process_calibration_hash": profile.calibration_hash,
            "stem_curvature_process_calibration": profile.model_dump(mode="json"),
        },
    )
    result = handler.execute(context, node, {"segments": source, "frame": frame})
    tracked = result.outputs["segments"]
    assert isinstance(tracked, SegmentPayload)
    overlay = result.outputs.get("overlay")
    assert overlay is None or isinstance(overlay, OverlayPayload)
    return tracked, overlay


def test_registry_loader_and_standard_designer_fields_expose_tracker() -> None:
    definition = BUILTIN_NODE_TYPE_BY_KEY["analysis.active_stem_tracker"]

    assert [(port.key, port.data_type, port.required) for port in definition.input_ports] == [
        ("segments", "segments", True),
        ("frame", "frame", False),
        ("events", "events", False),
        ("trigger", "trigger", False),
    ]
    assert [(port.key, port.data_type, port.required) for port in definition.output_ports] == [
        ("segments", "segments", True),
        ("overlay", "overlay", False),
    ]
    assert definition.compatibility_constraints["stateful"] is True
    assert definition.runtime_handler_ref == (
        "cv_runtime.nodes.stem_orientation:analysis_active_stem_tracker"
    )
    fields = {field.key: field for field in definition.property_schema}
    assert fields["transfer_rois"].field_type == "roi"
    assert fields["longitudinal_conveyor_rois"].field_type == "roi"
    assert fields["transfer_y_motion_sensitivity"].field_type == "number"
    assert fields["longitudinal_motion_window_frames"].field_type == "integer"
    assert fields["longitudinal_minimum_forward_displacement_mm"].field_type == "number"
    assert fields["longitudinal_minimum_forward_progress_ratio"].field_type == "number"
    assert fields["longitudinal_entry_centroid_required"].field_type == "boolean"
    assert definition.default_config["transfer_y_motion_sensitivity"] == pytest.approx(0.727273)
    assert definition.default_config["longitudinal_motion_window_frames"] == 6
    assert definition.default_config["longitudinal_minimum_forward_displacement_mm"] == 120
    assert {field.field_type for field in fields.values()} <= {
        "boolean",
        "integer",
        "number",
        "roi",
        "select",
        "text",
    }
    handler = load_runtime_handler(
        definition.runtime_handler_ref,
        expected_node_type=definition.node_type,
    )
    assert isinstance(handler, ActiveStemTrackerHandler)


def test_transfer_y_motion_sensitivity_controls_mixed_process_motion() -> None:
    insensitive = _tracker_config(
        _node(
            config={
                "minimum_process_motion_mm_per_frame": 0.1,
                "transfer_y_motion_sensitivity": 0.2,
            }
        ).config
    )
    sensitive = _tracker_config(
        _node(
            config={
                "minimum_process_motion_mm_per_frame": 0.1,
                "transfer_y_motion_sensitivity": 0.9,
            }
        ).config
    )

    assert _tracker_process_motion_axis((2.0, 3.0), config=insensitive)[0] == "ambiguous"
    assert _tracker_process_motion_axis((2.0, 3.0), config=sensitive)[0] == "transfer_y"


def test_strong_longitudinal_motion_has_priority_over_transfer_y_sensitivity() -> None:
    config = _tracker_config(
        _node(
            config={
                "minimum_process_motion_mm_per_frame": 0.1,
                "transfer_y_motion_sensitivity": 1.0,
            }
        ).config
    )

    assert _tracker_process_motion_axis((4.0, 1.0), config=config)[0] == "longitudinal_x"


def test_track_id_stays_stable_during_normal_motion_and_payload_serializes() -> None:
    handler = ActiveStemTrackerHandler()
    context = _context()
    node = _node()

    first = _execute(handler, context, node, [0.25], sequence=1)
    second = _execute(handler, context, node, [0.30], sequence=2)
    third = _execute(handler, context, node, [0.36], sequence=3)

    track_ids = [payload.objects[0].track_id for payload in (first, second, third)]
    assert track_ids[0]
    assert len(set(track_ids)) == 1
    assert all(len(payload.segments) == 1 for payload in (first, second, third))
    assert _tracking(third)["status"] == "active"
    assert _tracking(third)["selection_reason"] == "temporal_continuity"
    assert 0 < _tracking(third)["visible_length_fraction"] <= 1
    movement = _tracking(third)["movement"]
    assert isinstance(movement, dict)
    assert movement["movement_direction"] == "right"
    assert movement["velocity_x_normalized_per_frame"] > 0
    assert movement["absolute_roll_angle_claimed"] is False
    assert SegmentPayload.model_validate_json(third.model_dump_json()) == third
    assert third.segments[0] == _segments_payload([0.36], sequence=3).segments[0]

    tracker_state = context.state[node.node_key]
    assert "polygon" not in tracker_state["active_track"]
    assert "frame" not in tracker_state["active_track"]


def test_polygon_rois_follow_oblique_conveyor_and_empty_class_filter_accepts_model_label() -> None:
    handler = ActiveStemTrackerHandler()
    context = _context()
    node = _node(
        config={
            "transfer_rois": [
                {
                    "label": "right-side entry",
                    "points": [
                        {"x": 0.62, "y": 0.15},
                        {"x": 1.0, "y": 0.0},
                        {"x": 1.0, "y": 1.0},
                        {"x": 0.72, "y": 0.85},
                    ],
                }
            ],
            "longitudinal_conveyor_rois": [
                {
                    "label": "oblique conveyor",
                    "points": [
                        {"x": 0.0, "y": 0.25},
                        {"x": 0.72, "y": 0.1},
                        {"x": 0.82, "y": 0.9},
                        {"x": 0.0, "y": 0.75},
                    ],
                }
            ],
            "class_filter": "",
            "handover_reference_mode": "manual_point",
            "expected_handover_x": 0.82,
            "expected_handover_y": 0.5,
            "prefer_longitudinal_on_acquisition": False,
        }
    )

    tracked = _execute(handler, context, node, [0.35, 0.82], sequence=1)

    assert len(tracked.segments) == 1
    assert tracked.segments[0].label == "stem"
    assert _tracking(tracked)["process_zone"] == "transfer"
    result = handler.execute(
        _context(),
        node,
        {
            "segments": _segments_payload([0.82], sequence=2),
            "frame": FramePayload(image_base64="", width=1000, height=600),
        },
    )
    overlay = result.outputs["overlay"]
    assert isinstance(overlay, OverlayPayload)
    assert any(
        primitive.primitive_type == "polygon" and primitive.label == "right-side entry"
        for primitive in overlay.primitives
    )


def test_handover_reference_is_derived_from_transfer_roi_by_default() -> None:
    node = _node(
        config={
            "handover_reference_mode": "transfer_roi_center",
            "transfer_rois": [{"x": 0.7, "y": 0.1, "width": 0.3, "height": 0.8}],
            "longitudinal_conveyor_rois": [{"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}],
            "expected_handover_x": 0.0,
            "expected_handover_y": 0.0,
            "candidate_selection_policy": "legacy_handover_score",
        }
    )

    tracked = _execute(ActiveStemTrackerHandler(), _context(), node, [0.2, 0.82], sequence=1)

    assert len(tracked.segments) == 1
    assert _tracking(tracked)["centroid_normalized"][0] == pytest.approx(0.82)


def test_tracker_does_not_duplicate_upstream_inference_timing() -> None:
    source = _segments_payload([0.25], sequence=1).model_copy(
        update={"metadata": {"runtime_timings_ms": {"gateway_total_ms": 12.0}}}
    )
    result = ActiveStemTrackerHandler().execute(
        _context(),
        _node(),
        {
            "segments": source,
            "frame": FramePayload(image_base64="", width=1000, height=600),
        },
    )
    tracked = result.outputs["segments"]
    inference_node = SimpleNamespace(
        node_key="segment_stem",
        node_type="inference.segmentation",
        node_category=SimpleNamespace(value="inference"),
    )
    tracker_node = SimpleNamespace(
        node_key="active_stem",
        node_type="analysis.active_stem_tracker",
        node_category=SimpleNamespace(value="analysis"),
    )

    timings = _inference_timings(
        [
            (inference_node, "segments", source),
            (tracker_node, "segments", tracked),
        ]
    )

    assert [timing["node_key"] for timing in timings] == ["segment_stem"]


def test_temporary_occlusion_is_bridged_with_the_same_track_id() -> None:
    handler = ActiveStemTrackerHandler()
    context = _context()
    node = _node(config={"max_lost_frames": 2})

    first = _execute(handler, context, node, [0.25], sequence=1)
    moving = _execute(handler, context, node, [0.29], sequence=2)
    lost_one = _execute(handler, context, node, [], sequence=3)
    lost_two = _execute(handler, context, node, [], sequence=4)
    reacquired = _execute(handler, context, node, [0.36], sequence=5)

    track_id = first.objects[0].track_id
    assert moving.objects[0].track_id == track_id
    assert lost_one.segments == []
    assert lost_two.segments == []
    assert _tracking(lost_one)["status"] == "lost"
    assert _tracking(lost_two)["preserved_track_id"] == track_id
    assert reacquired.objects[0].track_id == track_id
    assert _tracking(reacquired)["selection_reason"] == ("reacquired_after_temporary_occlusion")


def test_calibrated_xy_motion_controls_monotonic_process_handover_and_reacquisition() -> None:
    computation = calculate_stem_curvature_process_calibration(
        source_channel_id="channel_stem",
        request=process_calibration_request(),
        generated_at="2026-08-04T10:00:00Z",
    )
    assert computation.profile is not None
    profile = computation.profile
    handler = ActiveStemTrackerHandler()
    context = _context()
    node = _node(
        config={
            "transfer_rois": [{"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}],
            "longitudinal_conveyor_rois": [{"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}],
            "minimum_visible_length_fraction": 0.1,
            "prefer_longitudinal_on_acquisition": True,
            "motion_smoothing_alpha": 1.0,
            "process_motion_axis_dominance_ratio": 1.2,
            "minimum_process_motion_mm_per_frame": 0.1,
            # Older stored pipelines may still contain two; calibrated tracking
            # enforces the safer three-frame minimum at runtime.
            "longitudinal_motion_confirmation_frames": 2,
            "max_lost_frames": 3,
            "complete_at_decision_gate": False,
        }
    )

    first, _ = _execute_calibrated(handler, context, node, profile, (0.14, 0.35), sequence=1)
    transfer_one, _ = _execute_calibrated(handler, context, node, profile, (0.14, 0.42), sequence=2)
    transfer_two, _ = _execute_calibrated(handler, context, node, profile, (0.14, 0.49), sequence=3)
    opposite_x, _ = _execute_calibrated(
        handler, context, node, profile, (0.10, 0.49), sequence=4
    )
    longitudinal_one, _ = _execute_calibrated(
        handler, context, node, profile, (0.14, 0.49), sequence=5
    )
    longitudinal_two, _ = _execute_calibrated(
        handler, context, node, profile, (0.18, 0.49), sequence=6
    )
    longitudinal_three, longitudinal_overlay = _execute_calibrated(
        handler, context, node, profile, (0.22, 0.49), sequence=7
    )

    track_id = first.objects[0].track_id
    assert _tracking(first)["process_zone"] == "transfer"
    assert _tracking(transfer_one)["movement"]["process_motion_axis"] == "transfer_y"
    assert _tracking(transfer_two)["process_zone"] == "transfer"
    assert _tracking(opposite_x)["movement"]["process_motion_axis"] == (
        "opposite_longitudinal_x"
    )
    assert _tracking(opposite_x)["process_zone"] == "transfer"
    assert _tracking(longitudinal_one)["movement"]["process_motion_axis"] == ("longitudinal_x")
    assert _tracking(longitudinal_one)["process_zone"] == "transfer"
    assert _tracking(longitudinal_two)["process_zone"] == "transfer"
    assert _tracking(longitudinal_three)["process_zone"] == "longitudinal_conveyor"
    assert _tracking(longitudinal_three)["process_zone_source"] == (
        "calibrated_spatial_temporal_motion_x"
    )
    movement = _tracking(longitudinal_three)["movement"]
    assert movement["longitudinal_motion_evidence_count"] >= 3
    assert movement["longitudinal_forward_displacement_mm"] >= 120
    assert movement["longitudinal_forward_progress_ratio"] == pytest.approx(1)
    assert movement["longitudinal_centroid_in_entry_roi"] is True
    assert movement["longitudinal_transition_confirmed"] is True
    assert longitudinal_three.objects[0].track_id == track_id
    assert longitudinal_overlay is not None
    selected_overlay = next(
        primitive
        for primitive in longitudinal_overlay.primitives
        if primitive.layer == "stem_tracking_candidates"
        and primitive.metadata.get("selected") is True
    )
    assert selected_overlay.metadata["display_role"] == "longitudinal_feed"
    assert selected_overlay.metadata["process_zone"] == "longitudinal_conveyor"
    assert selected_overlay.style["fill"] == "rgba(33,150,243,0.38)"

    lost, _ = _execute_calibrated(handler, context, node, profile, None, sequence=8)
    reacquired, _ = _execute_calibrated(handler, context, node, profile, (0.24, 0.49), sequence=9)
    lateral_after_handover, _ = _execute_calibrated(
        handler, context, node, profile, (0.24, 0.55), sequence=10
    )

    assert _tracking(lost)["preserved_track_id"] == track_id
    assert reacquired.objects[0].track_id == track_id
    assert _tracking(reacquired)["selection_reason"] == ("reacquired_after_temporary_occlusion")
    assert _tracking(reacquired)["process_zone"] == "longitudinal_conveyor"
    assert _tracking(lateral_after_handover)["movement"]["process_motion_axis"] == ("transfer_y")
    assert _tracking(lateral_after_handover)["process_zone"] == ("longitudinal_conveyor")


def test_calibrated_transfer_x_jitter_does_not_confirm_longitudinal_transport() -> None:
    computation = calculate_stem_curvature_process_calibration(
        source_channel_id="channel_stem",
        request=process_calibration_request(),
        generated_at="2026-08-04T10:00:00Z",
    )
    assert computation.profile is not None
    handler = ActiveStemTrackerHandler()
    context = _context()
    node = _node(
        config={
            "transfer_rois": [{"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}],
            "longitudinal_conveyor_rois": [{"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}],
            "minimum_visible_length_fraction": 0.1,
            "motion_smoothing_alpha": 1.0,
            "minimum_process_motion_mm_per_frame": 0.1,
            "longitudinal_motion_window_frames": 6,
            "longitudinal_minimum_forward_displacement_mm": 120.0,
            "longitudinal_minimum_forward_progress_ratio": 0.75,
            "complete_at_decision_gate": False,
        }
    )

    points = (
        (0.14, 0.35),
        (0.17, 0.36),
        (0.13, 0.38),
        (0.16, 0.40),
        (0.14, 0.43),
        (0.17, 0.45),
    )
    payloads = [
        _execute_calibrated(
            handler,
            context,
            node,
            computation.profile,
            point,
            sequence=sequence,
        )[0]
        for sequence, point in enumerate(points, start=1)
    ]

    assert all(_tracking(payload)["process_zone"] == "transfer" for payload in payloads)
    movement = _tracking(payloads[-1])["movement"]
    assert movement["longitudinal_motion_evidence_count"] < 3
    assert movement["longitudinal_transition_confirmed"] is False


def test_calibrated_longitudinal_motion_waits_for_spatial_entry_gate() -> None:
    computation = calculate_stem_curvature_process_calibration(
        source_channel_id="channel_stem",
        request=process_calibration_request(),
        generated_at="2026-08-04T10:00:00Z",
    )
    assert computation.profile is not None
    handler = ActiveStemTrackerHandler()
    context = _context()
    node = _node(
        config={
            "transfer_rois": [{"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}],
            "longitudinal_conveyor_rois": [
                {"x": 0.20, "y": 0.0, "width": 0.10, "height": 1.0}
            ],
            "minimum_visible_length_fraction": 0.1,
            "motion_smoothing_alpha": 1.0,
            "minimum_process_motion_mm_per_frame": 0.1,
            "longitudinal_motion_window_frames": 6,
            "longitudinal_minimum_forward_displacement_mm": 120.0,
            "longitudinal_minimum_forward_progress_ratio": 0.75,
            "longitudinal_entry_centroid_required": True,
            "complete_at_decision_gate": False,
        }
    )

    transfer = None
    for sequence, x_position in enumerate((0.05, 0.09, 0.13, 0.17), start=1):
        transfer, _ = _execute_calibrated(
            handler,
            context,
            node,
            computation.profile,
            (x_position, 0.5),
            sequence=sequence,
        )
    assert transfer is not None
    assert _tracking(transfer)["process_zone"] == "transfer"
    assert _tracking(transfer)["movement"]["longitudinal_centroid_in_entry_roi"] is False
    assert _tracking(transfer)["movement"]["longitudinal_forward_displacement_mm"] >= 120

    longitudinal, _ = _execute_calibrated(
        handler,
        context,
        node,
        computation.profile,
        (0.21, 0.5),
        sequence=5,
    )

    assert _tracking(longitudinal)["process_zone"] == "longitudinal_conveyor"
    assert _tracking(longitudinal)["movement"]["longitudinal_centroid_in_entry_roi"] is True
    assert _tracking(longitudinal)["movement"]["longitudinal_transition_confirmed"] is True


def test_transfer_only_candidate_cannot_inherit_active_longitudinal_track() -> None:
    computation = calculate_stem_curvature_process_calibration(
        source_channel_id="channel_stem",
        request=process_calibration_request(),
        generated_at="2026-08-04T10:00:00Z",
    )
    assert computation.profile is not None
    profile = computation.profile
    handler = ActiveStemTrackerHandler()
    context = _context()
    node = _node(
        config={
            "transfer_rois": [{"x": 0.0, "y": 0.0, "width": 0.18, "height": 1.0}],
            "longitudinal_conveyor_rois": [
                {"x": 0.18, "y": 0.0, "width": 0.12, "height": 1.0}
            ],
            "minimum_visible_length_fraction": 0.1,
            "minimum_continuity_score": 0.35,
            "complete_at_decision_gate": False,
        }
    )

    active_payload, _ = _execute_calibrated(
        handler, context, node, profile, (0.18, 0.5), sequence=1
    )
    active_state = context.state[node.node_key]["active_track"]
    active_state["process_stage"] = 1
    active_state["process_zone"] = "longitudinal_conveyor"
    active_state["process_zone_source"] = "calibrated_process_motion_x"
    active_state["velocity"] = [0.0, 0.0]
    active_state["process_velocity_mm_per_frame"] = [0.0, 0.0]

    waiting_transfer, _ = _execute_calibrated(
        handler, context, node, profile, (0.14, 0.5), sequence=2
    )

    assert active_payload.objects[0].track_id
    assert waiting_transfer.segments == []
    assert _tracking(waiting_transfer)["selection_reason"] == "temporary_occlusion"
    assert _tracking(waiting_transfer)["preserved_track_id"] == (
        active_payload.objects[0].track_id
    )


def test_stem_switch_after_lost_timeout_creates_a_new_track_id() -> None:
    handler = ActiveStemTrackerHandler()
    context = _context()
    node = _node(config={"max_lost_frames": 1})

    first = _execute(handler, context, node, [0.25], sequence=1)
    _execute(handler, context, node, [], sequence=2)
    timed_out = _execute(handler, context, node, [0.78], sequence=3)
    switched = _execute(handler, context, node, [0.78], sequence=4)

    assert timed_out.segments == []
    assert _tracking(timed_out)["selection_reason"] == "track_lost_timeout"
    assert len(switched.segments) == 1
    assert switched.objects[0].track_id
    assert switched.objects[0].track_id != first.objects[0].track_id
    state = context.state[node.node_key]
    assert state["completed_tracks"][0]["track_id"] == first.objects[0].track_id
    assert state["completed_tracks"][0]["completion_reason"] == "track_lost_timeout"


def test_equal_candidates_fail_closed_without_claiming_an_active_instance() -> None:
    handler = ActiveStemTrackerHandler()
    context = _context()
    node = _node(config={"ambiguity_margin": 0.02})

    ambiguous = _execute(handler, context, node, [0.18, 0.32], sequence=1)

    tracking = _tracking(ambiguous)
    assert ambiguous.segments == []
    assert ambiguous.objects == []
    assert tracking["status"] == "ambiguous"
    assert tracking["track_id"] is None
    assert tracking["selection_reason"] == "multiple_equally_plausible_candidates"
    assert tracking["fail_closed"] is True


def test_largest_mask_is_selected_per_region_and_other_regions_do_not_make_it_ambiguous() -> None:
    handler = ActiveStemTrackerHandler()
    context = _context()
    node = _node(
        config={
            "minimum_visible_length_fraction": 0.1,
            "minimum_region_area_dominance_ratio": 1.1,
        }
    )

    tracked = _execute(
        handler,
        context,
        node,
        [0.18, 0.36, 0.76, 0.88],
        sequence=1,
        lengths=[0.18, 0.36, 0.42, 0.20],
    )

    metadata = _tracking(tracked)
    assert len(tracked.segments) == 1
    assert metadata["status"] == "active"
    assert metadata["candidate_selection_policy"] == "largest_per_process_region"
    assert metadata["process_zone"] == "longitudinal_conveyor"
    assert metadata["centroid_normalized"][0] == pytest.approx(0.76)
    assert metadata["candidate_count"] == 4
    assert metadata["non_active_candidate_count"] == 3


def test_tracker_overlay_colors_transfer_green_and_longitudinal_feed_blue() -> None:
    handler = ActiveStemTrackerHandler()
    context = _context()
    node = _node(
        config={
            "debug_overlay": True,
            "minimum_visible_length_fraction": 0.1,
        }
    )
    first_frame = FramePayload(
        image_base64="",
        width=1000,
        height=600,
        metadata={"sequence": 1},
    )

    context.metadata["live_frame_sequence"] = 1
    handler.execute(
        context,
        node,
        {
            "segments": _segments_payload([0.2], sequence=1, lengths=[0.2]),
            "frame": first_frame,
        },
    )

    context.metadata["live_frame_sequence"] = 2
    result = handler.execute(
        context,
        node,
        {
            "segments": _segments_payload(
                [0.22, 0.8],
                sequence=2,
                lengths=[0.2, 0.2],
            ),
            "frame": first_frame.model_copy(update={"metadata": {"sequence": 2}}),
        },
    )

    overlay = result.outputs["overlay"]
    assert isinstance(overlay, OverlayPayload)
    candidates = [
        primitive
        for primitive in overlay.primitives
        if primitive.layer == "stem_tracking_candidates"
    ]
    assert len(candidates) == 2
    by_role = {primitive.metadata["display_role"]: primitive for primitive in candidates}
    transfer = by_role["rotation_observation"]
    longitudinal = by_role["longitudinal_feed"]
    assert transfer.style == {
        "stroke": "#49c47a",
        "stroke_width": 3.0,
        "fill": "rgba(73,196,122,0.38)",
    }
    assert transfer.metadata["selected"] is True
    assert longitudinal.style == {
        "stroke": "#2196f3",
        "stroke_width": 2.0,
        "fill": "rgba(33,150,243,0.38)",
    }
    assert longitudinal.metadata["selected"] is False


def test_longitudinal_region_wins_when_legacy_transfer_roi_covers_full_view() -> None:
    tracked = _execute(
        ActiveStemTrackerHandler(),
        _context(),
        _node(
            config={
                "transfer_rois": [{"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}],
                "longitudinal_conveyor_rois": [{"x": 0.55, "y": 0.0, "width": 0.45, "height": 1.0}],
                "minimum_visible_length_fraction": 0.1,
            }
        ),
        [0.25, 0.75],
        sequence=1,
        lengths=[0.4, 0.4],
    )

    assert len(tracked.segments) == 1
    assert _tracking(tracked)["process_zone"] == "longitudinal_conveyor"
    assert _tracking(tracked)["centroid_normalized"][0] == pytest.approx(0.75)


def test_existing_active_track_keeps_priority_over_a_larger_waiting_successor() -> None:
    handler = ActiveStemTrackerHandler()
    context = _context()
    node = _node(config={"minimum_visible_length_fraction": 0.1})

    first = _execute(
        handler,
        context,
        node,
        [0.72, 0.25],
        sequence=1,
        lengths=[0.4, 0.3],
    )
    continued = _execute(
        handler,
        context,
        node,
        [0.75, 0.25],
        sequence=2,
        lengths=[0.35, 0.5],
    )

    assert continued.objects[0].track_id == first.objects[0].track_id
    assert _tracking(continued)["centroid_normalized"][0] == pytest.approx(0.75)
    assert _tracking(continued)["selection_reason"] == "temporal_continuity"


def test_separate_transfer_and_longitudinal_candidates_do_not_drop_existing_track() -> None:
    handler = ActiveStemTrackerHandler()
    context = _context()
    node = _node(
        config={
            "minimum_visible_length_fraction": 0.1,
            "ambiguity_margin": 0.1,
        }
    )

    first = _execute(handler, context, node, [0.5], sequence=1)
    continued = _execute(handler, context, node, [0.32, 0.68], sequence=2)

    assert len(continued.segments) == 1
    assert continued.objects[0].track_id == first.objects[0].track_id
    assert _tracking(continued)["status"] == "active"
    assert _tracking(continued)["selection_reason"] == "temporal_continuity"


def test_short_previous_remnant_is_ignored_when_next_transfer_stem_is_acquired() -> None:
    tracked = _execute(
        ActiveStemTrackerHandler(),
        _context(),
        _node(config={"minimum_visible_length_fraction": 0.55}),
        [0.78, 0.25],
        sequence=1,
        lengths=[0.12, 0.42],
    )

    assert len(tracked.segments) == 1
    assert _tracking(tracked)["process_zone"] == "transfer"
    assert _tracking(tracked)["centroid_normalized"][0] == pytest.approx(0.25)


def test_rectified_process_frame_applies_calibrated_candidate_roi_and_session_gate() -> None:
    computation = calculate_stem_curvature_process_calibration(
        source_channel_id="channel_stem",
        request=process_calibration_request(),
        generated_at="2026-08-04T10:00:00Z",
    )
    assert computation.profile is not None
    profile = computation.profile
    frame = FramePayload(
        image_base64="",
        width=profile.image_width,
        height=profile.image_height,
        image_context=RuntimeImageContext(
            context_id="ctx_1",
            width=profile.image_width,
            height=profile.image_height,
            transform=RuntimeImageTransform(
                transform_type="perspective_rectify",
                coordinate_projection="homography",
                parameters={"process_calibration_hash": profile.calibration_hash},
            ),
        ),
        metadata={
            "process_calibration_hash": profile.calibration_hash,
            "stem_curvature_process_calibration": profile.model_dump(mode="json"),
        },
    )
    segments = _segments_payload([0.2, 0.7], sequence=1).model_copy(
        update={
            "image_width": profile.image_width,
            "image_height": profile.image_height,
        }
    )

    result = ActiveStemTrackerHandler().execute(
        _context(),
        _node(),
        {"segments": segments, "frame": frame},
    )
    tracked = result.outputs["segments"]
    overlay = result.outputs["overlay"]

    assert isinstance(tracked, SegmentPayload)
    assert isinstance(overlay, OverlayPayload)
    assert len(tracked.segments) == 1
    metadata = _tracking(tracked)
    assert metadata["process_calibration_applied"] is True
    assert metadata["process_calibration_hash"] == profile.calibration_hash
    assert metadata["session_start_gate_normalized"]
    assert metadata["curvature_freeze_gate_normalized"]
    assert metadata["decision_gate_normalized"]
    assert metadata["session_start_gate_reached"] is True
    assert metadata["curvature_freeze_gate_reached"] is False
    assert metadata["decision_gate_reached"] is False
    assert metadata["decision_margin_entered"] is False
    assert metadata["remaining_distance_to_decision_gate_mm"] > 0
    assert metadata["rotation_reference_view"] == "LOOKING_IN_CONVEYOR_DIRECTION"
    assert metadata["positive_rotation"] == "CLOCKWISE"
    assert metadata["process_gate_reference"] == "leading_stem_edge"
    assert metadata["leading_x_mm"] is not None
    assert any(primitive.layer == "stem_tracking_process_gates" for primitive in overlay.primitives)
    session_overlay = next(
        primitive for primitive in overlay.primitives if primitive.label == "Session start gate"
    )
    expected_session_gate = process_points_to_image(
        [profile.session_start_gate.process_start, profile.session_start_gate.process_end],
        profile,
    )
    assert session_overlay.geometry["start"]["x"] == pytest.approx(
        expected_session_gate[0].x_px / (profile.image_width - 1)
    )
    assert session_overlay.geometry["start"]["y"] == pytest.approx(
        expected_session_gate[0].y_px / (profile.image_height - 1)
    )


def test_decision_gate_completes_previous_track_and_acquires_visible_successor() -> None:
    computation = calculate_stem_curvature_process_calibration(
        source_channel_id="channel_stem",
        request=process_calibration_request(gate_x=(40.0, 140.0, 170.0)),
        generated_at="2026-08-04T10:00:00Z",
    )
    assert computation.profile is not None
    profile = computation.profile
    handler = ActiveStemTrackerHandler()
    context = _context()
    node = _node(
        config={
            "transfer_rois": [{"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}],
            "longitudinal_conveyor_rois": [{"x": 0.15, "y": 0.0, "width": 0.85, "height": 1.0}],
            "minimum_visible_length_fraction": 0.1,
            "complete_at_decision_gate": True,
        }
    )

    def execute(centers: list[float], *, sequence: int) -> SegmentPayload:
        context.metadata["live_frame_sequence"] = sequence
        source = _segments_payload(
            centers,
            sequence=sequence,
            lengths=[0.12] * len(centers),
        ).model_copy(
            update={
                "image_width": profile.image_width,
                "image_height": profile.image_height,
            }
        )
        frame = FramePayload(
            image_base64="",
            width=profile.image_width,
            height=profile.image_height,
            timestamp=(_BASE_TIMESTAMP + timedelta(milliseconds=100 * sequence)).isoformat(),
            image_context=RuntimeImageContext(
                context_id=f"ctx_{sequence}",
                width=profile.image_width,
                height=profile.image_height,
                transform=RuntimeImageTransform(
                    transform_type="perspective_rectify",
                    coordinate_projection="homography",
                    parameters={"process_calibration_hash": profile.calibration_hash},
                ),
            ),
            metadata={
                "sequence": sequence,
                "process_calibration_hash": profile.calibration_hash,
                "stem_curvature_process_calibration": profile.model_dump(mode="json"),
            },
        )
        result = handler.execute(context, node, {"segments": source, "frame": frame})
        return result.outputs["segments"]

    first = execute([0.10], sequence=1)
    final_decision_frame = execute([0.22], sequence=2)
    first_track_id = first.objects[0].track_id

    assert final_decision_frame.objects[0].track_id == first_track_id
    assert _tracking(final_decision_frame)["decision_gate_reached"] is True
    state = context.state[node.node_key]
    assert "active_track" not in state
    assert state["completed_tracks"][-1]["completion_reason"] == ("process_decision_gate_reached")

    successor = execute([0.25, 0.08], sequence=3)

    assert len(successor.segments) == 1
    assert successor.objects[0].track_id != first_track_id
    assert _tracking(successor)["centroid_normalized"][0] == pytest.approx(0.08)


def test_stem_is_not_reacquired_as_a_new_transfer_track_after_curvature_freeze() -> None:
    computation = calculate_stem_curvature_process_calibration(
        source_channel_id="channel_stem",
        request=process_calibration_request(gate_x=(40.0, 140.0, 170.0)),
        generated_at="2026-08-04T10:00:00Z",
    )
    assert computation.profile is not None
    profile = computation.profile
    handler = ActiveStemTrackerHandler()
    context = _context()
    node = _node(
        config={
            "transfer_rois": [{"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}],
            "longitudinal_conveyor_rois": [
                {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}
            ],
            "minimum_visible_length_fraction": 0.1,
            "max_lost_frames": 0,
            "complete_at_decision_gate": True,
        }
    )

    first, _ = _execute_calibrated(handler, context, node, profile, (0.10, 0.5), sequence=1)
    frozen, _ = _execute_calibrated(handler, context, node, profile, (0.18, 0.5), sequence=2)
    lost, _ = _execute_calibrated(handler, context, node, profile, None, sequence=3)
    rejected, _ = _execute_calibrated(handler, context, node, profile, (0.22, 0.5), sequence=4)

    assert first.objects[0].track_id == frozen.objects[0].track_id
    assert _tracking(frozen)["curvature_freeze_gate_reached"] is True
    assert _tracking(frozen)["decision_gate_reached"] is False
    assert _tracking(lost)["selection_reason"] == "track_lost_timeout"
    assert rejected.segments == []
    assert _tracking(rejected)["selection_reason"] == (
        "candidates_past_curvature_freeze_gate"
    )


def test_process_gate_metadata_uses_calibrated_conveyor_direction_and_margin() -> None:
    geometry = SimpleNamespace(
        calibration_hash="process-hash",
        process_profile=None,
        session_start_gate=((0.2, 0.0), (0.2, 1.0)),
        curvature_freeze_gate=((0.5, 0.0), (0.5, 1.0)),
        decision_gate=((0.8, 0.0), (0.8, 1.0)),
        decision_margin_mm=100.0,
        process_x_span_mm=1000.0,
        session_start_gate_x_mm=200.0,
        curvature_freeze_gate_x_mm=500.0,
        decision_gate_x_mm=800.0,
        conveyor_direction="POSITIVE_X",
        rotation_reference_view="LOOKING_IN_CONVEYOR_DIRECTION",
        positive_rotation="CLOCKWISE",
    )
    metadata = _tracker_process_metadata(
        SimpleNamespace(process_geometry=geometry),  # type: ignore[arg-type]
        centroid=(0.75, 0.5),
    )

    assert metadata["session_start_gate_reached"] is True
    assert metadata["curvature_freeze_gate_reached"] is True
    assert metadata["decision_gate_reached"] is False
    assert metadata["decision_margin_entered"] is True
    assert metadata["remaining_distance_to_decision_gate_mm"] == pytest.approx(50)


def test_canonical_corridor_detects_lateral_entry_across_right_boundary() -> None:
    geometry = SimpleNamespace(
        process_profile=SimpleNamespace(measurement_geometry_mode="CANONICAL_VIEW_CORRIDOR_V1"),
        candidate_rois=(SimpleNamespace(points=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))),),
        session_start_gate=((1.0, 0.0), (1.0, 1.0)),
    )

    waiting = _tracker_session_start_gate_status(
        (1.05, 0.5),
        ((1.02, 0.35), (1.08, 0.35), (1.08, 0.65), (1.02, 0.65)),
        geometry,  # type: ignore[arg-type]
    )
    entering = _tracker_session_start_gate_status(
        (1.0, 0.5),
        ((1.04, 0.35), (0.98, 0.35), (0.98, 0.65), (1.04, 0.65)),
        geometry,  # type: ignore[arg-type]
    )
    inside = _tracker_session_start_gate_status(
        (0.9, 0.5),
        ((0.84, 0.35), (0.96, 0.35), (0.96, 0.65), (0.84, 0.65)),
        geometry,  # type: ignore[arg-type]
    )

    assert waiting[0] is False
    assert waiting[1] == pytest.approx(-0.02)
    assert entering[0] is True
    assert entering[1] == pytest.approx(0.02)
    assert inside[0] is True
    assert inside[2] == "active_mask_corridor_boundary"


def test_realtime_metrics_report_tracker_as_stateful_and_serialized() -> None:
    from realtime_executor.live import (
        _live_diagnostics,
        _pipeline_max_inflight,
        _stateful_pipeline_nodes,
    )

    settings = SimpleNamespace(
        runtime=SimpleNamespace(realtime_pipeline_max_inflight=4),
        inference_limits=SimpleNamespace(
            realtime_max_concurrency=4,
            realtime_per_release_max_concurrency=4,
        ),
    )
    stateful_nodes = _stateful_pipeline_nodes(
        [
            {"node_key": "source", "node_type": "source.channel_frame"},
            {
                "node_key": "active_stem",
                "node_type": "analysis.active_stem_tracker",
            },
        ]
    )
    max_inflight = _pipeline_max_inflight(
        settings,
        {},
        stateful_nodes=stateful_nodes,
        inference_nodes=[],
    )
    diagnostics = _live_diagnostics(
        settings=settings,
        source_type="file_test",
        source_config={},
        graph_nodes=[
            {
                "node_key": "active_stem",
                "node_type": "analysis.active_stem_tracker",
            }
        ],
        sample_every_n=1,
        max_fps=0.0,
        pipeline_max_inflight=max_inflight,
        stateful_nodes=stateful_nodes,
        inference_nodes=[],
        stage_timings_ms={},
        pipeline_timing={},
    )

    assert stateful_nodes == ["active_stem"]
    assert max_inflight == 1
    assert diagnostics.pipeline_stateful_nodes == ["active_stem"]
    assert diagnostics.pipeline_max_inflight == 1
    assert diagnostics.pipeline_inflight_limit_reason == "stateful_pipeline"
    assert any("serialized" in warning for warning in diagnostics.warnings)
