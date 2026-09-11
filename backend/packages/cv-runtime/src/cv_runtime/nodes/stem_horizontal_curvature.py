from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np
from cv_contracts.runtime import (
    RuntimeNodeSpec,
    RuntimeStemCurvatureProcessCalibrationProfile,
)

from cv_runtime.context import NodeInputs, RuntimeExecutionContext
from cv_runtime.handlers import NodeExecutionResult
from cv_runtime.payloads import (
    FramePayload,
    OverlayPayload,
    OverlayPrimitive,
    ProvenanceLink,
    ResultPayload,
    RuntimeObjectReference,
    SegmentPayload,
    StemHorizontalCommonSegmentDescriptor,
    StemHorizontalCurvaturePayload,
    StemHorizontalCurvatureProfilePoint,
    StemHorizontalWidthPoint,
)
from cv_runtime.stem_horizontal_curvature import (
    ALGORITHM_VERSION,
    REFERENCE_METHOD,
    StemHorizontalCurvatureConfig,
    StemHorizontalCurvatureResult,
    freeze_gate_reached,
    mask_leading_x_mm,
    measure_stem_horizontal_curvature,
)
from cv_runtime.stem_process_coordinates import (
    process_points_to_image,
    process_profile_for_aligned_measurement_grid,
    process_profile_for_rectified_view,
    validate_stem_curvature_process_calibration,
    warp_mask_to_rectified,
)


