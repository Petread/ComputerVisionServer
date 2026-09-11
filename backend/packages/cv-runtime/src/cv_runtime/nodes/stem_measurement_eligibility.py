from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from cv_contracts import SegmentationPrediction
from cv_contracts.runtime import RuntimeNodeSpec

from cv_runtime.context import NodeInputs, RuntimeExecutionContext
from cv_runtime.handlers import NodeExecutionResult
from cv_runtime.payloads import (
    FramePayload,
    OverlayPayload,
    OverlayPrimitive,
    ResultPayload,
    SegmentPayload,
)


class StemMeasurementEligibilityHandler:
    node_type = "analysis.stem_measurement_eligibility"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        frame = inputs.get("frame")
        all_segments = inputs.get("segments")
        active_stem = inputs.get("active_stem")
        if not isinstance(frame, FramePayload) or not isinstance(all_segments, SegmentPayload):
            return _result(
                node,
                eligible=False,
                task_role="REJECTED",
                reason_codes=["STEM_CAPTURE_INPUT_MISSING"],
            )
        if active_stem is not None and not isinstance(active_stem, SegmentPayload):
            active_stem = None
        config = node.config or {}
        expected_profile_id = str(config.get("view_profile_id") or "").strip()
        actual_profile_id = str(frame.metadata.get("view_profile_id") or "").strip()
        reasons: list[str] = []
        if not actual_profile_id:
            reasons.append("STEM_CAPTURE_CANONICAL_VIEW_REQUIRED")
        elif expected_profile_id and actual_profile_id != expected_profile_id:
            reasons.append("STEM_CAPTURE_VIEW_PROFILE_MISMATCH")
        if frame.metadata.get("canonical_model_view") is not True:
            reasons.append("STEM_CAPTURE_PIPELINE_IMAGE_OUTPUT_REQUIRED")

        segment_count = len(all_segments.segments)
        corridor = _corridor_polygon(config.get("measurement_corridor"))
        stems_in_corridor = [
            segment
            for segment in all_segments.segments
            if _point_in_polygon(_segment_centroid(segment), corridor)
        ]
        active = active_stem if isinstance(active_stem, SegmentPayload) else all_segments
        tracking = _tracking_metadata(active)
        active_count = len(active.segments)
        filtered_stem_count = (
            int(tracking["candidate_count"])
            if tracking.get("candidate_count") is not None
            else segment_count
        )
        selection_policy = str(tracking.get("candidate_selection_policy") or "").strip()
        multiple_candidates_resolved = (
            selection_policy == "largest_per_process_region"
            and tracking.get("status") == "active"
            and active_count == 1
        )
        negative_allowed = bool(config.get("allow_negative_frames", True))
        if segment_count == 0:
            task_role = "NEGATIVE"
            if not negative_allowed:
                reasons.append("STEM_CAPTURE_EMPTY_VIEW_DISABLED")
            negative_kind = str(config.get("negative_frame_kind") or "EMPTY_CORRIDOR")
            if negative_kind not in {
                "EMPTY_CORRIDOR",
                "MACHINE_MOTION",
                "LIGHT_CHANGE",
                "WOOD_DEBRIS",
            }:
                reasons.append("STEM_CAPTURE_NEGATIVE_KIND_INVALID")
        else:
            task_role = "POSITIVE"
            multiple_stems = (
                bool(config.get("reject_if_multiple_stems_in_view", True))
                and filtered_stem_count > 1
                and not multiple_candidates_resolved
            )
            if multiple_stems:
                reasons.append("STEM_CAPTURE_MULTIPLE_STEMS_IN_VIEW")
            elif active_count != 1 or (
                filtered_stem_count != 1 and not multiple_candidates_resolved
            ):
                reasons.append("STEM_CAPTURE_SINGLE_ACTIVE_STEM_REQUIRED")
            if tracking.get("status") != "active":
                reasons.append("STEM_CAPTURE_ACTIVE_TRACK_UNCLEAR")
            else:
                if int(tracking.get("track_age_frames") or 0) < int(
                    config.get("min_stable_frames") or 3
                ):
                    reasons.append("STEM_CAPTURE_TRACK_NOT_STABLE")
                visible_ratio = float(tracking.get("visible_length_fraction") or 0.0)
                if visible_ratio < float(config.get("min_visible_axis_ratio") or 0.55):
                    reasons.append("STEM_CAPTURE_VISIBLE_AXIS_TOO_SHORT")
                if tracking.get("curvature_freeze_gate_reached") is True:
                    reasons.append("STEM_CAPTURE_AFTER_CURVATURE_FREEZE")
                boundary = _crowd_boundary(config.get("crowd_boundary"))
                if (
                    bool(config.get("reject_if_touching_crowd_boundary", True))
                    and active.segments
                    and _segment_touches_boundary(
                        active.segments[0],
                        boundary,
                        tolerance=float(config.get("crowd_boundary_tolerance") or 0.01),
                    )
                ):
                    reasons.append("STEM_CAPTURE_CROWD_BOUNDARY_TOUCHED")

        blur_score, focus_measure = _blur_score(frame)
        if blur_score is None:
            reasons.append("STEM_CAPTURE_BLUR_SCORE_UNAVAILABLE")
        elif blur_score > float(config.get("max_blur_score") or 0.55):
            reasons.append("STEM_CAPTURE_MOTION_BLUR_EXCEEDED")

        eligible = not reasons and (task_role == "POSITIVE" or negative_allowed)
        payload = _result(
            node,
            eligible=eligible,
            task_role=task_role,
            reason_codes=reasons,
            values={
                "schema_version": "stem.measurement-eligibility.v1",
                "status": "ACCEPTED" if eligible else "REJECTED",
                "capture_source": "PIPELINE_IMAGE_OUTPUT",
                "view_profile_id": actual_profile_id or None,
                "view_profile_checksum": frame.metadata.get("view_profile_checksum"),
                "output_width": frame.width,
                "output_height": frame.height,
                "camera_calibration_id": frame.metadata.get("camera_calibration_id"),
                "source_frame_context_id": frame.metadata.get("source_frame_context_id"),
                "source_frame_timestamp": frame.metadata.get("source_frame_timestamp"),
                "track_id": tracking.get("track_id"),
                "track_age_frames": int(tracking.get("track_age_frames") or 0),
                "visible_axis_ratio": float(tracking.get("visible_length_fraction") or 0.0),
                "segment_count": segment_count,
                "filtered_stem_candidate_count": filtered_stem_count,
                "candidate_selection_policy": selection_policy or None,
                "non_active_candidate_count": int(
                    tracking.get("non_active_candidate_count") or 0
                ),
                "multiple_candidates_resolved": multiple_candidates_resolved,
                "stem_count_in_measurement_corridor": len(stems_in_corridor),
                "active_track_status": tracking.get("status"),
                "active_track_selection_reason": tracking.get("selection_reason"),
                "blur_score": blur_score,
                "focus_measure": focus_measure,
                "negative_frame_kind": (
                    str(config.get("negative_frame_kind") or "EMPTY_CORRIDOR")
                    if task_role == "NEGATIVE"
                    else None
                ),
                "frame_timestamp": frame.timestamp,
                "frame_eligible": eligible,
                "reason_codes": reasons,
            },
        )
        outputs: dict[str, Any] = {"result": payload}
        if bool(config.get("debug_overlay", True)):
            outputs["overlay"] = _overlay(
                frame,
                corridor=corridor,
                boundary=_crowd_boundary(config.get("crowd_boundary")),
                eligible=eligible,
                reasons=reasons,
            )
        return NodeExecutionResult(outputs=outputs)


