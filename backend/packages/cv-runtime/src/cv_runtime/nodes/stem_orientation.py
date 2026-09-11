from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from operator import attrgetter
from typing import Any, TypedDict

import numpy as np
from cv_contracts import SegmentationPrediction
from cv_contracts.runtime import (
    RuntimeNodeSpec,
    RuntimeStemCurvatureProcessCalibrationProfile,
)

from cv_runtime.context import NodeInputs, RuntimeExecutionContext
from cv_runtime.handlers import NodeExecutionResult
from cv_runtime.payloads import (
    EventsPayload,
    FramePayload,
    OverlayPayload,
    OverlayPrimitive,
    ProvenanceLink,
    RuntimeObjectReference,
    SegmentPayload,
    StemGeometryCrossSectionIssue,
    StemGeometryPayload,
    StemGeometryPoint,
    StemGeometryReason,
    StemGeometryReferenceAxis,
    TriggerPayload,
)
from cv_runtime.stem_geometry import (
    StemCrossSectionIssue,
    StemGeometryConfig,
    StemGeometryResult,
    StemImagePoint,
    analyze_stem_geometry,
)
from cv_runtime.stem_process_coordinates import (
    image_points_to_process,
    process_points_to_image,
    process_profile_for_rectified_view,
    validate_stem_curvature_process_calibration,
)

_FEATURE_SCHEMA_VERSION = "stem.geometry.v1"
_POINT_ATTRIBUTES = attrgetter("x", "y")
_REASON_MESSAGES = {
    "segments_input_missing": "The required SegmentPayload input is missing.",
    "no_matching_segment": "No segment matches the configured class and score filters.",
    "multiple_segments_without_selection": (
        "More than one matching stem remains without an unambiguous configured selection."
    ),
    "selection_strategy_invalid": "The configured segment selection strategy is unsupported.",
    "image_context_missing": "Image width and height are required for stem geometry.",
    "image_context_mismatch": (
        "The optional frame and segmentation payload refer to different image contexts."
    ),
    "configuration_invalid": "The stem geometry node configuration is invalid.",
}


@dataclass(frozen=True)
class _StemCandidate:
    index: int
    polygon: tuple[tuple[float, float], ...]
    coordinate_space: str
    label: str | None
    score: float
    source_object: RuntimeObjectReference | None
    selection_area: float


@dataclass(frozen=True)
class StemMaskGeometryHandler:
    node_type: str = "analysis.stem_mask_geometry"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        segment_payload = inputs.get("segments")
        frame = inputs.get("frame")
        if not isinstance(segment_payload, SegmentPayload):
            return _failure_result(
                context,
                node,
                segment_payload=None,
                frame=frame if isinstance(frame, FramePayload) else None,
                reason_code="segments_input_missing",
            )
        if frame is not None and not isinstance(frame, FramePayload):
            frame = None

        config = node.config or {}
        try:
            geometry_config = _geometry_config(config)
            minimum_score = _float_config(config, "minimum_score", 0.35)
            if not 0.0 <= minimum_score <= 1.0:
                raise ValueError("minimum_score must be between 0 and 1")
        except (TypeError, ValueError) as exc:
            return _failure_result(
                context,
                node,
                segment_payload=segment_payload,
                frame=frame,
                reason_code="configuration_invalid",
                reason_message=f"{_REASON_MESSAGES['configuration_invalid']} {exc}",
            )

        image_context_id = _image_context_id(segment_payload, frame)
        if _image_contexts_conflict(segment_payload, frame):
            return _failure_result(
                context,
                node,
                segment_payload=segment_payload,
                frame=frame,
                reason_code="image_context_mismatch",
                geometry_config=geometry_config,
            )

        image_width, image_height = _image_dimensions(segment_payload, frame)
        if image_width is None or image_height is None:
            return _failure_result(
                context,
                node,
                segment_payload=segment_payload,
                frame=frame,
                reason_code="image_context_missing",
                geometry_config=geometry_config,
            )

        candidates = _matching_candidates(
            segment_payload,
            class_filter=_class_filter(config),
            minimum_score=minimum_score,
        )
        selection_strategy = str(config.get("selection_strategy") or "largest_matching_segment")
        selected, selection_error = _select_candidate(
            candidates,
            strategy=selection_strategy,
        )
        if selected is None:
            return _failure_result(
                context,
                node,
                segment_payload=segment_payload,
                frame=frame,
                reason_code=selection_error or "no_matching_segment",
                geometry_config=geometry_config,
                matching_segment_count=len(candidates),
            )

        selected_geometry_config = replace(
            geometry_config,
            polygon_coordinate_space=selected.coordinate_space,
        )
        geometry = analyze_stem_geometry(
            selected.polygon,
            image_width=image_width,
            image_height=image_height,
            config=selected_geometry_config,
        )
        payload = _geometry_payload(
            context,
            node,
            segment_payload=segment_payload,
            frame=frame,
            candidate=selected,
            geometry=geometry,
            image_context_id=image_context_id,
            matching_segment_count=len(candidates),
            selection_strategy=selection_strategy,
        )
        outputs: dict[str, Any] = {"stem_geometry": payload}
        if bool(config.get("debug_overlay", True)):
            outputs["overlay"] = _geometry_overlay(payload, selected)
        return NodeExecutionResult(outputs=outputs)


def _geometry_config(config: Mapping[str, Any]) -> StemGeometryConfig:
    analysis_mode = str(config.get("analysis_mode") or "robust_full").strip().lower()
    if analysis_mode not in {"robust_full", "phase_tracking_fast"}:
        raise ValueError("analysis_mode must be robust_full or phase_tracking_fast")
    fast = analysis_mode == "phase_tracking_fast"
    centerline_count = 8 if fast else _int_config(config, "centerline_sample_count", 32)
    width_count = 8 if fast else _int_config(config, "width_sample_count", 16)
    end_trim_percent = _float_config(config, "end_trim_percent", 0.2)
    clipped_end_window_percent = _float_config(
        config,
        "clipped_end_window_percent",
        8.0,
    )
    return StemGeometryConfig(
        centerline_sample_count=centerline_count,
        width_sample_count=width_count,
        cross_section_count=(
            max(24, 2 * centerline_count, 2 * width_count)
            if fast
            else max(96, 2 * centerline_count, 2 * width_count)
        ),
        min_component_area_px=_int_config(config, "min_component_area_px", 300),
        min_visible_length_px=_float_config(config, "min_visible_length_px", 60.0),
        smoothing_passes=(0 if fast else _int_config(config, "smoothing_passes", 2)),
        longitudinal_trim_quantile=end_trim_percent / 100.0,
        clipped_end_window_fraction=clipped_end_window_percent / 100.0,
        polygon_rasterization_mode="oriented_roi" if fast else "full_image",
        axis_refinement_passes=0 if fast else 2,
    )