@dataclass(frozen=True)
class StemHorizontalCurvatureHandler:
    node_type: str = "analysis.stem_horizontal_curvature"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        segments = inputs.get("segments")
        frame = inputs.get("frame")
        eligibility = inputs.get("eligibility")
        config = node.config or {}
        if not isinstance(segments, SegmentPayload) or not isinstance(frame, FramePayload):
            return _invalid_result(
                context,
                node,
                segments=segments if isinstance(segments, SegmentPayload) else None,
                frame=frame if isinstance(frame, FramePayload) else None,
                reason="curvature_inputs_missing",
            )

        profile, profile_error = _process_profile(frame)
        if profile is None:
            return _invalid_result(
                context,
                node,
                segments=segments,
                frame=frame,
                reason=profile_error or "process_calibration_missing",
            )
        binding_error = _binding_error(segments, frame, profile, context=context)
        if binding_error is not None:
            return _invalid_result(
                context,
                node,
                segments=segments,
                frame=frame,
                profile=profile,
                reason=binding_error,
            )
        transform_parameters = (
            frame.image_context.transform.parameters if frame.image_context else None
        )
        view_process_profile = process_profile_for_rectified_view(
            profile,
            image_width=int(frame.width or profile.image_width),
            image_height=int(frame.height or profile.image_height),
            transform_parameters=transform_parameters,
        )

        if eligibility is not None:
            if not isinstance(eligibility, ResultPayload):
                return _invalid_result(
                    context,
                    node,
                    segments=segments,
                    frame=frame,
                    profile=view_process_profile,
                    reason="measurement_eligibility_payload_invalid",
                )
            eligibility_values = eligibility.result
            task_role = str(eligibility_values.get("task_role") or "").upper()
            if (
                eligibility.decision is not True
                or eligibility_values.get("eligible") is not True
                or task_role != "POSITIVE"
            ):
                reason_codes = eligibility_values.get("reason_codes")
                detail = (
                    ", ".join(str(item) for item in reason_codes)
                    if isinstance(reason_codes, list)
                    else "eligibility decision did not accept a positive measurement frame"
                )
                return _invalid_result(
                    context,
                    node,
                    segments=segments,
                    frame=frame,
                    profile=view_process_profile,
                    reason="measurement_eligibility_rejected",
                    detail=detail,
                )

        selected, selection_error = _active_stem(segments)
        if selected is None:
            return _invalid_result(
                context,
                node,
                segments=segments,
                frame=frame,
                profile=view_process_profile,
                reason=selection_error or "active_stem_missing",
            )
        segment, tracked_object = selected
        tracking = _tracking_metadata(segments, tracked_object)
        tracking_confidence = _bounded_confidence(tracking.get("selection_confidence"))
        segmentation_score = _bounded_confidence(
            tracked_object.score if tracked_object.score is not None else segment.score
        )
        try:
            algorithm_config = _algorithm_config(config)
            minimum_tracking_confidence = _bounded_config(
                config, "minimum_tracking_confidence", 0.35
            )
            minimum_segmentation_score = _bounded_config(
                config, "minimum_segmentation_score", 0.35
            )
            minimum_geometry_confidence = _bounded_config(
                config, "minimum_geometry_confidence", 0.4
            )
        except (TypeError, ValueError) as exc:
            return _invalid_result(
                context,
                node,
                segments=segments,
                frame=frame,
                profile=view_process_profile,
                track_id=tracked_object.track_id,
                source_object=tracked_object,
                reason="curvature_configuration_invalid",
                detail=str(exc),
            )

        if tracking.get("status") != "active":
            return _invalid_result(
                context,
                node,
                segments=segments,
                frame=frame,
                profile=view_process_profile,
                track_id=tracked_object.track_id,
                source_object=tracked_object,
                reason="active_stem_tracking_not_active",
            )
        if tracking_confidence < minimum_tracking_confidence:
            return _invalid_result(
                context,
                node,
                segments=segments,
                frame=frame,
                profile=view_process_profile,
                track_id=tracked_object.track_id,
                source_object=tracked_object,
                reason="active_stem_tracking_confidence_low",
            )
        if segmentation_score < minimum_segmentation_score:
            return _invalid_result(
                context,
                node,
                segments=segments,
                frame=frame,
                profile=view_process_profile,
                track_id=tracked_object.track_id,
                source_object=tracked_object,
                reason="active_stem_segmentation_score_low",
            )

        view_mask = _segment_mask(segment, view_process_profile)
        mask = (
            warp_mask_to_rectified(view_mask, view_process_profile)
            if isinstance(
                (transform_parameters or {}).get("homography_view_to_process"), list
            )
            else view_mask
        )
        measurement_profile = process_profile_for_aligned_measurement_grid(
            view_process_profile
        )
        leading_x = mask_leading_x_mm(mask, measurement_profile)
        if leading_x is not None and freeze_gate_reached(leading_x, measurement_profile):
            payload = _base_payload(
                context,
                node,
                segments=segments,
                frame=frame,
                profile=measurement_profile,
                track_id=tracked_object.track_id,
                source_object=tracked_object,
                valid=False,
                observation_state="curvature_frozen",
                reason_codes=["curvature_freeze_gate_reached"],
                metadata={
                    "leading_x_mm": leading_x,
                    "tracking_confidence": tracking_confidence,
                    "segmentation_score": segmentation_score,
                    "geometry_observation_performed": False,
                },
            )
            return NodeExecutionResult(
                outputs=_outputs(payload, view_process_profile, config=config, frozen=True)
            )

        minimum_interval_ms = max(
            float(config.get("minimum_observation_interval_ms", 0.0) or 0.0),
            0.0,
        )
        if minimum_interval_ms > 0:
            node_state = context.state.setdefault(node.node_key, {})
            if not isinstance(node_state, dict):
                node_state = {}
                context.state[node.node_key] = node_state
            current_track_id = tracked_object.track_id
            if node_state.get("track_id") != current_track_id:
                node_state.clear()
                node_state["track_id"] = current_track_id
            now_perf = time.perf_counter()
            previous_perf = float(node_state.get("last_observation_perf") or 0.0)
            if previous_perf and (now_perf - previous_perf) * 1000.0 < minimum_interval_ms:
                return _invalid_result(
                    context,
                    node,
                    segments=segments,
                    frame=frame,
                    profile=view_process_profile,
                    track_id=current_track_id,
                    source_object=tracked_object,
                    reason="curvature_rate_limited",
                    detail=(
                        "The next geometry observation is intentionally deferred so the "
                        "high-rate surface-phase path retains CPU time."
                    ),
                )
            node_state["last_observation_perf"] = now_perf

        try:
            measured = measure_stem_horizontal_curvature(
                mask,
                measurement_profile,
                config=algorithm_config,
                internal_aligned_process_grid=True,
            )
        except (TypeError, ValueError) as exc:
            return _invalid_result(
                context,
                node,
                segments=segments,
                frame=frame,
                profile=view_process_profile,
                track_id=tracked_object.track_id,
                source_object=tracked_object,
                reason="horizontal_curvature_measurement_failed",
                detail=str(exc),
            )

        calibration_confidence = max(
            0.0,
            1.0
            - profile.rms_reprojection_error_px
            / max(profile.maximum_allowed_reprojection_error_px, 1e-9),
        )
        input_confidence = min(
            tracking_confidence,
            segmentation_score,
            calibration_confidence,
        )
        geometry_confidence = min(measured.geometry_confidence, input_confidence)
        valid = measured.valid and geometry_confidence >= minimum_geometry_confidence
        reason_codes = list(measured.reason_codes)
        if measured.valid and not valid:
            reason_codes.append("horizontal_curvature_geometry_confidence_low")
        payload = _measured_payload(
            context,
            node,
            segments=segments,
            frame=frame,
            profile=measurement_profile,
            track_id=tracked_object.track_id or "",
            source_object=tracked_object,
            measured=measured,
            valid=valid,
            geometry_confidence=geometry_confidence,
            reason_codes=reason_codes,
            tracking_confidence=tracking_confidence,
            segmentation_score=segmentation_score,
            calibration_confidence=calibration_confidence,
        )
        return NodeExecutionResult(outputs=_outputs(payload, view_process_profile, config=config))