def _result(
    node: RuntimeNodeSpec,
    *,
    eligible: bool,
    task_role: str,
    reason_codes: list[str],
    values: dict[str, Any] | None = None,
) -> ResultPayload:
    return ResultPayload(
        decision=eligible,
        result={
            "result": eligible,
            "eligible": eligible,
            "task_role": task_role,
            "reason_codes": reason_codes,
            "eligibility_node_key": node.node_key,
            **(values or {}),
        },
        metadata={
            "schema_version": "stem.measurement-eligibility.v1",
            "fail_closed": True,
        },
    )


def _tracking_metadata(payload: SegmentPayload) -> Mapping[str, Any]:
    value = payload.metadata.get("active_stem_tracking")
    return value if isinstance(value, Mapping) else {}


def _corridor_polygon(value: object) -> list[tuple[float, float]]:
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, Mapping):
        points = value.get("points")
        if isinstance(points, list) and len(points) >= 3:
            parsed = [_point(item) for item in points]
            if all(item is not None for item in parsed):
                return [item for item in parsed if item is not None]
        source = value.get("box") if isinstance(value.get("box"), Mapping) else value
        try:
            x = float(source.get("x", 0.0))
            y = float(source.get("y", 0.0))
            width = float(source.get("width", 1.0))
            height = float(source.get("height", 1.0))
            if width > 0 and height > 0:
                return [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
        except (TypeError, ValueError):
            pass
    return [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]


def _crowd_boundary(value: object) -> tuple[tuple[float, float], tuple[float, float]]:
    if isinstance(value, Mapping):
        start = _point(value.get("start"))
        end = _point(value.get("end"))
        if start is not None and end is not None and start != end:
            return start, end
    return (0.98, 0.0), (0.98, 1.0)


def _point(value: object) -> tuple[float, float] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        x = float(value.get("x"))
        y = float(value.get("y"))
    except (TypeError, ValueError):
        return None
    if not (0 <= x <= 1 and 0 <= y <= 1):
        return None
    return x, y


def _segment_points(segment: SegmentationPrediction) -> list[tuple[float, float]]:
    return [(float(point.x), float(point.y)) for point in segment.polygon]


def _segment_centroid(segment: SegmentationPrediction) -> tuple[float, float]:
    points = _segment_points(segment)
    if not points:
        return 0.0, 0.0
    return (
        sum(item[0] for item in points) / len(points),
        sum(item[1] for item in points) / len(points),
    )


def _point_in_polygon(point: tuple[float, float], polygon: Sequence[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    previous = polygon[-1]
    for current in polygon:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > y) != (y2 > y):
            crossing = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing:
                inside = not inside
        previous = current
    return inside


def _segment_touches_boundary(
    segment: SegmentationPrediction,
    boundary: tuple[tuple[float, float], tuple[float, float]],
    *,
    tolerance: float,
) -> bool:
    points = _segment_points(segment)
    start, end = boundary
    sides = [_line_side(point, start, end) for point in points]
    if sides and min(sides) <= 0 <= max(sides):
        return True
    return any(_point_line_distance(point, start, end) <= tolerance for point in points)


def _line_side(
    point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]
) -> float:
    return (end[0] - start[0]) * (point[1] - start[1]) - (
        end[1] - start[1]
    ) * (point[0] - start[0])


def _point_line_distance(
    point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]
) -> float:
    length = math.hypot(end[0] - start[0], end[1] - start[1])
    return abs(_line_side(point, start, end)) / max(length, 1e-12)