def _matching_candidates(
    payload: SegmentPayload,
    *,
    class_filter: set[str],
    minimum_score: float,
) -> list[_StemCandidate]:
    candidates: list[_StemCandidate] = []
    for index, segment in enumerate(payload.segments):
        source_object = payload.objects[index] if index < len(payload.objects) else None
        label = source_object.label if source_object and source_object.label else segment.label
        score = (
            float(source_object.score)
            if source_object is not None and source_object.score is not None
            else float(segment.score)
        )
        polygon = _point_pairs(segment.polygon)
        coordinate_space = "normalized"
        if len(polygon) < 3 and source_object is not None:
            polygon = _object_polygon(source_object)
            coordinate_space = _object_coordinate_space(source_object)
        candidate = _candidate(
            index=index,
            polygon=polygon,
            coordinate_space=coordinate_space,
            label=label,
            score=score,
            source_object=source_object,
            class_filter=class_filter,
            minimum_score=minimum_score,
        )
        if candidate is not None:
            candidates.append(candidate)

    if payload.segments:
        return candidates

    for index, source_object in enumerate(payload.objects):
        candidate = _candidate(
            index=index,
            polygon=_object_polygon(source_object),
            coordinate_space=_object_coordinate_space(source_object),
            label=source_object.label,
            score=float(source_object.score or 0.0),
            source_object=source_object,
            class_filter=class_filter,
            minimum_score=minimum_score,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _candidate(
    *,
    index: int,
    polygon: tuple[tuple[float, float], ...],
    coordinate_space: str,
    label: str | None,
    score: float,
    source_object: RuntimeObjectReference | None,
    class_filter: set[str],
    minimum_score: float,
) -> _StemCandidate | None:
    if len(polygon) < 3 or score < minimum_score:
        return None
    if class_filter and str(label or "").strip().casefold() not in class_filter:
        return None
    selection_area = _polygon_area(polygon)
    if selection_area <= 0:
        return None
    return _StemCandidate(
        index=index,
        polygon=polygon,
        coordinate_space=coordinate_space,
        label=label,
        score=score,
        source_object=source_object,
        selection_area=selection_area,
    )


def _select_candidate(
    candidates: Sequence[_StemCandidate],
    *,
    strategy: str,
) -> tuple[_StemCandidate | None, str | None]:
    if not candidates:
        return None, "no_matching_segment"
    if strategy == "single_matching_segment":
        if len(candidates) != 1:
            return None, "multiple_segments_without_selection"
        return candidates[0], None
    if strategy != "largest_matching_segment":
        return None, "selection_strategy_invalid"
    ranked = sorted(candidates, key=lambda item: (-item.selection_area, item.index))
    if len(ranked) > 1 and np.isclose(
        ranked[0].selection_area,
        ranked[1].selection_area,
        rtol=1e-6,
        atol=1e-9,
    ):
        return None, "multiple_segments_without_selection"
    return ranked[0], None


def _geometry_payload(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    *,
    segment_payload: SegmentPayload,
    frame: FramePayload | None,
    candidate: _StemCandidate,
    geometry: StemGeometryResult,
    image_context_id: str | None,
    matching_segment_count: int,
    selection_strategy: str,
) -> StemGeometryPayload:
    source_object = candidate.source_object
    provenance = _provenance(node, image_context_id, source_object)
    geometry_quality = float(geometry.quality_score)
    quality_score = min(geometry_quality, candidate.score) if geometry.valid else 0.0
    return StemGeometryPayload(
        valid=geometry.valid,
        track_id=source_object.track_id if source_object is not None else None,
        source_object_id=source_object.object_id if source_object is not None else None,
        source_label=candidate.label,
        segmentation_score=candidate.score,
        image_context_id=image_context_id,
        image_width=geometry.image_width if geometry.image_width > 0 else None,
        image_height=geometry.image_height if geometry.image_height > 0 else None,
        frame_sequence=_frame_sequence(context, frame),
        timestamp=_observation_timestamp(context, frame),
        reference_axis=_reference_axis(geometry),
        visible_endpoints=_visible_endpoints(geometry),
        normalized_s=list(geometry.normalized_s),
        centerline_image_xy=[_payload_point(point) for point in geometry.centerline_image_xy],
        residual_profile_px=list(geometry.residual_profile_px),
        residual_profile_normalized=list(geometry.residual_profile_normalized),
        residual_valid_mask=list(geometry.centerline_valid_mask),
        width_normalized_s=list(geometry.width_normalized_s),
        width_profile_px=list(geometry.width_profile_px),
        width_valid_mask=list(geometry.width_valid_mask),
        signed_curvature=geometry.signed_curvature,
        signed_bow_px=geometry.signed_bow_px,
        signed_bow_normalized=geometry.signed_curvature,
        rms_residual_px=geometry.rms_residual_px,
        max_abs_residual_px=geometry.max_abs_residual_px,
        peak_to_peak_px=geometry.peak_to_peak_px,
        median_width_px=geometry.median_width_px,
        rms_residual_normalized=geometry.rms_residual_normalized,
        visible_fraction=geometry.visible_length_fraction,
        valid_cross_section_fraction=geometry.valid_cross_section_fraction,
        shape_class=geometry.shape_class,
        quality_score=quality_score,
        quality_flags=[flag.value for flag in geometry.quality_flags],
        reason_codes=[code.value for code in geometry.error_codes],
        reasons=[
            StemGeometryReason(code=issue.code.value, message=issue.message)
            for issue in geometry.issues
        ],
        rejected_cross_sections=[
            _payload_cross_section_issue(issue) for issue in geometry.rejected_cross_sections
        ],
        uncertain_cross_sections=[
            _payload_cross_section_issue(issue) for issue in geometry.uncertain_cross_sections
        ],
        selected_component_area_px=geometry.selected_component_area_px,
        meaningful_component_count=geometry.meaningful_component_count,
        selected_component_fraction=geometry.selected_component_fraction,
        segmentation_route=dict(segment_payload.route),
        provenance=provenance,
        metadata={
            "node_key": node.node_key,
            "analysis_mode": str((node.config or {}).get("analysis_mode") or "robust_full"),
            "polygon_rasterization_mode": (
                "oriented_roi"
                if str((node.config or {}).get("analysis_mode") or "robust_full").strip().lower()
                == "phase_tracking_fast"
                else "full_image"
            ),
            "selection_strategy": selection_strategy,
            "matching_segment_count": matching_segment_count,
            "selected_segment_index": candidate.index,
            "geometry_quality_score": geometry_quality,
            "timestamp_source": _timestamp_source(frame),
            "projected_2d_only": True,
            "metric_calibration_applied": False,
            "feature_schema_version": _FEATURE_SCHEMA_VERSION,
        },
    )


def _failure_result(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    *,
    segment_payload: SegmentPayload | None,
    frame: FramePayload | None,
    reason_code: str,
    reason_message: str | None = None,
    geometry_config: StemGeometryConfig | None = None,
    matching_segment_count: int = 0,
) -> NodeExecutionResult:
    config = geometry_config or StemGeometryConfig()
    image_context_id = _image_context_id(segment_payload, frame)
    image_width, image_height = _image_dimensions(segment_payload, frame)
    normalized_s = np.linspace(0.0, 1.0, config.centerline_sample_count).tolist()
    width_s = np.linspace(0.0, 1.0, config.width_sample_count).tolist()
    payload = StemGeometryPayload(
        valid=False,
        image_context_id=image_context_id,
        image_width=image_width,
        image_height=image_height,
        frame_sequence=_frame_sequence(context, frame),
        timestamp=_observation_timestamp(context, frame),
        normalized_s=normalized_s,
        residual_valid_mask=[False] * config.centerline_sample_count,
        width_normalized_s=width_s,
        width_valid_mask=[False] * config.width_sample_count,
        reason_codes=[reason_code],
        reasons=[
            StemGeometryReason(
                code=reason_code,
                message=reason_message or _REASON_MESSAGES.get(reason_code, reason_code),
            )
        ],
        segmentation_route=dict(segment_payload.route) if segment_payload is not None else {},
        provenance=_provenance(node, image_context_id, None),
        metadata={
            "node_key": node.node_key,
            "selection_strategy": str(
                (node.config or {}).get("selection_strategy") or "largest_matching_segment"
            ),
            "matching_segment_count": matching_segment_count,
            "timestamp_source": _timestamp_source(frame),
            "projected_2d_only": True,
            "metric_calibration_applied": False,
            "feature_schema_version": _FEATURE_SCHEMA_VERSION,
        },
    )
    outputs: dict[str, Any] = {"stem_geometry": payload}
    if bool((node.config or {}).get("debug_overlay", True)):
        outputs["overlay"] = _geometry_overlay(payload, None)
    return NodeExecutionResult(outputs=outputs)


def _geometry_overlay(
    payload: StemGeometryPayload,
    candidate: _StemCandidate | None,
) -> OverlayPayload:
    primitives: list[OverlayPrimitive] = []
    common_metadata = {
        "feature_schema_version": payload.feature_schema_version,
        "projected_2d_only": True,
        "metric_calibration_applied": False,
    }
    if candidate is not None:
        primitives.append(
            OverlayPrimitive(
                primitive_type="polygon",
                layer="polygons",
                label=payload.source_label or "Selected stem",
                confidence=payload.segmentation_score,
                geometry={
                    "points": [{"x": point[0], "y": point[1]} for point in candidate.polygon],
                    "coordinate_system": (
                        "normalized_polygon"
                        if candidate.coordinate_space == "normalized"
                        else "pixel_xy"
                    ),
                },
                image_context_id=payload.image_context_id,
                object_id=payload.source_object_id,
                source_object_id=payload.source_object_id,
                style={
                    "stroke": "#5db7ff",
                    "fill": "rgba(93, 183, 255, 0.08)",
                    "stroke_width": 1.5,
                },
                metadata=common_metadata,
            )
        )

    if not payload.valid or payload.reference_axis is None:
        reason_label = ", ".join(payload.reason_codes) or "unknown"
        primitives.append(
            OverlayPrimitive(
                primitive_type="line",
                layer="lines",
                label=f"Stem geometry invalid: {reason_label}",
                confidence=0.0,
                geometry={
                    "start": {"x": 12.0, "y": 24.0},
                    "end": {"x": 96.0, "y": 24.0},
                    "coordinate_system": "pixel_xy",
                },
                image_context_id=payload.image_context_id,
                style={"stroke": "#ff5252", "stroke_width": 3.0},
                metadata={**common_metadata, "reason_codes": payload.reason_codes},
            )
        )
        return OverlayPayload(
            primitives=primitives,
            display_context_id=payload.image_context_id,
            metadata=common_metadata,
        )

    axis = payload.reference_axis
    bow = payload.signed_bow_normalized or 0.0
    primitives.append(
        OverlayPrimitive(
            primitive_type="axis",
            layer="lines",
            label=f"Projected bow {bow:+.3f} widths · quality {payload.quality_score:.2f}",
            confidence=payload.quality_score,
            geometry={
                "start": axis.start.model_dump(mode="json"),
                "end": axis.end.model_dump(mode="json"),
                "coordinate_system": "pixel_xy",
            },
            image_context_id=payload.image_context_id,
            object_id=payload.source_object_id,
            source_object_id=payload.source_object_id,
            style={"stroke": "#49c47a", "stroke_width": 3.0},
            metadata=common_metadata,
        )
    )
    for index, (start, end) in enumerate(
        zip(payload.centerline_image_xy, payload.centerline_image_xy[1:], strict=False)
    ):
        primitives.append(
            OverlayPrimitive(
                primitive_type="line",
                layer="lines",
                label="Robust stem centerline" if index == 0 else None,
                confidence=payload.quality_score,
                geometry={
                    "start": start.model_dump(mode="json"),
                    "end": end.model_dump(mode="json"),
                    "coordinate_system": "pixel_xy",
                },
                image_context_id=payload.image_context_id,
                source_object_id=payload.source_object_id,
                style={"stroke": "#00e5ff", "stroke_width": 2.0},
                metadata=common_metadata,
            )
        )
    for index, point in enumerate(payload.centerline_image_xy):
        valid = index < len(payload.residual_valid_mask) and payload.residual_valid_mask[index]
        primitives.append(
            OverlayPrimitive(
                primitive_type="point",
                layer="measurements",
                geometry={
                    "x": point.x,
                    "y": point.y,
                    "coordinate_system": "pixel_xy",
                },
                image_context_id=payload.image_context_id,
                source_object_id=payload.source_object_id,
                style={
                    "stroke": "#ffffff" if valid else "#ffab40",
                    "fill": "#ffffff" if valid else "#ffab40",
                    "radius": 2.0,
                },
                metadata={**common_metadata, "sample_index": index, "valid": valid},
            )
        )
    primitives.extend(_width_support_primitives(payload, common_metadata))
    primitives.extend(_issue_primitives(payload, common_metadata))
    return OverlayPayload(
        primitives=primitives,
        display_context_id=payload.image_context_id,
        metadata=common_metadata,
    )


def _width_support_primitives(
    payload: StemGeometryPayload,
    common_metadata: dict[str, Any],
) -> list[OverlayPrimitive]:
    if payload.reference_axis is None or len(payload.centerline_image_xy) < 2:
        return []
    center_s = np.asarray(payload.normalized_s, dtype=np.float64)
    center_x = np.asarray([point.x for point in payload.centerline_image_xy])
    center_y = np.asarray([point.y for point in payload.centerline_image_xy])
    normal_x = payload.reference_axis.normal.x
    normal_y = payload.reference_axis.normal.y
    primitives: list[OverlayPrimitive] = []
    for index, (normalized_s, width) in enumerate(
        zip(payload.width_normalized_s, payload.width_profile_px, strict=False)
    ):
        x = float(np.interp(normalized_s, center_s, center_x))
        y = float(np.interp(normalized_s, center_s, center_y))
        half_width = float(width) * 0.5
        valid = index < len(payload.width_valid_mask) and payload.width_valid_mask[index]
        primitives.append(
            OverlayPrimitive(
                primitive_type="line",
                layer="lines",
                geometry={
                    "start": {
                        "x": x - normal_x * half_width,
                        "y": y - normal_y * half_width,
                    },
                    "end": {
                        "x": x + normal_x * half_width,
                        "y": y + normal_y * half_width,
                    },
                    "coordinate_system": "pixel_xy",
                },
                image_context_id=payload.image_context_id,
                source_object_id=payload.source_object_id,
                style={
                    "stroke": "#7c4dff" if valid else "#ffab40",
                    "stroke_width": 1.0,
                    "stroke_opacity": 0.75,
                },
                metadata={
                    **common_metadata,
                    "width_sample_index": index,
                    "normalized_s": normalized_s,
                    "valid": valid,
                },
            )
        )
    return primitives


def _issue_primitives(
    payload: StemGeometryPayload,
    common_metadata: dict[str, Any],
) -> list[OverlayPrimitive]:
    if len(payload.centerline_image_xy) < 2:
        return []
    center_s = np.asarray(payload.normalized_s, dtype=np.float64)
    center_x = np.asarray([point.x for point in payload.centerline_image_xy])
    center_y = np.asarray([point.y for point in payload.centerline_image_xy])
    primitives: list[OverlayPrimitive] = []
    for status, color, issues in (
        ("rejected", "#ff5252", payload.rejected_cross_sections),
        ("uncertain", "#ffab40", payload.uncertain_cross_sections),
    ):
        for issue in issues:
            primitives.append(
                OverlayPrimitive(
                    primitive_type="point",
                    layer="measurements",
                    geometry={
                        "x": float(np.interp(issue.normalized_s, center_s, center_x)),
                        "y": float(np.interp(issue.normalized_s, center_s, center_y)),
                        "coordinate_system": "pixel_xy",
                    },
                    image_context_id=payload.image_context_id,
                    source_object_id=payload.source_object_id,
                    style={"stroke": color, "fill": color, "radius": 3.0},
                    metadata={
                        **common_metadata,
                        "cross_section_status": status,
                        "reason": issue.reason,
                        "normalized_s": issue.normalized_s,
                    },
                )
            )
    return primitives


def _reference_axis(geometry: StemGeometryResult) -> StemGeometryReferenceAxis | None:
    axis = geometry.reference_axis
    if axis is None:
        return None
    return StemGeometryReferenceAxis(
        start=_payload_point(axis.start),
        end=_payload_point(axis.end),
        direction=_payload_point(axis.direction),
        normal=_payload_point(axis.normal),
        length_px=axis.length_px,
    )


def _visible_endpoints(
    geometry: StemGeometryResult,
) -> tuple[StemGeometryPoint, StemGeometryPoint] | None:
    if geometry.visible_endpoints is None:
        return None
    start, end = geometry.visible_endpoints
    return _payload_point(start), _payload_point(end)


def _payload_point(point: StemImagePoint) -> StemGeometryPoint:
    return StemGeometryPoint(x=point.x, y=point.y)


def _payload_cross_section_issue(
    issue: StemCrossSectionIssue,
) -> StemGeometryCrossSectionIssue:
    return StemGeometryCrossSectionIssue(
        normalized_s=issue.normalized_s,
        reason=issue.reason.value,
        observed_width_px=issue.observed_width_px,
        expected_width_px=issue.expected_width_px,
    )


def _provenance(
    node: RuntimeNodeSpec,
    image_context_id: str | None,
    source_object: RuntimeObjectReference | None,
) -> list[ProvenanceLink]:
    links: list[ProvenanceLink] = []
    if image_context_id:
        links.append(
            ProvenanceLink(
                source_kind="image_context",
                source_id=image_context_id,
                relationship="analyzed_in_projected_image",
                node_key=node.node_key,
            )
        )
    if source_object is not None:
        links.append(
            ProvenanceLink(
                source_kind="object",
                source_id=source_object.object_id,
                relationship="geometry_derived_from_segment",
                node_key=node.node_key,
            )
        )
    return links


def _point_pairs(values: Sequence[object]) -> tuple[tuple[float, float], ...]:
    points: list[tuple[float, float]] = []
    for value in values:
        if isinstance(value, Mapping):
            x = value.get("x")
            y = value.get("y")
        else:
            try:
                x, y = _POINT_ATTRIBUTES(value)
            except AttributeError:
                continue
        try:
            point = (float(x), float(y))
        except (TypeError, ValueError):
            continue
        if np.isfinite(point).all():
            points.append(point)
    return tuple(points)


def _object_polygon(source_object: RuntimeObjectReference) -> tuple[tuple[float, float], ...]:
    raw = source_object.geometry.get("points") or source_object.geometry.get("polygon") or []
    return _point_pairs(raw if isinstance(raw, Sequence) else [])


def _object_coordinate_space(source_object: RuntimeObjectReference) -> str:
    coordinate_system = source_object.coordinate_system.casefold()
    return "normalized" if "normalized" in coordinate_system else "pixel"


def _polygon_area(polygon: Sequence[tuple[float, float]]) -> float:
    points = np.asarray(polygon, dtype=np.float64)
    x = points[:, 0]
    y = points[:, 1]
    return abs(float(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))) * 0.5


def _class_filter(config: Mapping[str, Any]) -> set[str]:
    raw = config.get("class_filter", config.get("class_labels", config.get("classes", "")))
    if isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
        values = raw
    else:
        values = str(raw or "").split(",")
    return {str(value).strip().casefold() for value in values if str(value).strip()}


def _image_dimensions(
    segment_payload: SegmentPayload | None,
    frame: FramePayload | None,
) -> tuple[int | None, int | None]:
    width = segment_payload.image_width if segment_payload is not None else None
    height = segment_payload.image_height if segment_payload is not None else None
    if frame is not None:
        width = width or frame.width
        height = height or frame.height
        if frame.image_context is not None:
            width = width or frame.image_context.width
            height = height or frame.image_context.height
    return width, height


def _image_context_id(
    segment_payload: SegmentPayload | None,
    frame: FramePayload | None,
) -> str | None:
    if segment_payload is not None and segment_payload.image_context_id:
        return segment_payload.image_context_id
    if frame is not None and frame.image_context is not None:
        return frame.image_context.context_id
    return None


def _image_contexts_conflict(
    segment_payload: SegmentPayload,
    frame: FramePayload | None,
) -> bool:
    if frame is None or frame.image_context is None or not segment_payload.image_context_id:
        return False
    return frame.image_context.context_id != segment_payload.image_context_id


def _frame_sequence(
    context: RuntimeExecutionContext,
    frame: FramePayload | None,
) -> int | None:
    raw = context.metadata.get("live_frame_sequence")
    if raw is None and frame is not None:
        raw = frame.metadata.get("sequence")
    try:
        value = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return value if value is None or value >= 0 else None


def _observation_timestamp(
    context: RuntimeExecutionContext,
    frame: FramePayload | None,
) -> str:
    if frame is not None and frame.timestamp:
        return frame.timestamp
    return context.now_iso()


def _timestamp_source(frame: FramePayload | None) -> str:
    return "frame" if frame is not None and frame.timestamp else "runtime_clock"