def _process_profile(
    frame: FramePayload,
) -> tuple[RuntimeStemCurvatureProcessCalibrationProfile | None, str | None]:
    value = frame.metadata.get("stem_curvature_process_calibration")
    if value is None:
        return None, "process_calibration_missing"
    try:
        profile = validate_stem_curvature_process_calibration(value)
    except Exception:
        return None, "process_calibration_invalid"
    return profile, None


def _binding_error(
    segments: SegmentPayload,
    frame: FramePayload,
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
    *,
    context: RuntimeExecutionContext,
) -> str | None:
    image_context = frame.image_context
    if image_context is None:
        return "process_rectified_image_context_missing"
    if image_context.transform.transform_type not in {"perspective_rectify", "masked_crop"}:
        return "process_rectified_frame_required"
    expected_width = image_context.transform.parameters.get(
        "output_width", profile.image_width
    )
    expected_height = image_context.transform.parameters.get(
        "output_height", profile.image_height
    )
    if (frame.width, frame.height) != (expected_width, expected_height):
        return "process_calibration_resolution_mismatch"
    if (segments.image_width, segments.image_height) != (expected_width, expected_height):
        return "active_stem_mask_resolution_mismatch"
    if segments.image_context_id != image_context.context_id:
        return "active_stem_image_context_mismatch"
    if frame.metadata.get("process_calibration_hash") != profile.calibration_hash:
        return "process_calibration_hash_mismatch"
    if context.channel_id is not None and context.channel_id != profile.source_channel_id:
        return "process_calibration_channel_mismatch"
    return None


def _active_stem(
    segments: SegmentPayload,
) -> tuple[tuple[Any, RuntimeObjectReference] | None, str | None]:
    if len(segments.segments) != 1 or len(segments.objects) != 1:
        return None, "single_active_stem_required"
    tracked = segments.objects[0]
    if tracked.object_type != "tracked_object" or not tracked.track_id:
        return None, "active_stem_track_id_missing"
    if len(segments.segments[0].polygon) < 3:
        return None, "active_stem_polygon_missing"
    return (segments.segments[0], tracked), None


def _tracking_metadata(
    segments: SegmentPayload,
    tracked_object: RuntimeObjectReference,
) -> Mapping[str, Any]:
    object_value = tracked_object.metadata.get("active_stem_tracking")
    if isinstance(object_value, Mapping):
        return object_value
    payload_value = segments.metadata.get("active_stem_tracking")
    return payload_value if isinstance(payload_value, Mapping) else {}