def _blur_score(frame: FramePayload) -> tuple[float | None, float | None]:
    try:
        import cv2
        import numpy as np

        cached = frame.cached_cv2_frame()
        image = np.asarray(cached) if cached is not None else cv2.imdecode(
            np.frombuffer(frame.image_bytes(), dtype=np.uint8), cv2.IMREAD_GRAYSCALE
        )
        if image is None:
            return None, None
        if image.ndim == 3:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        focus = float(cv2.Laplacian(image, cv2.CV_64F).var())
        return 1.0 / (1.0 + focus / 100.0), focus
    except Exception:
        return None, None


def _overlay(
    frame: FramePayload,
    *,
    corridor: list[tuple[float, float]],
    boundary: tuple[tuple[float, float], tuple[float, float]],
    eligible: bool,
    reasons: list[str],
) -> OverlayPayload:
    context_id = frame.image_context.context_id if frame.image_context else None
    return OverlayPayload(
        display_context_id=context_id,
        primitives=[
            OverlayPrimitive(
                primitive_type="polygon",
                layer="stem_measurement_corridor",
                label="Measurement corridor",
                geometry={
                    "points": [{"x": x, "y": y} for x, y in corridor],
                    "coordinate_system": "normalized_xy",
                },
                image_context_id=context_id,
                style={"stroke": "#00b8d4", "fill_opacity": 0.03},
            ),
            OverlayPrimitive(
                primitive_type="line",
                layer="stem_crowd_boundary",
                label="Crowd boundary",
                geometry={
                    "start": {"x": boundary[0][0], "y": boundary[0][1]},
                    "end": {"x": boundary[1][0], "y": boundary[1][1]},
                    "coordinate_system": "normalized_xy",
                },
                image_context_id=context_id,
                style={"stroke": "#ff9100", "stroke_width": 2},
            ),
            OverlayPrimitive(
                primitive_type="label",
                layer="stem_capture_eligibility",
                label="Capture eligible" if eligible else ", ".join(reasons),
                geometry={"x": 0.02, "y": 0.04, "coordinate_system": "normalized_xy"},
                image_context_id=context_id,
                style={"fill": "#00e676" if eligible else "#ff5252"},
                metadata={
                    "status": "ACCEPTED" if eligible else "REJECTED",
                    "frame_eligible": eligible,
                    "reason_codes": reasons,
                },
            ),
        ],
        metadata={"eligible": eligible, "reason_codes": reasons},
    )


analysis_stem_measurement_eligibility = StemMeasurementEligibilityHandler()