def _int_config(config: Mapping[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return int(value)


def _float_config(config: Mapping[str, Any], key: str, default: float) -> float:
    value = config.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a number")
    return float(value)


analysis_stem_mask_geometry = StemMaskGeometryHandler()


_TRACKER_SCHEMA_VERSION = "stem.active-track.v2"


@dataclass(frozen=True)
class _TrackerBox:
    x_min: float
    y_min: float
    x_max: float
    y_max: float

    @property
    def area(self) -> float:
        return max(self.x_max - self.x_min, 0.0) * max(
            self.y_max - self.y_min,
            0.0,
        )


@dataclass(frozen=True)
class _TrackerRegion:
    points: tuple[tuple[float, float], ...]
    bounds: _TrackerBox
    label: str | None = None
    source_shape: str = "polygon"


@dataclass(frozen=True)
class _TrackerCandidate:
    index: int
    segment: SegmentationPrediction
    source_object: RuntimeObjectReference | None
    score: float
    polygon: tuple[tuple[float, float], ...]
    box: _TrackerBox
    centroid: tuple[float, float]
    area: float
    projected_length: float
    projected_width: float
    axis: tuple[float, float]
    visible_length_fraction: float
    process_stage: int
    process_zone: str
    roi_overlap: float
    transfer_overlap: float
    longitudinal_overlap: float
    upstream_track_id: str | None


@dataclass(frozen=True)
class _TrackerProcessGeometry:
    calibration_hash: str
    process_profile: RuntimeStemCurvatureProcessCalibrationProfile
    candidate_rois: tuple[_TrackerRegion, ...]
    session_start_gate: tuple[tuple[float, float], tuple[float, float]]
    curvature_freeze_gate: tuple[tuple[float, float], tuple[float, float]]
    decision_gate: tuple[tuple[float, float], tuple[float, float]]
    decision_margin_mm: float
    process_x_span_mm: float
    session_start_gate_x_mm: float
    curvature_freeze_gate_x_mm: float
    decision_gate_x_mm: float
    conveyor_direction: str
    rotation_reference_view: str
    positive_rotation: str


class _TrackerLongitudinalMotionSample(TypedDict):
    frame_sequence: int
    axis: str
    forward_x_mm: float
    absolute_x_mm: float


@dataclass(frozen=True)
class _TrackerConfig:
    transfer_rois: tuple[_TrackerRegion, ...]
    longitudinal_rois: tuple[_TrackerRegion, ...]
    class_filter: frozenset[str]
    minimum_score: float
    expected_handover_point: tuple[float, float]
    expected_motion: tuple[float, float] | None
    minimum_roi_overlap: float
    expected_full_length_normalized: float
    minimum_visible_length_fraction: float
    maximum_centroid_distance: float
    minimum_acquisition_score: float
    minimum_continuity_score: float
    ambiguity_margin: float
    candidate_selection_policy: str
    minimum_region_area_dominance_ratio: float
    prefer_longitudinal_on_acquisition: bool
    complete_at_decision_gate: bool
    max_lost_frames: int
    reacquisition_minimum_score: float
    reacquisition_ambiguity_margin: float
    preserve_upstream_identity_during_occlusion: bool
    completed_exclusion_frames: int
    completed_exclusion_iou: float
    max_completed_tracks: int
    motion_smoothing_alpha: float
    process_motion_axis_dominance_ratio: float
    transfer_y_motion_sensitivity: float
    minimum_process_motion_mm_per_frame: float
    longitudinal_motion_confirmation_frames: int
    longitudinal_motion_window_frames: int
    longitudinal_minimum_forward_displacement_mm: float
    longitudinal_minimum_forward_progress_ratio: float
    longitudinal_entry_centroid_required: bool
    maximum_time_gap_seconds: float
    completion_event_types: frozenset[str]
    reset_event_types: frozenset[str]
    debug_overlay: bool
    process_geometry: _TrackerProcessGeometry | None


@dataclass(frozen=True)
class ActiveStemTrackerHandler:
    node_type: str = "analysis.active_stem_tracker"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        segments = inputs.get("segments")
        frame = inputs.get("frame")
        events = inputs.get("events")
        trigger = inputs.get("trigger")
        if not isinstance(segments, SegmentPayload):
            return _tracker_empty_result(
                node,
                None,
                status="lost",
                reason="segments_input_missing",
            )
        if frame is not None and not isinstance(frame, FramePayload):
            frame = None
        if _image_contexts_conflict(segments, frame):
            return _tracker_empty_result(
                node,
                segments,
                status="lost",
                reason="image_context_mismatch",
                preserved_track_id=_tracker_active_track_id(context, node),
            )
        try:
            process_geometry = _tracker_process_geometry(segments, frame)
            config = _tracker_config(
                node.config or {},
                process_geometry=process_geometry,
            )
        except (TypeError, ValueError) as exc:
            return _tracker_empty_result(
                node,
                segments,
                status="lost",
                reason="configuration_invalid",
                detail=str(exc),
            )

        state = context.state.setdefault(node.node_key, {})
        if not isinstance(state, dict):
            state = {}
            context.state[node.node_key] = state
        state.setdefault("schema_version", _TRACKER_SCHEMA_VERSION)
        state.setdefault("next_track_sequence", 1)
        state.setdefault("completed_tracks", [])

        frame_sequence = _tracker_frame_sequence(context, frame, state)
        timestamp = _observation_timestamp(context, frame)
        reset_reason = _tracker_context_reset_reason(
            state,
            segments=segments,
            frame=frame,
            frame_sequence=frame_sequence,
            timestamp=timestamp,
            maximum_time_gap_seconds=config.maximum_time_gap_seconds,
        )
        if reset_reason is not None:
            _reset_tracker_state(state)
        state["last_frame_sequence"] = frame_sequence
        state["last_timestamp"] = timestamp
        state["image_signature"] = _tracker_image_signature(segments, frame)
        _prune_completed_tracks(state, frame_sequence, config)

        machine_events = _tracker_machine_events(events, trigger)
        configured_machine_events = sorted(
            machine_events & (config.completion_event_types | config.reset_event_types)
        )
        if machine_events & config.reset_event_types:
            _complete_active_track(
                state,
                frame_sequence=frame_sequence,
                reason="machine_reset_event",
                config=config,
            )
            _reset_tracker_state(state, preserve_sequence=True)
            return _tracker_empty_result(
                node,
                segments,
                status="lost",
                reason="machine_reset_event",
                machine_events=configured_machine_events,
                reset_reason=reset_reason,
                config=config,
            )
        if machine_events & config.completion_event_types:
            completed_track_id = _tracker_active_track_id_from_state(state)
            _complete_active_track(
                state,
                frame_sequence=frame_sequence,
                reason="machine_completion_event",
                config=config,
            )
            return _tracker_empty_result(
                node,
                segments,
                status="lost",
                reason="machine_completion_event",
                preserved_track_id=completed_track_id,
                machine_events=configured_machine_events,
                reset_reason=reset_reason,
                config=config,
            )

        candidates = _tracker_candidates(segments, config=config)
        active = state.get("active_track")
        eligible_after_completed_exclusion = [
            candidate
            for candidate in candidates
            if not _matches_completed_track(candidate, state, frame_sequence, config)
        ]
        eligible = eligible_after_completed_exclusion
        if not isinstance(active, dict):
            eligible = _exclude_candidates_past_curvature_freeze_gate(eligible, config=config)
        if isinstance(active, dict):
            selected, score, score_components, ambiguous = _match_active_candidate(
                active,
                eligible,
                frame_sequence=frame_sequence,
                config=config,
            )
            if ambiguous:
                active["lost_frames"] = int(active.get("lost_frames") or 0) + 1
                if int(active["lost_frames"]) > config.max_lost_frames:
                    _complete_active_track(
                        state,
                        frame_sequence=frame_sequence,
                        reason="persistent_ambiguity_timeout",
                        config=config,
                    )
                return _tracker_empty_result(
                    node,
                    segments,
                    status="ambiguous",
                    reason="multiple_equally_plausible_candidates",
                    preserved_track_id=str(active.get("track_id") or "") or None,
                    selection_confidence=score,
                    candidate_count=len(candidates),
                    eligible_candidate_count=len(eligible),
                    candidates=eligible,
                    machine_events=configured_machine_events,
                    reset_reason=reset_reason,
                    config=config,
                )
            if selected is not None:
                was_lost = int(active.get("lost_frames") or 0) > 0
                movement = _update_active_track(
                    active,
                    selected,
                    frame_sequence=frame_sequence,
                    timestamp=timestamp,
                    selection_confidence=score,
                    config=config,
                )
                selected = _tracker_candidate_with_active_zone(selected, active)
                reason = (
                    "reacquired_after_temporary_occlusion" if was_lost else "temporal_continuity"
                )
                result = _tracker_selected_result(
                    node,
                    segments,
                    selected,
                    candidates=eligible,
                    active=active,
                    status="active",
                    selection_confidence=score,
                    selection_reason=reason,
                    score_components=score_components,
                    movement=movement,
                    candidate_count=len(candidates),
                    eligible_candidate_count=len(eligible),
                    machine_events=configured_machine_events,
                    reset_reason=reset_reason,
                    config=config,
                )
                if _candidate_reached_decision_gate(selected, config=config):
                    _complete_active_track(
                        state,
                        frame_sequence=frame_sequence,
                        reason="process_decision_gate_reached",
                        config=config,
                    )
                return result

            active["lost_frames"] = int(active.get("lost_frames") or 0) + 1
            if int(active["lost_frames"]) <= config.max_lost_frames:
                return _tracker_empty_result(
                    node,
                    segments,
                    status="lost",
                    reason="temporary_occlusion",
                    preserved_track_id=str(active.get("track_id") or "") or None,
                    candidate_count=len(candidates),
                    eligible_candidate_count=len(eligible),
                    candidates=eligible,
                    machine_events=configured_machine_events,
                    reset_reason=reset_reason,
                    config=config,
                )
            _complete_active_track(
                state,
                frame_sequence=frame_sequence,
                reason="track_lost_timeout",
                config=config,
            )
            # Never replace a timed-out physical stem with another visible stem in the
            # same frame. Acquisition may start on the following observation after the
            # completed-track exclusion has been applied.
            return _tracker_empty_result(
                node,
                segments,
                status="lost",
                reason="track_lost_timeout",
                preserved_track_id=str(active.get("track_id") or "") or None,
                candidate_count=len(candidates),
                eligible_candidate_count=len(eligible),
                candidates=eligible,
                machine_events=configured_machine_events,
                reset_reason=reset_reason,
                config=config,
            )

        selected, score, score_components, ambiguous = _acquire_candidate(
            eligible,
            config=config,
        )
        if ambiguous:
            return _tracker_empty_result(
                node,
                segments,
                status="ambiguous",
                reason="multiple_equally_plausible_candidates",
                selection_confidence=score,
                candidate_count=len(candidates),
                eligible_candidate_count=len(eligible),
                candidates=eligible,
                machine_events=configured_machine_events,
                reset_reason=reset_reason,
                config=config,
            )
        if selected is None:
            reason = "no_matching_candidate" if candidates else "no_filtered_stem_candidate"
            if candidates and not eligible:
                reason = (
                    "candidates_match_completed_tracks"
                    if not eligible_after_completed_exclusion
                    else "candidates_past_curvature_freeze_gate"
                )
            return _tracker_empty_result(
                node,
                segments,
                status="lost",
                reason=reason,
                selection_confidence=score,
                candidate_count=len(candidates),
                eligible_candidate_count=len(eligible),
                candidates=eligible,
                machine_events=configured_machine_events,
                reset_reason=reset_reason,
                config=config,
            )

        active = _new_active_track(
            state,
            context=context,
            node=node,
            candidate=selected,
            frame_sequence=frame_sequence,
            timestamp=timestamp,
            selection_confidence=score,
            config=config,
        )
        selected = _tracker_candidate_with_active_zone(selected, active)
        result = _tracker_selected_result(
            node,
            segments,
            selected,
            candidates=eligible,
            active=active,
            status="active",
            selection_confidence=score,
            selection_reason=(
                "acquired_largest_region_candidate"
                if config.candidate_selection_policy == "largest_per_process_region"
                else "acquired_nearest_handover"
            ),
            score_components=score_components,
            movement=_zero_tracker_movement(),
            candidate_count=len(candidates),
            eligible_candidate_count=len(eligible),
            machine_events=configured_machine_events,
            reset_reason=reset_reason,
            config=config,
        )
        if _candidate_reached_decision_gate(selected, config=config):
            _complete_active_track(
                state,
                frame_sequence=frame_sequence,
                reason="process_decision_gate_reached",
                config=config,
            )
        return result


def _tracker_config(
    config: Mapping[str, Any],
    *,
    process_geometry: _TrackerProcessGeometry | None = None,
) -> _TrackerConfig:
    transfer_rois = _tracker_rois(config.get("transfer_rois"), field_name="transfer_rois")
    longitudinal_rois = _tracker_rois(
        config.get("longitudinal_conveyor_rois"),
        field_name="longitudinal_conveyor_rois",
    )
    if not transfer_rois or not longitudinal_rois:
        raise ValueError("transfer_rois and longitudinal_conveyor_rois must each contain a ROI")
    minimum_score = _tracker_bounded_float(config, "minimum_score", 0.35, 0.0, 1.0)
    minimum_roi_overlap = _tracker_bounded_float(
        config,
        "minimum_roi_overlap",
        0.05,
        0.0,
        1.0,
    )
    expected_full_length = _tracker_bounded_float(
        config,
        "expected_full_length_normalized",
        0.55,
        0.01,
        math.sqrt(2.0),
    )
    return _TrackerConfig(
        transfer_rois=transfer_rois,
        longitudinal_rois=longitudinal_rois,
        class_filter=frozenset(_class_filter(config)),
        minimum_score=minimum_score,
        expected_handover_point=_tracker_handover_point(config, transfer_rois),
        expected_motion=_tracker_motion_vector(
            str(config.get("expected_motion_direction") or "any")
        ),
        minimum_roi_overlap=minimum_roi_overlap,
        expected_full_length_normalized=expected_full_length,
        minimum_visible_length_fraction=_tracker_bounded_float(
            config,
            "minimum_visible_length_fraction",
            0.25,
            0.0,
            1.0,
        ),
        maximum_centroid_distance=_tracker_bounded_float(
            config,
            "maximum_centroid_distance",
            0.25,
            0.01,
            math.sqrt(2.0),
        ),
        minimum_acquisition_score=_tracker_bounded_float(
            config,
            "minimum_acquisition_score",
            0.35,
            0.0,
            1.0,
        ),
        minimum_continuity_score=_tracker_bounded_float(
            config,
            "minimum_continuity_score",
            0.5,
            0.0,
            1.0,
        ),
        ambiguity_margin=_tracker_bounded_float(
            config,
            "ambiguity_margin",
            0.08,
            0.0,
            1.0,
        ),
        candidate_selection_policy=_tracker_candidate_selection_policy(config),
        minimum_region_area_dominance_ratio=_tracker_bounded_float(
            config,
            "minimum_region_area_dominance_ratio",
            1.10,
            1.0,
            10.0,
        ),
        prefer_longitudinal_on_acquisition=bool(
            config.get("prefer_longitudinal_on_acquisition", True)
        ),
        complete_at_decision_gate=bool(config.get("complete_at_decision_gate", True)),
        max_lost_frames=_tracker_bounded_int(config, "max_lost_frames", 12, 0, 120),
        reacquisition_minimum_score=_tracker_bounded_float(
            config,
            "reacquisition_minimum_score",
            0.65,
            0.0,
            1.0,
        ),
        reacquisition_ambiguity_margin=_tracker_bounded_float(
            config,
            "reacquisition_ambiguity_margin",
            0.12,
            0.0,
            1.0,
        ),
        preserve_upstream_identity_during_occlusion=bool(
            config.get("preserve_upstream_identity_during_occlusion", True)
        ),
        completed_exclusion_frames=_tracker_bounded_int(
            config,
            "completed_exclusion_frames",
            90,
            0,
            10_000,
        ),
        completed_exclusion_iou=_tracker_bounded_float(
            config,
            "completed_exclusion_iou",
            0.5,
            0.0,
            1.0,
        ),
        max_completed_tracks=_tracker_bounded_int(
            config,
            "max_completed_tracks",
            32,
            1,
            256,
        ),
        motion_smoothing_alpha=_tracker_bounded_float(
            config,
            "motion_smoothing_alpha",
            0.6,
            0.0,
            1.0,
        ),
        process_motion_axis_dominance_ratio=_tracker_bounded_float(
            config,
            "process_motion_axis_dominance_ratio",
            1.75,
            1.0,
            10.0,
        ),
        transfer_y_motion_sensitivity=_tracker_bounded_float(
            config,
            "transfer_y_motion_sensitivity",
            8.0 / 11.0,
            0.0,
            1.0,
        ),
        minimum_process_motion_mm_per_frame=_tracker_bounded_float(
            config,
            "minimum_process_motion_mm_per_frame",
            2.0,
            0.0,
            1_000.0,
        ),
        longitudinal_motion_confirmation_frames=_tracker_bounded_int(
            config,
            "longitudinal_motion_confirmation_frames",
            3,
            1,
            30,
        ),
        longitudinal_motion_window_frames=_tracker_bounded_int(
            config,
            "longitudinal_motion_window_frames",
            6,
            3,
            60,
        ),
        longitudinal_minimum_forward_displacement_mm=_tracker_bounded_float(
            config,
            "longitudinal_minimum_forward_displacement_mm",
            120.0,
            0.0,
            2_000.0,
        ),
        longitudinal_minimum_forward_progress_ratio=_tracker_bounded_float(
            config,
            "longitudinal_minimum_forward_progress_ratio",
            0.75,
            0.0,
            1.0,
        ),
        longitudinal_entry_centroid_required=bool(
            config.get("longitudinal_entry_centroid_required", True)
        ),
        maximum_time_gap_seconds=_tracker_bounded_float(
            config,
            "maximum_time_gap_seconds",
            2.0,
            0.01,
            300.0,
        ),
        completion_event_types=frozenset(_tracker_string_set(config.get("completion_event_types"))),
        reset_event_types=frozenset(_tracker_string_set(config.get("reset_event_types"))),
        debug_overlay=bool(config.get("debug_overlay", True)),
        process_geometry=process_geometry,
    )


def _tracker_candidate_selection_policy(config: Mapping[str, Any]) -> str:
    value = (
        str(config.get("candidate_selection_policy") or "largest_per_process_region")
        .strip()
        .casefold()
    )
    if value not in {"largest_per_process_region", "legacy_handover_score"}:
        raise ValueError(
            "candidate_selection_policy must be largest_per_process_region or legacy_handover_score"
        )
    return value


def _tracker_handover_point(
    config: Mapping[str, Any],
    transfer_rois: tuple[_TrackerRegion, ...],
) -> tuple[float, float]:
    mode = str(config.get("handover_reference_mode") or "transfer_roi_center").strip().casefold()
    if mode == "transfer_roi_center":
        largest = max(transfer_rois, key=lambda region: _polygon_area(region.points))
        return (
            sum(point[0] for point in largest.points) / len(largest.points),
            sum(point[1] for point in largest.points) / len(largest.points),
        )
    if mode != "manual_point":
        raise ValueError("handover_reference_mode must be transfer_roi_center or manual_point")
    return (
        _tracker_bounded_float(config, "expected_handover_x", 0.5, 0.0, 1.0),
        _tracker_bounded_float(config, "expected_handover_y", 0.5, 0.0, 1.0),
    )


def _tracker_process_geometry(
    segments: SegmentPayload,
    frame: FramePayload | None,
) -> _TrackerProcessGeometry | None:
    frame_profile = frame.metadata.get("stem_curvature_process_calibration") if frame else None
    segment_profile = segments.metadata.get("stem_curvature_process_calibration")
    raw_profile = frame_profile if frame_profile is not None else segment_profile
    image_context = frame.image_context if frame is not None else None
    transform_type = image_context.transform.transform_type if image_context is not None else None
    if raw_profile is None:
        if transform_type in {"perspective_rectify", "masked_crop"}:
            raise ValueError(
                "rectified process frames must carry their versioned process calibration"
            )
        return None
    if frame is not None and transform_type not in {"perspective_rectify", "masked_crop"}:
        raise ValueError("stem process calibration geometry requires a canonical calibrated frame")
    profile = validate_stem_curvature_process_calibration(raw_profile)
    if frame is not None:
        expected_width = image_context.transform.parameters.get("output_width", profile.image_width)
        expected_height = image_context.transform.parameters.get(
            "output_height", profile.image_height
        )
        if (frame.width, frame.height) != (expected_width, expected_height):
            raise ValueError("rectified frame resolution does not match its image view profile")
        profile = process_profile_for_rectified_view(
            profile,
            image_width=int(expected_width),
            image_height=int(expected_height),
            transform_parameters=image_context.transform.parameters,
        )
    expected_hash = (
        image_context.transform.parameters.get("process_calibration_hash")
        if image_context is not None
        else segments.metadata.get("process_calibration_hash")
    )
    if expected_hash != profile.calibration_hash:
        raise ValueError(
            "rectified frame process calibration hash does not match its geometry profile"
        )
    return _tracker_geometry_from_process_profile(profile)


def _tracker_geometry_from_process_profile(
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
) -> _TrackerProcessGeometry:
    bounds = profile.process_bounds
    x_span = bounds.x_max_mm - bounds.x_min_mm

    def normalized(points: Sequence[Any]) -> tuple[tuple[float, float], ...]:
        projected = process_points_to_image(points, profile)
        width = max(profile.image_width - 1, 1)
        height = max(profile.image_height - 1, 1)
        return tuple(
            (
                min(max(float(point.x_px) / width, 0.0), 1.0),
                min(max(float(point.y_px) / height, 0.0), 1.0),
            )
            for point in projected
        )

    candidate_points = normalized(profile.candidate_roi.process_points)
    candidate_roi = _tracker_region(candidate_points, label="Calibrated candidate ROI")
    if candidate_roi is None:
        raise ValueError("calibrated candidate ROI is not a valid normalized polygon")
    session_gate = profile.session_start_gate
    freeze_gate = profile.curvature_freeze_gate
    decision_gate = profile.decision_gate
    return _TrackerProcessGeometry(
        calibration_hash=profile.calibration_hash,
        process_profile=profile,
        candidate_rois=(candidate_roi,),
        session_start_gate=normalized((session_gate.process_start, session_gate.process_end)),
        curvature_freeze_gate=normalized((freeze_gate.process_start, freeze_gate.process_end)),
        decision_gate=normalized((decision_gate.process_start, decision_gate.process_end)),
        decision_margin_mm=profile.decision_margin_mm,
        process_x_span_mm=x_span,
        session_start_gate_x_mm=(session_gate.process_start.x_mm + session_gate.process_end.x_mm)
        / 2.0,
        curvature_freeze_gate_x_mm=(freeze_gate.process_start.x_mm + freeze_gate.process_end.x_mm)
        / 2.0,
        decision_gate_x_mm=(decision_gate.process_start.x_mm + decision_gate.process_end.x_mm)
        / 2.0,
        conveyor_direction=profile.conveyor_direction,
        rotation_reference_view=profile.rotation_reference_view,
        positive_rotation=profile.positive_rotation,
    )


def _tracker_rois(
    value: object,
    *,
    field_name: str = "tracker ROI",
) -> tuple[_TrackerRegion, ...]:
    raw_items = value if isinstance(value, list) else [value] if isinstance(value, Mapping) else []
    rois: list[_TrackerRegion] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        label = str(item.get("label") or "").strip() or None
        raw_points = item.get("points", item.get("polygon"))
        if isinstance(raw_points, Sequence) and not isinstance(raw_points, str | bytes):
            region = _tracker_region(_point_pairs(raw_points), label=label)
            if region is None:
                raise ValueError(
                    f"{field_name} polygons must be convex, ordered, normalized, and non-empty"
                )
            rois.append(region)
            continue
        source = item.get("box") if isinstance(item.get("box"), Mapping) else item
        try:
            if {"x_min", "y_min", "x_max", "y_max"}.issubset(source):
                box = _TrackerBox(
                    x_min=float(source["x_min"]),
                    y_min=float(source["y_min"]),
                    x_max=float(source["x_max"]),
                    y_max=float(source["y_max"]),
                )
            elif {"x", "y", "width", "height"}.issubset(source):
                x = float(source["x"])
                y = float(source["y"])
                box = _TrackerBox(
                    x_min=x,
                    y_min=y,
                    x_max=x + float(source["width"]),
                    y_max=y + float(source["height"]),
                )
            else:
                continue
        except (TypeError, ValueError):
            continue
        if 0.0 <= box.x_min < box.x_max <= 1.0 and 0.0 <= box.y_min < box.y_max <= 1.0:
            region = _tracker_region(
                (
                    (box.x_min, box.y_min),
                    (box.x_max, box.y_min),
                    (box.x_max, box.y_max),
                    (box.x_min, box.y_max),
                ),
                label=label,
                source_shape="box",
            )
            if region is not None:
                rois.append(region)
    return tuple(rois)


def _tracker_region(
    points: Sequence[tuple[float, float]],
    *,
    label: str | None = None,
    source_shape: str = "polygon",
) -> _TrackerRegion | None:
    normalized = tuple((float(x), float(y)) for x, y in points)
    if (
        len(normalized) < 3
        or not np.isfinite(np.asarray(normalized, dtype=np.float64)).all()
        or any(x < 0.0 or x > 1.0 or y < 0.0 or y > 1.0 for x, y in normalized)
        or _polygon_area(normalized) <= 1e-8
        or not _tracker_polygon_is_convex(normalized)
    ):
        return None
    return _TrackerRegion(
        points=normalized,
        bounds=_TrackerBox(
            x_min=min(point[0] for point in normalized),
            y_min=min(point[1] for point in normalized),
            x_max=max(point[0] for point in normalized),
            y_max=max(point[1] for point in normalized),
        ),
        label=label,
        source_shape=source_shape,
    )


def _tracker_polygon_is_convex(points: Sequence[tuple[float, float]]) -> bool:
    signs: set[bool] = set()
    for index, current in enumerate(points):
        following = points[(index + 1) % len(points)]
        next_point = points[(index + 2) % len(points)]
        cross = (following[0] - current[0]) * (next_point[1] - following[1]) - (
            following[1] - current[1]
        ) * (next_point[0] - following[0])
        if abs(cross) > 1e-10:
            signs.add(cross > 0.0)
    return len(signs) == 1


def _tracker_candidates(
    payload: SegmentPayload,
    *,
    config: _TrackerConfig,
) -> list[_TrackerCandidate]:
    candidates: list[_TrackerCandidate] = []
    for index, segment in enumerate(payload.segments):
        source_object = payload.objects[index] if index < len(payload.objects) else None
        label = source_object.label if source_object and source_object.label else segment.label
        score = (
            float(source_object.score)
            if source_object is not None and source_object.score is not None
            else float(segment.score)
        )
        if score < config.minimum_score:
            continue
        if config.class_filter and str(label or "").strip().casefold() not in config.class_filter:
            continue
        polygon = _point_pairs(segment.polygon)
        if len(polygon) < 3:
            continue
        shape = _tracker_shape(polygon)
        if shape is None:
            continue
        box, centroid, area, length, width, axis = shape
        transfer_overlap = _maximum_roi_overlap(polygon, box, config.transfer_rois)
        longitudinal_overlap = _maximum_roi_overlap(
            polygon,
            box,
            config.longitudinal_rois,
        )
        largest_overlap = max(transfer_overlap, longitudinal_overlap)
        if largest_overlap < config.minimum_roi_overlap:
            continue
        process_geometry = config.process_geometry
        if (
            process_geometry is not None
            and _maximum_roi_overlap(
                polygon,
                box,
                process_geometry.candidate_rois,
            )
            < config.minimum_roi_overlap
        ):
            continue
        # A calibrated pipeline derives the actual handover from motion in process
        # X/Y. ROI overlap is only an initial hint: an overlapping candidate starts
        # in transfer and must prove X-dominant motion before becoming longitudinal.
        # Legacy pipelines without process coordinates retain their historic ROI rule.
        if config.process_geometry is not None:
            longitudinal_initial = (
                longitudinal_overlap >= config.minimum_roi_overlap
                and transfer_overlap < config.minimum_roi_overlap
            )
        else:
            longitudinal_initial = longitudinal_overlap >= max(
                config.minimum_roi_overlap,
                0.5 * transfer_overlap,
            )
        if longitudinal_initial:
            process_stage = 1
            process_zone = "longitudinal_conveyor"
        else:
            process_stage = 0
            process_zone = "transfer"
        visible_fraction = min(
            length / config.expected_full_length_normalized,
            1.0,
        )
        if visible_fraction < config.minimum_visible_length_fraction:
            continue
        upstream_track_id = source_object.track_id if source_object is not None else None
        candidates.append(
            _TrackerCandidate(
                index=index,
                segment=segment,
                source_object=source_object,
                score=score,
                polygon=polygon,
                box=box,
                centroid=centroid,
                area=area,
                projected_length=length,
                projected_width=width,
                axis=axis,
                visible_length_fraction=visible_fraction,
                process_stage=process_stage,
                process_zone=process_zone,
                roi_overlap=largest_overlap,
                transfer_overlap=transfer_overlap,
                longitudinal_overlap=longitudinal_overlap,
                upstream_track_id=upstream_track_id,
            )
        )
    return candidates


def _candidate_reached_decision_gate(
    candidate: _TrackerCandidate,
    *,
    config: _TrackerConfig,
) -> bool:
    if not config.complete_at_decision_gate or config.process_geometry is None:
        return False
    metadata = _tracker_process_metadata(
        config,
        centroid=candidate.centroid,
        polygon=candidate.polygon,
    )
    return metadata.get("decision_gate_reached") is True


def _candidate_reached_curvature_freeze_gate(
    candidate: _TrackerCandidate,
    *,
    config: _TrackerConfig,
) -> bool:
    if config.process_geometry is None:
        return False
    metadata = _tracker_process_metadata(
        config,
        centroid=candidate.centroid,
        polygon=candidate.polygon,
    )
    return metadata.get("curvature_freeze_gate_reached") is True


def _exclude_candidates_past_curvature_freeze_gate(
    candidates: Sequence[_TrackerCandidate],
    *,
    config: _TrackerConfig,
) -> list[_TrackerCandidate]:
    """Do not create a second session after curvature observation has frozen.

    An already active track may continue through this gate so surface phase can still
    be followed. If that track has ended or timed out, a mask behind the gate cannot
    be reinterpreted as a new green transfer stem.
    """

    if config.process_geometry is None:
        return list(candidates)
    return [
        candidate
        for candidate in candidates
        if not _candidate_reached_curvature_freeze_gate(candidate, config=config)
    ]


def _tracker_shape(
    polygon: tuple[tuple[float, float], ...],
) -> tuple[_TrackerBox, tuple[float, float], float, float, float, tuple[float, float]] | None:
    points = np.asarray(polygon, dtype=np.float64)
    if points.shape[0] < 3 or not np.isfinite(points).all():
        return None
    area = _polygon_area(polygon)
    if area <= 1e-9:
        return None
    centroid_array = np.mean(points, axis=0)
    centered = points - centroid_array
    covariance = centered.T @ centered / max(points.shape[0], 1)
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    axis = eigenvectors[:, int(np.argmax(eigenvalues))]
    axis_norm = float(np.linalg.norm(axis))
    if axis_norm <= 1e-12:
        return None
    axis = axis / axis_norm
    normal = np.asarray((-axis[1], axis[0]))
    longitudinal = centered @ axis
    transverse = centered @ normal
    length = float(np.max(longitudinal) - np.min(longitudinal))
    width = float(np.max(transverse) - np.min(transverse))
    if length <= 1e-6 or width <= 1e-6:
        return None
    return (
        _TrackerBox(
            x_min=float(np.min(points[:, 0])),
            y_min=float(np.min(points[:, 1])),
            x_max=float(np.max(points[:, 0])),
            y_max=float(np.max(points[:, 1])),
        ),
        (float(centroid_array[0]), float(centroid_array[1])),
        area,
        length,
        width,
        (float(axis[0]), float(axis[1])),
    )


def _maximum_roi_overlap(
    polygon: tuple[tuple[float, float], ...],
    box: _TrackerBox,
    rois: tuple[_TrackerRegion, ...],
) -> float:
    best = 0.0
    for roi in rois:
        if _tracker_intersection_area(box, roi.bounds) <= 0.0:
            continue
        overlap = _tracker_polygon_intersection_area(polygon, roi.points) / max(
            _polygon_area(polygon), 1e-12
        )
        best = max(best, overlap)
    return min(best, 1.0)


def _tracker_polygon_intersection_area(
    subject: Sequence[tuple[float, float]],
    clip: Sequence[tuple[float, float]],
) -> float:
    """Clip an ordered segmentation polygon against a convex tracker ROI."""

    output = list(subject)
    if len(output) < 3 or len(clip) < 3:
        return 0.0
    orientation = 1.0 if _tracker_signed_polygon_area(clip) >= 0.0 else -1.0
    for edge_index, edge_start in enumerate(clip):
        edge_end = clip[(edge_index + 1) % len(clip)]
        source = output
        output = []
        if not source:
            break
        previous = source[-1]
        previous_inside = _tracker_clip_inside(previous, edge_start, edge_end, orientation)
        for current in source:
            current_inside = _tracker_clip_inside(current, edge_start, edge_end, orientation)
            if current_inside:
                if not previous_inside:
                    output.append(
                        _tracker_line_intersection(previous, current, edge_start, edge_end)
                    )
                output.append(current)
            elif previous_inside:
                output.append(_tracker_line_intersection(previous, current, edge_start, edge_end))
            previous = current
            previous_inside = current_inside
    return _polygon_area(output) if len(output) >= 3 else 0.0


def _tracker_signed_polygon_area(points: Sequence[tuple[float, float]]) -> float:
    return 0.5 * sum(
        x1 * y2 - x2 * y1
        for (x1, y1), (x2, y2) in zip(points, (*points[1:], points[0]), strict=True)
    )


def _tracker_clip_inside(
    point: tuple[float, float],
    edge_start: tuple[float, float],
    edge_end: tuple[float, float],
    orientation: float,
) -> bool:
    cross = (edge_end[0] - edge_start[0]) * (point[1] - edge_start[1]) - (
        edge_end[1] - edge_start[1]
    ) * (point[0] - edge_start[0])
    return orientation * cross >= -1e-12


def _tracker_line_intersection(
    start: tuple[float, float],
    end: tuple[float, float],
    edge_start: tuple[float, float],
    edge_end: tuple[float, float],
) -> tuple[float, float]:
    segment = (end[0] - start[0], end[1] - start[1])
    edge = (edge_end[0] - edge_start[0], edge_end[1] - edge_start[1])
    denominator = segment[0] * edge[1] - segment[1] * edge[0]
    if abs(denominator) <= 1e-12:
        return end
    offset = (edge_start[0] - start[0], edge_start[1] - start[1])
    factor = (offset[0] * edge[1] - offset[1] * edge[0]) / denominator
    return start[0] + factor * segment[0], start[1] + factor * segment[1]


def _acquire_candidate(
    candidates: Sequence[_TrackerCandidate],
    *,
    config: _TrackerConfig,
) -> tuple[_TrackerCandidate | None, float, dict[str, float], bool]:
    if config.candidate_selection_policy == "largest_per_process_region":
        return _acquire_largest_region_candidate(candidates, config=config)

    ranked: list[tuple[float, _TrackerCandidate, dict[str, float]]] = []
    for candidate in candidates:
        distance = math.dist(candidate.centroid, config.expected_handover_point)
        proximity = max(0.0, 1.0 - distance / math.sqrt(2.0))
        zone_priority = 1.0 if candidate.process_stage == 0 else 0.6
        components = {
            "handover_proximity": proximity,
            "process_zone_priority": zone_priority,
            "segmentation_score": candidate.score,
            "visible_length_fraction": candidate.visible_length_fraction,
        }
        score = (
            0.4 * proximity
            + 0.25 * zone_priority
            + 0.2 * candidate.score
            + 0.15 * candidate.visible_length_fraction
        )
        ranked.append((score, candidate, components))
    ranked.sort(key=lambda item: (-item[0], item[1].index))
    if not ranked or ranked[0][0] < config.minimum_acquisition_score:
        return None, ranked[0][0] if ranked else 0.0, ranked[0][2] if ranked else {}, False
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] <= config.ambiguity_margin:
        return None, ranked[0][0], ranked[0][2], True
    return ranked[0][1], ranked[0][0], ranked[0][2], False


def _acquire_largest_region_candidate(
    candidates: Sequence[_TrackerCandidate],
    *,
    config: _TrackerConfig,
) -> tuple[_TrackerCandidate | None, float, dict[str, float], bool]:
    """Acquire one visible stem without treating other process regions as an error.

    A stem already being tracked is matched before this function is called. During a
    fresh acquisition, the longitudinal conveyor wins over the transfer corridor so a
    waiting successor cannot replace a stem that is already in process. Inside the
    selected region, mask area is the primary discriminator. A near tie remains
    ambiguous instead of selecting an arbitrary instance.
    """

    if not candidates:
        return None, 0.0, {}, False
    by_stage = {
        stage: [candidate for candidate in candidates if candidate.process_stage == stage]
        for stage in (0, 1)
    }
    if config.prefer_longitudinal_on_acquisition and by_stage[1]:
        selected_stage = 1
    elif by_stage[0]:
        selected_stage = 0
    else:
        selected_stage = 1
    contenders = sorted(
        by_stage[selected_stage],
        key=lambda candidate: (-candidate.area, -candidate.projected_length, candidate.index),
    )
    largest = contenders[0]
    runner_up = contenders[1] if len(contenders) > 1 else None
    area_ratio = largest.area / max(runner_up.area, 1e-12) if runner_up is not None else None
    area_separation = (
        min(
            max(
                (area_ratio - 1.0) / max(config.minimum_region_area_dominance_ratio - 1.0, 1e-12),
                0.0,
            ),
            1.0,
        )
        if area_ratio is not None
        else 1.0
    )
    components = {
        "largest_mask_area": 1.0,
        "region_area_separation": area_separation,
        "segmentation_score": largest.score,
        "visible_length_fraction": largest.visible_length_fraction,
        "roi_overlap": largest.roi_overlap,
        "process_zone_priority": 1.0,
    }
    score = (
        0.25 * area_separation
        + 0.25 * largest.score
        + 0.25 * largest.visible_length_fraction
        + 0.15 * largest.roi_overlap
        + 0.10
    )
    if area_ratio is not None and area_ratio < config.minimum_region_area_dominance_ratio:
        return None, score, components, True
    if score < config.minimum_acquisition_score:
        return None, score, components, False
    return largest, score, components, False


def _match_active_candidate(
    active: Mapping[str, Any],
    candidates: Sequence[_TrackerCandidate],
    *,
    frame_sequence: int,
    config: _TrackerConfig,
) -> tuple[_TrackerCandidate | None, float, dict[str, float], bool]:
    ranked: list[tuple[float, _TrackerCandidate, dict[str, float]]] = []
    previous_box = _tracker_box_from_state(active.get("last_box"))
    previous_centroid = _tracker_point_from_state(active.get("last_centroid"))
    if previous_box is None or previous_centroid is None:
        return None, 0.0, {}, False
    previous_sequence = int(active.get("last_sequence") or frame_sequence)
    frame_delta = max(frame_sequence - previous_sequence, 1)
    velocity = _tracker_point_from_state(active.get("velocity")) or (0.0, 0.0)
    predicted_centroid = (
        previous_centroid[0] + velocity[0] * frame_delta,
        previous_centroid[1] + velocity[1] * frame_delta,
    )
    previous_area = max(float(active.get("area") or 0.0), 1e-12)
    previous_length = max(float(active.get("projected_length") or 0.0), 1e-12)
    previous_axis = _tracker_point_from_state(active.get("axis")) or (1.0, 0.0)
    previous_stage = int(active.get("process_stage") or 0)
    previous_upstream_id = str(active.get("upstream_track_id") or "") or None
    reacquiring = int(active.get("lost_frames") or 0) > 0
    for candidate in candidates:
        if (
            previous_stage >= 1
            and candidate.longitudinal_overlap < config.minimum_roi_overlap
            and not _candidate_reached_curvature_freeze_gate(candidate, config=config)
        ):
            # A waiting stem that exists only in the transverse transfer corridor
            # must never inherit the blue longitudinal state of its predecessor.
            continue
        if (
            reacquiring
            and config.process_geometry is None
            and candidate.process_stage < previous_stage
        ):
            continue
        if (
            reacquiring
            and config.preserve_upstream_identity_during_occlusion
            and previous_upstream_id is not None
            and candidate.upstream_track_id is not None
            and candidate.upstream_track_id != previous_upstream_id
        ):
            continue
        centroid_distance = math.dist(candidate.centroid, predicted_centroid)
        centroid_score = max(
            0.0,
            1.0 - centroid_distance / config.maximum_centroid_distance,
        )
        iou = _tracker_box_iou(previous_box, candidate.box)
        area_similarity = min(candidate.area / previous_area, previous_area / candidate.area)
        length_similarity = min(
            candidate.projected_length / previous_length,
            previous_length / candidate.projected_length,
        )
        axis_similarity = abs(
            candidate.axis[0] * previous_axis[0] + candidate.axis[1] * previous_axis[1]
        )
        displacement = (
            candidate.centroid[0] - previous_centroid[0],
            candidate.centroid[1] - previous_centroid[1],
        )
        direction_score = _tracker_direction_score(displacement, config.expected_motion)
        stage_score = (
            1.0
            if config.process_geometry is not None or candidate.process_stage >= previous_stage
            else 0.0
        )
        upstream_identity = (
            1.0
            if previous_upstream_id is not None
            and candidate.upstream_track_id == previous_upstream_id
            else 0.0
        )
        components = {
            "temporal_iou": iou,
            "predicted_centroid_continuity": centroid_score,
            "area_similarity": area_similarity,
            "length_similarity": length_similarity,
            "axis_similarity": axis_similarity,
            "expected_motion_consistency": direction_score,
            "process_order_consistency": stage_score,
            "upstream_identity": upstream_identity,
        }
        score = (
            0.27 * iou
            + 0.28 * centroid_score
            + 0.13 * area_similarity
            + 0.09 * length_similarity
            + 0.05 * axis_similarity
            + 0.07 * direction_score
            + 0.06 * stage_score
            + 0.05 * upstream_identity
        )
        ranked.append((score, candidate, components))
    ranked.sort(key=lambda item: (-item[0], item[1].index))
    minimum_score = (
        max(config.minimum_continuity_score, config.reacquisition_minimum_score)
        if reacquiring
        else config.minimum_continuity_score
    )
    qualified = [item for item in ranked if item[0] >= minimum_score]
    if not qualified:
        return None, ranked[0][0] if ranked else 0.0, ranked[0][2] if ranked else {}, False
    ambiguity_margin = (
        max(config.ambiguity_margin, config.reacquisition_ambiguity_margin)
        if reacquiring
        else config.ambiguity_margin
    )
    if len(qualified) > 1 and qualified[0][0] - qualified[1][0] <= ambiguity_margin:
        first_candidate = qualified[0][1]
        second_candidate = qualified[1][1]
        if (
            config.candidate_selection_policy != "largest_per_process_region"
            or _continuity_candidates_spatially_compete(first_candidate, second_candidate)
        ):
            return None, qualified[0][0], qualified[0][2], True
    return qualified[0][1], qualified[0][0], qualified[0][2], False


def _continuity_candidates_spatially_compete(
    first: _TrackerCandidate,
    second: _TrackerCandidate,
) -> bool:
    """Return true only when two temporal candidates represent the same image area.

    Separate stems are expected in this plant. Similar aggregate match scores must not
    erase an existing active track merely because a predecessor or successor is visible
    elsewhere. Overlapping detections remain ambiguous because selecting between them
    could switch the physical stem identity.
    """

    if _tracker_box_iou(first.box, second.box) >= 0.05:
        return True
    if first.process_stage != second.process_stage:
        return False
    separation = math.dist(first.centroid, second.centroid)
    characteristic_width = max(
        min(first.projected_width, second.projected_width),
        1e-6,
    )
    return separation <= 1.5 * characteristic_width


def _new_active_track(
    state: dict[str, Any],
    *,
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    candidate: _TrackerCandidate,
    frame_sequence: int,
    timestamp: str,
    selection_confidence: float,
    config: _TrackerConfig,
) -> dict[str, Any]:
    sequence = int(state.get("next_track_sequence") or 1)
    state["next_track_sequence"] = sequence + 1
    namespace = "|".join(
        (
            context.runtime_execution_id or context.pipeline_version_id,
            context.channel_id or "no-channel",
            node.node_key,
        )
    )
    digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:12]
    process_centroid = _tracker_process_centroid_mm(
        candidate.centroid,
        config.process_geometry,
    )
    active = {
        "track_id": f"trk_stem_{digest}_{sequence:04d}",
        "age_frames": 1,
        "lost_frames": 0,
        "first_sequence": frame_sequence,
        "last_sequence": frame_sequence,
        "last_timestamp": timestamp,
        "last_box": _tracker_box_list(candidate.box),
        "last_centroid": list(candidate.centroid),
        "area": candidate.area,
        "projected_length": candidate.projected_length,
        "projected_width": candidate.projected_width,
        "axis": list(candidate.axis),
        "velocity": [0.0, 0.0],
        "last_process_centroid_mm": (
            list(process_centroid) if process_centroid is not None else None
        ),
        "process_velocity_mm_per_frame": [0.0, 0.0],
        "longitudinal_motion_streak": 0,
        "longitudinal_motion_history": [],
        "process_stage": candidate.process_stage,
        "process_zone": candidate.process_zone,
        "process_zone_source": (
            "calibrated_roi_initial"
            if config.process_geometry is not None
            else "legacy_roi_overlap"
        ),
        "upstream_track_id": candidate.upstream_track_id,
        "selection_confidence": selection_confidence,
    }
    state["active_track"] = active
    return active