def _segment_mask(segment: Any, profile: RuntimeStemCurvatureProcessCalibrationProfile) -> Any:
    points = np.asarray(
        [
            [
                float(point.x) * max(profile.image_width - 1, 1),
                float(point.y) * max(profile.image_height - 1, 1),
            ]
            for point in segment.polygon
        ],
        dtype=np.float32,
    )
    mask = np.zeros((profile.image_height, profile.image_width), dtype=np.uint8)
    cv2.fillPoly(mask, [np.rint(points).astype(np.int32)], 255)
    return mask


def _algorithm_config(config: Mapping[str, Any]) -> StemHorizontalCurvatureConfig:
    return StemHorizontalCurvatureConfig(
        station_count=_integer_config(config, "station_count", 96),
        inner_longitudinal_fraction=_bounded_config(
            config, "inner_longitudinal_fraction", 0.8, lower_exclusive=True
        ),
        minimum_visible_length_ratio=_bounded_config(
            config, "minimum_visible_length_ratio", 0.55, lower_exclusive=True
        ),
        minimum_common_segment_ratio=_bounded_config(
            config, "minimum_common_segment_ratio", 0.4, lower_exclusive=True
        ),
        minimum_valid_station_ratio=_bounded_config(
            config, "minimum_valid_station_ratio", 0.65, lower_exclusive=True
        ),
        minimum_valid_station_count=_integer_config(
            config, "minimum_valid_station_count", 24
        ),
        morphology_kernel_px=_integer_config(config, "morphology_kernel_px", 3),
        minimum_component_area_px=_integer_config(
            config, "minimum_component_area_px", 120
        ),
        meaningful_component_fraction=_bounded_config(
            config, "meaningful_component_fraction", 0.08, lower_exclusive=True
        ),
        minimum_selected_component_fraction=_bounded_config(
            config, "minimum_selected_component_fraction", 0.85, lower_exclusive=True
        ),
        minimum_width_mm=_positive_config(config, "minimum_width_mm", 10.0),
        maximum_local_width_deviation_ratio=_positive_config(
            config, "maximum_local_width_deviation_ratio", 0.15
        ),
        width_outlier_mad_scale=_positive_config(
            config, "width_outlier_mad_scale", 4.0
        ),
        centerline_outlier_mad_scale=_positive_config(
            config, "centerline_outlier_mad_scale", 4.5
        ),
        smoothing_window=_integer_config(config, "smoothing_window", 7),
        endpoint_window_fraction=_bounded_config(
            config, "endpoint_window_fraction", 0.12, lower_exclusive=True
        ),
        straight_bow_threshold_mm=_nonnegative_config(
            config, "straight_bow_threshold_mm", 2.0
        ),
        s_bow_secondary_lobe_ratio=_bounded_config(
            config, "s_bow_secondary_lobe_ratio", 0.35, lower_exclusive=True
        ),
    ).validate()


