from __future__ import annotations

import numpy as np
from cv_contracts import SegmentationPoint, SegmentationPrediction
from cv_contracts.runtime import RuntimeNodeSpec
from cv_runtime import BUILTIN_NODE_TYPE_BY_KEY
from cv_runtime.context import RuntimeExecutionContext
from cv_runtime.nodes.stem_measurement_eligibility import StemMeasurementEligibilityHandler
from cv_runtime.payloads import FramePayload, SegmentPayload
from cv_status import RuntimeMode


def _node(**overrides: object) -> RuntimeNodeSpec:
    definition = BUILTIN_NODE_TYPE_BY_KEY["analysis.stem_measurement_eligibility"]
    return RuntimeNodeSpec(
        node_key="eligibility",
        node_type=definition.node_type,
        node_category=definition.node_category,
        node_contract_version=definition.contract_version,
        input_ports=definition.input_ports,
        output_ports=definition.output_ports,
        config={
            **definition.default_config,
            "view_profile_id": "stem-measurement-view-v1",
            "measurement_corridor": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            "crowd_boundary": {"start": {"x": 0.8, "y": 0.0}, "end": {"x": 0.8, "y": 1.0}},
            "min_visible_axis_ratio": 0.55,
            "min_stable_frames": 3,
            "max_blur_score": 0.55,
            **overrides,
        },
    )


def test_canonical_view_is_the_default_corridor_without_a_second_roi_editor() -> None:
    definition = BUILTIN_NODE_TYPE_BY_KEY["analysis.stem_measurement_eligibility"]
    corridor = definition.default_config["measurement_corridor"]
    fields = {field.key: field for field in definition.property_schema}

    assert corridor == [
        {"label": "Measurement corridor", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}
    ]
    assert "measurement_corridor" not in fields
    assert definition.default_config["reject_if_multiple_stems_in_view"] is False
    assert fields["reject_if_multiple_stems_in_view"].field_type == "boolean"
    assert fields["reject_if_multiple_stems_in_view"].default_value is False


def _context() -> RuntimeExecutionContext:
    return RuntimeExecutionContext(
        correlation_id="corr_eligibility",
        pipeline_version_id="piv_eligibility",
        channel_id="chn_stem",
        mode=RuntimeMode.REALTIME,
    )