def _update_active_track(
    active: dict[str, Any],
    candidate: _TrackerCandidate,
    *,
    frame_sequence: int,
    timestamp: str,
    selection_confidence: float,
    config: _TrackerConfig,
) -> dict[str, Any]:
    previous_centroid = _tracker_point_from_state(active.get("last_centroid")) or candidate.centroid
    previous_axis = _tracker_point_from_state(active.get("axis")) or candidate.axis
    previous_area = max(float(active.get("area") or candidate.area), 1e-12)
    previous_length = max(
        float(active.get("projected_length") or candidate.projected_length),
        1e-12,
    )
    previous_sequence = int(active.get("last_sequence") or frame_sequence)
    frame_delta = max(frame_sequence - previous_sequence, 1)
    delta = (
        candidate.centroid[0] - previous_centroid[0],
        candidate.centroid[1] - previous_centroid[1],
    )
    raw_velocity = (delta[0] / frame_delta, delta[1] / frame_delta)
    previous_velocity = _tracker_point_from_state(active.get("velocity")) or (0.0, 0.0)
    alpha = config.motion_smoothing_alpha
    velocity = (
        alpha * raw_velocity[0] + (1.0 - alpha) * previous_velocity[0],
        alpha * raw_velocity[1] + (1.0 - alpha) * previous_velocity[1],
    )
    process_centroid = _tracker_process_centroid_mm(
        candidate.centroid,
        config.process_geometry,
    )
    previous_process_centroid = _tracker_point_from_state(active.get("last_process_centroid_mm"))
    previous_process_velocity = _tracker_point_from_state(
        active.get("process_velocity_mm_per_frame")
    ) or (0.0, 0.0)
    process_delta: tuple[float, float] | None = None
    process_velocity: tuple[float, float] | None = None
    process_motion_axis = "unavailable"
    process_motion_confidence = 0.0
    if process_centroid is not None and previous_process_centroid is not None:
        process_delta = (
            process_centroid[0] - previous_process_centroid[0],
            process_centroid[1] - previous_process_centroid[1],
        )
        raw_process_velocity = (
            process_delta[0] / frame_delta,
            process_delta[1] / frame_delta,
        )
        process_velocity = (
            alpha * raw_process_velocity[0] + (1.0 - alpha) * previous_process_velocity[0],
            alpha * raw_process_velocity[1] + (1.0 - alpha) * previous_process_velocity[1],
        )
        process_motion_axis, process_motion_confidence = _tracker_process_motion_axis(
            process_velocity,
            config=config,
        )

    previous_stage = int(active.get("process_stage") or 0)
    longitudinal_streak = int(active.get("longitudinal_motion_streak") or 0)
    if previous_stage == 0:
        if process_motion_axis == "longitudinal_x":
            longitudinal_streak += 1
        elif process_motion_axis in {"transfer_y", "opposite_longitudinal_x"}:
            longitudinal_streak = 0
        elif process_motion_axis in {"ambiguous", "stationary"}:
            longitudinal_streak = max(longitudinal_streak - 1, 0)
    process_stage = previous_stage
    process_zone_source = str(active.get("process_zone_source") or "roi_initial")
    required_longitudinal_frames = config.longitudinal_motion_confirmation_frames
    motion_window_frames = max(
        config.longitudinal_motion_window_frames,
        required_longitudinal_frames,
    )
    if config.process_geometry is not None:
        # A thrown stem often changes its mask centroid along both axes while it rolls.
        # At least three conveyor-directed X observations within the bounded history
        # avoid interpreting that transient projection as longitudinal transport.
        # Existing hidden values from older pipelines are raised to this minimum.
        required_longitudinal_frames = max(required_longitudinal_frames, 3)
        motion_window_frames = max(motion_window_frames, required_longitudinal_frames)

    motion_history = _tracker_longitudinal_motion_history(active.get("longitudinal_motion_history"))
    if previous_stage == 0 and process_delta is not None:
        # Conveyor-opposite motion starts a new evidence run. Keeping it in the
        # previous forward window would delay a real handover after transfer recoil.
        if process_motion_axis == "opposite_longitudinal_x":
            motion_history = []
        conveyor_sign = 1.0
        if (
            config.process_geometry is not None
            and config.process_geometry.conveyor_direction != "POSITIVE_X"
        ):
            conveyor_sign = -1.0
        if process_motion_axis != "opposite_longitudinal_x":
            motion_history.append(
                {
                    "frame_sequence": frame_sequence,
                    "axis": process_motion_axis,
                    "forward_x_mm": conveyor_sign * process_delta[0],
                    "absolute_x_mm": abs(process_delta[0]),
                }
            )
        motion_history = motion_history[-motion_window_frames:]
    elif previous_stage >= 1:
        motion_history = []

    longitudinal_evidence_count = sum(item["axis"] == "longitudinal_x" for item in motion_history)
    forward_displacement_mm = sum(item["forward_x_mm"] for item in motion_history)
    absolute_x_path_mm = sum(item["absolute_x_mm"] for item in motion_history)
    forward_progress_ratio = max(forward_displacement_mm, 0.0) / max(
        absolute_x_path_mm,
        1.0e-12,
    )
    centroid_in_longitudinal_roi = _tracker_point_in_regions(
        candidate.centroid,
        config.longitudinal_rois,
    )
    spatial_gate_satisfied = (
        not config.longitudinal_entry_centroid_required or centroid_in_longitudinal_roi
    )
    longitudinal_transition_pending = (
        previous_stage == 0
        and process_motion_axis == "longitudinal_x"
        and spatial_gate_satisfied
        and forward_displacement_mm > 0.0
        and forward_progress_ratio >= config.longitudinal_minimum_forward_progress_ratio
    )
    longitudinal_transition_confirmed = (
        longitudinal_transition_pending
        and longitudinal_evidence_count >= required_longitudinal_frames
        and forward_displacement_mm >= config.longitudinal_minimum_forward_displacement_mm
    )
    if process_stage == 0:
        if config.process_geometry is None:
            longitudinal_transition_confirmed = longitudinal_streak >= required_longitudinal_frames
        if longitudinal_transition_confirmed:
            process_stage = 1
            process_zone_source = (
                "calibrated_spatial_temporal_motion_x"
                if config.process_geometry is not None
                else "legacy_process_motion_x"
            )
    process_zone = "longitudinal_conveyor" if process_stage >= 1 else "transfer"
    axis_change = math.degrees(
        math.acos(
            min(
                max(
                    abs(
                        candidate.axis[0] * previous_axis[0] + candidate.axis[1] * previous_axis[1]
                    ),
                    0.0,
                ),
                1.0,
            )
        )
    )
    active.update(
        {
            "age_frames": int(active.get("age_frames") or 0) + 1,
            "lost_frames": 0,
            "last_sequence": frame_sequence,
            "last_timestamp": timestamp,
            "last_box": _tracker_box_list(candidate.box),
            "last_centroid": list(candidate.centroid),
            "area": candidate.area,
            "projected_length": candidate.projected_length,
            "projected_width": candidate.projected_width,
            "axis": list(candidate.axis),
            "velocity": list(velocity),
            "last_process_centroid_mm": (
                list(process_centroid) if process_centroid is not None else None
            ),
            "process_velocity_mm_per_frame": (
                list(process_velocity) if process_velocity is not None else [0.0, 0.0]
            ),
            "longitudinal_motion_streak": longitudinal_streak,
            "longitudinal_motion_history": motion_history,
            "process_stage": process_stage,
            "process_zone": process_zone,
            "process_zone_source": process_zone_source,
            "upstream_track_id": candidate.upstream_track_id,
            "selection_confidence": selection_confidence,
        }
    )
    return {
        "delta_x_normalized": delta[0],
        "delta_y_normalized": delta[1],
        "velocity_x_normalized_per_frame": velocity[0],
        "velocity_y_normalized_per_frame": velocity[1],
        "speed_normalized_per_frame": math.hypot(*velocity),
        "movement_direction": _tracker_direction_label(velocity),
        "delta_x_mm": process_delta[0] if process_delta is not None else None,
        "delta_y_mm": process_delta[1] if process_delta is not None else None,
        "velocity_x_mm_per_frame": (process_velocity[0] if process_velocity is not None else None),
        "velocity_y_mm_per_frame": (process_velocity[1] if process_velocity is not None else None),
        "process_movement_direction": (
            _tracker_process_direction_label(process_velocity)
            if process_velocity is not None
            else "unavailable"
        ),
        "process_motion_axis": process_motion_axis,
        "process_motion_axis_confidence": process_motion_confidence,
        "transfer_y_motion_sensitivity": config.transfer_y_motion_sensitivity,
        "transfer_y_minimum_motion_share": _tracker_transfer_y_minimum_motion_share(config),
        "longitudinal_motion_streak": longitudinal_streak,
        "longitudinal_motion_evidence_count": longitudinal_evidence_count,
        "longitudinal_motion_confirmation_frames_required": required_longitudinal_frames,
        "longitudinal_motion_window_frames": motion_window_frames,
        "longitudinal_forward_displacement_mm": forward_displacement_mm,
        "longitudinal_absolute_x_path_mm": absolute_x_path_mm,
        "longitudinal_forward_progress_ratio": forward_progress_ratio,
        "longitudinal_minimum_forward_displacement_mm": (
            config.longitudinal_minimum_forward_displacement_mm
        ),
        "longitudinal_minimum_forward_progress_ratio": (
            config.longitudinal_minimum_forward_progress_ratio
        ),
        "longitudinal_centroid_in_entry_roi": centroid_in_longitudinal_roi,
        "longitudinal_spatial_gate_satisfied": spatial_gate_satisfied,
        "longitudinal_transition_pending": (longitudinal_transition_pending and process_stage == 0),
        "longitudinal_transition_confirmed": process_stage >= 1,
        "process_zone": process_zone,
        "process_zone_source": process_zone_source,
        "frame_delta": frame_delta,
        "projected_axis_change_degrees": axis_change,
        "area_ratio": candidate.area / previous_area,
        "projected_length_ratio": candidate.projected_length / previous_length,
        "absolute_roll_angle_claimed": False,
    }