def _measured_payload(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    *,
    segments: SegmentPayload,
    frame: FramePayload,
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
    track_id: str,
    source_object: RuntimeObjectReference,
    measured: StemHorizontalCurvatureResult,
    valid: bool,
    geometry_confidence: float,
    reason_codes: list[str],
    tracking_confidence: float,
    segmentation_score: float,
    calibration_confidence: float,
) -> StemHorizontalCurvaturePayload:
    denominator_length = max(measured.common_segment.length_mm, 1e-9)
    denominator_width = max(measured.median_width_mm or 0.0, 1e-9)
    centerline = [
        StemHorizontalCurvatureProfilePoint(
            x_mm=station.x_mm,
            x_normalized=station.x_normalized,
            y_center_mm=station.y_center_mm,
            y_reference_mm=station.y_reference_mm,
            residual_mm=station.residual_mm,
            residual_normalized_length=station.residual_mm / denominator_length,
            residual_normalized_width=(
                station.residual_mm / denominator_width
                if measured.median_width_mm is not None
                else None
            ),
            weight=station.weight,
        )
        for station in measured.stations
        if station.valid
        and station.y_center_mm is not None
        and station.y_reference_mm is not None
        and station.residual_mm is not None
    ]
    widths = [
        StemHorizontalWidthPoint(
            x_mm=station.x_mm,
            x_normalized=station.x_normalized,
            width_mm=station.width_mm,
            width_normalized=(
                station.width_mm / measured.median_width_mm
                if station.width_mm is not None and measured.median_width_mm
                else None
            ),
            valid=station.valid,
            exclusion_reason=station.exclusion_reason,
        )
        for station in measured.stations
    ]
    descriptor = measured.common_segment
    metrics_allowed = valid
    return StemHorizontalCurvaturePayload(
        valid=valid,
        observation_state=(
            measured.observation_state
            if valid or not measured.valid
            else "invalid"
        ),
        track_id=track_id,
        source_object_id=source_object.object_id,
        image_context_id=segments.image_context_id,
        frame_sequence=_frame_sequence(context, frame),
        timestamp=frame.timestamp or context.now_iso(),
        calibration_id=profile.calibration_id,
        process_calibration_hash=profile.calibration_hash,
        signed_bow_mm=measured.signed_bow_mm if metrics_allowed else None,
        curvature_rms_mm=measured.curvature_rms_mm if metrics_allowed else None,
        curvature_maxabs_mm=measured.curvature_maxabs_mm if metrics_allowed else None,
        curvature_peak_to_peak_mm=(
            measured.curvature_peak_to_peak_mm if metrics_allowed else None
        ),
        signed_bow_normalized_length=(
            measured.signed_bow_normalized_length if metrics_allowed else None
        ),
        signed_bow_normalized_width=(
            measured.signed_bow_normalized_width if metrics_allowed else None
        ),
        curvature_rms_normalized_length=(
            measured.curvature_rms_normalized_length if metrics_allowed else None
        ),
        curvature_rms_normalized_width=(
            measured.curvature_rms_normalized_width if metrics_allowed else None
        ),
        curvature_maxabs_normalized_length=(
            measured.curvature_maxabs_normalized_length if metrics_allowed else None
        ),
        curvature_maxabs_normalized_width=(
            measured.curvature_maxabs_normalized_width if metrics_allowed else None
        ),
        curvature_peak_to_peak_normalized_length=(
            measured.curvature_peak_to_peak_normalized_length if metrics_allowed else None
        ),
        curvature_peak_to_peak_normalized_width=(
            measured.curvature_peak_to_peak_normalized_width if metrics_allowed else None
        ),
        centerline_profile=centerline,
        width_profile=widths,
        visible_length_ratio=measured.visible_length_ratio,
        valid_station_ratio=measured.valid_station_ratio,
        common_segment_descriptor=StemHorizontalCommonSegmentDescriptor(
            x_start_mm=descriptor.x_start_mm,
            x_end_mm=descriptor.x_end_mm,
            length_mm=descriptor.length_mm,
            visible_x_start_mm=descriptor.visible_x_start_mm,
            visible_x_end_mm=descriptor.visible_x_end_mm,
            visible_length_mm=descriptor.visible_length_mm,
            configured_inner_fraction=descriptor.configured_inner_fraction,
            common_segment_ratio=descriptor.common_segment_ratio,
            minimum_common_segment_ratio=descriptor.minimum_common_segment_ratio,
            comparable=descriptor.comparable and valid,
        ),
        median_width_mm=measured.median_width_mm,
        geometry_confidence=geometry_confidence,
        bow_shape_hint=measured.bow_shape_hint if valid else "uncertain",
        fragment_status=measured.fragment_status,
        meaningful_component_count=measured.meaningful_component_count,
        selected_component_fraction=measured.selected_component_fraction,
        curvature_roi_clipped=measured.curvature_roi_clipped,
        touches_curvature_roi_boundary=measured.touches_curvature_roi_boundary,
        quality_flags=list(measured.quality_flags),
        reason_codes=reason_codes,
        provenance=_provenance(node, frame, source_object, profile),
        metadata={
            "tracking_confidence": tracking_confidence,
            "segmentation_score": segmentation_score,
            "calibration_confidence": calibration_confidence,
            "measurement_geometry_mode": profile.measurement_geometry_mode,
            "canonical_view_profile_id": profile.canonical_view_profile_id,
            "canonical_view_profile_checksum": profile.canonical_view_profile_checksum,
            "calibration_rms_reprojection_error_px": (
                profile.rms_reprojection_error_px
            ),
            "geometry_observation_performed": True,
            "no_fragment_reconstruction": True,
            "reference_plane_id": profile.reference_plane.plane_id,
            "signed_bow_sign_convention": "POSITIVE_TOWARD_PROCESS_POSITIVE_Y",
            "comparison_strategy": "INNER_VISIBLE_LENGTH_FRACTION_V1",
        },
    )