def _frame(*, sharp: bool = True) -> FramePayload:
    if sharp:
        yy, xx = np.indices((120, 200))
        gray = (((xx // 4 + yy // 4) % 2) * 255).astype(np.uint8)
    else:
        gray = np.full((120, 200), 127, dtype=np.uint8)
    image = np.repeat(gray[:, :, None], 3, axis=2)
    return FramePayload.from_cv2_frame(
        image,
        timestamp="2026-08-05T11:00:00Z",
        metadata={
            "canonical_model_view": True,
            "view_profile_id": "stem-measurement-view-v1",
            "view_profile_checksum": "b" * 64,
            "camera_calibration_id": "camera-v1",
            "source_frame_context_id": "ctx_raw",
            "source_frame_timestamp": "2026-08-05T11:00:00Z",
        },
    )


def _polygon(center_x: float, *, length: float = 0.3) -> list[SegmentationPoint]:
    half = length / 2
    return [
        SegmentationPoint(x=center_x - half, y=0.42),
        SegmentationPoint(x=center_x + half, y=0.42),
        SegmentationPoint(x=center_x + half, y=0.58),
        SegmentationPoint(x=center_x - half, y=0.58),
    ]


def _segments(*centers: float) -> SegmentPayload:
    return SegmentPayload(
        segments=[
            SegmentationPrediction(label="stem", score=0.95, polygon=_polygon(center))
            for center in centers
        ],
        image_width=200,
        image_height=120,
    )


def _active(
    center: float = 0.5,
    *,
    stable_frames: int = 4,
    visible_ratio: float = 0.8,
    frozen: bool = False,
) -> SegmentPayload:
    payload = _segments(center)
    return payload.model_copy(
        update={
            "metadata": {
                "active_stem_tracking": {
                    "status": "active",
                    "track_id": "stem-1",
                    "track_age_frames": stable_frames,
                    "visible_length_fraction": visible_ratio,
                    "curvature_freeze_gate_reached": frozen,
                }
            }
        }
    )


def _result(
    *,
    frame: FramePayload,
    segments: SegmentPayload,
    active: SegmentPayload | None,
    node: RuntimeNodeSpec | None = None,
) -> dict[str, object]:
    inputs: dict[str, object] = {"frame": frame, "segments": segments}
    if active is not None:
        inputs["active_stem"] = active
    payload = StemMeasurementEligibilityHandler().execute(
        _context(), node or _node(), inputs
    ).outputs["result"]
    return payload.result


def test_isolated_stable_stem_is_accepted_as_positive_capture() -> None:
    result = _result(frame=_frame(), segments=_segments(0.5), active=_active())

    assert result["eligible"] is True
    assert result["task_role"] == "POSITIVE"
    assert result["reason_codes"] == []
    assert result["capture_source"] == "PIPELINE_IMAGE_OUTPUT"


def test_crowd_contact_multiple_stems_blur_and_freeze_are_rejected() -> None:
    touching = _result(frame=_frame(), segments=_segments(0.65), active=_active(0.65))
    multiple = _result(
        frame=_frame(),
        segments=_segments(0.4, 0.65),
        active=_active(0.4),
        node=_node(reject_if_multiple_stems_in_view=True),
    )
    blurred = _result(frame=_frame(sharp=False), segments=_segments(0.5), active=_active())
    frozen = _result(frame=_frame(), segments=_segments(0.5), active=_active(frozen=True))

    assert touching["eligible"] is False
    assert "STEM_CAPTURE_CROWD_BOUNDARY_TOUCHED" in touching["reason_codes"]
    assert multiple["eligible"] is False
    assert "STEM_CAPTURE_MULTIPLE_STEMS_IN_VIEW" in multiple["reason_codes"]
    assert blurred["eligible"] is False
    assert "STEM_CAPTURE_MOTION_BLUR_EXCEEDED" in blurred["reason_codes"]
    assert frozen["eligible"] is False
    assert "STEM_CAPTURE_AFTER_CURVATURE_FREEZE" in frozen["reason_codes"]


def test_empty_corridor_is_an_explicit_negative_but_unstable_positive_is_not_exportable() -> None:
    empty = _result(
        frame=_frame(),
        segments=_segments(),
        active=None,
        node=_node(negative_frame_kind="EMPTY_CORRIDOR"),
    )
    unstable = _result(
        frame=_frame(),
        segments=_segments(0.5),
        active=_active(stable_frames=1, visible_ratio=0.4),
    )

    assert empty["eligible"] is True
    assert empty["task_role"] == "NEGATIVE"
    assert empty["negative_frame_kind"] == "EMPTY_CORRIDOR"
    assert unstable["eligible"] is False
    assert "STEM_CAPTURE_TRACK_NOT_STABLE" in unstable["reason_codes"]
    assert "STEM_CAPTURE_VISIBLE_AXIS_TOO_SHORT" in unstable["reason_codes"]


def test_ambiguous_tracker_does_not_emit_cascaded_stability_or_length_errors() -> None:
    ambiguous = _segments().model_copy(
        update={
            "metadata": {
                "active_stem_tracking": {
                    "status": "ambiguous",
                    "track_age_frames": 0,
                    "visible_length_fraction": 0.0,
                }
            }
        }
    )

    result = _result(
        frame=_frame(),
        segments=_segments(0.4, 0.65),
        active=ambiguous,
        node=_node(reject_if_multiple_stems_in_view=True),
    )

    assert result["reason_codes"] == [
        "STEM_CAPTURE_MULTIPLE_STEMS_IN_VIEW",
        "STEM_CAPTURE_ACTIVE_TRACK_UNCLEAR",
    ]


def test_segments_rejected_by_tracker_filter_do_not_count_as_multiple_stems() -> None:
    active = _active().model_copy(
        update={
            "metadata": {
                "active_stem_tracking": {
                    **_active().metadata["active_stem_tracking"],
                    "candidate_count": 1,
                    "selection_reason": "acquired_nearest_handover",
                }
            }
        }
    )

    result = _result(
        frame=_frame(),
        segments=_segments(0.5, 0.82),
        active=active,
    )

    assert result["eligible"] is True
    assert result["segment_count"] == 2
    assert result["filtered_stem_candidate_count"] == 1
    assert "STEM_CAPTURE_MULTIPLE_STEMS_IN_VIEW" not in result["reason_codes"]


def test_resolved_largest_region_track_allows_other_visible_stems() -> None:
    active = _active().model_copy(
        update={
            "metadata": {
                "active_stem_tracking": {
                    **_active().metadata["active_stem_tracking"],
                    "candidate_count": 3,
                    "candidate_selection_policy": "largest_per_process_region",
                    "non_active_candidate_count": 2,
                    "selection_reason": "temporal_continuity",
                }
            }
        }
    )

    result = _result(
        frame=_frame(),
        segments=_segments(0.2, 0.5, 0.82),
        active=active,
    )

    assert result["eligible"] is True
    assert result["filtered_stem_candidate_count"] == 3
    assert result["non_active_candidate_count"] == 2
    assert result["multiple_candidates_resolved"] is True
    assert "STEM_CAPTURE_MULTIPLE_STEMS_IN_VIEW" not in result["reason_codes"]


def test_noncanonical_frame_fails_closed() -> None:
    frame = _frame().model_copy(update={"metadata": {}})
    result = _result(frame=frame, segments=_segments(0.5), active=_active())

    assert result["eligible"] is False
    assert "STEM_CAPTURE_CANONICAL_VIEW_REQUIRED" in result["reason_codes"]
    assert "STEM_CAPTURE_PIPELINE_IMAGE_OUTPUT_REQUIRED" in result["reason_codes"]