def _tracker_candidate_with_active_zone(
    candidate: _TrackerCandidate,
    active: Mapping[str, Any],
) -> _TrackerCandidate:
    """Keep the visible process role aligned with the monotonic physical track state."""

    process_stage = max(int(active.get("process_stage") or 0), 0)
    process_zone = "longitudinal_conveyor" if process_stage >= 1 else "transfer"
    if candidate.process_stage == process_stage and candidate.process_zone == process_zone:
        return candidate
    return replace(
        candidate,
        process_stage=process_stage,
        process_zone=process_zone,
    )


def _tracker_selected_result(
    node: RuntimeNodeSpec,
    source: SegmentPayload,
    candidate: _TrackerCandidate,
    *,
    candidates: Sequence[_TrackerCandidate],
    active: Mapping[str, Any],
    status: str,
    selection_confidence: float,
    selection_reason: str,
    score_components: Mapping[str, float],
    movement: Mapping[str, Any],
    candidate_count: int,
    eligible_candidate_count: int,
    machine_events: Sequence[str],
    reset_reason: str | None,
    config: _TrackerConfig,
) -> NodeExecutionResult:
    track_id = str(active["track_id"])
    tracking_metadata = _tracker_metadata(
        status=status,
        track_id=track_id,
        selection_confidence=selection_confidence,
        selection_reason=selection_reason,
        candidate_count=candidate_count,
        eligible_candidate_count=eligible_candidate_count,
        movement=movement,
        score_components=score_components,
        machine_events=machine_events,
        reset_reason=reset_reason,
        active=active,
    )
    tracking_metadata.update(
        {
            "candidate_selection_policy": config.candidate_selection_policy,
            "selected_candidate_count": 1,
            "non_active_candidate_count": max(eligible_candidate_count - 1, 0),
            "visible_length_fraction": candidate.visible_length_fraction,
            "visible_length_basis": ("projected_normalized_length_vs_configured_expected"),
            "centroid_normalized": list(candidate.centroid),
            "bbox_normalized": _tracker_box_list(candidate.box),
            "projected_length_normalized": candidate.projected_length,
            "projected_width_normalized": candidate.projected_width,
            **_tracker_process_metadata(
                config,
                centroid=candidate.centroid,
                polygon=candidate.polygon,
            ),
        }
    )
    object_reference = _tracked_object_reference(
        node,
        source,
        candidate,
        track_id=track_id,
        tracking_metadata=tracking_metadata,
    )
    payload = SegmentPayload(
        segments=[candidate.segment],
        image_width=source.image_width,
        image_height=source.image_height,
        image_context_id=source.image_context_id,
        objects=[object_reference],
        route=dict(source.route),
        metadata={**source.metadata, "active_stem_tracking": tracking_metadata},
    )
    outputs: dict[str, Any] = {"segments": payload}
    if config.debug_overlay:
        outputs["overlay"] = _tracker_overlay(
            source,
            config=config,
            status=status,
            reason=selection_reason,
            selected=candidate,
            candidates=candidates,
            track_id=track_id,
            confidence=selection_confidence,
            movement=movement,
            candidate_count=candidate_count,
            eligible_candidate_count=eligible_candidate_count,
            track_age_frames=int(active.get("age_frames") or 0),
        )
    return NodeExecutionResult(outputs=outputs)