def _base_payload(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    *,
    segments: SegmentPayload | None,
    frame: FramePayload | None,
    profile: RuntimeStemCurvatureProcessCalibrationProfile | None,
    track_id: str | None,
    source_object: RuntimeObjectReference | None,
    valid: bool,
    observation_state: str,
    reason_codes: list[str],
    metadata: dict[str, Any] | None = None,
) -> StemHorizontalCurvaturePayload:
    payload_metadata: dict[str, Any] = {
        "geometry_observation_performed": False,
        **(
            {
                "measurement_geometry_mode": profile.measurement_geometry_mode,
                "canonical_view_profile_id": profile.canonical_view_profile_id,
                "canonical_view_profile_checksum": profile.canonical_view_profile_checksum,
            }
            if profile is not None
            else {}
        ),
        **(metadata or {}),
    }
    return StemHorizontalCurvaturePayload(
        valid=valid,
        observation_state=observation_state,
        track_id=track_id,
        source_object_id=source_object.object_id if source_object else None,
        image_context_id=(
            segments.image_context_id
            if segments is not None
            else frame.image_context.context_id
            if frame is not None and frame.image_context is not None
            else None
        ),
        frame_sequence=_frame_sequence(context, frame),
        timestamp=(frame.timestamp if frame is not None else None) or context.now_iso(),
        calibration_id=profile.calibration_id if profile else None,
        process_calibration_hash=profile.calibration_hash if profile else None,
        reason_codes=reason_codes,
        provenance=(
            _provenance(node, frame, source_object, profile)
            if frame is not None and profile is not None
            else []
        ),
        metadata=payload_metadata,
    )


def _invalid_result(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    *,
    segments: SegmentPayload | None,
    frame: FramePayload | None,
    reason: str,
    profile: RuntimeStemCurvatureProcessCalibrationProfile | None = None,
    track_id: str | None = None,
    source_object: RuntimeObjectReference | None = None,
    detail: str | None = None,
) -> NodeExecutionResult:
    payload = _base_payload(
        context,
        node,
        segments=segments,
        frame=frame,
        profile=profile,
        track_id=track_id,
        source_object=source_object,
        valid=False,
        observation_state="invalid",
        reason_codes=[reason],
        metadata={
            "geometry_observation_performed": False,
            **({"detail": detail} if detail else {}),
        },
    )
    return NodeExecutionResult(
        outputs=_outputs(payload, profile, config=node.config or {})
    )


def _provenance(
    node: RuntimeNodeSpec,
    frame: FramePayload,
    source_object: RuntimeObjectReference | None,
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
) -> list[ProvenanceLink]:
    links = [
        ProvenanceLink(
            source_kind="image_context",
            source_id=(
                frame.image_context.context_id if frame.image_context else "missing-context"
            ),
            relationship="measured_in_rectified_process_frame",
            node_key=node.node_key,
        ),
        ProvenanceLink(
            source_kind="operator_config",
            source_id=profile.calibration_hash,
            relationship="uses_stem_curvature_process_calibration",
            node_key=node.node_key,
            metadata={"calibration_id": profile.calibration_id},
        ),
    ]
    if source_object is not None:
        links.append(
            ProvenanceLink(
                source_kind="object",
                source_id=source_object.object_id,
                relationship="measures_active_stem_mask",
                node_key=node.node_key,
                metadata={"track_id": source_object.track_id},
            )
        )
    return links