def _tracker_empty_result(
    node: RuntimeNodeSpec,
    source: SegmentPayload | None,
    *,
    status: str,
    reason: str,
    preserved_track_id: str | None = None,
    selection_confidence: float = 0.0,
    candidate_count: int = 0,
    eligible_candidate_count: int = 0,
    candidates: Sequence[_TrackerCandidate] = (),
    machine_events: Sequence[str] = (),
    reset_reason: str | None = None,
    detail: str | None = None,
    config: _TrackerConfig | None = None,
) -> NodeExecutionResult:
    tracking_metadata = _tracker_metadata(
        status=status,
        track_id=None,
        selection_confidence=selection_confidence,
        selection_reason=reason,
        candidate_count=candidate_count,
        eligible_candidate_count=eligible_candidate_count,
        movement=_zero_tracker_movement(),
        score_components={},
        machine_events=machine_events,
        reset_reason=reset_reason,
        active=None,
        preserved_track_id=preserved_track_id,
        detail=detail,
    )
    if config is not None:
        tracking_metadata.update(
            {
                "candidate_selection_policy": config.candidate_selection_policy,
                "selected_candidate_count": 0,
                "non_active_candidate_count": eligible_candidate_count,
                **_tracker_process_metadata(config),
            }
        )
    payload = SegmentPayload(
        segments=[],
        image_width=source.image_width if source is not None else None,
        image_height=source.image_height if source is not None else None,
        image_context_id=source.image_context_id if source is not None else None,
        objects=[],
        route=dict(source.route) if source is not None else {},
        metadata={
            **(source.metadata if source is not None else {}),
            "active_stem_tracking": tracking_metadata,
        },
    )
    outputs: dict[str, Any] = {"segments": payload}
    if config is not None and config.debug_overlay:
        outputs["overlay"] = _tracker_overlay(
            source,
            config=config,
            status=status,
            reason=reason,
            selected=None,
            candidates=candidates,
            track_id=preserved_track_id,
            confidence=selection_confidence,
            movement=_zero_tracker_movement(),
            candidate_count=candidate_count,
            eligible_candidate_count=eligible_candidate_count,
            track_age_frames=0,
        )
    return NodeExecutionResult(outputs=outputs)


def _tracked_object_reference(
    node: RuntimeNodeSpec,
    source: SegmentPayload,
    candidate: _TrackerCandidate,
    *,
    track_id: str,
    tracking_metadata: Mapping[str, Any],
) -> RuntimeObjectReference:
    source_object = candidate.source_object
    image_context_id = (
        source_object.source_image_context_id
        if source_object is not None
        else source.image_context_id or "unknown-image-context"
    )
    provenance = list(source_object.provenance) if source_object is not None else []
    provenance.append(
        ProvenanceLink(
            source_kind="object",
            source_id=(
                source_object.object_id
                if source_object is not None
                else f"segment_{candidate.index + 1}"
            ),
            relationship="selected_as_active_stem",
            node_key=node.node_key,
        )
    )
    geometry = (
        dict(source_object.geometry)
        if source_object is not None
        else {
            "points": [{"x": x, "y": y} for x, y in candidate.polygon],
            "box": {
                "x_min": candidate.box.x_min,
                "y_min": candidate.box.y_min,
                "x_max": candidate.box.x_max,
                "y_max": candidate.box.y_max,
            },
        }
    )
    return RuntimeObjectReference(
        object_id=(
            source_object.object_id
            if source_object is not None
            else f"tracked_segment_{candidate.index + 1}"
        ),
        object_type="tracked_object",
        source_image_context_id=image_context_id,
        geometry=geometry,
        coordinate_system=(
            source_object.coordinate_system if source_object is not None else "normalized_polygon"
        ),
        label=(source_object.label if source_object is not None else candidate.segment.label),
        score=(source_object.score if source_object is not None else candidate.segment.score),
        class_id=source_object.class_id if source_object is not None else None,
        track_id=track_id,
        producer_node_key=node.node_key,
        provenance=provenance,
        metadata={
            **(source_object.metadata if source_object is not None else {}),
            "active_stem_tracking": dict(tracking_metadata),
            "upstream_object_id": source_object.object_id if source_object is not None else None,
            "upstream_track_id": candidate.upstream_track_id,
        },
    )


def _tracker_metadata(
    *,
    status: str,
    track_id: str | None,
    selection_confidence: float,
    selection_reason: str,
    candidate_count: int,
    eligible_candidate_count: int,
    movement: Mapping[str, Any],
    score_components: Mapping[str, float],
    machine_events: Sequence[str],
    reset_reason: str | None,
    active: Mapping[str, Any] | None,
    preserved_track_id: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": _TRACKER_SCHEMA_VERSION,
        "status": status,
        "track_id": track_id,
        "preserved_track_id": preserved_track_id,
        "selection_confidence": min(max(float(selection_confidence), 0.0), 1.0),
        "selection_reason": selection_reason,
        "candidate_count": candidate_count,
        "eligible_candidate_count": eligible_candidate_count,
        "movement": dict(movement),
        "score_components": {
            key: min(max(float(value), 0.0), 1.0) for key, value in score_components.items()
        },
        "process_zone": active.get("process_zone") if active is not None else None,
        "process_zone_source": (active.get("process_zone_source") if active is not None else None),
        "longitudinal_motion_streak": (
            int(active.get("longitudinal_motion_streak") or 0) if active is not None else 0
        ),
        "track_age_frames": int(active.get("age_frames") or 0) if active is not None else 0,
        "lost_frames": int(active.get("lost_frames") or 0) if active is not None else 0,
        "machine_events": list(machine_events),
        "state_reset_reason": reset_reason,
        "detail": detail,
        "fail_closed": status in {"lost", "ambiguous"},
        "projected_2d_motion_only": True,
        "absolute_roll_angle_claimed": False,
    }


def _tracker_process_metadata(
    config: _TrackerConfig,
    *,
    centroid: tuple[float, float] | None = None,
    polygon: Sequence[tuple[float, float]] | None = None,
) -> dict[str, Any]:
    geometry = config.process_geometry
    if geometry is None:
        return {
            "process_calibration_applied": False,
            "process_calibration_hash": None,
        }
    metadata: dict[str, Any] = {
        "process_calibration_applied": True,
        "process_calibration_hash": geometry.calibration_hash,
        "candidate_roi_source": "stem_curvature_process_calibration",
        "session_start_gate_normalized": {
            "start": list(geometry.session_start_gate[0]),
            "end": list(geometry.session_start_gate[1]),
        },
        "curvature_freeze_gate_normalized": {
            "start": list(geometry.curvature_freeze_gate[0]),
            "end": list(geometry.curvature_freeze_gate[1]),
        },
        "decision_gate_normalized": {
            "start": list(geometry.decision_gate[0]),
            "end": list(geometry.decision_gate[1]),
        },
        "decision_margin_mm": geometry.decision_margin_mm,
        "conveyor_direction": geometry.conveyor_direction,
        "rotation_reference_view": geometry.rotation_reference_view,
        "positive_rotation": geometry.positive_rotation,
    }
    if centroid is None:
        metadata.update(
            {
                "session_start_gate_reached": False,
                "session_start_gate_reference": "no_active_stem",
                "session_start_gate_progress_normalized": None,
                "curvature_freeze_gate_reached": False,
                "decision_gate_reached": False,
                "decision_margin_entered": False,
                "remaining_distance_to_decision_gate_mm": None,
            }
        )
        return metadata

    session_start_reached, session_start_progress, session_start_reference = (
        _tracker_session_start_gate_status(
            centroid,
            polygon,
            geometry,
        )
    )
    reference_process_x_mm = _tracker_leading_process_x_mm(polygon, geometry)
    freeze_remaining = _tracker_gate_remaining_mm(
        centroid,
        geometry.curvature_freeze_gate,
        geometry,
        gate_x_mm=geometry.curvature_freeze_gate_x_mm,
        reference_process_x_mm=reference_process_x_mm,
    )
    decision_remaining = _tracker_gate_remaining_mm(
        centroid,
        geometry.decision_gate,
        geometry,
        gate_x_mm=geometry.decision_gate_x_mm,
        reference_process_x_mm=reference_process_x_mm,
    )
    metadata.update(
        {
            "session_start_gate_reached": session_start_reached,
            "session_start_gate_reference": session_start_reference,
            "session_start_gate_progress_normalized": session_start_progress,
            "curvature_freeze_gate_reached": freeze_remaining <= 0,
            "decision_gate_reached": decision_remaining <= 0,
            "decision_margin_entered": decision_remaining <= geometry.decision_margin_mm,
            "remaining_distance_to_decision_gate_mm": max(decision_remaining, 0.0),
            "process_gate_reference": (
                "leading_stem_edge" if reference_process_x_mm is not None else "centroid"
            ),
            "leading_x_mm": reference_process_x_mm,
        }
    )
    return metadata


def _tracker_session_start_gate_status(
    centroid: tuple[float, float],
    polygon: Sequence[tuple[float, float]] | None,
    geometry: _TrackerProcessGeometry,
) -> tuple[bool, float | None, str]:
    """Evaluate a canonical corridor entry edge without assuming it is an X gate.

    The simplified stem workflow uses the right corridor boundary as its session
    entry while the log subsequently travels along process X.  That boundary is
    commonly parallel to X, so reducing it to an average X value starts the
    session at an unrelated longitudinal position.  For canonical corridors the
    candidate ROI identifies the interior side of the configured entry line; a
    session starts as soon as the selected mask crosses or touches that side.
    Explicit/legacy process geometries retain their calibrated X-gate behavior.
    """

    profile = getattr(geometry, "process_profile", None)
    if (
        profile is not None
        and getattr(profile, "measurement_geometry_mode", None) == "CANONICAL_VIEW_CORRIDOR_V1"
        and geometry.candidate_rois
    ):
        gate_start, gate_end = geometry.session_start_gate
        gate_dx = gate_end[0] - gate_start[0]
        gate_dy = gate_end[1] - gate_start[1]
        gate_length = math.hypot(gate_dx, gate_dy)
        if gate_length > 1.0e-9:

            def signed_distance(point: tuple[float, float]) -> float:
                return (
                    gate_dx * (point[1] - gate_start[1]) - gate_dy * (point[0] - gate_start[0])
                ) / gate_length

            corridor_distances = [
                signed_distance(point)
                for region in geometry.candidate_rois
                for point in region.points
            ]
            interior_reference = max(
                corridor_distances,
                key=lambda value: abs(value),
                default=0.0,
            )
            if abs(interior_reference) > 1.0e-9:
                interior_sign = 1.0 if interior_reference > 0 else -1.0
                sample_points = tuple(polygon or (centroid,))
                progress = max(interior_sign * signed_distance(point) for point in sample_points)
                return (
                    progress >= -1.0e-6,
                    float(progress),
                    (
                        "active_mask_corridor_boundary"
                        if polygon
                        else "active_centroid_corridor_boundary"
                    ),
                )

    reference_process_x_mm = _tracker_leading_process_x_mm(polygon, geometry)
    start_remaining = _tracker_gate_remaining_mm(
        centroid,
        geometry.session_start_gate,
        geometry,
        gate_x_mm=geometry.session_start_gate_x_mm,
        reference_process_x_mm=reference_process_x_mm,
    )
    return (
        start_remaining <= 0,
        None,
        "leading_stem_edge_x" if reference_process_x_mm is not None else "centroid_x",
    )


def _tracker_leading_process_x_mm(
    polygon: Sequence[tuple[float, float]] | None,
    geometry: _TrackerProcessGeometry,
) -> float | None:
    profile = getattr(geometry, "process_profile", None)
    if profile is None or not polygon:
        return None
    process_points = image_points_to_process(
        [
            (
                point[0] * max(profile.image_width - 1, 1),
                point[1] * max(profile.image_height - 1, 1),
            )
            for point in polygon
        ],
        profile,
    )
    x_values = [point.x_mm for point in process_points]
    if not x_values:
        return None
    return max(x_values) if geometry.conveyor_direction == "POSITIVE_X" else min(x_values)


def _tracker_gate_remaining_mm(
    centroid: tuple[float, float],
    gate: tuple[tuple[float, float], tuple[float, float]],
    geometry: _TrackerProcessGeometry,
    *,
    gate_x_mm: float | None = None,
    reference_process_x_mm: float | None = None,
) -> float:
    profile = getattr(geometry, "process_profile", None)
    if profile is not None and gate_x_mm is not None:
        if reference_process_x_mm is None:
            reference_process_x_mm = image_points_to_process(
                [
                    (
                        centroid[0] * max(profile.image_width - 1, 1),
                        centroid[1] * max(profile.image_height - 1, 1),
                    )
                ],
                profile,
            )[0].x_mm
        remaining = gate_x_mm - reference_process_x_mm
        if geometry.conveyor_direction == "NEGATIVE_X":
            remaining *= -1.0
        return remaining
    gate_x = (gate[0][0] + gate[1][0]) / 2.0
    normalized_remaining = gate_x - centroid[0]
    if geometry.conveyor_direction == "NEGATIVE_X":
        normalized_remaining *= -1.0
    return normalized_remaining * geometry.process_x_span_mm


def _tracker_overlay(
    source: SegmentPayload | None,
    *,
    config: _TrackerConfig,
    status: str,
    reason: str,
    selected: _TrackerCandidate | None,
    candidates: Sequence[_TrackerCandidate],
    track_id: str | None,
    confidence: float,
    movement: Mapping[str, Any],
    candidate_count: int,
    eligible_candidate_count: int,
    track_age_frames: int,
) -> OverlayPayload:
    image_context_id = source.image_context_id if source is not None else None
    primitives: list[OverlayPrimitive] = []
    for label, color, rois in (
        ("Transfer ROI", "#f4b740", config.transfer_rois),
        ("Longitudinal conveyor ROI", "#5db7ff", config.longitudinal_rois),
        (
            "Calibrated candidate ROI",
            "#ba68c8",
            config.process_geometry.candidate_rois if config.process_geometry else (),
        ),
    ):
        for roi in rois:
            geometry: dict[str, Any]
            primitive_type: str
            if roi.source_shape == "box":
                primitive_type = "zone"
                geometry = {
                    "x_min": roi.bounds.x_min,
                    "y_min": roi.bounds.y_min,
                    "x_max": roi.bounds.x_max,
                    "y_max": roi.bounds.y_max,
                }
            else:
                primitive_type = "polygon"
                geometry = {
                    "points": [{"x": x, "y": y} for x, y in roi.points],
                    "coordinate_system": "normalized_xy",
                }
            primitives.append(
                OverlayPrimitive(
                    primitive_type=primitive_type,
                    layer="stem_tracking_rois",
                    label=roi.label or label,
                    geometry=geometry,
                    image_context_id=image_context_id,
                    style={"stroke": color, "fill": "rgba(0,0,0,0.03)"},
                    metadata={"tracker_schema_version": _TRACKER_SCHEMA_VERSION},
                )
            )
    if config.process_geometry is not None:
        for label, color, gate in (
            ("Session start gate", "#ab47bc", config.process_geometry.session_start_gate),
            (
                "Curvature freeze gate",
                "#ffb300",
                config.process_geometry.curvature_freeze_gate,
            ),
            ("Decision gate", "#ef5350", config.process_geometry.decision_gate),
        ):
            gate_start, gate_end = gate
            primitives.append(
                OverlayPrimitive(
                    primitive_type="line",
                    layer="stem_tracking_process_gates",
                    label=label,
                    geometry={
                        "start": {"x": gate_start[0], "y": gate_start[1]},
                        "end": {"x": gate_end[0], "y": gate_end[1]},
                        "coordinate_system": "normalized_xy",
                    },
                    image_context_id=image_context_id,
                    style={"stroke": color, "stroke_width": 3.0},
                    metadata={
                        "process_calibration_hash": config.process_geometry.calibration_hash,
                        "conveyor_direction": config.process_geometry.conveyor_direction,
                    },
                )
            )
    for candidate in candidates:
        is_selected = selected is not None and candidate.index == selected.index
        display_candidate = selected if is_selected and selected is not None else candidate
        if display_candidate.process_zone == "transfer":
            display_role = "rotation_observation"
            display_label = "Rotation observation"
            color = "#49c47a"
            fill = "rgba(73,196,122,0.38)"
        else:
            display_role = "longitudinal_feed"
            display_label = "Longitudinal feed"
            color = "#2196f3"
            fill = "rgba(33,150,243,0.38)"
        primitives.append(
            OverlayPrimitive(
                primitive_type="polygon",
                layer="stem_tracking_candidates",
                label=(
                    f"{display_label} - {track_id or 'active stem'}"
                    if is_selected
                    else display_label
                ),
                confidence=confidence if is_selected else candidate.score,
                geometry={
                    "points": [{"x": x, "y": y} for x, y in candidate.polygon],
                    "coordinate_system": "normalized_polygon",
                },
                image_context_id=image_context_id,
                source_object_id=(
                    candidate.source_object.object_id
                    if candidate.source_object is not None
                    else None
                ),
                style={
                    "stroke": color,
                    "stroke_width": 3.0 if is_selected else 2.0,
                    "fill": fill,
                },
                metadata={
                    "status": status,
                    "selection_reason": reason,
                    "selected": is_selected,
                    "display_role": display_role,
                    "visible_length_fraction": display_candidate.visible_length_fraction,
                    "process_zone": display_candidate.process_zone,
                },
            )
        )
    handover = config.expected_handover_point
    primitives.append(
        OverlayPrimitive(
            primitive_type="point",
            layer="stem_tracking_status",
            label=f"{status}: {reason}",
            confidence=confidence,
            geometry={
                "x": handover[0],
                "y": handover[1],
                "coordinate_system": "normalized_xy",
            },
            image_context_id=image_context_id,
            style={"stroke": "#49c47a" if status == "active" else "#ffab40", "radius": 4},
            metadata={
                "track_id": track_id,
                "status": status,
                "selection_reason": reason,
                "candidate_count": candidate_count,
                "eligible_candidate_count": eligible_candidate_count,
                "process_zone": selected.process_zone if selected is not None else None,
                "track_age_frames": track_age_frames,
                "decision_gate_reached": (
                    _candidate_reached_decision_gate(selected, config=config)
                    if selected is not None
                    else False
                ),
                "movement": dict(movement),
            },
        )
    )
    if selected is not None:
        velocity_x = float(movement.get("velocity_x_normalized_per_frame") or 0.0)
        velocity_y = float(movement.get("velocity_y_normalized_per_frame") or 0.0)
        if math.hypot(velocity_x, velocity_y) > 1e-6:
            primitives.append(
                OverlayPrimitive(
                    primitive_type="arrow",
                    layer="stem_tracking_motion",
                    label=str(movement.get("movement_direction") or "motion"),
                    geometry={
                        "start": {"x": selected.centroid[0], "y": selected.centroid[1]},
                        "end": {
                            "x": selected.centroid[0] + velocity_x * 3.0,
                            "y": selected.centroid[1] + velocity_y * 3.0,
                        },
                        "coordinate_system": "normalized_xy",
                    },
                    image_context_id=image_context_id,
                    style={"stroke": "#00e5ff", "stroke_width": 2},
                    metadata={"projected_2d_motion_only": True},
                )
            )
    return OverlayPayload(
        primitives=primitives,
        display_context_id=image_context_id,
        metadata={
            "tracker_schema_version": _TRACKER_SCHEMA_VERSION,
            "status": status,
            "selection_reason": reason,
        },
    )