def _outputs(
    payload: StemHorizontalCurvaturePayload,
    profile: RuntimeStemCurvatureProcessCalibrationProfile | None,
    *,
    config: Mapping[str, Any],
    frozen: bool = False,
) -> dict[str, Any]:
    outputs: dict[str, Any] = {"curvature": payload}
    if bool(config.get("debug_overlay", True)) and profile is not None:
        outputs["overlay"] = _overlay(payload, profile, frozen=frozen)
    return outputs


def _overlay(
    payload: StemHorizontalCurvaturePayload,
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
    *,
    frozen: bool,
) -> OverlayPayload:
    context_id = payload.image_context_id
    primitives: list[OverlayPrimitive] = []
    roi_points = [
        _normalized_process_point(point.x_mm, point.y_mm, profile)
        for point in profile.curvature_observation_roi.process_points
    ]
    primitives.append(
        OverlayPrimitive(
            primitive_type="polygon",
            layer="stem_curvature_roi",
            label="Curvature Observation ROI",
            geometry={"points": roi_points, "coordinate_system": "normalized_xy"},
            image_context_id=context_id,
            style={"stroke": "#40c4ff", "stroke_width": 2, "fill_opacity": 0.04},
        )
    )
    bounds = profile.process_bounds
    axis_y = min(max(0.0, bounds.y_min_mm), bounds.y_max_mm)
    axis_x = min(max(0.0, bounds.x_min_mm), bounds.x_max_mm)
    primitives.extend(
        [
            OverlayPrimitive(
                primitive_type="axis",
                layer="stem_process_axes",
                label="X [mm]",
                geometry={
                    "start": _normalized_process_point(bounds.x_min_mm, axis_y, profile),
                    "end": _normalized_process_point(bounds.x_max_mm, axis_y, profile),
                    "coordinate_system": "normalized_xy",
                },
                image_context_id=context_id,
                style={"stroke": "#ffca28", "stroke_width": 1.5},
            ),
            OverlayPrimitive(
                primitive_type="axis",
                layer="stem_process_axes",
                label="Y [mm]",
                geometry={
                    "start": _normalized_process_point(axis_x, bounds.y_min_mm, profile),
                    "end": _normalized_process_point(axis_x, bounds.y_max_mm, profile),
                    "coordinate_system": "normalized_xy",
                },
                image_context_id=context_id,
                style={"stroke": "#ffca28", "stroke_width": 1.5},
            ),
        ]
    )
    gate = profile.curvature_freeze_gate
    primitives.append(
        OverlayPrimitive(
            primitive_type="line",
            layer="stem_curvature_gate",
            label="Curvature Freeze Gate",
            geometry={
                "start": _normalized_process_point(
                    gate.process_start.x_mm, gate.process_start.y_mm, profile
                ),
                "end": _normalized_process_point(
                    gate.process_end.x_mm, gate.process_end.y_mm, profile
                ),
                "coordinate_system": "normalized_xy",
            },
            image_context_id=context_id,
            style={"stroke": "#ff5252" if frozen else "#ff8a80", "stroke_width": 2},
        )
    )
    if payload.centerline_profile:
        centerline = [
            _normalized_process_point(item.x_mm, item.y_center_mm, profile)
            for item in payload.centerline_profile
        ]
        reference = [
            _normalized_process_point(item.x_mm, item.y_reference_mm, profile)
            for item in payload.centerline_profile
        ]
        primitives.extend(
            [
                OverlayPrimitive(
                    primitive_type="line",
                    layer="stem_curvature_centerline",
                    label="Metric centerline",
                    geometry={"points": centerline, "coordinate_system": "normalized_xy"},
                    image_context_id=context_id,
                    style={"stroke": "#00e676", "stroke_width": 2.5},
                ),
                OverlayPrimitive(
                    primitive_type="line",
                    layer="stem_curvature_reference",
                    label=REFERENCE_METHOD,
                    geometry={"points": reference, "coordinate_system": "normalized_xy"},
                    image_context_id=context_id,
                    style={"stroke": "#ffffff", "stroke_width": 1.5, "dash": [5, 4]},
                ),
            ]
        )
        stride = max(len(payload.centerline_profile) // 16, 1)
        for item in payload.centerline_profile[::stride]:
            primitives.append(
                OverlayPrimitive(
                    primitive_type="measurement",
                    layer="stem_curvature_residual",
                    label=f"r={item.residual_mm:.1f} mm",
                    geometry={
                        "start": _normalized_process_point(
                            item.x_mm, item.y_reference_mm, profile
                        ),
                        "end": _normalized_process_point(
                            item.x_mm, item.y_center_mm, profile
                        ),
                        "coordinate_system": "normalized_xy",
                    },
                    image_context_id=context_id,
                    style={"stroke": "#ff4081", "stroke_width": 1},
                    metadata={"residual_mm": item.residual_mm},
                )
            )
    status = (
        f"bow={payload.signed_bow_mm:.1f} mm, rms={payload.curvature_rms_mm:.1f} mm"
        if payload.valid
        and payload.signed_bow_mm is not None
        and payload.curvature_rms_mm is not None
        else f"curvature invalid: {', '.join(payload.reason_codes) or payload.observation_state}"
    )
    primitives.append(
        OverlayPrimitive(
            primitive_type="label",
            layer="stem_curvature_status",
            label=status,
            confidence=payload.geometry_confidence,
            geometry={"x": 0.02, "y": 0.04, "coordinate_system": "normalized_xy"},
            image_context_id=context_id,
            style={"fill": "#00e676" if payload.valid else "#ff5252"},
            metadata={
                "valid": payload.valid,
                "observation_state": payload.observation_state,
                "reason_codes": list(payload.reason_codes),
                "visible_length_ratio": payload.visible_length_ratio,
                "valid_station_ratio": payload.valid_station_ratio,
                "common_segment_comparable": payload.common_segment_descriptor.comparable,
                "algorithm_version": ALGORITHM_VERSION,
            },
        )
    )
    return OverlayPayload(
        primitives=primitives,
        display_context_id=context_id,
        metadata={
            "track_id": payload.track_id,
            "calibration_id": profile.calibration_id,
            "geometry_confidence": payload.geometry_confidence,
        },
    )


def _normalized_process_point(
    x_mm: float,
    y_mm: float,
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
) -> dict[str, float]:
    # Process bounds can legitimately extend a few pixels beyond the finite image
    # after a projective calibration. Diagnostic overlays must remain bounded and,
    # more importantly, must never invalidate an otherwise valid measurement.
    image_point = process_points_to_image(
        [(x_mm, y_mm)],
        profile,
        clip_to_image=True,
    )[0]
    return {
        "x": image_point.x_px / max(profile.image_width - 1, 1),
        "y": image_point.y_px / max(profile.image_height - 1, 1),
    }


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


def _bounded_confidence(value: Any) -> float:
    try:
        return min(max(float(value), 0.0), 1.0)
    except (TypeError, ValueError):
        return 0.0


def _bounded_config(
    config: Mapping[str, Any],
    key: str,
    default: float,
    *,
    lower_exclusive: bool = False,
) -> float:
    value = float(config.get(key, default))
    if value > 1 or value < 0 or (lower_exclusive and value == 0):
        interval = "(0, 1]" if lower_exclusive else "[0, 1]"
        raise ValueError(f"{key} must be in {interval}")
    return value


def _positive_config(config: Mapping[str, Any], key: str, default: float) -> float:
    value = float(config.get(key, default))
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


def _nonnegative_config(config: Mapping[str, Any], key: str, default: float) -> float:
    value = float(config.get(key, default))
    if value < 0:
        raise ValueError(f"{key} must be non-negative")
    return value


def _integer_config(config: Mapping[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    if isinstance(value, bool) or int(value) != float(value):
        raise ValueError(f"{key} must be an integer")
    return int(value)


analysis_stem_horizontal_curvature = StemHorizontalCurvatureHandler()