def _tracker_context_reset_reason(
    state: Mapping[str, Any],
    *,
    segments: SegmentPayload,
    frame: FramePayload | None,
    frame_sequence: int,
    timestamp: str,
    maximum_time_gap_seconds: float,
) -> str | None:
    previous_signature = state.get("image_signature")
    current_signature = _tracker_image_signature(segments, frame)
    if isinstance(previous_signature, Mapping) and previous_signature != current_signature:
        return "image_geometry_changed"
    previous_sequence = state.get("last_frame_sequence")
    if previous_sequence is not None and frame_sequence <= int(previous_sequence):
        return "frame_sequence_non_monotonic"
    previous_timestamp = _parse_tracker_timestamp(state.get("last_timestamp"))
    current_timestamp = _parse_tracker_timestamp(timestamp)
    if previous_timestamp is not None and current_timestamp is not None:
        delta_seconds = (current_timestamp - previous_timestamp).total_seconds()
        if delta_seconds <= 0:
            return "timestamp_non_monotonic"
        if delta_seconds > maximum_time_gap_seconds:
            return "timestamp_gap_exceeded"
    return None


def _tracker_image_signature(
    segments: SegmentPayload,
    frame: FramePayload | None,
) -> dict[str, Any]:
    width, height = _image_dimensions(segments, frame)
    calibration_hash = segments.metadata.get("calibration_hash")
    if frame is not None:
        calibration_hash = frame.metadata.get("calibration_hash", calibration_hash)
        if frame.image_context is not None:
            calibration_hash = frame.image_context.transform.parameters.get(
                "calibration_hash",
                calibration_hash,
            )
    return {
        "width": width,
        "height": height,
        "calibration_hash": calibration_hash,
    }


def _tracker_frame_sequence(
    context: RuntimeExecutionContext,
    frame: FramePayload | None,
    state: Mapping[str, Any],
) -> int:
    sequence = _frame_sequence(context, frame)
    if sequence is not None:
        return sequence
    previous = state.get("last_frame_sequence")
    return int(previous) + 1 if previous is not None else 0


def _tracker_machine_events(events: object, trigger: object) -> set[str]:
    event_types: set[str] = set()
    if isinstance(events, EventsPayload):
        event_types.update(item.event_type.strip().casefold() for item in events.events)
    if isinstance(trigger, TriggerPayload):
        body = trigger.body
        for key in ("event_type", "event", "machine_event", "state"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                event_types.add(value.strip().casefold())
        values = body.get("events")
        if isinstance(values, list):
            event_types.update(
                str(value).strip().casefold() for value in values if str(value).strip()
            )
    return event_types


def _complete_active_track(
    state: dict[str, Any],
    *,
    frame_sequence: int,
    reason: str,
    config: _TrackerConfig,
) -> None:
    active = state.pop("active_track", None)
    if not isinstance(active, Mapping):
        return
    completed = state.setdefault("completed_tracks", [])
    if not isinstance(completed, list):
        completed = []
        state["completed_tracks"] = completed
    completed.append(
        {
            "track_id": active.get("track_id"),
            "last_box": active.get("last_box"),
            "last_centroid": active.get("last_centroid"),
            "process_stage": active.get("process_stage"),
            "completed_sequence": frame_sequence,
            "completion_reason": reason,
        }
    )
    del completed[: -config.max_completed_tracks]


def _matches_completed_track(
    candidate: _TrackerCandidate,
    state: Mapping[str, Any],
    frame_sequence: int,
    config: _TrackerConfig,
) -> bool:
    for completed in state.get("completed_tracks") or []:
        if not isinstance(completed, Mapping):
            continue
        completed_sequence = int(completed.get("completed_sequence") or 0)
        if frame_sequence - completed_sequence > config.completed_exclusion_frames:
            continue
        box = _tracker_box_from_state(completed.get("last_box"))
        if box is None:
            continue
        if _tracker_box_iou(box, candidate.box) >= config.completed_exclusion_iou:
            return True
    return False


def _prune_completed_tracks(
    state: dict[str, Any],
    frame_sequence: int,
    config: _TrackerConfig,
) -> None:
    completed = state.get("completed_tracks")
    if not isinstance(completed, list):
        state["completed_tracks"] = []
        return
    state["completed_tracks"] = [
        item
        for item in completed[-config.max_completed_tracks :]
        if isinstance(item, Mapping)
        and frame_sequence - int(item.get("completed_sequence") or 0)
        <= config.completed_exclusion_frames
    ]


def _reset_tracker_state(
    state: dict[str, Any],
    *,
    preserve_sequence: bool = True,
) -> None:
    next_sequence = int(state.get("next_track_sequence") or 1)
    state.clear()
    state["schema_version"] = _TRACKER_SCHEMA_VERSION
    state["next_track_sequence"] = next_sequence if preserve_sequence else 1
    state["completed_tracks"] = []


def _tracker_active_track_id(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
) -> str | None:
    state = context.state.get(node.node_key)
    return _tracker_active_track_id_from_state(state) if isinstance(state, Mapping) else None


def _tracker_active_track_id_from_state(state: Mapping[str, Any]) -> str | None:
    active = state.get("active_track")
    if not isinstance(active, Mapping):
        return None
    return str(active.get("track_id") or "") or None


def _tracker_bounded_float(
    config: Mapping[str, Any],
    key: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    value = config.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be numeric")
    parsed = float(value)
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return parsed


def _tracker_bounded_int(
    config: Mapping[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = config.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return parsed


def _tracker_string_set(value: object) -> set[str]:
    values = value if isinstance(value, list | tuple | set) else str(value or "").split(",")
    return {str(item).strip().casefold() for item in values if str(item).strip()}


def _tracker_motion_vector(direction: str) -> tuple[float, float] | None:
    normalized = direction.strip().casefold()
    directions = {
        "any": None,
        "left": (-1.0, 0.0),
        "right": (1.0, 0.0),
        "up": (0.0, -1.0),
        "down": (0.0, 1.0),
    }
    if normalized not in directions:
        raise ValueError("expected_motion_direction must be one of any, left, right, up, or down")
    return directions[normalized]


def _tracker_direction_score(
    displacement: tuple[float, float],
    expected: tuple[float, float] | None,
) -> float:
    if expected is None:
        return 1.0
    magnitude = math.hypot(*displacement)
    if magnitude <= 1e-9:
        return 0.5
    cosine = (displacement[0] * expected[0] + displacement[1] * expected[1]) / magnitude
    return min(max((cosine + 1.0) * 0.5, 0.0), 1.0)


def _tracker_direction_label(velocity: tuple[float, float]) -> str:
    if math.hypot(*velocity) <= 1e-5:
        return "stationary"
    if abs(velocity[0]) >= abs(velocity[1]):
        return "right" if velocity[0] > 0 else "left"
    return "down" if velocity[1] > 0 else "up"


def _tracker_process_centroid_mm(
    centroid: tuple[float, float],
    geometry: _TrackerProcessGeometry | None,
) -> tuple[float, float] | None:
    if geometry is None or geometry.process_profile is None:
        return None
    profile = geometry.process_profile
    try:
        point = image_points_to_process(
            [
                (
                    centroid[0] * max(profile.image_width - 1, 1),
                    centroid[1] * max(profile.image_height - 1, 1),
                )
            ],
            profile,
        )[0]
    except (IndexError, TypeError, ValueError):
        return None
    return float(point.x_mm), float(point.y_mm)


def _tracker_process_motion_axis(
    velocity_mm_per_frame: tuple[float, float],
    *,
    config: _TrackerConfig,
) -> tuple[str, float]:
    absolute_x = abs(velocity_mm_per_frame[0])
    absolute_y = abs(velocity_mm_per_frame[1])
    speed = math.hypot(absolute_x, absolute_y)
    if speed < config.minimum_process_motion_mm_per_frame:
        return "stationary", 0.0
    confidence = max(absolute_x, absolute_y) / max(absolute_x + absolute_y, 1e-12)
    dominance = config.process_motion_axis_dominance_ratio
    if config.process_geometry is not None:
        # Projected centroid motion during the transverse throw contains a noticeable
        # X component. Require a clearer axial dominance in calibrated process space.
        dominance = max(dominance, 1.75)
    if absolute_x >= dominance * absolute_y:
        process_geometry = config.process_geometry
        if process_geometry is not None:
            conveyor_sign = 1.0 if process_geometry.conveyor_direction == "POSITIVE_X" else -1.0
            if velocity_mm_per_frame[0] * conveyor_sign <= 0.0:
                return "opposite_longitudinal_x", confidence
        return "longitudinal_x", confidence
    y_motion_share = absolute_y / max(absolute_x + absolute_y, 1e-12)
    if y_motion_share >= _tracker_transfer_y_minimum_motion_share(config):
        return "transfer_y", confidence
    return "ambiguous", confidence


def _tracker_longitudinal_motion_history(
    value: object,
) -> list[_TrackerLongitudinalMotionSample]:
    if not isinstance(value, list):
        return []
    output: list[_TrackerLongitudinalMotionSample] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        try:
            frame_sequence = int(item.get("frame_sequence") or 0)
            axis = str(item.get("axis") or "unavailable")
            forward_x_mm = float(item.get("forward_x_mm") or 0.0)
            absolute_x_mm = float(item.get("absolute_x_mm") or 0.0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(forward_x_mm) or not math.isfinite(absolute_x_mm):
            continue
        output.append(
            {
                "frame_sequence": frame_sequence,
                "axis": axis,
                "forward_x_mm": forward_x_mm,
                "absolute_x_mm": max(absolute_x_mm, 0.0),
            }
        )
    return output


def _tracker_point_in_regions(
    point: tuple[float, float],
    regions: Sequence[_TrackerRegion],
) -> bool:
    return any(_tracker_point_in_region(point, region) for region in regions)


def _tracker_point_in_region(
    point: tuple[float, float],
    region: _TrackerRegion,
) -> bool:
    if not _tracker_point_in_box(point, region.bounds):
        return False
    signs: set[bool] = set()
    for index, current in enumerate(region.points):
        following = region.points[(index + 1) % len(region.points)]
        cross = (following[0] - current[0]) * (point[1] - current[1]) - (
            following[1] - current[1]
        ) * (point[0] - current[0])
        if abs(cross) > 1.0e-10:
            signs.add(cross > 0.0)
    return len(signs) <= 1


def _tracker_transfer_y_minimum_motion_share(config: _TrackerConfig) -> float:
    """Map an operator-facing sensitivity to the required share of Y motion.

    At sensitivity zero, only pure Y motion is classified as transfer. At one,
    Y only needs to be at least as large as X. The default preserves the former
    calibrated 1.75:1 Y/X dominance threshold.
    """

    return 1.0 - 0.5 * config.transfer_y_motion_sensitivity


def _tracker_process_direction_label(
    velocity_mm_per_frame: tuple[float, float],
) -> str:
    if math.hypot(*velocity_mm_per_frame) <= 1e-9:
        return "stationary"
    if abs(velocity_mm_per_frame[0]) >= abs(velocity_mm_per_frame[1]):
        return "positive_x" if velocity_mm_per_frame[0] > 0 else "negative_x"
    return "positive_y" if velocity_mm_per_frame[1] > 0 else "negative_y"


def _zero_tracker_movement() -> dict[str, Any]:
    return {
        "delta_x_normalized": 0.0,
        "delta_y_normalized": 0.0,
        "velocity_x_normalized_per_frame": 0.0,
        "velocity_y_normalized_per_frame": 0.0,
        "speed_normalized_per_frame": 0.0,
        "movement_direction": "stationary",
        "delta_x_mm": None,
        "delta_y_mm": None,
        "velocity_x_mm_per_frame": None,
        "velocity_y_mm_per_frame": None,
        "process_movement_direction": "unavailable",
        "process_motion_axis": "unavailable",
        "process_motion_axis_confidence": 0.0,
        "longitudinal_motion_streak": 0,
        "longitudinal_motion_evidence_count": 0,
        "longitudinal_motion_confirmation_frames_required": 0,
        "longitudinal_motion_window_frames": 0,
        "longitudinal_forward_displacement_mm": 0.0,
        "longitudinal_absolute_x_path_mm": 0.0,
        "longitudinal_forward_progress_ratio": 0.0,
        "longitudinal_minimum_forward_displacement_mm": 0.0,
        "longitudinal_minimum_forward_progress_ratio": 0.0,
        "longitudinal_centroid_in_entry_roi": False,
        "longitudinal_spatial_gate_satisfied": False,
        "longitudinal_transition_pending": False,
        "longitudinal_transition_confirmed": False,
        "process_zone": None,
        "frame_delta": 0,
        "projected_axis_change_degrees": 0.0,
        "area_ratio": 1.0,
        "projected_length_ratio": 1.0,
        "absolute_roll_angle_claimed": False,
    }


def _tracker_box_iou(first: _TrackerBox, second: _TrackerBox) -> float:
    intersection = _tracker_intersection_area(first, second)
    union = first.area + second.area - intersection
    return intersection / union if union > 1e-12 else 0.0


def _tracker_intersection_area(first: _TrackerBox, second: _TrackerBox) -> float:
    width = max(min(first.x_max, second.x_max) - max(first.x_min, second.x_min), 0.0)
    height = max(min(first.y_max, second.y_max) - max(first.y_min, second.y_min), 0.0)
    return width * height


def _tracker_point_in_box(point: tuple[float, float], box: _TrackerBox) -> bool:
    return box.x_min <= point[0] <= box.x_max and box.y_min <= point[1] <= box.y_max


def _tracker_box_list(box: _TrackerBox) -> list[float]:
    return [box.x_min, box.y_min, box.x_max, box.y_max]


def _tracker_box_from_state(value: object) -> _TrackerBox | None:
    if not isinstance(value, list | tuple) or len(value) != 4:
        return None
    try:
        return _TrackerBox(*(float(item) for item in value))
    except (TypeError, ValueError):
        return None


def _tracker_point_from_state(value: object) -> tuple[float, float] | None:
    if not isinstance(value, list | tuple) or len(value) != 2:
        return None
    try:
        return float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None


def _parse_tracker_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


analysis_active_stem_tracker = ActiveStemTrackerHandler()
