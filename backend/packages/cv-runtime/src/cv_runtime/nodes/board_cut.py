from __future__ import annotations

import base64
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import combinations
from typing import Any, Literal

import numpy as np
from cv_contracts import DetectionBox
from cv_contracts.runtime import RuntimeNodeSpec
from cv_status import RuntimeMode

from cv_runtime.context import NodeInputs, RuntimeExecutionContext
from cv_runtime.handlers import NodeExecutionResult
from cv_runtime.payloads import (
    BoardCutConstraintsPayload,
    BoardCutDecision,
    BoardCutDecisionPayload,
    BoardFeatureConstraint,
    CoordinateFramePayload,
    DetectionPayload,
    FramePayload,
    InspectionCoordinateFrame,
    InspectionPoint,
    InspectionVector,
    LineGeometry,
    OverlayPayload,
    OverlayPrimitive,
    ReferenceEdge,
    ReferenceEdgesPayload,
    RegionSetPayload,
    ResultPayload,
    RuntimeImageContext,
    RuntimeImageTransform,
    SegmentPayload,
    SynchronizedCameraFrame,
    SynchronizedFrameSetPayload,
)

ViewRole = Literal["upper", "lower", "end_face"]
SurfaceRole = Literal["top", "bottom", "side_a", "side_b", "end_face"]
LineSegment = tuple[float, float, float, float]
LONGITUDINAL_SURFACES: dict[str, set[str]] = {
    "upper": {"top", "side_a"},
    "lower": {"bottom", "side_b"},
}
REQUIRED_VIEW_ROLES: tuple[ViewRole, ...] = ("upper", "lower", "end_face")
_TRANSPARENT_PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@dataclass(frozen=True)
class _FeatureGeometry:
    feature_id: str
    label: str
    class_id: int | None
    score: float
    points: list[tuple[float, float]]
    source_image_context_id: str | None
    is_segmentation: bool


@dataclass(frozen=True)
class _SurfaceCalibration:
    role: SurfaceRole
    image_polygon: list[tuple[float, float]]
    homography: np.ndarray
    world_polygon: list[tuple[float, float]]


@dataclass(frozen=True)
class _TrackedSurface:
    calibration: _SurfaceCalibration
    homography: np.ndarray
    image_polygon: list[tuple[float, float]]
    visible_image_polygon: list[tuple[float, float]]
    visible_world_polygon: list[tuple[float, float]]
    origin_world: tuple[float, float]
    longitudinal_axis_world: tuple[float, float]
    calibrated_limit_mm: float


@dataclass(frozen=True)
class _CalibrationBoundary:
    anchor: tuple[float, float]
    longitudinal: tuple[float, float]
    bindings: tuple[tuple[SurfaceRole, str], ...]


@dataclass(frozen=True)
class _GuidedBoardPose:
    origin: tuple[float, float]
    axis_endpoint: tuple[float, float]
    end_line: tuple[tuple[float, float], tuple[float, float]]
    reference_anchor: tuple[float, float]
    surface_polygons: dict[str, list[tuple[float, float]]]
    confidence: float
    source: dict[str, Any]


@dataclass(frozen=True)
class _BoardEndDetection:
    origin: tuple[float, float]
    longitudinal: tuple[float, float]
    confidence: float
    source: dict[str, Any]


@dataclass(frozen=True)
class _BoardColorRegion:
    mask: np.ndarray
    outline: list[tuple[float, float]]
    confidence: float
    source: dict[str, Any]


@dataclass(frozen=True)
class _CutConstraintResolution:
    active_constraints: list[BoardFeatureConstraint]
    dominant_constraint: BoardFeatureConstraint | None
    required_cut_mm: float
    clipped_constraint: BoardFeatureConstraint | None


@dataclass(frozen=True)
class SynchronizedCameraFramesHandler:
    node_type: str = "source.synchronized_camera_frames"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        config = {**context.source_config, **(node.config or {})}
        max_skew = max(float(config.get("max_capture_skew_ms", 50.0)), 0.0)
        frame_set = context.synchronized_frame_set
        if frame_set is None:
            frame_set = _build_frame_set(context, config, max_skew)
        else:
            frame_set = frame_set.model_copy(update={"max_capture_skew_ms": max_skew})

        frames = {
            entry.role: _ensure_role_frame_context(context, entry.role, entry.frame)
            for entry in frame_set.frames
        }
        for entry in frame_set.frames:
            entry.frame = frames[entry.role]
        outputs: dict[str, Any] = {"frame_set": frame_set}
        for role in REQUIRED_VIEW_ROLES:
            frame = frames.get(role)
            if frame is None:
                frame = FramePayload.from_bytes(
                    _TRANSPARENT_PIXEL,
                    mime_type="image/png",
                    width=1,
                    height=1,
                    timestamp=context.now_iso(),
                    metadata={"missing_view_placeholder": role},
                )
            outputs[f"{role}_frame"] = frame
        if context.trigger is not None:
            outputs["trigger"] = context.trigger
        return NodeExecutionResult(outputs=outputs)


@dataclass(frozen=True)
class SingleBoardCameraTestSourceHandler:
    """Expose one explicitly selected board camera for commissioning and dry-runs."""

    node_type: str = "source.single_board_camera_test"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        config = {**context.source_config, **(node.config or {})}
        configured_role = str(config.get("camera_role") or "upper")
        role: ViewRole = configured_role if configured_role in REQUIRED_VIEW_ROLES else "upper"  # type: ignore[assignment]
        frame_set = context.synchronized_frame_set
        if frame_set is None:
            frame_set = _build_single_test_frame_set(context, config, role)
        else:
            captured_roles = {entry.role for entry in frame_set.frames}
            if captured_roles != {role}:
                frame_set = frame_set.model_copy(
                    update={
                        "valid": False,
                        "status": "camera_missing",
                        "reason_code": "single_camera_role_mismatch",
                    }
                )

        entry = next((item for item in frame_set.frames if item.role == role), None)
        if entry is not None:
            frame = _ensure_role_frame_context(context, role, entry.frame)
            entry.frame = frame
        else:
            frame = FramePayload.from_bytes(
                _TRANSPARENT_PIXEL,
                mime_type="image/png",
                width=1,
                height=1,
                timestamp=context.now_iso(),
                metadata={"missing_view_placeholder": role, "single_camera_test": True},
            )
        outputs: dict[str, Any] = {"frame": frame, "frame_set": frame_set}
        if context.trigger is not None:
            outputs["trigger"] = context.trigger
        return NodeExecutionResult(outputs=outputs)


@dataclass(frozen=True)
class BoardEndCoordinateFrameHandler:
    node_type: str = "analysis.board_end_coordinate_frame"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        frame = inputs.get("frame")
        if not isinstance(frame, FramePayload):
            return NodeExecutionResult()
        config = node.config or {}
        geometry_source = str(config.get("geometry_source") or "image_edges")
        side = str(config.get("board_end_side") or "left")
        context_id = (
            frame.image_context.context_id
            if frame.image_context is not None
            else f"ctx_{context.correlation_id}_{node.node_key}"
        )
        origin = InspectionPoint(
            x=0.0 if side == "left" else float(frame.width or 1),
            y=float(frame.height or 1) / 2.0,
        )
        longitudinal = InspectionVector(x=1.0 if side == "left" else -1.0, y=0.0)
        transverse = InspectionVector(x=0.0, y=1.0)
        confidence = 0.0
        source: dict[str, Any] = {
            "method": (
                "segmentation_polygons"
                if geometry_source == "segmentation"
                else "canny_hough_fail_closed"
            ),
            "found": False,
            "node_key": node.node_key,
        }
        try:
            if geometry_source == "segmentation":
                detection = _segmentation_board_end_detection(
                    inputs.get("segments"),
                    config=config,
                    side=side,
                    image_width=float(frame.width or 1),
                    image_height=float(frame.height or 1),
                )
            elif geometry_source == "image_edges":
                import cv2

                encoded = np.frombuffer(frame.image_bytes(), dtype=np.uint8)
                image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
                if image is None:
                    raise ValueError("image_decode_failed")
                source_height, source_width = image.shape[:2]
                processing_max_dimension = max(
                    int(config.get("processing_max_dimension_px", 640)),
                    128,
                )
                processing_scale = min(
                    1.0,
                    processing_max_dimension / max(source_width, source_height, 1),
                )
                processing_config = dict(config)
                if processing_scale < 1.0:
                    image = cv2.resize(
                        image,
                        (
                            max(int(round(source_width * processing_scale)), 1),
                            max(int(round(source_height * processing_scale)), 1),
                        ),
                        interpolation=cv2.INTER_AREA,
                    )
                    processing_config["minimum_line_length_px"] = max(
                        float(config.get("minimum_line_length_px", 25)) * processing_scale,
                        1.0,
                    )
                    processing_config["max_line_gap_px"] = max(
                        float(config.get("max_line_gap_px", 8)) * processing_scale,
                        0.0,
                    )
                    processing_config["maximum_reference_join_gap_px"] = max(
                        float(config.get("maximum_reference_join_gap_px", 15))
                        * processing_scale,
                        1.0,
                    )
                blur = max(int(config.get("blur_kernel_size", 5)), 0)
                if blur > 1:
                    blur = blur if blur % 2 == 1 else blur + 1
                    image = cv2.GaussianBlur(image, (blur, blur), 0)
                detection = _detect_board_end_reference(
                    image,
                    config=processing_config,
                    side=side,
                )
                if processing_scale < 1.0:
                    detection = _rescale_board_end_detection(
                        detection,
                        processing_scale=processing_scale,
                        source_width=source_width,
                        source_height=source_height,
                        processing_width=image.shape[1],
                        processing_height=image.shape[0],
                    )
            else:
                raise ValueError("geometry_source_invalid")
            origin = InspectionPoint(x=detection.origin[0], y=detection.origin[1])
            longitudinal = InspectionVector(
                x=detection.longitudinal[0],
                y=detection.longitudinal[1],
            )
            transverse = InspectionVector(
                x=-detection.longitudinal[1],
                y=detection.longitudinal[0],
            )
            confidence = detection.confidence
            source = dict(detection.source)
        except ValueError as exc:
            reason = str(exc)
            source["reason_code"] = (
                reason
                if reason
                in {
                    "segmentation_unavailable",
                    "surface_mapping_invalid",
                    "surface_missing",
                    "surface_ambiguous",
                    "invalid_polygon",
                    "geometry_source_invalid",
                }
                else "board_end_detection_failed"
            )
            source["detail"] = reason[:240]
        except Exception as exc:
            source["reason_code"] = "geometry_internal_error"
            source["detail"] = str(exc)[:240]

        coordinate = InspectionCoordinateFrame(
            frame_id=f"board_frame_{context.correlation_id}_{node.node_key}",
            image_context_id=context_id,
            origin=origin,
            longitudinal_axis=longitudinal,
            transverse_axis=transverse,
            confidence=max(min(confidence, 1.0), 0.0),
            source=source,
            metadata={"image_width": frame.width, "image_height": frame.height},
        )
        outputs: dict[str, Any] = {
            "coordinate_frame": CoordinateFramePayload(frame=coordinate),
            "reference_edges": _board_reference_edges(coordinate),
        }
        if bool(config.get("debug_overlay", True)):
            outputs["overlay"] = _board_end_debug_overlay(coordinate)
        return NodeExecutionResult(outputs=outputs)


@dataclass(frozen=True)
class BoardViewCutConstraintsHandler:
    node_type: str = "analysis.board_view_cut_constraints"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        try:
            return self._execute(context, node, inputs)
        except Exception as exc:
            config = node.config or {}
            view_role = str(config.get("view_role") or "upper")
            image_context_id = _geometry_input_context_id(inputs)
            return NodeExecutionResult(
                outputs={
                    "constraints": _invalid_constraints(
                        context,
                        view_role,
                        {},
                        "calibration_invalid",
                        "geometry_internal_error",
                    ),
                    "overlay": _board_geometry_warning_overlay(
                        image_context_id,
                        "geometry_internal_error",
                        detail=str(exc)[:240],
                        node_key=node.node_key,
                    ),
                }
            )

    def _execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        config = node.config or {}
        view_role = str(config.get("view_role") or "upper")
        if view_role not in LONGITUDINAL_SURFACES:
            return NodeExecutionResult(
                outputs={
                    "constraints": _invalid_constraints(
                        context, view_role, config, "calibration_invalid", "invalid_view_role"
                    )
                }
            )
        inspection_length = _positive_float(config.get("inspection_length_mm"), 300.0)
        rms = max(float(config.get("calibration_rms_mm", 0.0)), 0.0)
        max_rms = _positive_float(config.get("max_calibration_rms_mm"), 2.0)
        uncertainty = rms * max(float(config.get("uncertainty_multiplier", 3.0)), 0.0)
        if rms > max_rms:
            return NodeExecutionResult(
                outputs={
                    "constraints": _invalid_constraints(
                        context,
                        view_role,
                        config,
                        "calibration_invalid",
                        "calibration_rms_exceeded",
                        rms=rms,
                        uncertainty=uncertainty,
                    )
                }
            )

        coordinate_payload = inputs.get("coordinate_frame")
        if not isinstance(coordinate_payload, CoordinateFramePayload):
            return NodeExecutionResult(
                outputs={
                    "constraints": _invalid_constraints(
                        context,
                        view_role,
                        config,
                        "board_end_not_found",
                        "coordinate_frame_missing",
                        rms=rms,
                        uncertainty=uncertainty,
                    )
                }
            )
        frame = inputs.get("frame")
        image_width, image_height = _image_dimensions(frame, inputs.get("features"))
        intrinsics = config.get("camera_intrinsics")
        features = _features_from_payload(inputs.get("features"))
        segmentation_geometry = (
            coordinate_payload.frame.source.get("method") == "segmentation_polygons"
        )
        try:
            calibrations = _surface_calibrations(
                config.get("surfaces"),
                allowed_roles=LONGITUDINAL_SURFACES[view_role],
                intrinsics=intrinsics,
                image_width=image_width,
                image_height=image_height,
            )
            if segmentation_geometry:
                calibrations = _dimensioned_longitudinal_calibrations(
                    calibrations,
                    _board_format(context, config),
                )
            rules = _validated_class_rules(config.get("class_rules"))
        except ValueError as exc:
            outputs: dict[str, Any] = {
                "constraints": _invalid_constraints(
                    context,
                    view_role,
                    config,
                    "calibration_invalid",
                    str(exc),
                    rms=rms,
                    uncertainty=uncertainty,
                )
            }
            if segmentation_geometry:
                outputs["overlay"] = _board_geometry_warning_overlay(
                    _geometry_input_context_id(inputs),
                    str(exc),
                    node_key=node.node_key,
                )
            return NodeExecutionResult(outputs=outputs)
        if not _payload_class_map_matches_rules(inputs.get("features"), rules) or any(
            _rule_for_feature(rules, feature) is None for feature in features
        ):
            return NodeExecutionResult(
                outputs={
                    "constraints": _invalid_constraints(
                        context,
                        view_role,
                        config,
                        "calibration_invalid",
                        "model_class_map_changed",
                        rms=rms,
                        uncertainty=uncertainty,
                    )
                }
            )

        guided_pose: _GuidedBoardPose | None = None
        guided_pose_error: str | None = None
        if bool(config.get("use_calibration_pose_reference", True)):
            try:
                guided_pose = _calibration_guided_board_pose(
                    coordinate_payload.frame,
                    calibrations=calibrations,
                    config=config,
                    intrinsics=intrinsics,
                    image_width=image_width,
                    image_height=image_height,
                )
            except ValueError as exc:
                guided_pose_error = str(exc)
            if segmentation_geometry and guided_pose is None:
                context.state.setdefault(node.node_key, {}).pop(
                    "board_cut_pose_stabilization",
                    None,
                )
            else:
                guided_pose = _stabilize_realtime_guided_pose(
                    context,
                    node,
                    guided_pose,
                    error=guided_pose_error,
                    calibrations=calibrations,
                    config=config,
                    image_width=image_width,
                    image_height=image_height,
                )
            if guided_pose is None and guided_pose_error is not None:
                outputs = {
                    "constraints": _invalid_constraints(
                        context,
                        view_role,
                        config,
                        "board_end_not_found",
                        guided_pose_error,
                        rms=rms,
                        uncertainty=uncertainty,
                    )
                }
                if segmentation_geometry:
                    outputs["overlay"] = _board_geometry_warning_overlay(
                        _geometry_input_context_id(inputs),
                        guided_pose_error,
                        node_key=node.node_key,
                    )
                return NodeExecutionResult(outputs=outputs)

        if guided_pose is not None:
            origin = guided_pose.origin
            axis_endpoint = guided_pose.axis_endpoint
            current_end_line = guided_pose.end_line
            reference_anchor = guided_pose.reference_anchor
            current_surface_polygons = guided_pose.surface_polygons
            board_pose_confidence = guided_pose.confidence
            board_pose_metadata = guided_pose.source
        else:
            origin_raw = (
                coordinate_payload.frame.origin.x / image_width,
                coordinate_payload.frame.origin.y / image_height,
            )
            axis_raw = (
                coordinate_payload.frame.longitudinal_axis.x / image_width,
                coordinate_payload.frame.longitudinal_axis.y / image_height,
            )
            origin, axis_endpoint = _undistort_normalized_points(
                [origin_raw, (origin_raw[0] + axis_raw[0], origin_raw[1] + axis_raw[1])],
                intrinsics,
                image_width=image_width,
                image_height=image_height,
            )
            current_end_line = _undistorted_coordinate_start_line(
                coordinate_payload.frame,
                intrinsics=intrinsics,
                image_width=image_width,
                image_height=image_height,
            )
            reference_anchor = None
            current_surface_polygons = None
            board_pose_confidence = coordinate_payload.frame.confidence
            board_pose_metadata = {
                "method": "preliminary_board_end_coordinate",
                "confidence": board_pose_confidence,
            }

        minimum_edge_confidence = float(config.get("minimum_board_end_confidence", 0.5))
        if board_pose_confidence < minimum_edge_confidence:
            outputs = {
                "constraints": _invalid_constraints(
                    context,
                    view_role,
                    config,
                    "board_end_not_found",
                    "board_end_confidence_too_low",
                    rms=rms,
                    uncertainty=uncertainty,
                )
            }
            if segmentation_geometry:
                outputs["overlay"] = _board_geometry_warning_overlay(
                    _geometry_input_context_id(inputs),
                    "board_end_confidence_too_low",
                    node_key=node.node_key,
                )
            return NodeExecutionResult(outputs=outputs)
        try:
            tracked_surfaces = _track_board_surfaces(
                calibrations,
                origin=origin,
                axis_endpoint=axis_endpoint,
                current_end_line=current_end_line,
                reference_anchor=reference_anchor,
                current_surface_polygons=current_surface_polygons,
                follow_board_position=bool(config.get("follow_board_position", True)),
            )
        except ValueError as exc:
            outputs = {
                "constraints": _invalid_constraints(
                    context,
                    view_role,
                    config,
                    "calibration_invalid",
                    str(exc),
                    rms=rms,
                    uncertainty=uncertainty,
                )
            }
            if segmentation_geometry:
                outputs["overlay"] = _board_geometry_warning_overlay(
                    _geometry_input_context_id(inputs),
                    str(exc),
                    node_key=node.node_key,
                )
            return NodeExecutionResult(outputs=outputs)

        surface_mappings = [
            _tracked_surface_mapping(
                surface,
                image_context_id=coordinate_payload.frame.image_context_id,
            )
            for surface in tracked_surfaces
        ]
        tracking_metadata = {
            "surface_tracking_mode": (
                "follow_detected_board"
                if bool(config.get("follow_board_position", True))
                else "fixed"
            ),
            "surface_mappings": surface_mappings,
            "board_pose": board_pose_metadata,
        }
        surface_overlay = _tracked_surface_overlay(
            tracked_surfaces,
            image_context_id=coordinate_payload.frame.image_context_id,
            tracking_mode=str(tracking_metadata["surface_tracking_mode"]),
        )
        if guided_pose is not None:
            guided_overlay = _guided_board_pose_overlay(
                guided_pose,
                image_context_id=coordinate_payload.frame.image_context_id,
                image_width=image_width,
                image_height=image_height,
            )
            surface_overlay = OverlayPayload(
                primitives=[
                    *guided_overlay.primitives,
                    *surface_overlay.primitives,
                ],
                display_context_id=coordinate_payload.frame.image_context_id,
            )
        calibrated_limit = min(
            (surface.calibrated_limit_mm for surface in tracked_surfaces),
            default=0.001,
        )
        configured_limit = config.get("calibrated_limit_mm")
        if configured_limit is not None:
            calibrated_limit = min(
                calibrated_limit,
                _positive_float(configured_limit, calibrated_limit),
            )
        required_coverage = inspection_length + max(
            float(config.get("safe_visibility_margin_mm", 100.0)), 0.0
        )
        if calibrated_limit + 1e-6 < required_coverage:
            payload = BoardCutConstraintsPayload(
                workpiece_id=_workpiece_id(context),
                view_role=view_role,  # type: ignore[arg-type]
                valid=False,
                status="safe_cut_outside_inspection_range",
                reason_code="tracked_board_does_not_cover_inspection_and_safety_margin",
                inspection_length_mm=inspection_length,
                calibrated_limit_mm=max(calibrated_limit, 0.001),
                calibration_rms_mm=rms,
                uncertainty_mm=uncertainty,
                image_context_id=coordinate_payload.frame.image_context_id,
                metadata=tracking_metadata,
            )
            outputs: dict[str, Any] = {"constraints": payload}
            if bool(config.get("show_dynamic_surface_overlay", True)):
                outputs["overlay"] = surface_overlay
            return NodeExecutionResult(outputs=outputs)

        constraints: list[BoardFeatureConstraint] = []
        unassigned_relevant_features: list[str] = []
        for feature in features:
            rule = _rule_for_feature(rules, feature)
            if rule is None or not bool(rule.get("relevant", False)):
                continue
            if feature.score < float(rule.get("minimum_confidence", 0.35)):
                continue
            undistorted_points = _undistort_normalized_points(
                feature.points,
                intrinsics,
                image_width=image_width,
                image_height=image_height,
            )
            tracked_surface = _best_surface_for_geometry(
                undistorted_points,
                tracked_surfaces,
            )
            if tracked_surface is None:
                unassigned_relevant_features.append(feature.feature_id)
                continue
            projected = _project_points(
                tracked_surface.homography,
                undistorted_points,
            )
            longitudinal = [
                _longitudinal_position_mm(
                    point,
                    origin=tracked_surface.origin_world,
                    axis=tracked_surface.longitudinal_axis_world,
                )
                for point in projected
            ]
            start_mm = min(longitudinal)
            end_mm = max(longitudinal)
            if end_mm < 0 or start_mm > tracked_surface.calibrated_limit_mm:
                continue
            clipped = _touches_image_boundary(feature.points) or (
                start_mm <= inspection_length <= end_mm
            )
            basis = _measurement_basis(rule)
            feature_size = _feature_size(projected, basis)
            clearance_multiplier = max(float(rule.get("clearance_multiplier", 1.0)), 0.0)
            clearance = clearance_multiplier * feature_size
            required_cut = max(end_mm, 0.0) + clearance + uncertainty
            class_id = _feature_class_id(feature, rule)
            constraints.append(
                BoardFeatureConstraint(
                    constraint_id=(
                        f"{view_role}:{tracked_surface.calibration.role}:{feature.feature_id}"
                    ),
                    view_role=view_role,
                    surface_role=tracked_surface.calibration.role,
                    feature_id=feature.feature_id,
                    class_id=class_id,
                    label_snapshot=str(rule.get("label_snapshot") or feature.label),
                    confidence=feature.score,
                    measurement_basis=basis,
                    feature_start_mm=round(start_mm, 6),
                    feature_end_mm=round(end_mm, 6),
                    feature_size_mm=round(feature_size, 6),
                    clearance_mm=round(clearance, 6),
                    uncertainty_mm=round(uncertainty, 6),
                    required_cut_mm=round(required_cut, 6),
                    clipped=clipped,
                    source_image_context_id=feature.source_image_context_id,
                    metadata={
                        "measurement_geometry": (
                            "segment_polygon" if feature.is_segmentation else "detection_box"
                        ),
                        "class_map_version": rule.get("class_map_version"),
                        "surface_tracking_mode": tracking_metadata["surface_tracking_mode"],
                        "required_free_before_feature_mm": round(
                            clearance + uncertainty,
                            6,
                        ),
                        "required_free_after_feature_mm": round(
                            clearance + uncertainty,
                            6,
                        ),
                    },
                )
            )

        payload = BoardCutConstraintsPayload(
            workpiece_id=_workpiece_id(context),
            view_role=view_role,
            valid=True,
            status="evaluated",
            reason_code=None,
            inspection_length_mm=inspection_length,
            calibrated_limit_mm=calibrated_limit,
            calibration_rms_mm=rms,
            uncertainty_mm=uncertainty,
            constraints=constraints,
            image_context_id=coordinate_payload.frame.image_context_id,
            metadata={
                **tracking_metadata,
                "unassigned_relevant_feature_count": len(unassigned_relevant_features),
                "unassigned_relevant_feature_ids": unassigned_relevant_features[:20],
                "candidate_constraint_count": len(constraints),
                "cut_activation_deferred_to_decision": True,
            },
        )
        outputs = {"constraints": payload}
        if bool(config.get("show_dynamic_surface_overlay", True)):
            outputs["overlay"] = surface_overlay
        return NodeExecutionResult(outputs=outputs)


@dataclass(frozen=True)
class EndFaceCutConstraintsHandler:
    node_type: str = "analysis.end_face_cut_constraints"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        try:
            return self._execute(context, node, inputs)
        except Exception as exc:
            image_context_id = _geometry_input_context_id(inputs)
            return NodeExecutionResult(
                outputs={
                    "constraints": _invalid_constraints(
                        context,
                        "end_face",
                        {},
                        "calibration_invalid",
                        "geometry_internal_error",
                    ),
                    "overlay": _board_geometry_warning_overlay(
                        image_context_id,
                        "geometry_internal_error",
                        detail=str(exc)[:240],
                        node_key=node.node_key,
                    ),
                }
            )

    def _execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        config = node.config or {}
        inspection_length = _positive_float(config.get("inspection_length_mm"), 300.0)
        rms = max(float(config.get("calibration_rms_mm", 0.0)), 0.0)
        max_rms = _positive_float(config.get("max_calibration_rms_mm"), 2.0)
        if rms > max_rms:
            return NodeExecutionResult(
                outputs={
                    "constraints": _invalid_constraints(
                        context,
                        "end_face",
                        config,
                        "calibration_invalid",
                        "calibration_rms_exceeded",
                        rms=rms,
                        uncertainty=rms,
                    )
                }
            )
        try:
            image_width, image_height = _image_dimensions(
                inputs.get("frame"), inputs.get("features")
            )
            intrinsics = config.get("camera_intrinsics")
            segment_payload = inputs.get("segments")
            if isinstance(segment_payload, SegmentPayload):
                calibration, end_face_polygon, board_format = _dynamic_end_face_calibration(
                    segment_payload,
                    config=config,
                    context=context,
                )
                calibrations = [calibration]
            else:
                calibrations = _surface_calibrations(
                    [config.get("surface") or {}],
                    allowed_roles={"end_face"},
                    intrinsics=intrinsics,
                    image_width=image_width,
                    image_height=image_height,
                )
                end_face_polygon = None
                board_format = None
            rules = _validated_class_rules(config.get("class_rules"))
        except ValueError as exc:
            invalid = _invalid_constraints(
                context,
                "end_face",
                config,
                "calibration_invalid",
                str(exc),
                rms=rms,
                uncertainty=rms,
            )
            return NodeExecutionResult(
                outputs={
                    "constraints": invalid,
                    "overlay": _board_geometry_warning_overlay(
                        _payload_image_context_id(inputs.get("segments")),
                        str(exc),
                        node_key=node.node_key,
                    ),
                }
            )
        except Exception as exc:
            return NodeExecutionResult(
                outputs={
                    "constraints": _invalid_constraints(
                        context,
                        "end_face",
                        config,
                        "calibration_invalid",
                        "geometry_internal_error",
                        rms=rms,
                        uncertainty=rms,
                    ),
                    "overlay": _board_geometry_warning_overlay(
                        _payload_image_context_id(inputs.get("segments")),
                        "geometry_internal_error",
                        detail=str(exc)[:240],
                        node_key=node.node_key,
                    ),
                }
            )

        calibration = calibrations[0]
        features = _features_from_payload(inputs.get("features"))
        if not _payload_class_map_matches_rules(inputs.get("features"), rules) or any(
            _rule_for_feature(rules, feature) is None for feature in features
        ):
            return NodeExecutionResult(
                outputs={
                    "constraints": _invalid_constraints(
                        context,
                        "end_face",
                        config,
                        "calibration_invalid",
                        "model_class_map_changed",
                        rms=rms,
                        uncertainty=rms,
                    )
                }
            )
        constraints: list[BoardFeatureConstraint] = []
        boundary_clipped = False
        for feature in features:
            undistorted_points = _undistort_normalized_points(
                feature.points,
                intrinsics,
                image_width=image_width,
                image_height=image_height,
            )
            if not _geometry_intersects_surface(undistorted_points, calibration.image_polygon):
                continue
            rule = _rule_for_feature(rules, feature)
            if rule is None or not bool(rule.get("relevant", False)):
                continue
            if feature.score < float(rule.get("minimum_confidence", 0.35)):
                continue
            projected = _project_points(calibration.homography, undistorted_points)
            basis = _measurement_basis(rule)
            diameter = _feature_size(projected, basis)
            depth_factor = max(float(rule.get("depth_factor", 1.0)), 0.0)
            clearance_factor = max(float(rule.get("clearance_factor", 1.0)), 0.0)
            required_cut = (depth_factor + clearance_factor) * diameter
            clipped = _touches_image_boundary(feature.points)
            boundary_clipped = boundary_clipped or clipped
            constraints.append(
                BoardFeatureConstraint(
                    constraint_id=f"end_face:end_face:{feature.feature_id}",
                    view_role="end_face",
                    surface_role="end_face",
                    feature_id=feature.feature_id,
                    class_id=_feature_class_id(feature, rule),
                    label_snapshot=str(rule.get("label_snapshot") or feature.label),
                    confidence=feature.score,
                    measurement_basis=basis,
                    feature_size_mm=round(diameter, 6),
                    clearance_mm=round(clearance_factor * diameter, 6),
                    uncertainty_mm=round(rms, 6),
                    required_cut_mm=round(required_cut, 6),
                    clipped=clipped,
                    source_image_context_id=feature.source_image_context_id,
                    metadata={
                        "assumed_depth_mm": round(depth_factor * diameter, 6),
                        "depth_factor": depth_factor,
                        "clearance_factor": clearance_factor,
                        "measurement_geometry": "segment_polygon"
                        if feature.is_segmentation
                        else "detection_box_conservative",
                        "class_map_version": rule.get("class_map_version"),
                    },
                )
            )

        outputs: dict[str, Any] = {
            "constraints": BoardCutConstraintsPayload(
                    workpiece_id=_workpiece_id(context),
                    view_role="end_face",
                    valid=not boundary_clipped,
                    status="feature_clipped" if boundary_clipped else "evaluated",
                    reason_code="feature_touches_image_boundary" if boundary_clipped else None,
                    inspection_length_mm=inspection_length,
                    calibrated_limit_mm=inspection_length,
                    calibration_rms_mm=rms,
                    uncertainty_mm=rms,
                    constraints=constraints,
                    image_context_id=_payload_image_context_id(inputs.get("features")),
                )
        }
        if end_face_polygon is not None:
            outputs["overlay"] = OverlayPayload(
                primitives=[
                    OverlayPrimitive(
                        primitive_type="polygon",
                        layer="board_dynamic_surface",
                        label="Segmented board end face",
                        geometry={
                            "points": [{"x": x, "y": y} for x, y in end_face_polygon],
                            "coordinate_system": "normalized_polygon",
                        },
                        image_context_id=_payload_image_context_id(inputs.get("segments")),
                        style={"stroke": "#00b8d4", "fill": "rgba(0, 184, 212, 0.08)"},
                        metadata={"surface_role": "end_face", "board_format": board_format},
                    )
                ],
                display_context_id=_payload_image_context_id(inputs.get("segments")),
            )
        return NodeExecutionResult(outputs=outputs)


def _resolve_cut_constraint_chain(
    constraints: list[BoardFeatureConstraint],
    *,
    inspection_length_mm: float,
) -> _CutConstraintResolution:
    active: list[BoardFeatureConstraint] = []
    active_ids: set[str] = set()
    current_cut = 0.0

    # End-face constraints and legacy constraints without a longitudinal interval
    # touch the current board end by definition and therefore start the cut chain.
    for constraint in constraints:
        if (
            constraint.feature_start_mm is None
            or constraint.feature_end_mm is None
        ):
            active.append(constraint)
            active_ids.add(constraint.constraint_id)
            current_cut = max(current_cut, constraint.required_cut_mm)

    longitudinal = sorted(
        (
            constraint
            for constraint in constraints
            if constraint.feature_start_mm is not None
            and constraint.feature_end_mm is not None
            and constraint.feature_end_mm >= 0
        ),
        key=lambda item: (
            float(item.feature_start_mm or 0.0),
            item.required_cut_mm,
            item.constraint_id,
        ),
    )
    while True:
        additions: list[BoardFeatureConstraint] = []
        for constraint in longitudinal:
            if constraint.constraint_id in active_ids:
                continue
            start_mm = float(constraint.feature_start_mm or 0.0)
            if current_cut <= 1e-9 and start_mm > inspection_length_mm:
                continue
            required_free_before_feature = (
                constraint.clearance_mm + constraint.uncertainty_mm
            )
            if start_mm + 1e-9 >= current_cut + required_free_before_feature:
                continue
            additions.append(constraint)
        if not additions:
            break
        for constraint in additions:
            active.append(constraint)
            active_ids.add(constraint.constraint_id)
            current_cut = max(current_cut, constraint.required_cut_mm)

    dominant = max(
        active,
        key=lambda item: item.required_cut_mm,
        default=None,
    )
    clipped = next(
        (constraint for constraint in active if constraint.clipped),
        None,
    )
    return _CutConstraintResolution(
        active_constraints=active,
        dominant_constraint=dominant,
        required_cut_mm=(
            dominant.required_cut_mm if dominant is not None else 0.0
        ),
        clipped_constraint=clipped,
    )


@dataclass(frozen=True)
class MultiViewBoardCutFusionHandler:
    node_type: str = "analysis.multi_view_board_cut_fusion"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        config = node.config or {}
        inspection_length = _positive_float(config.get("inspection_length_mm"), 300.0)
        frame_set = inputs.get("frame_set")
        if not isinstance(frame_set, SynchronizedFrameSetPayload):
            frame_set = context.synchronized_frame_set

        capture_skew = (
            frame_set.capture_skew_ms
            if isinstance(frame_set, SynchronizedFrameSetPayload)
            else max(float(context.metadata.get("capture_skew_ms", 0.0)), 0.0)
        )
        workpiece_id = (
            frame_set.workpiece_id
            if isinstance(frame_set, SynchronizedFrameSetPayload)
            else _workpiece_id(context)
        )
        if isinstance(frame_set, SynchronizedFrameSetPayload) and not frame_set.valid:
            decision = _invalid_decision(
                workpiece_id,
                frame_set.status,
                frame_set.reason_code,
                inspection_length,
                capture_skew,
            )
            return _decision_outputs(decision)

        expected: dict[str, Any] = {
            "upper": inputs.get("upper"),
            "lower": inputs.get("lower"),
            "end_face": inputs.get("end_face"),
        }
        missing = [
            role
            for role, payload in expected.items()
            if not isinstance(payload, BoardCutConstraintsPayload)
        ]
        if missing:
            decision = _invalid_decision(
                workpiece_id,
                "camera_missing",
                f"missing_constraints:{','.join(missing)}",
                inspection_length,
                capture_skew,
            )
            return _decision_outputs(decision)

        payloads = [expected[role] for role in REQUIRED_VIEW_ROLES]
        role_mismatch = [role for role, payload in expected.items() if payload.view_role != role]
        if role_mismatch:
            decision = _invalid_decision(
                workpiece_id,
                "camera_missing",
                f"view_role_mismatch:{','.join(role_mismatch)}",
                inspection_length,
                capture_skew,
            )
            return _decision_outputs(decision)

        first_invalid = next((payload for payload in payloads if not payload.valid), None)
        if first_invalid is not None:
            decision = _invalid_decision(
                workpiece_id,
                first_invalid.status,
                first_invalid.reason_code,
                inspection_length,
                capture_skew,
                views=[payload.view_role for payload in payloads],
                uncertainty=max(payload.uncertainty_mm for payload in payloads),
            )
            return _decision_outputs(decision)

        mismatched_lengths = [
            payload.view_role
            for payload in payloads
            if not math.isclose(
                payload.inspection_length_mm,
                inspection_length,
                rel_tol=0.0,
                abs_tol=1e-6,
            )
        ]
        if mismatched_lengths:
            decision = _invalid_decision(
                workpiece_id,
                "calibration_invalid",
                f"inspection_length_mismatch:{','.join(mismatched_lengths)}",
                inspection_length,
                capture_skew,
                views=[payload.view_role for payload in payloads],
                uncertainty=max(payload.uncertainty_mm for payload in payloads),
            )
            return _decision_outputs(decision)

        constraints = [constraint for payload in payloads for constraint in payload.constraints]
        resolution = _resolve_cut_constraint_chain(
            constraints,
            inspection_length_mm=inspection_length,
        )
        dominant = resolution.dominant_constraint
        required_cut = resolution.required_cut_mm
        if resolution.clipped_constraint is not None:
            decision = _invalid_decision(
                workpiece_id,
                "feature_clipped",
                "active_cut_chain_contains_clipped_feature",
                inspection_length,
                capture_skew,
                views=[payload.view_role for payload in payloads],
                uncertainty=max(payload.uncertainty_mm for payload in payloads),
                dominant=resolution.clipped_constraint,
            )
            return _decision_outputs(decision)
        safe_limit = min(
            payload.calibrated_limit_mm
            for payload in payloads
            if payload.view_role in {"upper", "lower"}
        )
        if required_cut > safe_limit:
            decision = _invalid_decision(
                workpiece_id,
                "safe_cut_outside_inspection_range",
                "required_cut_exceeds_calibrated_longitudinal_view",
                inspection_length,
                capture_skew,
                views=[payload.view_role for payload in payloads],
                uncertainty=max(payload.uncertainty_mm for payload in payloads),
                dominant=dominant,
            )
            return _decision_outputs(decision)

        decision = BoardCutDecision(
            workpiece_id=workpiece_id,
            valid=True,
            status="cut_required" if dominant is not None else "no_cut_required",
            cut_position_mm=round(required_cut, 6),
            longitudinal_shift_mm=round(required_cut, 6),
            inspection_length_mm=inspection_length,
            capture_skew_ms=capture_skew,
            views_used=[payload.view_role for payload in payloads],
            uncertainty_mm=max(payload.uncertainty_mm for payload in payloads),
            dominant_constraint=dominant,
            reason_code=None,
        )
        return _decision_outputs(
            decision,
            constraints_payloads=payloads,
            show_cut_line=bool(config.get("show_cut_line_overlay", True)),
        )


@dataclass(frozen=True)
class SingleViewBoardCutDecisionHandler:
    """Create the production result contract from exactly one test camera view."""

    node_type: str = "analysis.single_view_board_cut_decision"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        inspection_length = _positive_float((node.config or {}).get("inspection_length_mm"), 300.0)
        constraints = inputs.get("constraints")
        frame_set = inputs.get("frame_set")
        if not isinstance(frame_set, SynchronizedFrameSetPayload):
            frame_set = context.synchronized_frame_set
        capture_skew = (
            frame_set.capture_skew_ms if isinstance(frame_set, SynchronizedFrameSetPayload) else 0.0
        )
        workpiece_id = (
            frame_set.workpiece_id
            if isinstance(frame_set, SynchronizedFrameSetPayload)
            else _workpiece_id(context)
        )
        realtime_frame = context.mode == RuntimeMode.REALTIME and not isinstance(
            frame_set, SynchronizedFrameSetPayload
        )
        if not isinstance(frame_set, SynchronizedFrameSetPayload) and not realtime_frame:
            return _decision_outputs(
                _invalid_decision(
                    workpiece_id,
                    "camera_missing",
                    "single_camera_frame_set_missing",
                    inspection_length,
                    capture_skew,
                )
            )
        if isinstance(frame_set, SynchronizedFrameSetPayload) and not frame_set.valid:
            return _decision_outputs(
                _invalid_decision(
                    workpiece_id,
                    frame_set.status,
                    frame_set.reason_code,
                    inspection_length,
                    capture_skew,
                )
            )

        if not isinstance(constraints, BoardCutConstraintsPayload):
            return _decision_outputs(
                _invalid_decision(
                    workpiece_id,
                    "camera_missing",
                    "single_view_constraints_missing",
                    inspection_length,
                    capture_skew,
                )
            )
        captured_roles = (
            {entry.role for entry in frame_set.frames}
            if isinstance(frame_set, SynchronizedFrameSetPayload)
            else {constraints.view_role}
        )
        if captured_roles != {constraints.view_role}:
            return _decision_outputs(
                _invalid_decision(
                    workpiece_id,
                    "camera_missing",
                    "single_camera_role_mismatch",
                    inspection_length,
                    capture_skew,
                )
            )
        if not constraints.valid:
            return _decision_outputs(
                _invalid_decision(
                    workpiece_id,
                    constraints.status,
                    constraints.reason_code,
                    inspection_length,
                    capture_skew,
                    views=[constraints.view_role],
                    uncertainty=constraints.uncertainty_mm,
                )
            )

        if not math.isclose(
            constraints.inspection_length_mm,
            inspection_length,
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            return _decision_outputs(
                _invalid_decision(
                    workpiece_id,
                    "calibration_invalid",
                    "inspection_length_mismatch",
                    inspection_length,
                    capture_skew,
                    views=[constraints.view_role],
                    uncertainty=constraints.uncertainty_mm,
                )
            )

        resolution = _resolve_cut_constraint_chain(
            constraints.constraints,
            inspection_length_mm=inspection_length,
        )
        dominant = resolution.dominant_constraint
        required_cut = resolution.required_cut_mm
        if resolution.clipped_constraint is not None:
            return _decision_outputs(
                _invalid_decision(
                    workpiece_id,
                    "feature_clipped",
                    "active_cut_chain_contains_clipped_feature",
                    inspection_length,
                    capture_skew,
                    views=[constraints.view_role],
                    uncertainty=constraints.uncertainty_mm,
                    dominant=resolution.clipped_constraint,
                )
            )
        if required_cut > constraints.calibrated_limit_mm:
            return _decision_outputs(
                _invalid_decision(
                    workpiece_id,
                    "safe_cut_outside_inspection_range",
                    "required_cut_exceeds_calibrated_single_view",
                    inspection_length,
                    capture_skew,
                    views=[constraints.view_role],
                    uncertainty=constraints.uncertainty_mm,
                    dominant=dominant,
                )
            )

        decision = BoardCutDecision(
            workpiece_id=workpiece_id,
            valid=True,
            status="cut_required" if dominant is not None else "no_cut_required",
            cut_position_mm=round(required_cut, 6),
            longitudinal_shift_mm=round(required_cut, 6),
            inspection_length_mm=inspection_length,
            capture_skew_ms=capture_skew,
            views_used=[constraints.view_role],
            uncertainty_mm=constraints.uncertainty_mm,
            dominant_constraint=dominant,
            reason_code=None,
        )
        return _decision_outputs(
            decision,
            constraints_payloads=[constraints],
            show_cut_line=bool((node.config or {}).get("show_cut_line_overlay", True)),
        )


def _decision_outputs(
    decision: BoardCutDecision,
    *,
    constraints_payloads: list[BoardCutConstraintsPayload] | None = None,
    show_cut_line: bool = True,
) -> NodeExecutionResult:
    outputs: dict[str, Any] = {
        "decision": BoardCutDecisionPayload(decision=decision),
        "result": ResultPayload(
            result=decision.model_dump(mode="json"),
            decision=decision.valid,
        ),
    }
    if show_cut_line and constraints_payloads:
        overlay = _board_cut_line_overlay(decision, constraints_payloads)
        if overlay.primitives:
            outputs["overlay"] = overlay
    return NodeExecutionResult(outputs=outputs)


def _board_cut_line_overlay(
    decision: BoardCutDecision,
    constraints_payloads: list[BoardCutConstraintsPayload],
) -> OverlayPayload:
    cut_position = decision.cut_position_mm
    if not decision.valid or cut_position is None or cut_position <= 0:
        return OverlayPayload()
    primitives: list[OverlayPrimitive] = []
    for payload in constraints_payloads:
        raw_mappings = payload.metadata.get("surface_mappings")
        if not isinstance(raw_mappings, list):
            continue
        view_has_label = False
        view_primitives: list[OverlayPrimitive] = []
        for mapping in raw_mappings:
            primitive = _board_cut_line_primitive(
                mapping,
                cut_position_mm=cut_position,
                view_role=payload.view_role,
                show_label=not view_has_label,
            )
            if primitive is not None:
                view_primitives.append(primitive)
                view_has_label = True
        primitives.extend(_couple_adjacent_cut_lines(view_primitives))
    return OverlayPayload(primitives=primitives)


def _couple_adjacent_cut_lines(
    primitives: list[OverlayPrimitive],
) -> list[OverlayPrimitive]:
    if len(primitives) != 2:
        return primitives
    first, second = primitives
    first_role = str(first.metadata.get("surface_role") or "")
    second_role = str(second.metadata.get("surface_role") or "")
    if first_role == second_role:
        return primitives
    candidates: list[tuple[float, str, str]] = []
    for first_endpoint in ("start", "end"):
        for second_endpoint in ("start", "end"):
            try:
                first_point = first.geometry[first_endpoint]
                second_point = second.geometry[second_endpoint]
                distance = math.hypot(
                    float(first_point["x"]) - float(second_point["x"]),
                    float(first_point["y"]) - float(second_point["y"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            candidates.append((distance, first_endpoint, second_endpoint))
    if not candidates:
        return primitives
    distance, first_endpoint, second_endpoint = min(candidates)
    if distance > 0.03:
        return primitives
    first_geometry = {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in first.geometry.items()
    }
    second_geometry = {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in second.geometry.items()
    }
    shared = {
        "x": (
            float(first_geometry[first_endpoint]["x"])
            + float(second_geometry[second_endpoint]["x"])
        )
        / 2.0,
        "y": (
            float(first_geometry[first_endpoint]["y"])
            + float(second_geometry[second_endpoint]["y"])
        )
        / 2.0,
    }
    first_geometry[first_endpoint] = shared
    second_geometry[second_endpoint] = dict(shared)
    return [
        first.model_copy(update={"geometry": first_geometry}, deep=True),
        second.model_copy(update={"geometry": second_geometry}, deep=True),
    ]


def _board_cut_line_primitive(
    mapping: object,
    *,
    cut_position_mm: float,
    view_role: str,
    show_label: bool,
) -> OverlayPrimitive | None:
    if not isinstance(mapping, dict):
        return None
    try:
        homography = np.asarray(mapping["homography_image_to_world"], dtype=np.float64)
        if homography.shape != (3, 3) or abs(float(np.linalg.det(homography))) <= 1e-12:
            return None
        origin = mapping["origin_world"]
        axis = mapping["longitudinal_axis_world"]
        origin_world = (float(origin["x"]), float(origin["y"]))
        longitudinal_axis_world = (float(axis["x"]), float(axis["y"]))
        visible_world_polygon = _mapping_points(mapping.get("visible_world_polygon"))
    except (KeyError, TypeError, ValueError):
        return None
    if (
        len(visible_world_polygon) < 3
        or abs(math.hypot(*longitudinal_axis_world) - 1.0) > 1e-6
    ):
        return None
    intersections = _longitudinal_line_polygon_intersections(
        visible_world_polygon,
        origin=origin_world,
        axis=longitudinal_axis_world,
        position_mm=cut_position_mm,
    )
    if len(intersections) < 2:
        return None
    first, second = max(
        combinations(intersections, 2),
        key=lambda pair: math.dist(pair[0], pair[1]),
    )
    try:
        image_points = _project_points(np.linalg.inv(homography), [first, second])
    except (ValueError, np.linalg.LinAlgError):
        return None
    if any(
        not math.isfinite(value)
        for point in image_points
        for value in point
    ):
        return None
    image_context_id = mapping.get("image_context_id")
    surface_role = str(mapping.get("surface_role") or "surface")
    return OverlayPrimitive(
        primitive_type="line",
        layer="board_cut_line",
        label=f"Cut {cut_position_mm:.2f} mm" if show_label else None,
        geometry={
            "start": {"x": image_points[0][0], "y": image_points[0][1]},
            "end": {"x": image_points[1][0], "y": image_points[1][1]},
            "coordinate_system": "normalized_xy",
        },
        image_context_id=str(image_context_id) if image_context_id else None,
        style={"stroke": "#ff1744", "stroke_width": 4.0},
        metadata={
            "view_role": view_role,
            "surface_role": surface_role,
            "cut_position_mm": cut_position_mm,
            "virtual_cut_line": True,
        },
    )


def _mapping_points(value: object) -> list[tuple[float, float]]:
    if not isinstance(value, list):
        return []
    points: list[tuple[float, float]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            points.append((float(item["x"]), float(item["y"])))
        except (KeyError, TypeError, ValueError):
            continue
    return points


def _longitudinal_position_mm(
    point: tuple[float, float],
    *,
    origin: tuple[float, float],
    axis: tuple[float, float],
) -> float:
    return (point[0] - origin[0]) * axis[0] + (point[1] - origin[1]) * axis[1]


def _longitudinal_line_polygon_intersections(
    polygon: list[tuple[float, float]],
    *,
    origin: tuple[float, float],
    axis: tuple[float, float],
    position_mm: float,
) -> list[tuple[float, float]]:
    intersections: list[tuple[float, float]] = []
    for first, second in zip(polygon, polygon[1:] + polygon[:1], strict=False):
        first_position = _longitudinal_position_mm(first, origin=origin, axis=axis)
        second_position = _longitudinal_position_mm(second, origin=origin, axis=axis)
        minimum_position = min(first_position, second_position)
        maximum_position = max(first_position, second_position)
        if (
            position_mm < minimum_position - 1e-9
            or position_mm > maximum_position + 1e-9
        ):
            continue
        delta_position = second_position - first_position
        if abs(delta_position) <= 1e-12:
            if abs(position_mm - first_position) <= 1e-9:
                intersections.extend([first, second])
            continue
        fraction = (position_mm - first_position) / delta_position
        if -1e-9 <= fraction <= 1.0 + 1e-9:
            intersections.append(
                (
                    first[0] + (second[0] - first[0]) * fraction,
                    first[1] + (second[1] - first[1]) * fraction,
                )
            )
    deduplicated: list[tuple[float, float]] = []
    for point in intersections:
        if not any(math.dist(point, existing) <= 1e-7 for existing in deduplicated):
            deduplicated.append(point)
    return deduplicated


def _invalid_decision(
    workpiece_id: str,
    status: str,
    reason_code: str | None,
    inspection_length: float,
    capture_skew: float,
    *,
    views: list[str] | None = None,
    uncertainty: float = 0.0,
    dominant: BoardFeatureConstraint | None = None,
) -> BoardCutDecision:
    allowed = {
        "camera_missing",
        "capture_not_synchronized",
        "capture_timeout",
        "board_end_not_found",
        "calibration_invalid",
        "feature_clipped",
        "safe_cut_outside_inspection_range",
    }
    normalized_status = status if status in allowed else "calibration_invalid"
    return BoardCutDecision(
        workpiece_id=workpiece_id,
        valid=False,
        status=normalized_status,  # type: ignore[arg-type]
        cut_position_mm=None,
        longitudinal_shift_mm=None,
        inspection_length_mm=inspection_length,
        capture_skew_ms=capture_skew,
        views_used=views or [],  # type: ignore[arg-type]
        uncertainty_mm=uncertainty,
        dominant_constraint=dominant,
        reason_code=reason_code,
    )


def _invalid_constraints(
    context: RuntimeExecutionContext,
    view_role: str,
    config: dict[str, Any],
    status: str,
    reason: str,
    *,
    rms: float = 0.0,
    uncertainty: float = 0.0,
) -> BoardCutConstraintsPayload:
    normalized_role = view_role if view_role in REQUIRED_VIEW_ROLES else "upper"
    return BoardCutConstraintsPayload(
        workpiece_id=_workpiece_id(context),
        view_role=normalized_role,  # type: ignore[arg-type]
        valid=False,
        status=status,  # type: ignore[arg-type]
        reason_code=reason,
        inspection_length_mm=_positive_float(config.get("inspection_length_mm"), 300.0),
        calibrated_limit_mm=_positive_float(config.get("calibrated_limit_mm"), 0.001),
        calibration_rms_mm=rms,
        uncertainty_mm=uncertainty,
    )


def _build_frame_set(
    context: RuntimeExecutionContext,
    config: dict[str, Any],
    max_skew: float,
) -> SynchronizedFrameSetPayload:
    channels = {role: str(config.get(f"{role}_channel_id") or role) for role in REQUIRED_VIEW_ROLES}
    frames: list[SynchronizedCameraFrame] = []
    timestamps: list[datetime] = []
    for role in REQUIRED_VIEW_ROLES:
        frame = context.input_frames.get(role)
        if frame is None:
            continue
        captured_at = str(frame.metadata.get("captured_at") or frame.timestamp or "") or None
        parsed = _parse_timestamp(captured_at)
        if parsed is not None:
            timestamps.append(parsed)
        frames.append(
            SynchronizedCameraFrame(
                role=role,
                channel_id=channels[role],
                frame=frame,
                captured_at=captured_at,
            )
        )
    missing = [role for role in REQUIRED_VIEW_ROLES if role not in context.input_frames]
    skew = max(float(context.metadata.get("capture_skew_ms", 0.0)), 0.0)
    if len(timestamps) == len(REQUIRED_VIEW_ROLES):
        skew = max((max(timestamps) - min(timestamps)).total_seconds() * 1000.0, 0.0)
    if missing:
        valid, status, reason = False, "camera_missing", f"missing_views:{','.join(missing)}"
    elif len(timestamps) != len(REQUIRED_VIEW_ROLES):
        valid, status, reason = (
            False,
            "capture_not_synchronized",
            "capture_timestamp_invalid",
        )
    elif skew > max_skew:
        valid, status, reason = False, "capture_not_synchronized", "capture_skew_exceeded"
    else:
        valid, status, reason = True, "captured", None
    return SynchronizedFrameSetPayload(
        workpiece_id=_workpiece_id(context),
        trigger_id=context.trigger.trigger_id if context.trigger is not None else None,
        frames=frames,
        capture_skew_ms=round(skew, 3),
        max_capture_skew_ms=max_skew,
        valid=valid,
        status=status,  # type: ignore[arg-type]
        reason_code=reason,
    )


def _build_single_test_frame_set(
    context: RuntimeExecutionContext,
    config: dict[str, Any],
    role: ViewRole,
) -> SynchronizedFrameSetPayload:
    frame = context.input_frames.get(role) or context.input_frame
    captured_at = None
    frames: list[SynchronizedCameraFrame] = []
    if frame is not None:
        captured_at = str(frame.metadata.get("captured_at") or frame.timestamp or "") or None
        frames.append(
            SynchronizedCameraFrame(
                role=role,
                channel_id=str(config.get("camera_channel_id") or role),
                frame=frame,
                captured_at=captured_at,
            )
        )
    if frame is None:
        valid, status, reason = False, "camera_missing", f"missing_view:{role}"
    elif _parse_timestamp(captured_at) is None:
        valid, status, reason = (
            False,
            "capture_not_synchronized",
            "capture_timestamp_invalid",
        )
    else:
        valid, status, reason = True, "captured", None
    return SynchronizedFrameSetPayload(
        workpiece_id=_workpiece_id(context),
        trigger_id=context.trigger.trigger_id if context.trigger is not None else None,
        frames=frames,
        capture_skew_ms=0.0,
        max_capture_skew_ms=0.0,
        valid=valid,
        status=status,  # type: ignore[arg-type]
        reason_code=reason,
        metadata={"single_camera_test": True, "camera_role": role},
    )


def _workpiece_id(context: RuntimeExecutionContext) -> str:
    body = context.trigger.body if context.trigger is not None else {}
    return str(body.get("workpiece_id") or body.get("sequence_id") or context.correlation_id)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _features_from_payload(payload: object) -> list[_FeatureGeometry]:
    features: list[_FeatureGeometry] = []
    if isinstance(payload, DetectionPayload):
        for index, detection in enumerate(payload.detections):
            box = detection.box
            object_ref = payload.objects[index] if index < len(payload.objects) else None
            features.append(
                _FeatureGeometry(
                    feature_id=object_ref.object_id
                    if object_ref is not None
                    else f"detection_{index + 1}",
                    label=detection.label,
                    class_id=object_ref.class_id if object_ref is not None else None,
                    score=detection.score,
                    points=_box_points(box),
                    source_image_context_id=payload.image_context_id,
                    is_segmentation=False,
                )
            )
    elif isinstance(payload, SegmentPayload):
        for index, segment in enumerate(payload.segments):
            if len(segment.polygon) < 3:
                continue
            object_ref = payload.objects[index] if index < len(payload.objects) else None
            features.append(
                _FeatureGeometry(
                    feature_id=object_ref.object_id
                    if object_ref is not None
                    else f"segment_{index + 1}",
                    label=segment.label,
                    class_id=object_ref.class_id if object_ref is not None else None,
                    score=segment.score,
                    points=[(point.x, point.y) for point in segment.polygon],
                    source_image_context_id=payload.image_context_id,
                    is_segmentation=True,
                )
            )
    elif isinstance(payload, RegionSetPayload):
        for index, region in enumerate(payload.regions):
            object_ref = (region.metadata or {}).get("object_ref") or {}
            class_id = object_ref.get("class_id") if isinstance(object_ref, dict) else None
            features.append(
                _FeatureGeometry(
                    feature_id=region.source_object_id or region.key or f"region_{index + 1}",
                    label=region.label or "object",
                    class_id=int(class_id) if class_id is not None else None,
                    score=region.score if region.score is not None else 1.0,
                    points=_box_points(region.box),
                    source_image_context_id=region.source_image_context_id,
                    is_segmentation=False,
                )
            )
    return features


def _payload_image_context_id(payload: object) -> str | None:
    return getattr(payload, "image_context_id", None)


def _geometry_input_context_id(inputs: NodeInputs) -> str | None:
    for port in ("segments", "features"):
        context_id = _payload_image_context_id(inputs.get(port))
        if context_id:
            return context_id
    coordinate_payload = inputs.get("coordinate_frame")
    if isinstance(coordinate_payload, CoordinateFramePayload):
        return coordinate_payload.frame.image_context_id
    frame = inputs.get("frame")
    if isinstance(frame, FramePayload) and frame.image_context is not None:
        return frame.image_context.context_id
    return None


def _box_points(box: DetectionBox) -> list[tuple[float, float]]:
    return [
        (box.x_min, box.y_min),
        (box.x_max, box.y_min),
        (box.x_max, box.y_max),
        (box.x_min, box.y_max),
    ]


def _validated_class_rules(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("class_rules_missing")
    rules = [item for item in value if isinstance(item, dict)]
    if len(rules) != len(value):
        raise ValueError("class_rules_invalid")
    if any(not bool(rule.get("confirmed", False)) for rule in rules):
        raise ValueError("class_rules_unconfirmed")
    class_ids = [rule.get("class_id") for rule in rules]
    if any(not isinstance(class_id, int) or class_id < 0 for class_id in class_ids):
        raise ValueError("class_rule_id_invalid")
    if len(set(class_ids)) != len(class_ids):
        raise ValueError("class_rule_id_duplicate")
    if any(not str(rule.get("label_snapshot") or "").strip() for rule in rules):
        raise ValueError("class_rule_snapshot_missing")
    return rules


def _rule_for_feature(
    rules: list[dict[str, Any]], feature: _FeatureGeometry
) -> dict[str, Any] | None:
    if feature.class_id is not None:
        match = next(
            (
                rule
                for rule in rules
                if rule.get("class_id") == feature.class_id
                and str(rule.get("label_snapshot") or "").casefold() == feature.label.casefold()
            ),
            None,
        )
        if match is not None:
            return match
    return next(
        (
            rule
            for rule in rules
            if str(rule.get("label_snapshot") or "").casefold() == feature.label.casefold()
        ),
        None,
    )


def _payload_class_map_matches_rules(
    payload: object,
    rules: list[dict[str, Any]],
) -> bool:
    metadata = getattr(payload, "metadata", None)
    if not isinstance(metadata, dict):
        return True
    capability = metadata.get("capability")
    if not isinstance(capability, dict):
        return True
    raw_labels = capability.get("class_labels")
    if not isinstance(raw_labels, list) or not raw_labels:
        return True
    labels = [
        str(item.get("label") or item.get("name") or item.get("class_name") or "")
        if isinstance(item, dict)
        else str(item)
        for item in raw_labels
    ]
    rules_by_id = {int(rule["class_id"]): rule for rule in rules}
    if set(rules_by_id) != set(range(len(labels))):
        return False
    if any(
        str(rules_by_id[index].get("label_snapshot") or "") != label
        for index, label in enumerate(labels)
    ):
        return False
    version = capability.get("class_map_version")
    return not version or all(
        str(rule.get("class_map_version") or "") == str(version) for rule in rules
    )


def _feature_class_id(feature: _FeatureGeometry, rule: dict[str, Any]) -> int:
    return feature.class_id if feature.class_id is not None else int(rule["class_id"])


def _measurement_basis(rule: dict[str, Any]) -> str:
    value = str(rule.get("measurement_basis") or "max_feret_diameter")
    allowed = {
        "max_feret_diameter",
        "equivalent_diameter",
        "longitudinal_extent",
        "transverse_extent",
    }
    return value if value in allowed else "max_feret_diameter"


def _segmentation_board_end_detection(
    value: object,
    *,
    config: dict[str, Any],
    side: str,
    image_width: float,
    image_height: float,
) -> _BoardEndDetection:
    if not isinstance(value, SegmentPayload):
        raise ValueError("segmentation_unavailable")
    simplification = value.metadata.get("polygon_simplification")
    if isinstance(simplification, dict) and not bool(simplification.get("valid", True)):
        raise ValueError(str(simplification.get("reason_code") or "invalid_polygon"))

    mapping = _validated_surface_class_mapping(
        config.get("board_surface_class_mapping"),
        required_roles={"board_main_face", "board_side_face"},
        payload=value,
    )
    minimum_score = min(max(float(config.get("minimum_surface_confidence", 0.35)), 0.0), 1.0)
    selected: dict[str, Any] = {}
    for role, item in mapping.items():
        matches = [
            segment
            for segment in value.segments
            if segment.label == item["label_snapshot"] and segment.score >= minimum_score
        ]
        if not matches:
            raise ValueError("surface_missing")
        if len(matches) != 1:
            raise ValueError("surface_ambiguous")
        if len(matches[0].polygon) < 3:
            raise ValueError("invalid_polygon")
        selected[role] = matches[0]

    normalized_polygons = {
        role: [(float(point.x), float(point.y)) for point in segment.polygon]
        for role, segment in selected.items()
    }
    pixel_polygons = {
        role: [(x * image_width, y * image_height) for x, y in points]
        for role, points in normalized_polygons.items()
    }
    all_points = np.asarray(
        [point for points in pixel_polygons.values() for point in points], dtype=np.float64
    )
    if len(all_points) < 6:
        raise ValueError("invalid_polygon")
    centered = all_points - np.mean(all_points, axis=0)
    covariance = np.cov(centered.T)
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    order = np.argsort(eigenvalues)[::-1]
    major = float(max(eigenvalues[order[0]], 0.0))
    minor = float(max(eigenvalues[order[1]], 0.0))
    if major <= 1e-9 or major / max(minor, 1e-9) < 1.25:
        raise ValueError("surface_ambiguous")
    longitudinal = np.asarray(eigenvectors[:, order[0]], dtype=np.float64)
    desired_x = 1.0 if side == "left" else -1.0
    if longitudinal[0] * desired_x < 0:
        longitudinal *= -1.0
    transverse = np.asarray([-longitudinal[1], longitudinal[0]], dtype=np.float64)
    longitudinal_projection = all_points @ longitudinal
    transverse_projection = all_points @ transverse
    start_s = float(np.min(longitudinal_projection))
    end_s = float(np.max(longitudinal_projection))
    start_band = max((end_s - start_s) * 0.08, 2.0)
    near_start = transverse_projection[longitudinal_projection <= start_s + start_band]
    if len(near_start) < 2:
        raise ValueError("surface_ambiguous")
    start_t_min = float(np.min(near_start))
    start_t_max = float(np.max(near_start))
    if start_t_max - start_t_min <= 2.0:
        raise ValueError("surface_ambiguous")
    start_first = longitudinal * start_s + transverse * start_t_min
    start_second = longitudinal * start_s + transverse * start_t_max
    origin = (start_first + start_second) * 0.5
    side_endpoint = longitudinal * end_s + transverse * ((start_t_min + start_t_max) * 0.5)

    view_role = str(config.get("view_role") or "upper")
    surface_roles = (
        {"board_main_face": "bottom", "board_side_face": "side_b"}
        if view_role == "lower"
        else {"board_main_face": "top", "board_side_face": "side_a"}
    )
    guided_polygons = {
        surface_roles[role]: [[round(x, 8), round(y, 8)] for x, y in points]
        for role, points in normalized_polygons.items()
    }
    origin_normalized = [origin[0] / image_width, origin[1] / image_height]
    axis_normalized = [
        side_endpoint[0] / image_width,
        side_endpoint[1] / image_height,
    ]
    end_line_normalized = [
        [start_first[0] / image_width, start_first[1] / image_height],
        [start_second[0] / image_width, start_second[1] / image_height],
    ]
    confidence = min(
        float(selected["board_main_face"].score),
        float(selected["board_side_face"].score),
        min(max((major / max(minor, 1e-9) - 1.0) / 3.0, 0.0), 1.0),
    )
    start_edge = [
        float(start_first[0]),
        float(start_first[1]),
        float(start_second[0]),
        float(start_second[1]),
    ]
    side_edge = [
        float(origin[0]),
        float(origin[1]),
        float(side_endpoint[0]),
        float(side_endpoint[1]),
    ]
    return _BoardEndDetection(
        origin=(float(origin[0]), float(origin[1])),
        longitudinal=(float(longitudinal[0]), float(longitudinal[1])),
        confidence=confidence,
        source={
            "method": "segmentation_polygons",
            "found": True,
            "start_edge": start_edge,
            "side_edge": side_edge,
            "validated_surface_edges": [start_edge, side_edge],
            "segmentation_surface_polygons": guided_polygons,
            "segmentation_origin": origin_normalized,
            "segmentation_axis_endpoint": axis_normalized,
            "segmentation_end_line": end_line_normalized,
            "segmentation_scores": {
                role: round(float(segment.score), 6) for role, segment in selected.items()
            },
            "class_map_version": next(iter(mapping.values())).get("class_map_version"),
            "deployment_release_id": value.metadata.get("deployment_release_id"),
        },
    )


def _validated_surface_class_mapping(
    value: object,
    *,
    required_roles: set[str],
    payload: SegmentPayload,
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError("surface_mapping_invalid")
    result: dict[str, dict[str, Any]] = {}
    labels: set[str] = set()
    payload_release = str(payload.metadata.get("deployment_release_id") or "")
    capability = payload.metadata.get("capability")
    payload_class_map = (
        str(capability.get("class_map_version") or "")
        if isinstance(capability, dict)
        else ""
    )
    for role in required_roles:
        item = value.get(role)
        if not isinstance(item, dict) or not bool(item.get("confirmed")):
            raise ValueError("surface_mapping_invalid")
        class_id = item.get("class_id")
        label = str(item.get("label_snapshot") or "").strip()
        if not isinstance(class_id, int) or class_id < 0 or not label or label in labels:
            raise ValueError("surface_mapping_invalid")
        configured_release = str(item.get("deployment_release_id") or "")
        configured_class_map = str(item.get("class_map_version") or "")
        if configured_release and payload_release and configured_release != payload_release:
            raise ValueError("surface_mapping_invalid")
        if configured_class_map and payload_class_map and configured_class_map != payload_class_map:
            raise ValueError("surface_mapping_invalid")
        result[role] = dict(item)
        labels.add(label)
    return result


def _board_format(
    context: RuntimeExecutionContext,
    config: dict[str, Any],
) -> dict[str, Any]:
    value = context.trigger.body.get("board_format") if context.trigger is not None else None
    if not isinstance(value, dict):
        if context.mode == RuntimeMode.TRIGGER:
            raise ValueError("invalid_board_format")
        value = {
            "format_id": str(config.get("board_format_id") or "realtime-test"),
            "format_version": str(config.get("board_format_version") or "1"),
            "width_mm": config.get("board_width_mm"),
            "thickness_mm": config.get("board_thickness_mm"),
        }
    try:
        width = float(value.get("width_mm"))
        thickness = float(value.get("thickness_mm"))
    except (TypeError, ValueError):
        raise ValueError("invalid_board_format") from None
    if not 0 < width <= 10_000 or not 0 < thickness <= 2_000:
        raise ValueError("invalid_board_format")
    format_id = str(value.get("format_id") or "").strip()
    format_version = str(value.get("format_version") or "").strip()
    if not format_id or not format_version:
        raise ValueError("invalid_board_format")
    return {
        "format_id": format_id,
        "format_version": format_version,
        "width_mm": width,
        "thickness_mm": thickness,
    }


def _dimensioned_longitudinal_calibrations(
    calibrations: list[_SurfaceCalibration],
    board_format: dict[str, Any],
) -> list[_SurfaceCalibration]:
    result: list[_SurfaceCalibration] = []
    for calibration in calibrations:
        target_span = (
            float(board_format["width_mm"])
            if calibration.role in {"top", "bottom"}
            else float(board_format["thickness_mm"])
        )
        ys = [point[1] for point in calibration.world_polygon]
        minimum = min(ys)
        span = max(ys) - minimum
        if span <= 1e-9:
            raise ValueError("invalid_board_format")
        world = [
            (point[0], (point[1] - minimum) * target_span / span)
            for point in calibration.world_polygon
        ]
        result.append(
            _SurfaceCalibration(
                role=calibration.role,
                image_polygon=calibration.image_polygon,
                homography=_homography(calibration.image_polygon, world),
                world_polygon=world,
            )
        )
    return result


def _dynamic_end_face_calibration(
    payload: SegmentPayload,
    *,
    config: dict[str, Any],
    context: RuntimeExecutionContext,
) -> tuple[_SurfaceCalibration, list[tuple[float, float]], dict[str, Any]]:
    mapping = _validated_surface_class_mapping(
        config.get("board_surface_class_mapping"),
        required_roles={"board_end_face"},
        payload=payload,
    )
    item = mapping["board_end_face"]
    minimum_score = min(max(float(config.get("minimum_surface_confidence", 0.35)), 0.0), 1.0)
    matches = [
        segment
        for segment in payload.segments
        if segment.label == item["label_snapshot"] and segment.score >= minimum_score
    ]
    if not matches:
        raise ValueError("surface_missing")
    if len(matches) != 1:
        raise ValueError("surface_ambiguous")
    polygon = [(float(point.x), float(point.y)) for point in matches[0].polygon]
    if len(polygon) < 4 or abs(_polygon_area(polygon)) <= 1e-9:
        raise ValueError("invalid_polygon")
    quad = _perspective_quad(polygon)
    board_format = _board_format(context, config)
    width = float(board_format["width_mm"])
    thickness = float(board_format["thickness_mm"])

    horizontal_span = (math.dist(quad[0], quad[1]) + math.dist(quad[3], quad[2])) * 0.5
    vertical_span = (math.dist(quad[0], quad[3]) + math.dist(quad[1], quad[2])) * 0.5
    physical_width_is_larger = width >= thickness
    image_horizontal_is_larger = horizontal_span >= vertical_span
    if physical_width_is_larger != image_horizontal_is_larger:
        quad = [quad[1], quad[2], quad[3], quad[0]]
    world = [(0.0, 0.0), (width, 0.0), (width, thickness), (0.0, thickness)]
    return (
        _SurfaceCalibration(
            role="end_face",
            image_polygon=polygon,
            homography=_homography(quad, world),
            world_polygon=world,
        ),
        polygon,
        board_format,
    )


def _perspective_quad(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    import cv2

    contour = np.asarray(points, dtype=np.float32).reshape((-1, 1, 2))
    hull = cv2.convexHull(contour)
    perimeter = float(cv2.arcLength(hull, True))
    candidates: list[np.ndarray] = []
    for ratio in np.linspace(0.005, 0.12, 48):
        approximation = cv2.approxPolyDP(hull, perimeter * float(ratio), True).reshape((-1, 2))
        if len(approximation) == 4:
            candidates.append(approximation)
            break
    if not candidates:
        raise ValueError("surface_ambiguous")
    raw = candidates[0]
    sums = raw[:, 0] + raw[:, 1]
    differences = raw[:, 1] - raw[:, 0]
    ordered = [
        raw[int(np.argmin(sums))],
        raw[int(np.argmin(differences))],
        raw[int(np.argmax(sums))],
        raw[int(np.argmax(differences))],
    ]
    result = [(float(point[0]), float(point[1])) for point in ordered]
    if len(set(result)) != 4 or abs(_polygon_area(result)) <= 1e-9:
        raise ValueError("surface_ambiguous")
    return result


def _board_geometry_warning_overlay(
    image_context_id: str | None,
    reason_code: str,
    *,
    detail: str | None = None,
    node_key: str | None = None,
) -> OverlayPayload:
    return OverlayPayload(
        primitives=[
            OverlayPrimitive(
                primitive_type="label",
                layer="board_geometry_warning",
                label=f"Board geometry invalid: {reason_code}",
                geometry={},
                image_context_id=image_context_id,
                style={"color": "#ff5252", "ttl_ms": 3000},
                metadata={
                    "valid": False,
                    "reason_code": reason_code,
                    "detail": detail,
                    "node_key": node_key,
                    "notification_cooldown_ms": 3000,
                },
            )
        ],
        display_context_id=image_context_id,
    )


def _surface_calibrations(
    value: object,
    *,
    allowed_roles: set[str],
    intrinsics: object = None,
    image_width: float = 1.0,
    image_height: float = 1.0,
) -> list[_SurfaceCalibration]:
    if not isinstance(value, list) or not value:
        raise ValueError("surface_calibration_missing")
    calibrations: list[_SurfaceCalibration] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("surface_calibration_invalid")
        role = str(item.get("surface_role") or "")
        if role not in allowed_roles or role in seen:
            raise ValueError("surface_role_invalid")
        image_points = _undistort_normalized_points(
            _points(item.get("image_points"), "image_points"),
            intrinsics,
            image_width=image_width,
            image_height=image_height,
        )
        world_points = _points(item.get("world_points_mm"), "world_points_mm")
        homography = _homography(image_points, world_points)
        calibrations.append(
            _SurfaceCalibration(
                role=role,  # type: ignore[arg-type]
                image_polygon=image_points,
                homography=homography,
                world_polygon=world_points,
            )
        )
        seen.add(role)
    if seen != allowed_roles:
        raise ValueError("required_surface_calibration_missing")
    return calibrations


def _segmentation_guided_board_pose(
    coordinate: InspectionCoordinateFrame,
    *,
    calibrations: list[_SurfaceCalibration],
) -> _GuidedBoardPose:
    source = coordinate.source
    raw_polygons = source.get("segmentation_surface_polygons")
    raw_origin = source.get("segmentation_origin")
    raw_axis_endpoint = source.get("segmentation_axis_endpoint")
    raw_end_line = source.get("segmentation_end_line")
    if not isinstance(raw_polygons, dict):
        raise ValueError("surface_missing")
    required_roles = {surface.role for surface in calibrations}
    if set(raw_polygons) != required_roles:
        raise ValueError("surface_missing")

    polygons: dict[str, list[tuple[float, float]]] = {}
    for role, value in raw_polygons.items():
        if not isinstance(value, list) or len(value) < 3:
            raise ValueError("invalid_polygon")
        points: list[tuple[float, float]] = []
        for point in value:
            if not isinstance(point, list | tuple) or len(point) != 2:
                raise ValueError("invalid_polygon")
            x, y = point
            if not isinstance(x, int | float) or not isinstance(y, int | float):
                raise ValueError("invalid_polygon")
            if not 0 <= float(x) <= 1 or not 0 <= float(y) <= 1:
                raise ValueError("invalid_polygon")
            points.append((float(x), float(y)))
        if abs(_polygon_area(points)) <= 1e-9:
            raise ValueError("invalid_polygon")
        polygons[str(role)] = points

    origin = _normalized_pair(raw_origin, "segmentation_origin")
    axis_endpoint = _normalized_pair(raw_axis_endpoint, "segmentation_axis_endpoint")
    if math.dist(origin, axis_endpoint) <= 1e-6:
        raise ValueError("surface_ambiguous")
    if not isinstance(raw_end_line, list) or len(raw_end_line) != 2:
        raise ValueError("surface_ambiguous")
    end_line = (
        _normalized_pair(raw_end_line[0], "segmentation_end_line"),
        _normalized_pair(raw_end_line[1], "segmentation_end_line"),
    )
    if math.dist(*end_line) <= 1e-6:
        raise ValueError("surface_ambiguous")
    return _GuidedBoardPose(
        origin=origin,
        axis_endpoint=axis_endpoint,
        end_line=end_line,
        reference_anchor=origin,
        surface_polygons=polygons,
        confidence=coordinate.confidence,
        source={
            **source,
            "method": "segmentation_calibrated_projective_pose",
            "surface_projection": "segmentation_polygons",
        },
    )


def _normalized_pair(value: object, name: str) -> tuple[float, float]:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise ValueError(f"{name}_invalid")
    x, y = value
    if not isinstance(x, int | float) or not isinstance(y, int | float):
        raise ValueError(f"{name}_invalid")
    point = (float(x), float(y))
    if not 0 <= point[0] <= 1 or not 0 <= point[1] <= 1:
        raise ValueError(f"{name}_invalid")
    return point


def _calibration_guided_board_pose(
    coordinate: InspectionCoordinateFrame,
    *,
    calibrations: list[_SurfaceCalibration],
    config: dict[str, Any],
    intrinsics: object,
    image_width: float,
    image_height: float,
    _use_preliminary_gate: bool = True,
) -> _GuidedBoardPose | None:
    if coordinate.source.get("method") == "segmentation_polygons":
        return _segmentation_guided_board_pose(coordinate, calibrations=calibrations)
    raw_candidates = coordinate.source.get("calibration_candidates")
    if not isinstance(raw_candidates, list):
        return None
    candidate_lines = _calibration_candidate_lines(
        raw_candidates,
        intrinsics=intrinsics,
        image_width=image_width,
        image_height=image_height,
    )
    if not candidate_lines:
        raise ValueError("calibration_guided_pose_candidates_missing")

    reference_end_normalized = _reference_board_end_line(calibrations)
    reference_end = (
        _normalized_to_pixel(reference_end_normalized[0], image_width, image_height),
        _normalized_to_pixel(reference_end_normalized[1], image_width, image_height),
    )
    boundary_merge_tolerance = max(
        float(config.get("calibration_boundary_merge_tolerance_px", 15.0)),
        1.0,
    )
    boundaries = _calibration_longitudinal_boundaries(
        calibrations,
        image_width=image_width,
        image_height=image_height,
        merge_tolerance_px=boundary_merge_tolerance,
    )
    if len(boundaries) < 2:
        raise ValueError("calibration_guided_pose_reference_incomplete")

    reference_end_direction = _oriented_line_direction(
        (
            reference_end[0][0],
            reference_end[0][1],
            reference_end[1][0],
            reference_end[1][1],
        )
    )
    reference_longitudinal = _average_directions(
        [boundary.longitudinal for boundary in boundaries]
    )
    preliminary_gate_min_confidence = min(
        max(
            float(config.get("calibration_preliminary_gate_min_confidence", 0.55)),
            0.0,
        ),
        1.0,
    )
    preliminary_gate_enabled = (
        _use_preliminary_gate
        and coordinate.confidence >= preliminary_gate_min_confidence
        and bool(coordinate.source.get("found", False))
    )
    preliminary_anchor: tuple[float, float] | None = None
    if preliminary_gate_enabled:
        undistorted_anchor = _undistort_normalized_points(
            [
                (
                    coordinate.origin.x / image_width,
                    coordinate.origin.y / image_height,
                )
            ],
            intrinsics,
            image_width=image_width,
            image_height=image_height,
        )[0]
        preliminary_anchor = (
            undistorted_anchor[0] * image_width,
            undistorted_anchor[1] * image_height,
        )
    preliminary_gate_tolerance = max(
        float(config.get("calibration_preliminary_gate_tolerance_px", 30.0)),
        min(image_width, image_height) * 0.035,
    )
    angle_tolerance_degrees = max(
        min(
            float(config.get("calibration_pose_angle_tolerance_degrees", 15.0)),
            45.0,
        ),
        1.0,
    )
    angle_tolerance = math.radians(angle_tolerance_degrees)
    end_candidates = [
        line
        for line in candidate_lines
        if _undirected_angle_difference(
            _oriented_line_direction(line),
            reference_end_direction,
        )
        <= angle_tolerance
    ]
    longitudinal_candidates = [
        line
        for line in candidate_lines
        if _undirected_angle_difference(
            _oriented_line_direction(line),
            reference_longitudinal,
        )
        <= angle_tolerance
    ]
    board_end_side = str(coordinate.source.get("board_end_side") or "left")

    position_tolerance = max(
        float(config.get("calibration_candidate_merge_distance_px", 8.0)),
        1.0,
    )
    merge_gap = max(
        float(config.get("calibration_candidate_merge_gap_px", 50.0)),
        0.0,
    )
    end_candidates = _merge_oriented_line_segments(
        end_candidates,
        reference_direction=reference_end_direction,
        position_tolerance=position_tolerance,
        gap_tolerance=merge_gap,
    )
    longitudinal_candidates = _merge_oriented_line_segments(
        longitudinal_candidates,
        reference_direction=reference_longitudinal,
        position_tolerance=position_tolerance,
        gap_tolerance=merge_gap,
    )
    if not end_candidates or not longitudinal_candidates:
        fallback = _preliminary_anchored_calibration_pose(
            coordinate,
            calibrations=calibrations,
            boundaries=boundaries,
            config=config,
            intrinsics=intrinsics,
            reference_end_direction=reference_end_direction,
            preliminary_anchor=preliminary_anchor,
            preliminary_gate_tolerance=preliminary_gate_tolerance,
            preliminary_gate_rejection_count=0,
            board_end_side=board_end_side,
            angle_tolerance=angle_tolerance,
            image_width=image_width,
            image_height=image_height,
        )
        if fallback is not None:
            return fallback
        raise ValueError("calibration_guided_pose_reference_edges_missing")

    association_tolerance = max(
        float(config.get("calibration_pose_join_tolerance_px", 30.0)),
        min(image_width, image_height) * 0.025,
    )
    boundary_tolerance = max(
        float(config.get("calibration_pose_boundary_tolerance_px", 30.0)),
        min(image_width, image_height) * 0.035,
    )
    # Every physical longitudinal boundary is required to construct both
    # perspective-correct surface polygons. Missing boundaries fail closed.
    minimum_matches = len(boundaries)
    reference_center = (
        (reference_end[0][0] + reference_end[1][0]) / 2.0,
        (reference_end[0][1] + reference_end[1][1]) / 2.0,
    )
    hypotheses: list[dict[str, Any]] = []
    preliminary_gate_rejections = 0
    for end_line in end_candidates:
        end_anchor_distance = (
            _point_to_line_distance(preliminary_anchor, end_line)
            if preliminary_anchor is not None
            else 0.0
        )
        if (
            preliminary_anchor is not None
            and end_anchor_distance > preliminary_gate_tolerance
        ):
            preliminary_gate_rejections += 1
            continue
        candidate_end_direction = _direction_aligned_with(
            _oriented_line_direction(end_line),
            reference_end_direction,
        )
        end_rotation = _signed_angle(reference_end_direction, candidate_end_direction)
        for longitudinal_line in longitudinal_candidates:
            longitudinal_anchor_distance = (
                _point_to_line_distance(preliminary_anchor, longitudinal_line)
                if preliminary_anchor is not None
                else 0.0
            )
            if (
                preliminary_anchor is not None
                and longitudinal_anchor_distance > preliminary_gate_tolerance
            ):
                preliminary_gate_rejections += 1
                continue
            current_anchor = _line_intersection(end_line, longitudinal_line)
            if current_anchor is None:
                continue
            if (
                _point_to_segment_distance(current_anchor, end_line)
                > association_tolerance
                or _point_to_segment_distance(current_anchor, longitudinal_line)
                > association_tolerance
            ):
                continue
            candidate_longitudinal = _direction_aligned_with(
                _oriented_line_direction(longitudinal_line),
                reference_longitudinal,
            )
            longitudinal_rotation = _signed_angle(
                reference_longitudinal,
                candidate_longitudinal,
            )
            rotation_disagreement = abs(
                _wrapped_angle(end_rotation - longitudinal_rotation)
            )
            if rotation_disagreement > angle_tolerance:
                continue
            rotation = _mean_angle(end_rotation, longitudinal_rotation)
            for reference_boundary in boundaries:
                transformed_boundaries = [
                    _transform_point_rigid(
                        boundary.anchor,
                        reference_anchor=reference_boundary.anchor,
                        current_anchor=current_anchor,
                        rotation=rotation,
                    )
                    for boundary in boundaries
                ]
                matches = _match_calibration_boundaries(
                    transformed_boundaries,
                    longitudinal_candidates,
                    boundary_tolerance=boundary_tolerance,
                )
                if len(matches) < minimum_matches:
                    continue
                match_quality = sum(
                    max(1.0 - distance / boundary_tolerance, 0.0)
                    for _, _, distance in matches
                )
                angle_quality = max(
                    1.0 - rotation_disagreement / angle_tolerance,
                    0.0,
                )
                support = min(
                    _line_length(end_line) / max(image_height * 0.25, 1.0),
                    1.0,
                ) + min(
                    _line_length(longitudinal_line) / max(image_width * 0.35, 1.0),
                    1.0,
                )
                transformed_center = _transform_point_rigid(
                    reference_center,
                    reference_anchor=reference_boundary.anchor,
                    current_anchor=current_anchor,
                    rotation=rotation,
                )
                hypotheses.append(
                    {
                        "score": (
                            2.0 * len(matches)
                            + match_quality
                            + 0.5 * angle_quality
                            + 0.25 * support
                        ),
                        "rotation": rotation,
                        "rotation_disagreement": rotation_disagreement,
                        "reference_anchor": reference_boundary.anchor,
                        "current_anchor": current_anchor,
                        "transformed_center": transformed_center,
                        "end_line": end_line,
                        "longitudinal_line": longitudinal_line,
                        "longitudinal": _rotate_vector(
                            reference_longitudinal,
                            rotation,
                        ),
                        "match_count": len(matches),
                        "match_quality": match_quality / len(matches),
                        "matches": matches,
                        "preliminary_end_distance_px": end_anchor_distance,
                        "preliminary_longitudinal_distance_px": (
                            longitudinal_anchor_distance
                        ),
                    }
                )
    if not hypotheses:
        fallback = _preliminary_anchored_calibration_pose(
            coordinate,
            calibrations=calibrations,
            boundaries=boundaries,
            config=config,
            intrinsics=intrinsics,
            reference_end_direction=reference_end_direction,
            preliminary_anchor=preliminary_anchor,
            preliminary_gate_tolerance=preliminary_gate_tolerance,
            preliminary_gate_rejection_count=preliminary_gate_rejections,
            board_end_side=board_end_side,
            angle_tolerance=angle_tolerance,
            image_width=image_width,
            image_height=image_height,
        )
        if fallback is not None:
            return fallback
        if preliminary_anchor is not None and preliminary_gate_rejections:
            return _calibration_guided_board_pose(
                coordinate,
                calibrations=calibrations,
                config=config,
                intrinsics=intrinsics,
                image_width=image_width,
                image_height=image_height,
                _use_preliminary_gate=False,
            )
        raise ValueError("calibration_guided_pose_not_found")

    hypotheses.sort(key=lambda item: float(item["score"]), reverse=True)
    pose_cluster_distance = max(
        float(config.get("calibration_pose_cluster_distance_px", 18.0)),
        1.0,
    )
    pose_cluster_angle = math.radians(
        max(
            float(config.get("calibration_pose_cluster_angle_degrees", 3.0)),
            0.5,
        )
    )
    distinct_hypotheses: list[dict[str, Any]] = []
    for hypothesis in hypotheses:
        if any(
            math.dist(
                hypothesis["transformed_center"],
                existing["transformed_center"],
            )
            <= pose_cluster_distance
            and abs(
                _wrapped_angle(hypothesis["rotation"] - existing["rotation"])
            )
            <= pose_cluster_angle
            for existing in distinct_hypotheses
        ):
            continue
        distinct_hypotheses.append(hypothesis)
    highest_score = float(distinct_hypotheses[0]["score"])
    leading_score_tolerance = max(
        float(config.get("calibration_leading_edge_score_tolerance", 1.25)),
        0.0,
    )
    leading_candidates = [
        hypothesis
        for hypothesis in distinct_hypotheses
        if float(hypothesis["score"]) >= highest_score - leading_score_tolerance
    ]
    best = (
        min(
            leading_candidates,
            key=lambda item: (
                (item["end_line"][0] + item["end_line"][2]) / 2.0,
                -float(item["score"]),
            ),
        )
        if board_end_side == "left"
        else max(
            leading_candidates,
            key=lambda item: (
                (item["end_line"][0] + item["end_line"][2]) / 2.0,
                float(item["score"]),
            ),
        )
    )
    ambiguity_margin = max(
        float(config.get("calibration_pose_ambiguity_margin", 0.6)),
        0.0,
    )
    selected_end_center_x = (
        best["end_line"][0] + best["end_line"][2]
    ) / 2.0
    nearby_competitors = [
        hypothesis
        for hypothesis in distinct_hypotheses
        if hypothesis is not best
        and abs(
            (hypothesis["end_line"][0] + hypothesis["end_line"][2]) / 2.0
            - selected_end_center_x
        )
        <= pose_cluster_distance * 1.5
    ]
    score_gap = (
        float(best["score"])
        - max(float(item["score"]) for item in nearby_competitors)
        if nearby_competitors
        else ambiguity_margin
    )
    if nearby_competitors and score_gap < ambiguity_margin:
        raise ValueError("calibration_guided_pose_ambiguous")

    match_ratio = float(best["match_count"]) / len(boundaries)
    angle_quality = max(
        1.0 - float(best["rotation_disagreement"]) / angle_tolerance,
        0.0,
    )
    gap_quality = (
        1.0
        if ambiguity_margin <= 0
        else min(max(score_gap / ambiguity_margin, 0.0), 1.0)
    )
    confidence = min(
        0.55 * match_ratio
        + 0.25 * float(best["match_quality"])
        + 0.15 * angle_quality
        + 0.05 * gap_quality,
        1.0,
    )
    origin_pixel = best["current_anchor"]
    longitudinal_pixel = _direction_aligned_with(
        best["longitudinal"],
        reference_longitudinal,
    )
    origin = (
        origin_pixel[0] / image_width,
        origin_pixel[1] / image_height,
    )
    axis_endpoint = (
        origin[0] + longitudinal_pixel[0] / image_width,
        origin[1] + longitudinal_pixel[1] / image_height,
    )
    end_line = best["end_line"]
    normalized_end_line = (
        (end_line[0] / image_width, end_line[1] / image_height),
        (end_line[2] / image_width, end_line[3] / image_height),
    )
    surface_polygons, surface_end_lines = _guided_surface_polygons(
        best,
        boundaries=boundaries,
        candidate_lines=candidate_lines,
        longitudinal_candidates=longitudinal_candidates,
        calibrations=calibrations,
        current_longitudinal=longitudinal_pixel,
        config=config,
        board_end_side=board_end_side,
        image_width=image_width,
        image_height=image_height,
    )
    reference_anchor_pixel = best["reference_anchor"]
    return _GuidedBoardPose(
        origin=origin,
        axis_endpoint=axis_endpoint,
        end_line=normalized_end_line,
        reference_anchor=(
            reference_anchor_pixel[0] / image_width,
            reference_anchor_pixel[1] / image_height,
        ),
        surface_polygons=surface_polygons,
        confidence=confidence,
        source={
            "method": "calibration_guided_hough_pose",
            "confidence": confidence,
            "matched_calibration_boundaries": int(best["match_count"]),
            "calibration_boundary_count": len(boundaries),
            "pose_score": float(best["score"]),
            "pose_score_gap": score_gap,
            "pose_hypothesis_count": len(distinct_hypotheses),
            "leading_edge_candidate_count": len(leading_candidates),
            "leading_edge_selection": (
                "leftmost_complete_calibration_match"
                if board_end_side == "left"
                else "rightmost_complete_calibration_match"
            ),
            "highest_pose_score": highest_score,
            "preliminary_gate_enabled": preliminary_anchor is not None,
            "preliminary_gate_tolerance_px": preliminary_gate_tolerance,
            "preliminary_gate_rejection_count": preliminary_gate_rejections,
            "preliminary_end_distance_px": float(
                best["preliminary_end_distance_px"]
            ),
            "preliminary_longitudinal_distance_px": float(
                best["preliminary_longitudinal_distance_px"]
            ),
            "board_end_line": list(end_line),
            "longitudinal_line": list(best["longitudinal_line"]),
            "reference_uses_point_4_to_1": True,
            "board_end_reference_surface": next(
                (
                    role
                    for role in ("top", "bottom")
                    if any(calibration.role == role for calibration in calibrations)
                ),
                calibrations[0].role,
            ),
            "board_end_reference_is_image_vertical": False,
            "reference_uses_point_1_to_2_and_4_to_3": True,
            "point_2_to_3_used_for_pose": False,
            "surface_projection": "detected_edges_to_image_boundary",
            "surface_end_lines": surface_end_lines,
        },
    )


def _preliminary_anchored_calibration_pose(
    coordinate: InspectionCoordinateFrame,
    *,
    calibrations: list[_SurfaceCalibration],
    boundaries: list[_CalibrationBoundary],
    config: dict[str, Any],
    intrinsics: object,
    reference_end_direction: tuple[float, float],
    preliminary_anchor: tuple[float, float] | None,
    preliminary_gate_tolerance: float,
    preliminary_gate_rejection_count: int,
    board_end_side: str,
    angle_tolerance: float,
    image_width: float,
    image_height: float,
) -> _GuidedBoardPose | None:
    """Transfer the commissioned surfaces from the trusted preliminary cross.

    The raw calibration Hough candidates are deliberately not accepted here.
    This keeps grain, knot and crack edges inside the board from moving the
    evaluated surfaces when the already selected board-end/longitudinal pair is
    geometrically plausible.
    """

    if preliminary_anchor is None:
        return None
    preliminary_end_lines = _calibration_candidate_lines(
        [coordinate.source.get("start_edge")],
        intrinsics=intrinsics,
        image_width=image_width,
        image_height=image_height,
    )
    preliminary_longitudinal_lines = _calibration_candidate_lines(
        [coordinate.source.get("side_edge")],
        intrinsics=intrinsics,
        image_width=image_width,
        image_height=image_height,
    )
    if not preliminary_end_lines or not preliminary_longitudinal_lines:
        return None
    preliminary_end_line = preliminary_end_lines[0]
    preliminary_longitudinal_line = preliminary_longitudinal_lines[0]
    if (
        _point_to_line_distance(preliminary_anchor, preliminary_end_line)
        > preliminary_gate_tolerance
        or _point_to_line_distance(
            preliminary_anchor,
            preliminary_longitudinal_line,
        )
        > preliminary_gate_tolerance
    ):
        return None

    current_anchor = _line_intersection(
        preliminary_end_line,
        preliminary_longitudinal_line,
    )
    if current_anchor is None:
        return None
    if math.dist(current_anchor, preliminary_anchor) > preliminary_gate_tolerance:
        return None

    reference_boundary, junction_fraction_error = _preliminary_reference_boundary(
        boundaries,
        current_anchor=current_anchor,
        current_end_line=preliminary_end_line,
    )
    junction_fraction_tolerance = min(
        max(
            float(
                config.get(
                    "calibration_preliminary_junction_fraction_tolerance",
                    0.22,
                )
            ),
            0.05,
        ),
        0.45,
    )
    if junction_fraction_error > junction_fraction_tolerance:
        return None
    current_end_direction = _direction_aligned_with(
        _oriented_line_direction(preliminary_end_line),
        reference_end_direction,
    )
    current_longitudinal = _direction_aligned_with(
        _oriented_line_direction(preliminary_longitudinal_line),
        reference_boundary.longitudinal,
    )
    reference_cross_angle = _signed_angle(
        reference_end_direction,
        reference_boundary.longitudinal,
    )
    current_cross_angle = _signed_angle(
        current_end_direction,
        current_longitudinal,
    )
    cross_angle_difference = abs(
        _wrapped_angle(current_cross_angle - reference_cross_angle)
    )
    if cross_angle_difference > angle_tolerance:
        return None

    affine_matrix = _basis_alignment_matrix(
        reference_end_direction,
        reference_boundary.longitudinal,
        current_end_direction,
        current_longitudinal,
    )
    end_rotation = _signed_angle(
        reference_end_direction,
        current_end_direction,
    )
    longitudinal_rotation = _signed_angle(
        reference_boundary.longitudinal,
        current_longitudinal,
    )
    hypothesis: dict[str, Any] = {
        "rotation": _mean_angle(end_rotation, longitudinal_rotation),
        "reference_anchor": reference_boundary.anchor,
        "current_anchor": current_anchor,
        "affine_matrix": affine_matrix,
        "matches": [],
    }

    synthetic_longitudinal_lines: list[LineSegment] = []
    line_extent = max(image_width, image_height) * 2.0
    for boundary_index, boundary in enumerate(boundaries):
        transformed_anchor = _transform_point_for_hypothesis(
            boundary.anchor,
            hypothesis=hypothesis,
        )
        transformed_direction_point = _transform_point_for_hypothesis(
            (
                boundary.anchor[0] + boundary.longitudinal[0],
                boundary.anchor[1] + boundary.longitudinal[1],
            ),
            hypothesis=hypothesis,
        )
        transformed_direction = _unit_vector(
            (
                transformed_direction_point[0] - transformed_anchor[0],
                transformed_direction_point[1] - transformed_anchor[1],
            )
        )
        synthetic_longitudinal_lines.append(
            (
                transformed_anchor[0] - transformed_direction[0] * line_extent,
                transformed_anchor[1] - transformed_direction[1] * line_extent,
                transformed_anchor[0] + transformed_direction[0] * line_extent,
                transformed_anchor[1] + transformed_direction[1] * line_extent,
            )
        )
        hypothesis["matches"].append(
            (boundary_index, boundary_index, 0.0)
        )

    surface_polygons, surface_end_lines = _guided_surface_polygons(
        hypothesis,
        boundaries=boundaries,
        candidate_lines=[],
        longitudinal_candidates=synthetic_longitudinal_lines,
        calibrations=calibrations,
        current_longitudinal=current_longitudinal,
        config=config,
        board_end_side=board_end_side,
        image_width=image_width,
        image_height=image_height,
    )
    angle_quality = max(
        1.0 - cross_angle_difference / max(angle_tolerance, 1e-9),
        0.0,
    )
    confidence = min(
        max(coordinate.confidence, 0.0)
        * (0.82 + 0.13 * angle_quality),
        0.95,
    )
    origin = (
        current_anchor[0] / image_width,
        current_anchor[1] / image_height,
    )
    axis_endpoint = (
        origin[0] + current_longitudinal[0] / image_width,
        origin[1] + current_longitudinal[1] / image_height,
    )
    primary_role = next(
        (
            role
            for role in ("top", "bottom")
            if any(calibration.role == role for calibration in calibrations)
        ),
        calibrations[0].role,
    )
    return _GuidedBoardPose(
        origin=origin,
        axis_endpoint=axis_endpoint,
        end_line=(
            (
                preliminary_end_line[0] / image_width,
                preliminary_end_line[1] / image_height,
            ),
            (
                preliminary_end_line[2] / image_width,
                preliminary_end_line[3] / image_height,
            ),
        ),
        reference_anchor=(
            reference_boundary.anchor[0] / image_width,
            reference_boundary.anchor[1] / image_height,
        ),
        surface_polygons=surface_polygons,
        confidence=confidence,
        source={
            "method": "preliminary_anchored_calibration_pose",
            "confidence": confidence,
            "matched_calibration_boundaries": 1,
            "inferred_calibration_boundaries": max(len(boundaries) - 1, 0),
            "calibration_boundary_count": len(boundaries),
            "preliminary_gate_enabled": True,
            "preliminary_gate_tolerance_px": preliminary_gate_tolerance,
            "preliminary_gate_rejection_count": (
                preliminary_gate_rejection_count
            ),
            "preliminary_cross_angle_difference_degrees": math.degrees(
                cross_angle_difference
            ),
            "preliminary_junction_fraction_error": junction_fraction_error,
            "preliminary_junction_fraction_tolerance": (
                junction_fraction_tolerance
            ),
            "raw_hough_candidates_used_for_surface": False,
            "ignored_raw_hough_candidate_count": len(
                coordinate.source.get("calibration_candidates") or []
            ),
            "board_end_line": list(preliminary_end_line),
            "longitudinal_line": list(preliminary_longitudinal_line),
            "reference_uses_point_4_to_1": True,
            "board_end_reference_surface": primary_role,
            "board_end_reference_is_image_vertical": False,
            "reference_uses_point_1_to_2_and_4_to_3": True,
            "point_2_to_3_used_for_pose": False,
            "surface_projection": (
                "preliminary_cross_with_calibrated_perspective"
            ),
            "surface_end_lines": surface_end_lines,
        },
    )


def _preliminary_reference_boundary(
    boundaries: list[_CalibrationBoundary],
    *,
    current_anchor: tuple[float, float],
    current_end_line: LineSegment,
) -> tuple[_CalibrationBoundary, float]:
    shared = [
        boundary
        for boundary in boundaries
        if len(boundary.bindings) > 1
    ]

    first, second = max(
        combinations([boundary.anchor for boundary in boundaries], 2),
        key=lambda pair: math.dist(pair[0], pair[1]),
    )
    reference_span: LineSegment = (
        first[0],
        first[1],
        second[0],
        second[1],
    )
    reference_direction = _oriented_line_direction(reference_span)
    current_direction = _oriented_line_direction(current_end_line)
    aligned_current_line = (
        current_end_line
        if (
            current_direction[0] * reference_direction[0]
            + current_direction[1] * reference_direction[1]
        )
        >= 0
        else (
            current_end_line[2],
            current_end_line[3],
            current_end_line[0],
            current_end_line[1],
        )
    )
    current_fraction = min(
        max(
            _line_projection_fraction(aligned_current_line, current_anchor),
            0.0,
        ),
        1.0,
    )
    candidates = shared or boundaries
    selected = min(
        candidates,
        key=lambda boundary: abs(
            min(
                max(
                    _line_projection_fraction(reference_span, boundary.anchor),
                    0.0,
                ),
                1.0,
            )
            - current_fraction
        ),
    )
    selected_fraction = min(
        max(
            _line_projection_fraction(reference_span, selected.anchor),
            0.0,
        ),
        1.0,
    )
    return selected, abs(selected_fraction - current_fraction)


def _basis_alignment_matrix(
    reference_end: tuple[float, float],
    reference_longitudinal: tuple[float, float],
    current_end: tuple[float, float],
    current_longitudinal: tuple[float, float],
) -> tuple[tuple[float, float], tuple[float, float]]:
    determinant = (
        reference_end[0] * reference_longitudinal[1]
        - reference_longitudinal[0] * reference_end[1]
    )
    if abs(determinant) <= 1e-6:
        raise ValueError("calibration_preliminary_pose_degenerate_reference")
    return (
        (
            (
                current_end[0] * reference_longitudinal[1]
                - current_longitudinal[0] * reference_end[1]
            )
            / determinant,
            (
                -current_end[0] * reference_longitudinal[0]
                + current_longitudinal[0] * reference_end[0]
            )
            / determinant,
        ),
        (
            (
                current_end[1] * reference_longitudinal[1]
                - current_longitudinal[1] * reference_end[1]
            )
            / determinant,
            (
                -current_end[1] * reference_longitudinal[0]
                + current_longitudinal[1] * reference_end[0]
            )
            / determinant,
        ),
    )


def _stabilize_realtime_guided_pose(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    pose: _GuidedBoardPose | None,
    *,
    error: str | None,
    calibrations: list[_SurfaceCalibration],
    config: dict[str, Any],
    image_width: float,
    image_height: float,
) -> _GuidedBoardPose | None:
    if (
        context.mode != RuntimeMode.REALTIME
        or not bool(config.get("realtime_pose_stabilization", True))
    ):
        return pose
    profiles = {
        "responsive": {
            "alpha": 0.60,
            "hold_frames": 1,
            "jump_tolerance_px": 28.0,
            "confirmation_frames": 2,
        },
        "balanced": {
            "alpha": 0.30,
            "hold_frames": 4,
            "jump_tolerance_px": 18.0,
            "confirmation_frames": 3,
        },
        "stable": {
            "alpha": 0.18,
            "hold_frames": 6,
            "jump_tolerance_px": 12.0,
            "confirmation_frames": 4,
        },
    }
    profile_name = str(config.get("realtime_stability_profile") or "balanced")
    profile = profiles.get(profile_name, profiles["balanced"])
    state = context.state.setdefault(node.node_key, {}).setdefault(
        "board_cut_pose_stabilization",
        {},
    )
    signature = (
        image_width,
        image_height,
        tuple(
            (
                calibration.role,
                tuple(calibration.image_polygon),
                tuple(calibration.world_polygon),
            )
            for calibration in calibrations
        ),
    )
    if state.get("calibration_signature") != signature:
        state.clear()
        state["calibration_signature"] = signature
    previous = state.get("pose")
    if not isinstance(previous, _GuidedBoardPose):
        previous = None

    if pose is None:
        missed_frames = int(state.get("missed_frames") or 0) + 1
        state["missed_frames"] = missed_frames
        if previous is not None and missed_frames <= int(profile["hold_frames"]):
            return _guided_pose_with_temporal_status(
                previous,
                profile=profile_name,
                status="held_after_missing_detection",
                missed_frames=missed_frames,
                detail=error,
            )
        state.pop("pose", None)
        state.pop("pending_pose", None)
        state.pop("pending_count", None)
        return None

    state["missed_frames"] = 0
    if previous is None:
        initialized = _guided_pose_with_temporal_status(
            pose,
            profile=profile_name,
            status="initialized",
        )
        state["pose"] = initialized
        return initialized

    jump_px = _guided_pose_distance_px(
        previous,
        pose,
        image_width=image_width,
        image_height=image_height,
    )
    jump_tolerance = float(profile["jump_tolerance_px"])
    if jump_px > jump_tolerance:
        pending = state.get("pending_pose")
        pending_count = int(state.get("pending_count") or 0)
        if (
            isinstance(pending, _GuidedBoardPose)
            and _guided_pose_distance_px(
                pending,
                pose,
                image_width=image_width,
                image_height=image_height,
            )
            <= jump_tolerance
        ):
            pending = _blend_guided_poses(
                pending,
                pose,
                alpha=0.5,
                image_width=image_width,
                image_height=image_height,
                temporal_status={
                    "enabled": True,
                    "profile": profile_name,
                    "status": "pending_jump",
                },
            )
            pending_count += 1
        else:
            pending = pose
            pending_count = 1
        state["pending_pose"] = pending
        state["pending_count"] = pending_count
        if pending_count < int(profile["confirmation_frames"]):
            return _guided_pose_with_temporal_status(
                previous,
                profile=profile_name,
                status="held_during_unconfirmed_jump",
                jump_px=jump_px,
                confirmation_count=pending_count,
            )
        accepted = _guided_pose_with_temporal_status(
            pending,
            profile=profile_name,
            status="confirmed_new_position",
            jump_px=jump_px,
            confirmation_count=pending_count,
        )
        state["pose"] = accepted
        state.pop("pending_pose", None)
        state.pop("pending_count", None)
        return accepted

    state.pop("pending_pose", None)
    state.pop("pending_count", None)
    smoothed = _blend_guided_poses(
        previous,
        pose,
        alpha=float(profile["alpha"]),
        image_width=image_width,
        image_height=image_height,
        temporal_status={
            "enabled": True,
            "profile": profile_name,
            "status": "smoothed",
            "raw_jump_px": jump_px,
        },
    )
    state["pose"] = smoothed
    return smoothed


def _guided_pose_distance_px(
    first: _GuidedBoardPose,
    second: _GuidedBoardPose,
    *,
    image_width: float,
    image_height: float,
) -> float:
    distances: list[float] = [
        math.hypot(
            (first.origin[0] - second.origin[0]) * image_width,
            (first.origin[1] - second.origin[1]) * image_height,
        )
    ]
    for role, first_polygon in first.surface_polygons.items():
        second_polygon = second.surface_polygons.get(role)
        if second_polygon is None or len(first_polygon) != len(second_polygon):
            return math.inf
        distances.extend(
            math.hypot(
                (first_point[0] - second_point[0]) * image_width,
                (first_point[1] - second_point[1]) * image_height,
            )
            for first_point, second_point in zip(
                first_polygon,
                second_polygon,
                strict=True,
            )
        )
    return max(distances, default=math.inf)


def _blend_guided_poses(
    previous: _GuidedBoardPose,
    current: _GuidedBoardPose,
    *,
    alpha: float,
    image_width: float,
    image_height: float,
    temporal_status: dict[str, Any],
) -> _GuidedBoardPose:
    alpha = min(max(alpha, 0.0), 1.0)

    def blend_point(
        first: tuple[float, float],
        second: tuple[float, float],
    ) -> tuple[float, float]:
        return (
            first[0] * (1.0 - alpha) + second[0] * alpha,
            first[1] * (1.0 - alpha) + second[1] * alpha,
        )

    surface_polygons = {
        role: [
            blend_point(first, second)
            for first, second in zip(
                previous.surface_polygons[role],
                current.surface_polygons[role],
                strict=True,
            )
        ]
        for role in current.surface_polygons
        if role in previous.surface_polygons
        and len(previous.surface_polygons[role])
        == len(current.surface_polygons[role])
    }
    source = _guided_pose_source_for_geometry(
        current.source,
        surface_polygons,
        image_width=image_width,
        image_height=image_height,
        temporal_status=temporal_status,
    )
    primary_role = next(
        (
            role
            for role in ("top", "bottom", "side_a", "side_b")
            if role in surface_polygons
        ),
        None,
    )
    if primary_role is not None:
        primary = surface_polygons[primary_role]
        end_line = (primary[0], primary[3])
    else:
        end_line = (
            blend_point(previous.end_line[0], current.end_line[0]),
            blend_point(previous.end_line[1], current.end_line[1]),
        )
    return _GuidedBoardPose(
        origin=blend_point(previous.origin, current.origin),
        axis_endpoint=blend_point(previous.axis_endpoint, current.axis_endpoint),
        end_line=end_line,
        reference_anchor=current.reference_anchor,
        surface_polygons=surface_polygons,
        confidence=(
            previous.confidence * (1.0 - alpha)
            + current.confidence * alpha
        ),
        source=source,
    )


def _guided_pose_source_for_geometry(
    source: dict[str, Any],
    surface_polygons: dict[str, list[tuple[float, float]]],
    *,
    image_width: float,
    image_height: float,
    temporal_status: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(source)
    existing_surface_ends = source.get("surface_end_lines")
    surface_end_lines: dict[str, dict[str, Any]] = {}
    for role, polygon in surface_polygons.items():
        if len(polygon) != 4:
            continue
        existing = (
            existing_surface_ends.get(role)
            if isinstance(existing_surface_ends, dict)
            and isinstance(existing_surface_ends.get(role), dict)
            else {}
        )
        surface_end_lines[role] = {
            **existing,
            "line": [
                polygon[0][0] * image_width,
                polygon[0][1] * image_height,
                polygon[3][0] * image_width,
                polygon[3][1] * image_height,
            ],
        }
    updated["surface_end_lines"] = surface_end_lines
    primary_role = next(
        (
            role
            for role in ("top", "bottom", "side_a", "side_b")
            if role in surface_polygons
        ),
        None,
    )
    if primary_role is not None:
        polygon = surface_polygons[primary_role]
        updated["board_end_line"] = surface_end_lines[primary_role]["line"]
        updated["longitudinal_line"] = [
            polygon[0][0] * image_width,
            polygon[0][1] * image_height,
            polygon[1][0] * image_width,
            polygon[1][1] * image_height,
        ]
    updated["temporal_stabilization"] = temporal_status
    return updated


def _guided_pose_with_temporal_status(
    pose: _GuidedBoardPose,
    *,
    profile: str,
    status: str,
    **details: object,
) -> _GuidedBoardPose:
    source = dict(pose.source)
    source["temporal_stabilization"] = {
        "enabled": True,
        "profile": profile,
        "status": status,
        **details,
    }
    return _GuidedBoardPose(
        origin=pose.origin,
        axis_endpoint=pose.axis_endpoint,
        end_line=pose.end_line,
        reference_anchor=pose.reference_anchor,
        surface_polygons=pose.surface_polygons,
        confidence=pose.confidence,
        source=source,
    )


def _calibration_candidate_lines(
    value: list[object],
    *,
    intrinsics: object,
    image_width: float,
    image_height: float,
) -> list[LineSegment]:
    lines: list[LineSegment] = []
    for item in value:
        if not isinstance(item, list) or len(item) < 4:
            continue
        try:
            normalized = [
                (float(item[0]) / image_width, float(item[1]) / image_height),
                (float(item[2]) / image_width, float(item[3]) / image_height),
            ]
        except (TypeError, ValueError):
            continue
        first, second = _undistort_normalized_points(
            normalized,
            intrinsics,
            image_width=image_width,
            image_height=image_height,
        )
        line = (
            first[0] * image_width,
            first[1] * image_height,
            second[0] * image_width,
            second[1] * image_height,
        )
        if _line_length(line) > 1.0:
            lines.append(line)
    return lines


def _calibration_longitudinal_boundaries(
    calibrations: list[_SurfaceCalibration],
    *,
    image_width: float,
    image_height: float,
    merge_tolerance_px: float,
) -> list[_CalibrationBoundary]:
    boundaries: list[_CalibrationBoundary] = []
    for calibration in calibrations:
        world_y_values = [point[1] for point in calibration.world_polygon]
        for boundary_name, boundary_y in (
            ("point_1_to_2", min(world_y_values)),
            ("point_4_to_3", max(world_y_values)),
        ):
            indices = [
                index
                for index, point in enumerate(calibration.world_polygon)
                if abs(point[1] - boundary_y) <= 1e-6
            ]
            if len(indices) < 2:
                continue
            start_index = min(
                indices,
                key=lambda index: calibration.world_polygon[index][0],
            )
            end_index = max(
                indices,
                key=lambda index: calibration.world_polygon[index][0],
            )
            anchor = _normalized_to_pixel(
                calibration.image_polygon[start_index],
                image_width,
                image_height,
            )
            end = _normalized_to_pixel(
                calibration.image_polygon[end_index],
                image_width,
                image_height,
            )
            direction = _unit_vector((end[0] - anchor[0], end[1] - anchor[1]))
            existing_index = next(
                (
                    index
                    for index, existing in enumerate(boundaries)
                    if math.dist(existing.anchor, anchor) <= merge_tolerance_px
                ),
                None,
            )
            if existing_index is None:
                boundaries.append(
                    _CalibrationBoundary(
                        anchor=anchor,
                        longitudinal=direction,
                        bindings=((calibration.role, boundary_name),),
                    )
                )
                continue
            existing = boundaries[existing_index]
            aligned_direction = _direction_aligned_with(
                direction,
                existing.longitudinal,
            )
            boundaries[existing_index] = _CalibrationBoundary(
                anchor=(
                    (existing.anchor[0] + anchor[0]) / 2.0,
                    (existing.anchor[1] + anchor[1]) / 2.0,
                ),
                longitudinal=_unit_vector(
                    (
                        existing.longitudinal[0] + aligned_direction[0],
                        existing.longitudinal[1] + aligned_direction[1],
                    )
                ),
                bindings=(
                    *existing.bindings,
                    (calibration.role, boundary_name),
                ),
            )
    return boundaries


def _normalized_to_pixel(
    point: tuple[float, float],
    image_width: float,
    image_height: float,
) -> tuple[float, float]:
    return point[0] * image_width, point[1] * image_height


def _unit_vector(vector: tuple[float, float]) -> tuple[float, float]:
    length = math.hypot(*vector)
    if length <= 1e-12:
        raise ValueError("calibration_guided_pose_degenerate_reference")
    return vector[0] / length, vector[1] / length


def _oriented_line_direction(line: LineSegment) -> tuple[float, float]:
    return _unit_vector((line[2] - line[0], line[3] - line[1]))


def _line_angle_from_horizontal(line: LineSegment) -> float:
    direction = _oriented_line_direction(line)
    return math.atan2(abs(direction[1]), abs(direction[0]))


def _direction_aligned_with(
    direction: tuple[float, float],
    reference: tuple[float, float],
) -> tuple[float, float]:
    if direction[0] * reference[0] + direction[1] * reference[1] < 0:
        return -direction[0], -direction[1]
    return direction


def _average_directions(
    directions: list[tuple[float, float]],
) -> tuple[float, float]:
    if not directions:
        raise ValueError("calibration_guided_pose_reference_incomplete")
    reference = directions[0]
    aligned = [_direction_aligned_with(direction, reference) for direction in directions]
    return _unit_vector(
        (
            sum(direction[0] for direction in aligned),
            sum(direction[1] for direction in aligned),
        )
    )


def _undirected_angle_difference(
    first: tuple[float, float],
    second: tuple[float, float],
) -> float:
    dot = min(max(abs(first[0] * second[0] + first[1] * second[1]), 0.0), 1.0)
    return math.acos(dot)


def _signed_angle(
    first: tuple[float, float],
    second: tuple[float, float],
) -> float:
    return math.atan2(
        first[0] * second[1] - first[1] * second[0],
        first[0] * second[0] + first[1] * second[1],
    )


def _wrapped_angle(value: float) -> float:
    return (value + math.pi) % (2.0 * math.pi) - math.pi


def _mean_angle(first: float, second: float) -> float:
    return math.atan2(
        math.sin(first) + math.sin(second),
        math.cos(first) + math.cos(second),
    )


def _rotate_vector(
    vector: tuple[float, float],
    rotation: float,
) -> tuple[float, float]:
    cosine = math.cos(rotation)
    sine = math.sin(rotation)
    return (
        cosine * vector[0] - sine * vector[1],
        sine * vector[0] + cosine * vector[1],
    )


def _transform_point_rigid(
    point: tuple[float, float],
    *,
    reference_anchor: tuple[float, float],
    current_anchor: tuple[float, float],
    rotation: float,
) -> tuple[float, float]:
    rotated = _rotate_vector(
        (
            point[0] - reference_anchor[0],
            point[1] - reference_anchor[1],
        ),
        rotation,
    )
    return current_anchor[0] + rotated[0], current_anchor[1] + rotated[1]


def _transform_point_for_hypothesis(
    point: tuple[float, float],
    *,
    hypothesis: dict[str, Any],
) -> tuple[float, float]:
    affine_matrix = hypothesis.get("affine_matrix")
    reference_anchor = hypothesis["reference_anchor"]
    current_anchor = hypothesis["current_anchor"]
    if (
        isinstance(affine_matrix, tuple)
        and len(affine_matrix) == 2
        and all(
            isinstance(row, tuple) and len(row) == 2
            for row in affine_matrix
        )
    ):
        offset_x = point[0] - reference_anchor[0]
        offset_y = point[1] - reference_anchor[1]
        return (
            current_anchor[0]
            + affine_matrix[0][0] * offset_x
            + affine_matrix[0][1] * offset_y,
            current_anchor[1]
            + affine_matrix[1][0] * offset_x
            + affine_matrix[1][1] * offset_y,
        )
    return _transform_point_rigid(
        point,
        reference_anchor=reference_anchor,
        current_anchor=current_anchor,
        rotation=float(hypothesis["rotation"]),
    )


def _merge_oriented_line_segments(
    lines: list[LineSegment],
    *,
    reference_direction: tuple[float, float],
    position_tolerance: float,
    gap_tolerance: float,
) -> list[LineSegment]:
    if not lines:
        return []
    direction = _unit_vector(reference_direction)
    normal = (-direction[1], direction[0])

    def position(line: LineSegment) -> float:
        center = ((line[0] + line[2]) / 2.0, (line[1] + line[3]) / 2.0)
        return center[0] * normal[0] + center[1] * normal[1]

    def interval(line: LineSegment) -> tuple[float, float]:
        values = (
            line[0] * direction[0] + line[1] * direction[1],
            line[2] * direction[0] + line[3] * direction[1],
        )
        return min(values), max(values)

    parents = list(range(len(lines)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parents[second_root] = first_root

    ordered = sorted(range(len(lines)), key=lambda index: position(lines[index]))
    for offset, first_index in enumerate(ordered):
        first_position = position(lines[first_index])
        first_interval = interval(lines[first_index])
        for second_index in ordered[offset + 1 :]:
            if position(lines[second_index]) - first_position > position_tolerance:
                break
            second_interval = interval(lines[second_index])
            interval_gap = max(
                first_interval[0] - second_interval[1],
                second_interval[0] - first_interval[1],
                0.0,
            )
            if interval_gap <= gap_tolerance:
                union(first_index, second_index)

    clusters: dict[int, list[LineSegment]] = {}
    for index, line in enumerate(lines):
        clusters.setdefault(find(index), []).append(line)
    return [
        _fit_oriented_line(cluster, reference_direction=direction)
        for cluster in clusters.values()
    ]


def _fit_oriented_line(
    lines: list[LineSegment],
    *,
    reference_direction: tuple[float, float],
) -> LineSegment:
    points = np.asarray(
        [(line[0], line[1]) for line in lines]
        + [(line[2], line[3]) for line in lines],
        dtype=np.float64,
    )
    center = np.mean(points, axis=0)
    centered = points - center
    _, _, vectors = np.linalg.svd(centered, full_matrices=False)
    direction = _direction_aligned_with(
        (float(vectors[0, 0]), float(vectors[0, 1])),
        reference_direction,
    )
    projections = centered @ np.asarray(direction, dtype=np.float64)
    first = center + np.asarray(direction) * float(np.min(projections))
    second = center + np.asarray(direction) * float(np.max(projections))
    return float(first[0]), float(first[1]), float(second[0]), float(second[1])


def _match_calibration_boundaries(
    predicted_anchors: list[tuple[float, float]],
    candidates: list[LineSegment],
    *,
    boundary_tolerance: float,
) -> list[tuple[int, int, float]]:
    possible = sorted(
        (
            (_point_to_segment_distance(anchor, candidate), anchor_index, candidate_index)
            for anchor_index, anchor in enumerate(predicted_anchors)
            for candidate_index, candidate in enumerate(candidates)
        ),
        key=lambda item: item[0],
    )
    matched_anchors: set[int] = set()
    matched_candidates: set[int] = set()
    matches: list[tuple[int, int, float]] = []
    for distance, anchor_index, candidate_index in possible:
        if distance > boundary_tolerance:
            break
        if anchor_index in matched_anchors or candidate_index in matched_candidates:
            continue
        matched_anchors.add(anchor_index)
        matched_candidates.add(candidate_index)
        matches.append((anchor_index, candidate_index, distance))
    return matches


def _guided_surface_polygons(
    hypothesis: dict[str, Any],
    *,
    boundaries: list[_CalibrationBoundary],
    candidate_lines: list[LineSegment],
    longitudinal_candidates: list[LineSegment],
    calibrations: list[_SurfaceCalibration],
    current_longitudinal: tuple[float, float],
    config: dict[str, Any],
    board_end_side: str,
    image_width: float,
    image_height: float,
) -> tuple[
    dict[str, list[tuple[float, float]]],
    dict[str, dict[str, Any]],
]:
    matches = hypothesis.get("matches")
    if not isinstance(matches, list):
        raise ValueError("calibration_guided_surface_matches_missing")
    lines_by_binding: dict[tuple[SurfaceRole, str], LineSegment] = {}
    for boundary_index, candidate_index, _ in matches:
        boundary = boundaries[int(boundary_index)]
        line = longitudinal_candidates[int(candidate_index)]
        for binding in boundary.bindings:
            lines_by_binding[binding] = line

    polygons: dict[str, list[tuple[float, float]]] = {}
    surface_end_lines: dict[str, dict[str, Any]] = {}
    end_points_by_boundary: dict[LineSegment, tuple[float, float]] = {}
    ordered_calibrations = sorted(
        calibrations,
        key=lambda item: {
            "top": 0,
            "bottom": 0,
            "side_a": 1,
            "side_b": 1,
        }.get(item.role, 2),
    )
    for calibration in ordered_calibrations:
        upper_line = lines_by_binding.get(
            (calibration.role, "point_1_to_2")
        )
        lower_line = lines_by_binding.get(
            (calibration.role, "point_4_to_3")
        )
        if upper_line is None or lower_line is None:
            raise ValueError(
                f"calibration_guided_surface_boundary_missing:{calibration.role}"
            )
        selected_end_line, end_source, end_offset = _guided_surface_end_line(
            calibration,
            hypothesis=hypothesis,
            candidate_lines=candidate_lines,
            upper_line=upper_line,
            lower_line=lower_line,
            allow_detected_end=calibration.role in {"top", "bottom"},
            config=config,
            board_end_side=board_end_side,
            image_width=image_width,
            image_height=image_height,
        )
        selected_direction = _direction_aligned_with(
            _oriented_line_direction(selected_end_line),
            _oriented_line_direction(
                _surface_reference_end_line(
                    calibration,
                    image_width=image_width,
                    image_height=image_height,
                )
            ),
        )
        point_1 = end_points_by_boundary.get(upper_line)
        point_4 = end_points_by_boundary.get(lower_line)
        if point_1 is not None:
            selected_end_line = (
                point_1[0],
                point_1[1],
                point_1[0] + selected_direction[0],
                point_1[1] + selected_direction[1],
            )
        elif point_4 is not None:
            selected_end_line = (
                point_4[0] - selected_direction[0],
                point_4[1] - selected_direction[1],
                point_4[0],
                point_4[1],
            )
        if point_1 is None:
            point_1 = _line_intersection(selected_end_line, upper_line)
        if point_4 is None:
            point_4 = _line_intersection(selected_end_line, lower_line)
        if point_1 is None or point_4 is None:
            raise ValueError(
                f"calibration_guided_surface_intersection_missing:{calibration.role}"
            )
        end_points_by_boundary[upper_line] = point_1
        end_points_by_boundary[lower_line] = point_4
        upper_direction = _direction_aligned_with(
            _oriented_line_direction(upper_line),
            current_longitudinal,
        )
        lower_direction = _direction_aligned_with(
            _oriented_line_direction(lower_line),
            current_longitudinal,
        )
        point_2 = _forward_image_boundary_intersection(
            point_1,
            upper_direction,
            image_width=image_width,
            image_height=image_height,
        )
        point_3 = _forward_image_boundary_intersection(
            point_4,
            lower_direction,
            image_width=image_width,
            image_height=image_height,
        )
        polygon = [
            (point_1[0] / image_width, point_1[1] / image_height),
            (point_2[0] / image_width, point_2[1] / image_height),
            (point_3[0] / image_width, point_3[1] / image_height),
            (point_4[0] / image_width, point_4[1] / image_height),
        ]
        if abs(_polygon_twice_area(polygon)) <= 1e-9:
            raise ValueError(
                f"calibration_guided_surface_degenerate:{calibration.role}"
            )
        polygons[calibration.role] = polygon
        surface_end_lines[calibration.role] = {
            "line": [point_1[0], point_1[1], point_4[0], point_4[1]],
            "source": end_source,
            "calibration_offset_px": end_offset,
        }
    return polygons, surface_end_lines


def _guided_surface_end_line(
    calibration: _SurfaceCalibration,
    *,
    hypothesis: dict[str, Any],
    candidate_lines: list[LineSegment],
    upper_line: LineSegment,
    lower_line: LineSegment,
    allow_detected_end: bool,
    config: dict[str, Any],
    board_end_side: str,
    image_width: float,
    image_height: float,
) -> tuple[LineSegment, str, float]:
    reference_line = _surface_reference_end_line(
        calibration,
        image_width=image_width,
        image_height=image_height,
    )
    predicted_first = _transform_point_for_hypothesis(
        (reference_line[0], reference_line[1]),
        hypothesis=hypothesis,
    )
    predicted_second = _transform_point_for_hypothesis(
        (reference_line[2], reference_line[3]),
        hypothesis=hypothesis,
    )
    predicted_line: LineSegment = (
        predicted_first[0],
        predicted_first[1],
        predicted_second[0],
        predicted_second[1],
    )
    expected_direction = _oriented_line_direction(predicted_line)
    angle_tolerance = math.radians(
        max(
            min(
                float(config.get("calibration_surface_end_angle_tolerance_degrees", 18.0)),
                45.0,
            ),
            1.0,
        )
    )
    position_tolerance = max(
        float(config.get("calibration_surface_end_tolerance_px", 24.0)),
        min(image_width, image_height) * 0.025,
    )
    possible: list[tuple[float, float, LineSegment]] = []
    for line in candidate_lines if allow_detected_end else []:
        if (
            _undirected_angle_difference(
                _oriented_line_direction(line),
                expected_direction,
            )
            > angle_tolerance
        ):
            continue
        first = _line_intersection(line, upper_line)
        second = _line_intersection(line, lower_line)
        if first is None or second is None:
            continue
        maximum_offset = max(
            math.dist(first, predicted_first),
            math.dist(second, predicted_second),
        )
        angle_offset = _undirected_angle_difference(
            _oriented_line_direction(line),
            expected_direction,
        )
        score = (
            maximum_offset
            + position_tolerance * angle_offset / max(angle_tolerance, 1e-9)
            - min(_line_length(line), math.dist(predicted_first, predicted_second))
            * 0.05
        )
        possible.append((score, maximum_offset, line))
    if possible:
        _, maximum_offset, selected = min(possible, key=lambda item: item[0])
        if maximum_offset <= position_tolerance:
            return selected, "detected_surface_board_end", maximum_offset
    return predicted_line, "calibrated_surface_direction", 0.0


def _surface_reference_end_line(
    calibration: _SurfaceCalibration,
    *,
    image_width: float,
    image_height: float,
) -> LineSegment:
    minimum_x = min(point[0] for point in calibration.world_polygon)
    points = [
        image_point
        for image_point, world_point in zip(
            calibration.image_polygon,
            calibration.world_polygon,
            strict=True,
        )
        if abs(world_point[0] - minimum_x) <= 1e-6
    ]
    if len(points) != 2:
        raise ValueError(
            f"surface_board_end_reference_invalid:{calibration.role}"
        )
    first, second = points
    return (
        first[0] * image_width,
        first[1] * image_height,
        second[0] * image_width,
        second[1] * image_height,
    )


def _forward_image_boundary_intersection(
    start: tuple[float, float],
    direction: tuple[float, float],
    *,
    image_width: float,
    image_height: float,
) -> tuple[float, float]:
    candidates: list[tuple[float, tuple[float, float]]] = []
    extended_candidates: list[tuple[float, tuple[float, float]]] = []
    if abs(direction[0]) > 1e-12:
        for boundary_x in (0.0, image_width):
            distance = (boundary_x - start[0]) / direction[0]
            y = start[1] + distance * direction[1]
            if distance > 1e-6:
                extended_candidates.append((distance, (boundary_x, y)))
            if distance > 1e-6 and -1e-6 <= y <= image_height + 1e-6:
                candidates.append((distance, (boundary_x, min(max(y, 0.0), image_height))))
    if abs(direction[1]) > 1e-12:
        for boundary_y in (0.0, image_height):
            distance = (boundary_y - start[1]) / direction[1]
            x = start[0] + distance * direction[0]
            if distance > 1e-6:
                extended_candidates.append((distance, (x, boundary_y)))
            if distance > 1e-6 and -1e-6 <= x <= image_width + 1e-6:
                candidates.append((distance, (min(max(x, 0.0), image_width), boundary_y)))
    if candidates:
        return min(candidates, key=lambda item: item[0])[1]
    if extended_candidates:
        # The transferred calibrated boundary may start a few pixels outside
        # the current image. Keep the projected endpoint outside as well; the
        # subsequent visible-polygon clipping then retains the surface portion
        # that is actually inside the frame.
        return min(extended_candidates, key=lambda item: item[0])[1]
    raise ValueError("calibration_guided_surface_image_boundary_missing")


def _polygon_twice_area(polygon: list[tuple[float, float]]) -> float:
    return sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(polygon, polygon[1:] + polygon[:1], strict=False)
    )


def _track_board_surfaces(
    calibrations: list[_SurfaceCalibration],
    *,
    origin: tuple[float, float],
    axis_endpoint: tuple[float, float],
    current_end_line: tuple[tuple[float, float], tuple[float, float]] | None,
    reference_anchor: tuple[float, float] | None,
    current_surface_polygons: dict[str, list[tuple[float, float]]] | None,
    follow_board_position: bool,
) -> list[_TrackedSurface]:
    if not calibrations:
        raise ValueError("surface_calibration_missing")
    transform = (
        _board_pose_transform(
            calibrations,
            origin=origin,
            axis_endpoint=axis_endpoint,
            current_end_line=current_end_line,
            reference_anchor=reference_anchor,
        )
        if follow_board_position
        else lambda point: point
    )
    tracked: list[_TrackedSurface] = []
    for calibration in calibrations:
        guided_polygon = (
            current_surface_polygons.get(calibration.role)
            if follow_board_position and current_surface_polygons is not None
            else None
        )
        image_polygon = (
            list(guided_polygon)
            if guided_polygon is not None
            else [transform(point) for point in calibration.image_polygon]
        )
        visible_image_polygon = _clip_polygon_to_unit_square(image_polygon)
        if len(visible_image_polygon) < 3:
            raise ValueError(f"tracked_surface_not_visible:{calibration.role}")
        try:
            current_homography = _homography(
                image_polygon,
                calibration.world_polygon,
            )
        except ValueError as exc:
            raise ValueError(
                f"tracked_surface_homography_invalid:{calibration.role}"
            ) from exc
        minimum_x = min(point[0] for point in calibration.world_polygon)
        maximum_x = max(point[0] for point in calibration.world_polygon)
        start_world_points = [
            point
            for point in calibration.world_polygon
            if abs(point[0] - minimum_x) <= 1e-6
        ]
        end_world_points = [
            point
            for point in calibration.world_polygon
            if abs(point[0] - maximum_x) <= 1e-6
        ]
        if not start_world_points or not end_world_points:
            raise ValueError(
                f"tracked_surface_longitudinal_direction_invalid:{calibration.role}"
            )
        origin_world = (
            sum(point[0] for point in start_world_points) / len(start_world_points),
            sum(point[1] for point in start_world_points) / len(start_world_points),
        )
        axis_world = (
            sum(point[0] for point in end_world_points) / len(end_world_points),
            sum(point[1] for point in end_world_points) / len(end_world_points),
        )
        axis_length = math.dist(origin_world, axis_world)
        if axis_length <= 1e-12:
            raise ValueError(f"tracked_surface_longitudinal_direction_invalid:{calibration.role}")
        longitudinal_axis_world = (
            (axis_world[0] - origin_world[0]) / axis_length,
            (axis_world[1] - origin_world[1]) / axis_length,
        )
        visible_world_polygon = _project_points(
            current_homography,
            visible_image_polygon,
        )
        calibrated_limit = max(
            (
                _longitudinal_position_mm(
                    point,
                    origin=origin_world,
                    axis=longitudinal_axis_world,
                )
                for point in visible_world_polygon
            ),
            default=0.0,
        )
        if calibrated_limit <= 0:
            raise ValueError(f"tracked_surface_longitudinal_direction_invalid:{calibration.role}")
        tracked.append(
            _TrackedSurface(
                calibration=calibration,
                homography=current_homography,
                image_polygon=image_polygon,
                visible_image_polygon=visible_image_polygon,
                visible_world_polygon=visible_world_polygon,
                origin_world=origin_world,
                longitudinal_axis_world=longitudinal_axis_world,
                calibrated_limit_mm=calibrated_limit,
            )
        )
    return tracked


def _board_pose_transform(
    calibrations: list[_SurfaceCalibration],
    *,
    origin: tuple[float, float],
    axis_endpoint: tuple[float, float],
    current_end_line: tuple[tuple[float, float], tuple[float, float]] | None,
    reference_anchor: tuple[float, float] | None,
) -> Callable[[tuple[float, float]], tuple[float, float]]:
    reference_start, reference_end = _reference_board_end_line(calibrations)
    reference_axis = _reference_longitudinal_axis(calibrations)
    current_axis = (
        axis_endpoint[0] - origin[0],
        axis_endpoint[1] - origin[1],
    )
    if math.hypot(*current_axis) <= 1e-12:
        raise ValueError("board_longitudinal_axis_invalid")
    reference_angle = math.atan2(reference_axis[1], reference_axis[0])
    current_angle = math.atan2(current_axis[1], current_axis[0])
    rotation = current_angle - reference_angle
    cosine = math.cos(rotation)
    sine = math.sin(rotation)

    reference_line_vector = (
        reference_end[0] - reference_start[0],
        reference_end[1] - reference_start[1],
    )
    rotated_reference_vector = (
        cosine * reference_line_vector[0] - sine * reference_line_vector[1],
        sine * reference_line_vector[0] + cosine * reference_line_vector[1],
    )
    if reference_anchor is None:
        if current_end_line is not None:
            current_start, current_end = current_end_line
            current_vector = (
                current_end[0] - current_start[0],
                current_end[1] - current_start[1],
            )
            if (
                rotated_reference_vector[0] * current_vector[0]
                + rotated_reference_vector[1] * current_vector[1]
                < 0
            ):
                current_start, current_end = current_end, current_start
            reference_fraction = max(
                min(
                    _line_projection_fraction(
                        (
                            current_start[0],
                            current_start[1],
                            current_end[0],
                            current_end[1],
                        ),
                        origin,
                    ),
                    1.0,
                ),
                0.0,
            )
        else:
            reference_fraction = max(
                min(
                    _line_projection_fraction(
                        (
                            reference_start[0],
                            reference_start[1],
                            reference_end[0],
                            reference_end[1],
                        ),
                        origin,
                    ),
                    1.0,
                ),
                0.0,
            )
        reference_anchor = (
            reference_start[0]
            + (reference_end[0] - reference_start[0]) * reference_fraction,
            reference_start[1]
            + (reference_end[1] - reference_start[1]) * reference_fraction,
        )

    def transform(point: tuple[float, float]) -> tuple[float, float]:
        offset_x = point[0] - reference_anchor[0]
        offset_y = point[1] - reference_anchor[1]
        return (
            origin[0] + cosine * offset_x - sine * offset_y,
            origin[1] + sine * offset_x + cosine * offset_y,
        )

    return transform


def _reference_board_end_line(
    calibrations: list[_SurfaceCalibration],
) -> tuple[tuple[float, float], tuple[float, float]]:
    primary = next(
        (
            calibration
            for role in ("top", "bottom")
            for calibration in calibrations
            if calibration.role == role
        ),
        calibrations[0] if calibrations else None,
    )
    if primary is None:
        raise ValueError("surface_board_end_reference_missing")
    minimum_x = min(point[0] for point in primary.world_polygon)
    start_points = [
        image_point
        for image_point, world_point in zip(
            primary.image_polygon,
            primary.world_polygon,
            strict=True,
        )
        if abs(world_point[0] - minimum_x) <= 1e-6
    ]
    if len(start_points) < 2:
        raise ValueError("surface_board_end_reference_missing")
    return max(
        combinations(start_points, 2),
        key=lambda pair: math.dist(pair[0], pair[1]),
    )


def _reference_longitudinal_axis(
    calibrations: list[_SurfaceCalibration],
) -> tuple[float, float]:
    vectors: list[tuple[float, float]] = []
    for calibration in calibrations:
        minimum_x = min(point[0] for point in calibration.world_polygon)
        maximum_x = max(point[0] for point in calibration.world_polygon)
        start_points = [
            image_point
            for image_point, world_point in zip(
                calibration.image_polygon,
                calibration.world_polygon,
                strict=True,
            )
            if abs(world_point[0] - minimum_x) <= 1e-6
        ]
        end_points = [
            image_point
            for image_point, world_point in zip(
                calibration.image_polygon,
                calibration.world_polygon,
                strict=True,
            )
            if abs(world_point[0] - maximum_x) <= 1e-6
        ]
        if not start_points or not end_points:
            continue
        start_center = (
            sum(point[0] for point in start_points) / len(start_points),
            sum(point[1] for point in start_points) / len(start_points),
        )
        end_center = (
            sum(point[0] for point in end_points) / len(end_points),
            sum(point[1] for point in end_points) / len(end_points),
        )
        length = math.dist(start_center, end_center)
        if length > 1e-12:
            vectors.append(
                (
                    (end_center[0] - start_center[0]) / length,
                    (end_center[1] - start_center[1]) / length,
                )
            )
    if not vectors:
        raise ValueError("surface_longitudinal_reference_missing")
    axis = (
        sum(vector[0] for vector in vectors),
        sum(vector[1] for vector in vectors),
    )
    length = math.hypot(*axis)
    if length <= 1e-12:
        raise ValueError("surface_longitudinal_reference_inconsistent")
    return axis[0] / length, axis[1] / length


def _undistorted_coordinate_start_line(
    coordinate: InspectionCoordinateFrame,
    *,
    intrinsics: object,
    image_width: float,
    image_height: float,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    line = _normalized_source_line(
        coordinate.source.get("start_edge"),
        image_width=image_width,
        image_height=image_height,
    )
    if line is None:
        return None
    first, second = _undistort_normalized_points(
        list(line),
        intrinsics,
        image_width=image_width,
        image_height=image_height,
    )
    return first, second


def _normalized_source_line(
    value: object,
    *,
    image_width: float,
    image_height: float,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    if not isinstance(value, list) or len(value) < 4:
        return None
    try:
        return (
            (float(value[0]) / image_width, float(value[1]) / image_height),
            (float(value[2]) / image_width, float(value[3]) / image_height),
        )
    except (TypeError, ValueError):
        return None


def _clip_polygon_to_unit_square(
    polygon: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    clipped = list(polygon)
    for axis, boundary, keep_greater in (
        (0, 0.0, True),
        (0, 1.0, False),
        (1, 0.0, True),
        (1, 1.0, False),
    ):
        clipped = _clip_polygon_axis(
            clipped,
            axis=axis,
            boundary=boundary,
            keep_greater=keep_greater,
        )
        if not clipped:
            break
    return clipped


def _clip_polygon_axis(
    polygon: list[tuple[float, float]],
    *,
    axis: int,
    boundary: float,
    keep_greater: bool,
) -> list[tuple[float, float]]:
    if not polygon:
        return []

    def inside(point: tuple[float, float]) -> bool:
        return point[axis] >= boundary if keep_greater else point[axis] <= boundary

    def intersection(
        first: tuple[float, float],
        second: tuple[float, float],
    ) -> tuple[float, float]:
        delta = second[axis] - first[axis]
        fraction = 0.0 if abs(delta) <= 1e-12 else (boundary - first[axis]) / delta
        return (
            first[0] + (second[0] - first[0]) * fraction,
            first[1] + (second[1] - first[1]) * fraction,
        )

    output: list[tuple[float, float]] = []
    previous = polygon[-1]
    previous_inside = inside(previous)
    for current in polygon:
        current_inside = inside(current)
        if current_inside != previous_inside:
            output.append(intersection(previous, current))
        if current_inside:
            output.append(current)
        previous = current
        previous_inside = current_inside
    return output


def _best_surface_for_geometry(
    geometry: list[tuple[float, float]],
    surfaces: list[_TrackedSurface],
) -> _TrackedSurface | None:
    candidates = [
        (_geometry_surface_score(geometry, surface.image_polygon), surface)
        for surface in surfaces
    ]
    score, surface = max(candidates, key=lambda item: item[0], default=(0, None))
    return surface if score > 0 else None


def _geometry_surface_score(
    geometry: list[tuple[float, float]],
    surface: list[tuple[float, float]],
) -> int:
    centroid = (
        sum(point[0] for point in geometry) / len(geometry),
        sum(point[1] for point in geometry) / len(geometry),
    )
    return (
        4 * int(_point_in_polygon(centroid, surface))
        + sum(int(_point_in_polygon(point, surface)) for point in geometry)
        + sum(int(_point_in_polygon(point, geometry)) for point in surface)
    )


def _tracked_surface_mapping(
    surface: _TrackedSurface,
    *,
    image_context_id: str | None,
) -> dict[str, Any]:
    return {
        "surface_role": surface.calibration.role,
        "image_context_id": image_context_id,
        "homography_image_to_world": surface.homography.tolist(),
        "current_image_polygon": [
            {"x": point[0], "y": point[1]} for point in surface.image_polygon
        ],
        "visible_image_polygon": [
            {"x": point[0], "y": point[1]}
            for point in surface.visible_image_polygon
        ],
        "visible_world_polygon": [
            {"x": point[0], "y": point[1]}
            for point in surface.visible_world_polygon
        ],
        "origin_world": {
            "x": surface.origin_world[0],
            "y": surface.origin_world[1],
        },
        "longitudinal_axis_world": {
            "x": surface.longitudinal_axis_world[0],
            "y": surface.longitudinal_axis_world[1],
        },
        "calibrated_limit_mm": surface.calibrated_limit_mm,
    }


def _guided_board_pose_overlay(
    pose: _GuidedBoardPose,
    *,
    image_context_id: str | None,
    image_width: float,
    image_height: float,
) -> OverlayPayload:
    end_line = pose.source.get("board_end_line")
    longitudinal_line = pose.source.get("longitudinal_line")
    surface_end_lines = pose.source.get("surface_end_lines")
    preliminary_anchored = (
        pose.source.get("method") == "preliminary_anchored_calibration_pose"
    )
    primitives: list[OverlayPrimitive] = []
    if isinstance(end_line, list) and len(end_line) >= 4:
        primitives.append(
            OverlayPrimitive(
                primitive_type="line",
                layer="calibrated_board_end",
                label=(
                    "Preliminary-anchored board end"
                    if preliminary_anchored
                    else "Calibrated board end"
                ),
                confidence=pose.confidence,
                geometry={
                    "start": {"x": end_line[0], "y": end_line[1]},
                    "end": {"x": end_line[2], "y": end_line[3]},
                    "coordinate_system": "pixel_xy",
                },
                image_context_id=image_context_id,
                style={"stroke": "#00e5ff", "stroke_width": 3.5},
                metadata=pose.source,
            )
        )
    if isinstance(surface_end_lines, dict):
        surface_end_colors = {
            "top": "#00e5ff",
            "bottom": "#00e5ff",
            "side_a": "#7c4dff",
            "side_b": "#7c4dff",
        }
        surface_end_labels = {
            "top": "Top board end",
            "bottom": "Bottom board end",
            "side_a": "Side A board end",
            "side_b": "Side B board end",
        }
        for role, raw_surface_end in surface_end_lines.items():
            if not isinstance(raw_surface_end, dict):
                continue
            line = raw_surface_end.get("line")
            if not isinstance(line, list) or len(line) < 4:
                continue
            primitives.append(
                OverlayPrimitive(
                    primitive_type="line",
                    layer="calibrated_surface_board_end",
                    label=surface_end_labels.get(str(role), f"{role} board end"),
                    confidence=pose.confidence,
                    geometry={
                        "start": {"x": line[0], "y": line[1]},
                        "end": {"x": line[2], "y": line[3]},
                        "coordinate_system": "pixel_xy",
                    },
                    image_context_id=image_context_id,
                    style={
                        "stroke": surface_end_colors.get(str(role), "#00e5ff"),
                        "stroke_width": 3.5,
                    },
                    metadata={
                        "surface_role": role,
                        **raw_surface_end,
                    },
                )
            )
    if isinstance(longitudinal_line, list) and len(longitudinal_line) >= 4:
        primitives.append(
            OverlayPrimitive(
                primitive_type="line",
                layer="calibrated_board_axis_reference",
                label=(
                    "Preliminary-anchored longitudinal edge"
                    if preliminary_anchored
                    else "Calibration-matched longitudinal edge"
                ),
                confidence=pose.confidence,
                geometry={
                    "start": {
                        "x": longitudinal_line[0],
                        "y": longitudinal_line[1],
                    },
                    "end": {
                        "x": longitudinal_line[2],
                        "y": longitudinal_line[3],
                    },
                    "coordinate_system": "pixel_xy",
                },
                image_context_id=image_context_id,
                style={"stroke": "#49c47a", "stroke_width": 3.0},
                metadata=pose.source,
            )
        )
    axis_vector = (
        (pose.axis_endpoint[0] - pose.origin[0]) * image_width,
        (pose.axis_endpoint[1] - pose.origin[1]) * image_height,
    )
    axis_length = max(image_width * 0.2, 40.0)
    primitives.append(
        OverlayPrimitive(
            primitive_type="arrow",
            layer="calibrated_board_longitudinal_axis",
            label=(
                "Board direction from preliminary reference"
                if preliminary_anchored
                else "Calibration-guided board direction"
            ),
            confidence=pose.confidence,
            geometry={
                "start": {
                    "x": pose.origin[0] * image_width,
                    "y": pose.origin[1] * image_height,
                },
                "end": {
                    "x": pose.origin[0] * image_width + axis_vector[0] * axis_length,
                    "y": pose.origin[1] * image_height + axis_vector[1] * axis_length,
                },
                "coordinate_system": "pixel_xy",
            },
            image_context_id=image_context_id,
            style={"stroke": "#49c47a", "stroke_width": 3.5},
            metadata=pose.source,
        )
    )
    return OverlayPayload(
        primitives=primitives,
        display_context_id=image_context_id,
    )


def _tracked_surface_overlay(
    surfaces: list[_TrackedSurface],
    *,
    image_context_id: str | None,
    tracking_mode: str,
) -> OverlayPayload:
    colors = {
        "top": "#00b8d4",
        "bottom": "#00b8d4",
        "side_a": "#7c4dff",
        "side_b": "#7c4dff",
    }
    labels = {
        "top": "Evaluated top surface",
        "bottom": "Evaluated bottom surface",
        "side_a": "Evaluated side A",
        "side_b": "Evaluated side B",
    }
    return OverlayPayload(
        primitives=[
            OverlayPrimitive(
                primitive_type="polygon",
                layer="board_dynamic_surface",
                label=labels.get(
                    surface.calibration.role,
                    f"Evaluated {surface.calibration.role}",
                ),
                geometry={
                    "points": [
                        {"x": point[0], "y": point[1]}
                        for point in surface.visible_image_polygon
                    ],
                    "coordinate_system": "normalized_polygon",
                },
                image_context_id=image_context_id,
                style={
                    "stroke": colors.get(surface.calibration.role, "#00b8d4"),
                    "fill": "rgba(0, 184, 212, 0.06)",
                    "stroke_width": 1.5,
                },
                metadata={
                    "surface_role": surface.calibration.role,
                    "calibrated_limit_mm": round(surface.calibrated_limit_mm, 6),
                    "tracking_mode": tracking_mode,
                },
            )
            for surface in surfaces
        ],
        display_context_id=image_context_id,
    )


def _points(value: object, name: str) -> list[tuple[float, float]]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{name}_requires_four_points")
    points: list[tuple[float, float]] = []
    for item in value:
        if isinstance(item, dict):
            x, y = item.get("x"), item.get("y")
        elif isinstance(item, list | tuple) and len(item) == 2:
            x, y = item
        else:
            raise ValueError(f"{name}_point_invalid")
        if not isinstance(x, int | float) or not isinstance(y, int | float):
            raise ValueError(f"{name}_point_invalid")
        points.append((float(x), float(y)))
    if abs(_polygon_area(points)) <= 1e-9:
        raise ValueError(f"{name}_points_collinear")
    return points


def _homography(source: list[tuple[float, float]], target: list[tuple[float, float]]) -> np.ndarray:
    rows: list[list[float]] = []
    values: list[float] = []
    for (x, y), (u, v) in zip(source, target, strict=True):
        rows.append([x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y])
        values.append(u)
        rows.append([0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y])
        values.append(v)
    matrix = np.asarray(rows, dtype=np.float64)
    vector = np.asarray(values, dtype=np.float64)
    if np.linalg.matrix_rank(matrix) < 8:
        raise ValueError("homography_not_invertible")
    coefficients = np.linalg.solve(matrix, vector)
    homography = np.append(coefficients, 1.0).reshape(3, 3)
    if abs(np.linalg.det(homography)) <= 1e-12:
        raise ValueError("homography_not_invertible")
    return homography


def _project_points(
    homography: np.ndarray, points: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    projected: list[tuple[float, float]] = []
    for x, y in points:
        vector = homography @ np.asarray([x, y, 1.0], dtype=np.float64)
        if abs(float(vector[2])) <= 1e-12:
            raise ValueError("homography_projection_singular")
        projected.append((float(vector[0] / vector[2]), float(vector[1] / vector[2])))
    return projected


def _undistort_normalized_points(
    points: list[tuple[float, float]],
    value: object,
    *,
    image_width: float,
    image_height: float,
) -> list[tuple[float, float]]:
    if not isinstance(value, dict) or not bool(value.get("enabled", True)):
        return points
    try:
        fx = float(value["fx"])
        fy = float(value["fy"])
        cx = float(value["cx"])
        cy = float(value["cy"])
        coefficients = value.get("distortion_coefficients") or value.get("distortion") or []
        distortion = [float(item) for item in coefficients]
    except (KeyError, TypeError, ValueError):
        raise ValueError("camera_intrinsics_invalid") from None
    if fx <= 0 or fy <= 0 or image_width <= 0 or image_height <= 0:
        raise ValueError("camera_intrinsics_invalid")
    k1, k2, p1, p2, k3 = (distortion + [0.0] * 5)[:5]
    corrected: list[tuple[float, float]] = []
    for normalized_x, normalized_y in points:
        distorted_x = (normalized_x * image_width - cx) / fx
        distorted_y = (normalized_y * image_height - cy) / fy
        x, y = distorted_x, distorted_y
        for _ in range(8):
            radius_squared = x * x + y * y
            radial = (
                1.0
                + k1 * radius_squared
                + k2 * radius_squared * radius_squared
                + k3 * radius_squared * radius_squared * radius_squared
            )
            if abs(radial) <= 1e-12:
                raise ValueError("camera_intrinsics_invalid")
            tangent_x = 2.0 * p1 * x * y + p2 * (radius_squared + 2.0 * x * x)
            tangent_y = p1 * (radius_squared + 2.0 * y * y) + 2.0 * p2 * x * y
            x = (distorted_x - tangent_x) / radial
            y = (distorted_y - tangent_y) / radial
        corrected.append(((x * fx + cx) / image_width, (y * fy + cy) / image_height))
    return corrected


def _feature_size(points: list[tuple[float, float]], basis: str) -> float:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    if basis == "longitudinal_extent":
        return max(xs) - min(xs)
    if basis == "transverse_extent":
        return max(ys) - min(ys)
    if basis == "equivalent_diameter":
        return 2.0 * math.sqrt(abs(_polygon_area(points)) / math.pi)
    return max(
        (math.dist(first, second) for first, second in combinations(points, 2)),
        default=0.0,
    )


def _polygon_area(points: list[tuple[float, float]]) -> float:
    return 0.5 * sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(points, points[1:] + points[:1], strict=False)
    )


def _geometry_intersects_surface(
    geometry: list[tuple[float, float]], surface: list[tuple[float, float]]
) -> bool:
    centroid = (
        sum(point[0] for point in geometry) / len(geometry),
        sum(point[1] for point in geometry) / len(geometry),
    )
    return _point_in_polygon(centroid, surface) or any(
        _point_in_polygon(point, surface) for point in geometry
    )


def _point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    previous = polygon[-1]
    for current in polygon:
        crosses = (current[1] > y) != (previous[1] > y)
        if crosses:
            intersection_x = (previous[0] - current[0]) * (y - current[1]) / (
                (previous[1] - current[1]) or 1e-12
            ) + current[0]
            if x < intersection_x:
                inside = not inside
        previous = current
    return inside


def _touches_image_boundary(points: list[tuple[float, float]], epsilon: float = 0.002) -> bool:
    return any(
        x <= epsilon or y <= epsilon or x >= 1.0 - epsilon or y >= 1.0 - epsilon for x, y in points
    )


def _image_dimensions(frame: object, feature_payload: object) -> tuple[float, float]:
    width = getattr(frame, "width", None) or getattr(feature_payload, "image_width", None)
    height = getattr(frame, "height", None) or getattr(feature_payload, "image_height", None)
    return max(float(width or 1), 1.0), max(float(height or 1), 1.0)


def _positive_float(value: object, default: float) -> float:
    try:
        parsed = float(value) if value is not None else float(default)
    except (TypeError, ValueError):
        parsed = float(default)
    return parsed if parsed > 0 else float(default)


def _ensure_role_frame_context(
    context: RuntimeExecutionContext,
    role: str,
    frame: FramePayload,
) -> FramePayload:
    if frame.image_context is not None:
        return frame
    return frame.model_copy(
        update={
            "image_context": RuntimeImageContext(
                context_id=f"ctx_{context.correlation_id}_{role}",
                kind="original",
                width=frame.width,
                height=frame.height,
                source_uri=frame.source_uri,
                source_node_key="source.synchronized_camera_frames",
                transform=RuntimeImageTransform(),
                metadata={"camera_role": role},
            )
        },
        deep=True,
    )


def _opencv_line_segments(lines: object) -> list[LineSegment]:
    """Normalize Hough/LSD output across OpenCV 4 and 5 array layouts."""

    if lines is None:
        return []
    values = np.asarray(lines, dtype=np.float64)
    if values.size == 0 or values.size % 4 != 0:
        return []
    return [
        tuple(float(value) for value in row)  # type: ignore[misc]
        for row in values.reshape(-1, 4)
    ]


def _detect_board_end_reference(
    image: np.ndarray,
    *,
    config: dict[str, Any],
    side: str,
) -> _BoardEndDetection:
    import cv2

    color_image = image if image.ndim == 3 else None
    analysis_image = (
        cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if color_image is not None
        else image
    )
    image_height, image_width = image.shape[:2]
    board_color_region = (
        _detect_board_color_region(
            color_image,
            board_end_side=side,
            color_tolerance_fraction=max(
                min(
                    float(config.get("board_color_tolerance_percent", 75.0))
                    / 100.0,
                    0.95,
                ),
                0.05,
            ),
        )
        if color_image is not None
        else None
    )
    lower_threshold = float(config.get("lower_threshold", 50))
    upper_threshold = float(config.get("upper_threshold", 150))
    minimum_line_length = max(float(config.get("minimum_line_length_px", 25)), 1.0)
    max_line_gap = max(float(config.get("max_line_gap_px", 8)), 0.0)
    maximum_reference_join_gap = max(
        float(config.get("maximum_reference_join_gap_px", 15.0)),
        1.0,
    )
    line_detector_mode = str(config.get("line_detector_mode") or "robust_lsd")
    if line_detector_mode not in {"robust_lsd", "legacy_hough"}:
        line_detector_mode = "robust_lsd"
    effective_hough_line_gap = min(max_line_gap, maximum_reference_join_gap)
    surface_boundary_candidates: list[tuple[LineSegment, float]] = []
    if line_detector_mode == "legacy_hough":
        edges = cv2.Canny(analysis_image, lower_threshold, upper_threshold)
        edge_y, edge_x = np.nonzero(edges)
        edge_points = np.column_stack((edge_x, edge_y)).astype(np.float64)
        gradient_x = cv2.Sobel(analysis_image, cv2.CV_32F, 1, 0, ksize=3)
        gradient_y = cv2.Sobel(analysis_image, cv2.CV_32F, 0, 1, ksize=3)
        edge_gradients = np.column_stack(
            (gradient_x[edge_y, edge_x], gradient_y[edge_y, edge_x])
        ).astype(np.float64)
        lines = cv2.HoughLinesP(
            edges,
            1,
            np.pi / 180.0,
            threshold=max(int(config.get("hough_threshold", 30)), 1),
            minLineLength=minimum_line_length,
            maxLineGap=effective_hough_line_gap,
        )
        candidates = _opencv_line_segments(lines)
    else:
        detector = cv2.createLineSegmentDetector(cv2.LSD_REFINE_STD)
        detected = detector.detect(analysis_image)[0]
        minimum_lsd_segment_length = max(
            min(image_width, image_height) * 0.012,
            6.0,
        )
        candidates = [
            line
            for line in _opencv_line_segments(detected)
            if _line_length(line) >= minimum_lsd_segment_length
        ]
        surface_boundary_candidates = _longitudinal_surface_boundary_candidates(
            analysis_image,
            maximum_count=16,
        )
        if board_color_region is not None:
            surface_boundary_candidates = [
                (line, quality)
                for line, quality in surface_boundary_candidates
                if _line_board_region_support(
                    line,
                    board_color_region.mask,
                    require_boundary_transition=False,
                )
                >= 0.45
            ]
        candidates.extend(line for line, _ in surface_boundary_candidates)
    if board_color_region is not None:
        candidates.extend(
            _board_color_region_line_candidates(
                board_color_region,
                image=analysis_image,
            )
        )
    legacy_angle_tolerance = float(config.get("axis_angle_tolerance_degrees", 30.0))
    board_end_angle_tolerance = max(
        min(
            float(
                config.get(
                    "board_end_angle_tolerance_degrees",
                    legacy_angle_tolerance,
                )
            ),
            44.0,
        ),
        1.0,
    )
    longitudinal_angle_tolerance = max(
        min(
            float(
                config.get(
                    "longitudinal_angle_tolerance_degrees",
                    min(legacy_angle_tolerance, 12.0),
                )
            ),
            30.0,
        ),
        1.0,
    )
    board_end_tolerance_radians = math.radians(board_end_angle_tolerance)
    longitudinal_tolerance_radians = math.radians(longitudinal_angle_tolerance)
    max_axis_segments = max(
        min(int(config.get("max_axis_segments", 200)), 1_000),
        16,
    )
    orientation_pool = _select_longest_line_segments(
        candidates,
        min(max_axis_segments * 2, 2_000),
    )
    longitudinal_segments = [
        line
        for line in orientation_pool
        if _line_angle_from_horizontal(line)
        <= (
            math.radians(30.0)
            if line_detector_mode == "robust_lsd"
            else longitudinal_tolerance_radians
        )
    ]
    unmasked_longitudinal_segment_count = len(longitudinal_segments)
    if board_color_region is not None:
        longitudinal_segments = [
            line
            for line in longitudinal_segments
            if _line_board_region_support(
                line,
                board_color_region.mask,
                require_boundary_transition=False,
            )
            >= 0.45
        ]
    longitudinal_segments = _select_longest_line_segments(
        longitudinal_segments,
        max_axis_segments,
    )
    end_segments = [
        line
        for line in orientation_pool
        if abs(math.pi / 2.0 - _line_angle_from_horizontal(line))
        <= (
            math.radians(44.0)
            if line_detector_mode == "robust_lsd"
            else board_end_tolerance_radians
        )
    ]
    unmasked_end_segment_count = len(end_segments)
    if board_color_region is not None:
        end_segments = [
            line
            for line in end_segments
            if _line_board_region_support(
                line,
                board_color_region.mask,
                require_boundary_transition=True,
            )
            >= 0.35
        ]
    raw_end_segment_count = len(end_segments)
    raw_longitudinal_segment_count = len(longitudinal_segments)
    end_segments = _select_longest_line_segments(end_segments, max_axis_segments)
    position_tolerance = max(min(image_width, image_height) * 0.012, 5.0)
    merge_gap = (
        max(effective_hough_line_gap, minimum_line_length, 18.0)
        if line_detector_mode == "legacy_hough"
        else max(
            min(max_line_gap, maximum_reference_join_gap),
            min(image_width, image_height) * 0.015,
            12.0,
        )
    )
    end_lines = _merge_free_oriented_line_segments(
        end_segments,
        position_tolerance=position_tolerance,
        gap_tolerance=merge_gap,
        angle_tolerance_degrees=5.0,
    )
    longitudinal_lines = _merge_free_oriented_line_segments(
        longitudinal_segments,
        position_tolerance=position_tolerance,
        gap_tolerance=merge_gap,
        angle_tolerance_degrees=5.0,
    )
    merged_end_line_count = len(end_lines)
    merged_longitudinal_line_count = len(longitudinal_lines)
    if line_detector_mode == "legacy_hough":
        refinement_band = max(position_tolerance, 3.0)
        refinement_extension = max(
            maximum_reference_join_gap * 2.0,
            minimum_line_length * 0.5,
            12.0,
        )
        refinement_support_gap = max(effective_hough_line_gap + 2.0, 4.0)
        end_lines_with_support = _refine_lines_from_edge_support(
            end_lines,
            edge_points=edge_points,
            edge_gradients=edge_gradients,
            band_tolerance=refinement_band,
            extension_tolerance=refinement_extension,
            support_gap=refinement_support_gap,
        )
        longitudinal_lines_with_support = _refine_lines_from_edge_support(
            longitudinal_lines,
            edge_points=edge_points,
            edge_gradients=edge_gradients,
            band_tolerance=refinement_band,
            extension_tolerance=refinement_extension,
            support_gap=refinement_support_gap,
        )
    else:
        end_lines_with_support = [(line, 1.0) for line in end_lines]
        longitudinal_lines_with_support = [
            (line, 1.0) for line in longitudinal_lines
        ]
    minimum_edge_support = max(
        min(float(config.get("minimum_edge_support_percent", 15.0)) / 100.0, 1.0),
        0.0,
    )
    end_lines_with_support = [
        item for item in end_lines_with_support if item[1] >= minimum_edge_support
    ]
    longitudinal_lines_with_support = [
        item
        for item in longitudinal_lines_with_support
        if item[1] >= minimum_edge_support
    ]
    end_support_by_line = dict(end_lines_with_support)
    longitudinal_support_by_line = dict(longitudinal_lines_with_support)
    end_lines = [item[0] for item in end_lines_with_support]
    longitudinal_lines = [item[0] for item in longitudinal_lines_with_support]
    longitudinal_boundary_quality_by_line = {
        line: _matched_surface_boundary_quality(
            line,
            surface_boundary_candidates,
            position_tolerance=max(position_tolerance * 2.0, 8.0),
            angle_tolerance_degrees=6.0,
        )
        for line in longitudinal_lines
    }
    minimum_end_coverage = max(
        min(float(config.get("minimum_board_end_span_percent", 25.0)) / 100.0, 0.95),
        0.01,
    )
    minimum_longitudinal_coverage = max(
        min(
            float(config.get("minimum_longitudinal_span_percent", 30.0)) / 100.0,
            0.95,
        ),
        0.01,
    )
    supported_end_line_count = len(end_lines)
    supported_longitudinal_line_count = len(longitudinal_lines)
    end_lines = [
        line
        for line in end_lines
        if _line_length(line) / max(float(image_height), 1.0)
        >= minimum_end_coverage
    ]
    longitudinal_lines = [
        line
        for line in longitudinal_lines
        if abs(line[2] - line[0]) / max(float(image_width), 1.0)
        >= minimum_longitudinal_coverage
    ]
    perpendicular_tolerance = (
        math.radians(35.0)
        if line_detector_mode == "robust_lsd"
        else math.radians(
            min(
                board_end_angle_tolerance + longitudinal_angle_tolerance,
                44.0,
            )
        )
    )
    source: dict[str, Any] = {
        "method": (
            "deterministic_lsd_reference_pair"
            if line_detector_mode == "robust_lsd"
            else "canny_hough_edge_supported_reference_pair"
        ),
        "found": False,
        "line_detector_mode": line_detector_mode,
        "board_end_side": side,
        "end_candidate_orientation": "near_image_vertical",
        "end_candidate_uses_image_vertical": True,
        "board_end_angle_tolerance_degrees": board_end_angle_tolerance,
        "longitudinal_angle_tolerance_degrees": longitudinal_angle_tolerance,
        "angle_handling": (
            "soft_quality_score"
            if line_detector_mode == "robust_lsd"
            else "hard_candidate_filter"
        ),
        "minimum_board_end_span_percent": minimum_end_coverage * 100.0,
        "minimum_longitudinal_span_percent": minimum_longitudinal_coverage * 100.0,
        "minimum_edge_support_percent": minimum_edge_support * 100.0,
        "board_color_region_found": board_color_region is not None,
        "board_color_region_confidence": (
            board_color_region.confidence
            if board_color_region is not None
            else 0.0
        ),
        "board_color_region_outline": (
            [list(point) for point in board_color_region.outline]
            if board_color_region is not None
            else []
        ),
        "board_color_region": (
            board_color_region.source
            if board_color_region is not None
            else {"reason_code": "board_color_region_not_found"}
        ),
        "surface_boundary_method": (
            "cross_surface_contrast_projection"
            if line_detector_mode == "robust_lsd"
            else None
        ),
        "surface_boundary_candidate_count": len(surface_boundary_candidates),
        "surface_boundary_candidates": [
            {
                "line": list(line),
                "quality": float(quality),
            }
            for line, quality in surface_boundary_candidates
        ],
        "configured_hough_line_gap_px": max_line_gap,
        "effective_hough_line_gap_px": effective_hough_line_gap,
        "candidate_count": len(candidates),
        "raw_end_segment_count": raw_end_segment_count,
        "raw_longitudinal_segment_count": raw_longitudinal_segment_count,
        "color_region_rejected_end_segment_count": (
            unmasked_end_segment_count - raw_end_segment_count
        ),
        "color_region_rejected_longitudinal_segment_count": (
            unmasked_longitudinal_segment_count
            - raw_longitudinal_segment_count
        ),
        "used_end_segment_count": len(end_segments),
        "used_longitudinal_segment_count": len(longitudinal_segments),
        "segment_limit": max_axis_segments,
        "segments_truncated": (
            raw_end_segment_count > len(end_segments)
            or raw_longitudinal_segment_count > len(longitudinal_segments)
        ),
        "merged_end_line_count": merged_end_line_count,
        "merged_longitudinal_line_count": merged_longitudinal_line_count,
        "supported_end_line_count": supported_end_line_count,
        "supported_longitudinal_line_count": supported_longitudinal_line_count,
        "qualified_end_line_count": len(end_lines),
        "qualified_longitudinal_line_count": len(longitudinal_lines),
        "end_candidates": [list(line) for line in end_lines[:20]],
        "longitudinal_candidates": [list(line) for line in longitudinal_lines[:20]],
        "end_candidate_support": [
            float(end_support_by_line.get(line, 0.0)) for line in end_lines[:20]
        ],
        "longitudinal_candidate_support": [
            float(longitudinal_support_by_line.get(line, 0.0))
            for line in longitudinal_lines[:20]
        ],
        "longitudinal_candidate_boundary_quality": [
            float(longitudinal_boundary_quality_by_line.get(line, 0.0))
            for line in longitudinal_lines[:20]
        ],
        "calibration_candidates": [
            list(line)
            for line in _select_longest_line_segments(
                [*end_lines, *longitudinal_lines],
                min(max(max_axis_segments * 2, 100), 400),
            )
        ],
    }
    if not end_lines or not longitudinal_lines:
        source["reason_code"] = "required_reference_lines_missing"
        return _BoardEndDetection(
            origin=(0.0 if side == "left" else float(image_width), image_height / 2.0),
            longitudinal=(1.0 if side == "left" else -1.0, 0.0),
            confidence=0.0,
            source=source,
        )

    association_tolerance = max(
        maximum_reference_join_gap,
        min(image_width, image_height) * 0.008,
    )
    source["maximum_reference_join_gap_px"] = association_tolerance
    expected_end_coverage = 0.20
    expected_longitudinal_coverage = 0.35
    pairs: list[dict[str, Any]] = []
    pair_rejections = {
        "angle": 0,
        "intersection": 0,
        "corner_gap": 0,
        "directional_span": 0,
    }
    region_recovered_pair_count = 0
    maximum_region_recovered_join_gap = 0.0
    nearest_join_distance = math.inf
    greatest_directional_span = 0.0
    for end_line in end_lines:
        edge_center_x = (end_line[0] + end_line[2]) / 2.0
        for longitudinal_line in longitudinal_lines:
            perpendicular_error = abs(
                math.pi / 2.0
                - _undirected_angle_difference(
                    _oriented_line_direction(end_line),
                    _oriented_line_direction(longitudinal_line),
                )
            )
            if perpendicular_error > perpendicular_tolerance:
                pair_rejections["angle"] += 1
                continue
            intersection = _line_intersection(end_line, longitudinal_line)
            if intersection is None:
                pair_rejections["intersection"] += 1
                continue
            end_distance = _point_to_segment_distance(intersection, end_line)
            longitudinal_distance = _point_to_segment_distance(intersection, longitudinal_line)
            join_distance = max(end_distance, longitudinal_distance)
            nearest_join_distance = min(nearest_join_distance, join_distance)
            corner_association = "direct_segment_junction"
            corner_bridge_quality = 0.0
            if join_distance > association_tolerance:
                corner_bridge_quality = (
                    _board_region_corner_bridge_quality(
                        intersection,
                        end_line=end_line,
                        longitudinal_line=longitudinal_line,
                        region=board_color_region,
                        side=side,
                        direct_join_tolerance=association_tolerance,
                    )
                    if (
                        line_detector_mode == "robust_lsd"
                        and board_color_region is not None
                        and longitudinal_boundary_quality_by_line.get(
                            longitudinal_line,
                            0.0,
                        )
                        >= 0.25
                    )
                    else 0.0
                )
                if corner_bridge_quality <= 0.0:
                    pair_rejections["corner_gap"] += 1
                    continue
                corner_association = "board_region_projected_junction"
                region_recovered_pair_count += 1
                maximum_region_recovered_join_gap = max(
                    maximum_region_recovered_join_gap,
                    join_distance,
                )
            x_values = (longitudinal_line[0], longitudinal_line[2])
            directional_span = (
                max(x_values) - intersection[0]
                if side == "left"
                else intersection[0] - min(x_values)
            )
            greatest_directional_span = max(greatest_directional_span, directional_span)
            if directional_span < image_width * minimum_longitudinal_coverage:
                pair_rejections["directional_span"] += 1
                continue
            end_coverage = _line_length(end_line) / max(float(image_height), 1.0)
            longitudinal_coverage = directional_span / max(float(image_width), 1.0)
            end_support = min(end_coverage / expected_end_coverage, 1.0)
            longitudinal_support = min(
                longitudinal_coverage / expected_longitudinal_coverage,
                1.0,
            )
            support = min(end_support, longitudinal_support)
            end_angle_error_degrees = math.degrees(
                abs(math.pi / 2.0 - _line_angle_from_horizontal(end_line))
            )
            longitudinal_angle_error_degrees = math.degrees(
                _line_angle_from_horizontal(longitudinal_line)
            )
            end_angle_quality = max(
                1.0
                - max(
                    end_angle_error_degrees - board_end_angle_tolerance,
                    0.0,
                )
                / max(44.0 - board_end_angle_tolerance, 1.0),
                0.0,
            )
            longitudinal_angle_quality = max(
                1.0
                - max(
                    longitudinal_angle_error_degrees
                    - longitudinal_angle_tolerance,
                    0.0,
                )
                / max(30.0 - longitudinal_angle_tolerance, 1.0),
                0.0,
            )
            orientation_quality = min(
                end_angle_quality,
                longitudinal_angle_quality,
            )
            alignment = max(
                1.0 - perpendicular_error / max(perpendicular_tolerance, 1e-9),
                0.0,
            )
            junction = (
                max(1.0 - join_distance / association_tolerance, 0.0)
                if corner_association == "direct_segment_junction"
                else 0.45 + 0.45 * corner_bridge_quality
            )
            confidence = max(
                min(
                    support
                    * (
                        0.60
                        + 0.15 * alignment
                        + 0.15 * junction
                        + 0.10 * orientation_quality
                    ),
                    1.0,
                ),
                0.0,
            )
            end_fraction = _line_projection_fraction(end_line, intersection)
            interior_junction = 1.0 if 0.12 <= end_fraction <= 0.88 else 0.45
            side_preference = (
                1.0 - edge_center_x / max(float(image_width), 1.0)
                if side == "left"
                else edge_center_x / max(float(image_width), 1.0)
            )
            raw_coverage = min(end_coverage / 0.5, longitudinal_coverage / 0.6, 1.0)
            edge_support = min(
                end_support_by_line.get(end_line, 0.0),
                longitudinal_support_by_line.get(longitudinal_line, 0.0),
            )
            surface_boundary_quality = longitudinal_boundary_quality_by_line.get(
                longitudinal_line,
                0.0,
            )
            pairs.append(
                {
                    "end_line": end_line,
                    "longitudinal_line": longitudinal_line,
                    "intersection": intersection,
                    "confidence": confidence,
                    "selection_score": (
                        confidence
                        + 0.25 * interior_junction
                        + 0.15 * side_preference
                        + 0.10 * raw_coverage
                        + 0.10 * edge_support
                        + 0.10 * orientation_quality
                        + 0.30 * surface_boundary_quality
                    ),
                    "end_coverage": end_coverage,
                    "longitudinal_coverage": longitudinal_coverage,
                    "join_distance_px": join_distance,
                    "end_fraction": end_fraction,
                    "alignment": alignment,
                    "orientation_quality": orientation_quality,
                    "board_end_angle_error_degrees": end_angle_error_degrees,
                    "longitudinal_angle_error_degrees": (
                        longitudinal_angle_error_degrees
                    ),
                    "edge_support": edge_support,
                    "surface_boundary_quality": surface_boundary_quality,
                    "corner_association": corner_association,
                    "corner_bridge_quality": corner_bridge_quality,
                }
            )
    surface_boundaries_by_end_line: dict[
        LineSegment,
        list[LineSegment],
    ] = {}
    for end_line in end_lines:
        distinct_boundaries: list[LineSegment] = []
        related_pairs = [
            pair for pair in pairs if pair["end_line"] == end_line
        ]
        related_pairs.sort(
            key=lambda pair: (
                -float(pair["surface_boundary_quality"]),
                -float(pair["longitudinal_coverage"]),
            )
        )
        for pair in related_pairs:
            line = pair["longitudinal_line"]
            if any(
                _average_longitudinal_line_separation(
                    line,
                    existing,
                    image_width=image_width,
                )
                <= max(min(image_width, image_height) * 0.025, 8.0)
                for existing in distinct_boundaries
            ):
                continue
            distinct_boundaries.append(line)
        distinct_boundaries.sort(
            key=lambda line: (line[1] + line[3]) / 2.0
        )
        surface_boundaries_by_end_line[end_line] = distinct_boundaries
    for pair in pairs:
        consensus_count = len(
            surface_boundaries_by_end_line.get(
                pair["end_line"],
                [pair["longitudinal_line"]],
            )
        )
        pair["surface_boundary_consensus_count"] = consensus_count
        pair["selection_score"] = float(pair["selection_score"]) + 0.12 * min(
            max(consensus_count - 1, 0),
            2,
        )
    source.update(
        {
            "pair_attempt_count": len(end_lines) * len(longitudinal_lines),
            "pair_rejections": pair_rejections,
            "region_recovered_pair_count": region_recovered_pair_count,
            "maximum_region_recovered_join_gap_px": (
                maximum_region_recovered_join_gap
                if region_recovered_pair_count
                else None
            ),
            "nearest_join_distance_px": (
                float(nearest_join_distance)
                if math.isfinite(nearest_join_distance)
                else None
            ),
            "greatest_directional_span_px": greatest_directional_span,
            "required_directional_span_px": (
                image_width * minimum_longitudinal_coverage
            ),
        }
    )
    if not pairs:
        source["reason_code"] = "reference_line_pair_missing"
        return _BoardEndDetection(
            origin=(0.0 if side == "left" else float(image_width), image_height / 2.0),
            longitudinal=(1.0 if side == "left" else -1.0, 0.0),
            confidence=0.0,
            source=source,
        )

    highest_pair_score = max(
        float(pair["selection_score"])
        for pair in pairs
    )
    highest_pair_confidence = max(
        float(pair["confidence"])
        for pair in pairs
    )
    leading_quality_margin = max(
        float(config.get("leading_edge_score_tolerance", 0.55)),
        0.0,
    )
    plausible_pairs = [
        pair
        for pair in pairs
        if float(pair["selection_score"])
        >= highest_pair_score - leading_quality_margin
        and float(pair["confidence"])
        >= max(highest_pair_confidence - 0.40, 0.35)
    ]
    selected = (
        min(
            plausible_pairs,
            key=lambda pair: (
                (pair["end_line"][0] + pair["end_line"][2]) / 2.0,
                -float(pair["selection_score"]),
            ),
        )
        if side == "left"
        else max(
            plausible_pairs,
            key=lambda pair: (
                (pair["end_line"][0] + pair["end_line"][2]) / 2.0,
                float(pair["selection_score"]),
            ),
        )
    )
    selected_longitudinal = selected["longitudinal_line"]
    dx = float(selected_longitudinal[2] - selected_longitudinal[0])
    dy = float(selected_longitudinal[3] - selected_longitudinal[1])
    if (side == "left" and dx < 0) or (side == "right" and dx > 0):
        dx = -dx
        dy = -dy
    direction_length = max(math.hypot(dx, dy), 1e-9)
    direction = (dx / direction_length, dy / direction_length)
    intersection = selected["intersection"]
    validated_surface_edges = surface_boundaries_by_end_line.get(
        selected["end_line"],
        [selected_longitudinal],
    )
    source.update(
        {
            "found": True,
            "start_edge": list(selected["end_line"]),
            "side_edge": list(selected_longitudinal),
            "origin": [float(intersection[0]), float(intersection[1])],
            "confidence": float(selected["confidence"]),
            "end_coverage": float(selected["end_coverage"]),
            "longitudinal_coverage": float(selected["longitudinal_coverage"]),
            "join_distance_px": float(selected["join_distance_px"]),
            "junction_fraction": float(selected["end_fraction"]),
            "alignment": float(selected["alignment"]),
            "orientation_quality": float(selected["orientation_quality"]),
            "board_end_angle_error_degrees": float(
                selected["board_end_angle_error_degrees"]
            ),
            "longitudinal_angle_error_degrees": float(
                selected["longitudinal_angle_error_degrees"]
            ),
            "edge_support": float(selected["edge_support"]),
            "surface_boundary_quality": float(
                selected["surface_boundary_quality"]
            ),
            "corner_association": str(selected["corner_association"]),
            "corner_bridge_quality": float(
                selected["corner_bridge_quality"]
            ),
            "surface_boundary_consensus_count": int(
                selected["surface_boundary_consensus_count"]
            ),
            "validated_surface_edges": [
                list(line) for line in validated_surface_edges
            ],
            "board_edges": {
                "end": list(selected["end_line"]),
                "longitudinal_reference": list(selected_longitudinal),
                "visible_longitudinal": [
                    list(line) for line in validated_surface_edges
                ],
                "corner": [
                    float(intersection[0]),
                    float(intersection[1]),
                ],
                "corner_association": str(
                    selected["corner_association"]
                ),
            },
            "pair_count": len(pairs),
            "plausible_leading_pair_count": len(plausible_pairs),
            "highest_pair_score": highest_pair_score,
            "selected_pair_score": float(selected["selection_score"]),
            "leading_edge_selection": (
                "leftmost_plausible_pair"
                if side == "left"
                else "rightmost_plausible_pair"
            ),
        }
    )
    return _BoardEndDetection(
        origin=(float(intersection[0]), float(intersection[1])),
        longitudinal=direction,
        confidence=float(selected["confidence"]),
        source=source,
    )


def _longitudinal_surface_boundary_candidates(
    image: np.ndarray,
    *,
    maximum_count: int,
) -> list[tuple[LineSegment, float]]:
    """Find long boundaries that separate two image regions, not merely thin lines."""

    import cv2

    source_height, source_width = image.shape[:2]
    analysis_scale = min(1.0, 320.0 / max(source_width, source_height, 1))
    if analysis_scale < 1.0:
        image = cv2.resize(
            image,
            (
                max(int(round(source_width * analysis_scale)), 1),
                max(int(round(source_height * analysis_scale)), 1),
            ),
            interpolation=cv2.INTER_AREA,
        )
    image_height, image_width = image.shape[:2]
    if image_height < 32 or image_width < 64 or maximum_count <= 0:
        return []

    grey = image.astype(np.float32, copy=False)
    minimum_dimension = min(image_height, image_width)
    # Wide comparison bands suppress thin grain and scratches: both sides of a
    # thin mark still belong to the same wood surface, while a face boundary
    # separates two sustained image regions.
    band_height = max(int(round(minimum_dimension * 0.03)), 9)
    if band_height % 2 == 0:
        band_height += 1
    band_offset = max(band_height, 3)
    if image_height <= 2 * band_offset + 2:
        return []

    vertical_band_mean = cv2.boxFilter(
        grey,
        cv2.CV_32F,
        (1, band_height),
        normalize=True,
        borderType=cv2.BORDER_REFLECT101,
    )
    boundary_response = np.zeros_like(vertical_band_mean, dtype=np.float32)
    boundary_response[band_offset : image_height - band_offset] = np.abs(
        vertical_band_mean[2 * band_offset :]
        - vertical_band_mean[: -2 * band_offset]
    )
    boundary_response = cv2.GaussianBlur(
        boundary_response,
        (3, 3),
        0,
        borderType=cv2.BORDER_REFLECT101,
    )

    interior_response = boundary_response[
        band_offset : image_height - band_offset
    ]
    response_median = float(np.median(interior_response))
    response_mad = float(
        np.median(np.abs(interior_response - response_median))
    )
    pixel_support_threshold = response_median + max(2.0 * response_mad, 1.0)
    image_center_x = (image_width - 1.0) / 2.0
    valid_source = np.ones_like(boundary_response, dtype=np.float32)
    raw_candidates: list[tuple[LineSegment, float, float]] = []

    # A fixed search lattice is intentional. Candidate availability must not
    # jump merely because an operator changes an angle preference by one degree.
    angle_lattice = tuple(float(value) for value in range(-30, 31, 5))
    for angle_degrees in angle_lattice:
        slope = math.tan(math.radians(float(angle_degrees)))
        transform = np.asarray(
            [
                [1.0, 0.0, 0.0],
                [-slope, 1.0, slope * image_center_x],
            ],
            dtype=np.float32,
        )
        aligned_response = cv2.warpAffine(
            boundary_response,
            transform,
            (image_width, image_height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        aligned_valid = cv2.warpAffine(
            valid_source,
            transform,
            (image_width, image_height),
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        valid_counts = np.sum(aligned_valid, axis=1)
        usable_rows = valid_counts >= image_width * 0.60
        if not np.any(usable_rows):
            continue
        profile = np.divide(
            np.sum(aligned_response, axis=1),
            np.maximum(valid_counts, 1.0),
        )
        profile = cv2.GaussianBlur(
            profile.reshape(-1, 1).astype(np.float32),
            (1, 5),
            0,
        ).reshape(-1)
        support = np.divide(
            np.sum(
                (aligned_response >= pixel_support_threshold) * aligned_valid,
                axis=1,
            ),
            np.maximum(valid_counts, 1.0),
        )
        supported_pixels = (
            (aligned_response >= pixel_support_threshold)
            & (aligned_valid > 0.5)
        ).astype(np.uint8)
        closing_width = max(int(round(image_width * 0.025)), 3)
        supported_runs = cv2.morphologyEx(
            supported_pixels,
            cv2.MORPH_CLOSE,
            np.ones((1, closing_width), dtype=np.uint8),
        )
        usable_profile = profile[usable_rows]
        profile_median = float(np.median(usable_profile))
        profile_mad = float(
            np.median(np.abs(usable_profile - profile_median))
        )
        minimum_profile = profile_median + max(2.5 * profile_mad, 0.75)
        local_maximum = cv2.dilate(
            profile.reshape(-1, 1),
            np.ones((9, 1), dtype=np.uint8),
        ).reshape(-1)
        peak_indices = np.flatnonzero(
            usable_rows
            & (profile >= local_maximum - 1e-6)
            & (profile >= minimum_profile)
            & (support >= 0.12)
        )
        if not len(peak_indices):
            continue
        peak_indices = sorted(
            peak_indices,
            key=lambda index: (
                -float(profile[index]),
                -float(support[index]),
                int(index),
            ),
        )[:6]
        profile_scale = max(3.0 * profile_mad, 1.0)
        for peak_index in peak_indices:
            run = _longest_supported_run(supported_runs[int(peak_index)])
            if run is None:
                continue
            run_start, run_end = run
            run_length = run_end - run_start + 1
            if run_length < image_width * 0.30:
                continue
            raw_support_fraction = float(
                np.mean(
                    supported_pixels[
                        int(peak_index),
                        run_start : run_end + 1,
                    ]
                )
            )
            if raw_support_fraction < 0.18:
                continue
            line = _slope_segment_from_aligned_span(
                slope=slope,
                center_intercept=float(peak_index),
                start_x=float(run_start),
                end_x=float(run_end),
                image_width=image_width,
                image_height=image_height,
            )
            if line is None:
                continue
            run_profile = float(
                np.mean(
                    aligned_response[
                        int(peak_index),
                        run_start : run_end + 1,
                    ]
                )
            )
            contrast_strength = max(
                (run_profile - profile_median) / profile_scale,
                0.0,
            )
            run_fraction = run_length / max(float(image_width), 1.0)
            raw_score = (
                contrast_strength
                * run_fraction**1.5
                * (0.65 + 0.35 * raw_support_fraction)
            )
            raw_candidates.append((line, raw_score, float(angle_degrees)))

    if not raw_candidates:
        return []
    raw_candidates.sort(
        key=lambda item: (
            -item[1],
            abs(item[2]),
            item[0],
        )
    )
    selected: list[tuple[LineSegment, float, float]] = []
    separation_tolerance = max(minimum_dimension * 0.018, 6.0)
    for candidate in raw_candidates:
        if any(
            _average_longitudinal_line_separation(
                candidate[0],
                existing[0],
                image_width=image_width,
            )
            <= separation_tolerance
            for existing in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) >= maximum_count:
            break

    highest_score = max((item[1] for item in selected), default=1.0)
    normalized = [
        (line, min(max(score / max(highest_score, 1e-6), 0.0), 1.0))
        for line, score, _ in selected
    ]
    qualified = [
        (line, quality)
        for line, quality in normalized
        if quality >= 0.30
    ]
    if analysis_scale >= 1.0:
        return qualified
    inverse_scale = 1.0 / analysis_scale
    return [
        (
            tuple(float(value) * inverse_scale for value in line),  # type: ignore[misc]
            quality,
        )
        for line, quality in qualified
    ]


def _detect_board_color_region(
    image: np.ndarray,
    *,
    board_end_side: str,
    color_tolerance_fraction: float,
) -> _BoardColorRegion | None:
    """Build a deterministic board proposal from wood chroma and scene contrast."""

    import cv2

    source_height, source_width = image.shape[:2]
    if source_height < 32 or source_width < 64:
        return None
    analysis_scale = min(1.0, 320.0 / max(source_width, source_height, 1))
    working = (
        cv2.resize(
            image,
            (
                max(int(round(source_width * analysis_scale)), 1),
                max(int(round(source_height * analysis_scale)), 1),
            ),
            interpolation=cv2.INTER_AREA,
        )
        if analysis_scale < 1.0
        else image
    )
    image_height, image_width = working.shape[:2]
    lab = cv2.cvtColor(working, cv2.COLOR_BGR2LAB)
    hsv = cv2.cvtColor(working, cv2.COLOR_BGR2HSV)
    yellow = np.maximum(lab[:, :, 2].astype(np.float32) - 128.0, 0.0)
    saturation = hsv[:, :, 1].astype(np.float32)
    value = hsv[:, :, 2].astype(np.float32)
    grey = cv2.cvtColor(working, cv2.COLOR_BGR2GRAY).astype(np.float32)
    wood_score = cv2.GaussianBlur(
        1.8 * yellow + 0.35 * saturation,
        (5, 5),
        0,
        borderType=cv2.BORDER_REFLECT101,
    )
    texture_window = max(int(round(min(image_width, image_height) * 0.045)), 7)
    if texture_window % 2 == 0:
        texture_window += 1
    local_mean = cv2.boxFilter(
        grey,
        cv2.CV_32F,
        (texture_window, texture_window),
        normalize=True,
        borderType=cv2.BORDER_REFLECT101,
    )
    local_squared_mean = cv2.boxFilter(
        grey * grey,
        cv2.CV_32F,
        (texture_window, texture_window),
        normalize=True,
        borderType=cv2.BORDER_REFLECT101,
    )
    texture_score = np.sqrt(
        np.maximum(local_squared_mean - local_mean * local_mean, 0.0)
    )
    texture_score = cv2.GaussianBlur(
        texture_score,
        (5, 5),
        0,
        borderType=cv2.BORDER_REFLECT101,
    )
    score_median = float(np.median(wood_score))
    score_high = float(np.percentile(wood_score, 97.0))
    candidate_mask = np.zeros((image_height, image_width), dtype=np.uint8)
    seed_mask = np.zeros((image_height, image_width), dtype=np.uint8)
    color_separation = score_high - score_median
    color_threshold = None
    color_expansion_threshold = None
    if color_separation >= 3.0:
        clipped_score = np.clip(wood_score, 0.0, 255.0).astype(np.uint8)
        otsu_threshold, _ = cv2.threshold(
            clipped_score,
            0,
            255,
            cv2.THRESH_BINARY + cv2.THRESH_OTSU,
        )
        # Otsu separates the neutral background from the one or two differently
        # shaded wood faces. A median/MAD floor is intentionally not used here:
        # the board can occupy more than half of the frame and would then raise
        # that floor above the brighter top face.
        color_threshold = max(float(otsu_threshold) + 1.0, 2.0)
        color_expansion_threshold = max(
            color_threshold * (1.0 - color_tolerance_fraction),
            1.0,
        )
        seed_mask[wood_score >= color_threshold] = 255
        candidate_mask[wood_score >= color_expansion_threshold] = 255

    texture_median = float(np.median(texture_score))
    texture_high = float(np.percentile(texture_score, 97.0))
    texture_separation = texture_high - texture_median
    texture_threshold = None
    texture_expansion_threshold = None
    texture_candidate_mask: np.ndarray | None = None
    if texture_separation >= 0.75:
        clipped_texture = np.clip(texture_score, 0.0, 255.0).astype(np.uint8)
        otsu_texture, _ = cv2.threshold(
            clipped_texture,
            0,
            255,
            cv2.THRESH_BINARY + cv2.THRESH_OTSU,
        )
        texture_threshold = max(float(otsu_texture) + 0.5, 0.75)
        texture_expansion_threshold = max(
            texture_threshold
            * (1.0 - 1.15 * color_tolerance_fraction),
            0.5,
        )
        texture_candidate_mask = (
            texture_score >= texture_expansion_threshold
        )

    scene_value_median = float(np.median(value))
    dark_background_mode = scene_value_median < 95.0
    brightness_threshold = None
    brightness_expansion_threshold = None
    if dark_background_mode:
        value_u8 = value.astype(np.uint8)
        otsu_value, _ = cv2.threshold(
            value_u8,
            0,
            255,
            cv2.THRESH_BINARY + cv2.THRESH_OTSU,
        )
        brightness_threshold = max(
            float(otsu_value),
            scene_value_median + 20.0,
        )
        brightness_expansion_threshold = (
            scene_value_median
            + (brightness_threshold - scene_value_median)
            * (1.0 - color_tolerance_fraction)
        )
        seed_mask[value >= brightness_threshold] = 255
        candidate_mask[value >= brightness_expansion_threshold] = 255

    if not np.any(seed_mask):
        return None
    texture_envelope = None
    allowed_texture_pixel_count = 0
    if texture_candidate_mask is not None:
        seed_component_count, _, seed_statistics, _ = (
            cv2.connectedComponentsWithStats(seed_mask, connectivity=8)
        )
        seed_components = [
            (
                int(seed_statistics[index, cv2.CC_STAT_AREA]),
                int(seed_statistics[index, cv2.CC_STAT_WIDTH]),
                index,
            )
            for index in range(1, seed_component_count)
            if int(seed_statistics[index, cv2.CC_STAT_AREA]) >= 12
        ]
        if seed_components:
            _, _, seed_component_id = max(seed_components)
            seed_x = int(
                seed_statistics[seed_component_id, cv2.CC_STAT_LEFT]
            )
            seed_y = int(
                seed_statistics[seed_component_id, cv2.CC_STAT_TOP]
            )
            seed_width = int(
                seed_statistics[seed_component_id, cv2.CC_STAT_WIDTH]
            )
            seed_height = int(
                seed_statistics[seed_component_id, cv2.CC_STAT_HEIGHT]
            )
            envelope_x0 = max(
                seed_x - int(round(image_width * 0.04)),
                0,
            )
            envelope_x1 = min(
                seed_x + seed_width + int(round(image_width * 0.04)),
                image_width,
            )
            envelope_y0 = max(
                seed_y - int(round(image_height * 0.70)),
                0,
            )
            envelope_y1 = min(
                seed_y + seed_height + int(round(image_height * 0.03)),
                image_height,
            )
            texture_envelope = (
                envelope_x0,
                envelope_y0,
                envelope_x1,
                envelope_y1,
            )
            allowed_texture = np.zeros_like(texture_candidate_mask)
            allowed_texture[
                envelope_y0:envelope_y1,
                envelope_x0:envelope_x1,
            ] = texture_candidate_mask[
                envelope_y0:envelope_y1,
                envelope_x0:envelope_x1,
            ]
            allowed_texture_pixel_count = int(
                np.count_nonzero(allowed_texture)
            )
            candidate_mask[allowed_texture] = 255

    if not np.any(candidate_mask):
        return None
    opening_size = max(int(round(min(image_width, image_height) * 0.008)), 3)
    closing_size = max(int(round(min(image_width, image_height) * 0.035)), 7)
    candidate_mask = cv2.morphologyEx(
        candidate_mask,
        cv2.MORPH_OPEN,
        np.ones((1, opening_size), dtype=np.uint8),
    )
    candidate_mask = cv2.morphologyEx(
        candidate_mask,
        cv2.MORPH_CLOSE,
        np.ones((closing_size, closing_size), dtype=np.uint8),
    )

    component_count, labels, statistics, _ = cv2.connectedComponentsWithStats(
        candidate_mask,
        connectivity=8,
    )
    components: list[dict[str, float | int]] = []
    image_area = float(image_width * image_height)
    for component_id in range(1, component_count):
        x = int(statistics[component_id, cv2.CC_STAT_LEFT])
        y = int(statistics[component_id, cv2.CC_STAT_TOP])
        width = int(statistics[component_id, cv2.CC_STAT_WIDTH])
        height = int(statistics[component_id, cv2.CC_STAT_HEIGHT])
        area = int(statistics[component_id, cv2.CC_STAT_AREA])
        seed_overlap = int(np.count_nonzero(seed_mask[labels == component_id]))
        width_fraction = width / max(float(image_width), 1.0)
        height_fraction = height / max(float(image_height), 1.0)
        area_fraction = area / max(image_area, 1.0)
        aspect_ratio = width / max(float(height), 1.0)
        fill_ratio = area / max(float(width * height), 1.0)
        if (
            width_fraction < 0.28
            or height_fraction < 0.08
            or area_fraction < 0.012
            or aspect_ratio < 1.15
        ):
            continue
        side_extent = (
            1.0 - x / max(float(image_width), 1.0)
            if board_end_side == "left"
            else (x + width) / max(float(image_width), 1.0)
        )
        components.append(
            {
                "id": component_id,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "area": area,
                "seed_overlap": seed_overlap,
                "score": (
                    2.0 * width_fraction
                    + 0.8 * min(aspect_ratio / 4.0, 1.0)
                    + 0.6 * min(fill_ratio, 1.0)
                    + 0.25 * min(max(side_extent, 0.0), 1.0)
                    + 0.35 * min(area_fraction / 0.25, 1.0)
                ),
            }
        )
    if not components:
        return None
    seeded_components = [
        component
        for component in components
        if int(component["seed_overlap"])
        >= max(int(int(component["area"]) * 0.002), 12)
    ]
    if not seeded_components:
        return None
    primary = max(
        seeded_components,
        key=lambda item: (
            float(item["score"]),
            int(item["area"]),
            -int(item["y"]),
        ),
    )
    primary_x0 = int(primary["x"])
    primary_x1 = primary_x0 + int(primary["width"])
    primary_y0 = int(primary["y"])
    primary_y1 = primary_y0 + int(primary["height"])
    selected_ids: set[int] = {int(primary["id"])}
    for component in components:
        component_x0 = int(component["x"])
        component_x1 = component_x0 + int(component["width"])
        component_y0 = int(component["y"])
        component_y1 = component_y0 + int(component["height"])
        horizontal_overlap = max(
            min(primary_x1, component_x1) - max(primary_x0, component_x0),
            0,
        )
        overlap_fraction = horizontal_overlap / max(
            min(int(primary["width"]), int(component["width"])),
            1,
        )
        vertical_gap = max(
            primary_y0 - component_y1,
            component_y0 - primary_y1,
            0,
        )
        if overlap_fraction >= 0.55 and vertical_gap <= image_height * 0.08:
            selected_ids.add(int(component["id"]))

    selected_mask = np.isin(labels, list(selected_ids)).astype(np.uint8) * 255
    selected_points = cv2.findNonZero(selected_mask)
    if selected_points is None or len(selected_points) < 4:
        return None
    hull = cv2.convexHull(selected_points)
    board_mask = np.zeros_like(selected_mask)
    cv2.fillConvexPoly(board_mask, hull, 255)
    contours, _ = cv2.findContours(
        board_mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    area_fraction = float(cv2.contourArea(contour)) / max(image_area, 1.0)
    x, y, width, height = cv2.boundingRect(contour)
    width_fraction = width / max(float(image_width), 1.0)
    if area_fraction < 0.02 or area_fraction > 0.85 or width_fraction < 0.30:
        return None
    approximation = cv2.approxPolyDP(
        contour,
        max(cv2.arcLength(contour, True) * 0.008, 1.0),
        True,
    )
    outline_working = [
        (float(point[0][0]), float(point[0][1]))
        for point in approximation
    ]
    if len(outline_working) < 3:
        return None

    if analysis_scale < 1.0:
        board_mask = cv2.resize(
            board_mask,
            (source_width, source_height),
            interpolation=cv2.INTER_NEAREST,
        )
        inverse_scale = 1.0 / analysis_scale
        outline = [
            (x_value * inverse_scale, y_value * inverse_scale)
            for x_value, y_value in outline_working
        ]
    else:
        outline = outline_working
    confidence = min(
        max(
            0.35
            + 0.35 * min(width_fraction / 0.65, 1.0)
            + 0.20 * min(area_fraction / 0.25, 1.0)
            + 0.10 * min(color_separation / 20.0, 1.0),
            0.0,
        ),
        1.0,
    )
    return _BoardColorRegion(
        mask=board_mask,
        outline=outline,
        confidence=confidence,
        source={
            "method": "adaptive_wood_chroma_and_dark_background",
            "area_fraction": area_fraction,
            "width_fraction": width_fraction,
            "component_count": len(components),
            "component_summaries": [
                {
                    "x": int(component["x"]),
                    "y": int(component["y"]),
                    "width": int(component["width"]),
                    "height": int(component["height"]),
                    "area": int(component["area"]),
                    "seed_overlap": int(component["seed_overlap"]),
                }
                for component in components
            ],
            "merged_component_count": len(selected_ids),
            "primary_component": {
                "x": int(primary["x"]),
                "y": int(primary["y"]),
                "width": int(primary["width"]),
                "height": int(primary["height"]),
                "area": int(primary["area"]),
                "seed_overlap": int(primary["seed_overlap"]),
            },
            "color_separation": color_separation,
            "color_threshold": color_threshold,
            "color_expansion_threshold": color_expansion_threshold,
            "color_tolerance_percent": color_tolerance_fraction * 100.0,
            "texture_separation": texture_separation,
            "texture_threshold": texture_threshold,
            "texture_expansion_threshold": texture_expansion_threshold,
            "texture_envelope": texture_envelope,
            "texture_candidate_pixel_count": (
                int(np.count_nonzero(texture_candidate_mask))
                if texture_candidate_mask is not None
                else 0
            ),
            "allowed_texture_pixel_count": allowed_texture_pixel_count,
            "dark_background_mode": dark_background_mode,
            "brightness_threshold": brightness_threshold,
            "brightness_expansion_threshold": brightness_expansion_threshold,
            "analysis_scale": analysis_scale,
        },
    )


def _board_color_region_line_candidates(
    region: _BoardColorRegion,
    *,
    image: np.ndarray,
) -> list[LineSegment]:
    """Use the region only as a proposal, then snap board ends to image evidence."""

    if len(region.outline) < 3:
        return []
    image_height, image_width = image.shape[:2]
    minimum_length = max(min(image_width, image_height) * 0.08, 12.0)
    search_radius = max(int(round(min(image_width, image_height) * 0.05)), 6)
    probe_distance = max(min(image_width, image_height) * 0.004, 1.5)
    candidates: list[LineSegment] = []
    for start, end in zip(
        region.outline,
        [*region.outline[1:], region.outline[0]],
        strict=True,
    ):
        line = (start[0], start[1], end[0], end[1])
        line_length = _line_length(line)
        if line_length < minimum_length:
            continue
        if abs(math.pi / 2.0 - _line_angle_from_horizontal(line)) > math.radians(
            44.0
        ):
            continue
        direction_x = (line[2] - line[0]) / line_length
        direction_y = (line[3] - line[1]) / line_length
        normal_x = -direction_y
        normal_y = direction_x
        sample_count = max(min(int(round(line_length / 2.0)), 240), 48)
        fractions = np.linspace(0.0, 1.0, sample_count, dtype=np.float64)
        base_x = line[0] + fractions * (line[2] - line[0])
        base_y = line[1] + fractions * (line[3] - line[1])
        best_shift = 0.0
        best_score = -math.inf
        best_response = 0.0
        for shift in range(-search_radius, search_radius + 1):
            center_x = base_x + shift * normal_x
            center_y = base_y + shift * normal_y
            positive_x = np.rint(center_x + probe_distance * normal_x).astype(int)
            positive_y = np.rint(center_y + probe_distance * normal_y).astype(int)
            negative_x = np.rint(center_x - probe_distance * normal_x).astype(int)
            negative_y = np.rint(center_y - probe_distance * normal_y).astype(int)
            valid = (
                (positive_x >= 0)
                & (positive_x < image_width)
                & (positive_y >= 0)
                & (positive_y < image_height)
                & (negative_x >= 0)
                & (negative_x < image_width)
                & (negative_y >= 0)
                & (negative_y < image_height)
            )
            if np.count_nonzero(valid) < sample_count * 0.55:
                continue
            response = float(
                np.mean(
                    np.abs(
                        image[positive_y[valid], positive_x[valid]].astype(np.float32)
                        - image[negative_y[valid], negative_x[valid]].astype(
                            np.float32
                        )
                    )
                )
            )
            # A tiny distance preference makes equal plateaus deterministic
            # without overpowering even a weak pale-board transition.
            score = response - abs(float(shift)) * 0.01
            if score > best_score:
                best_score = score
                best_response = response
                best_shift = float(shift)
        if best_response < 1.0:
            continue
        candidates.append(
            (
                line[0] + best_shift * normal_x,
                line[1] + best_shift * normal_y,
                line[2] + best_shift * normal_x,
                line[3] + best_shift * normal_y,
            )
        )
    return candidates


def _line_board_region_support(
    line: LineSegment,
    board_mask: np.ndarray,
    *,
    require_boundary_transition: bool,
) -> float:
    image_height, image_width = board_mask.shape[:2]
    line_length = _line_length(line)
    if line_length <= 1e-6:
        return 0.0
    sample_count = max(min(int(round(line_length / 3.0)), 160), 32)
    fractions = np.linspace(0.0, 1.0, sample_count, dtype=np.float64)
    x_values = line[0] + fractions * (line[2] - line[0])
    y_values = line[1] + fractions * (line[3] - line[1])
    normal_x = -(line[3] - line[1]) / line_length
    normal_y = (line[2] - line[0]) / line_length
    base_offset = max(min(image_width, image_height) * 0.012, 3.0)

    def inside_at(offset_value: float) -> np.ndarray:
        sample_x = np.rint(x_values + offset_value * normal_x).astype(int)
        sample_y = np.rint(y_values + offset_value * normal_y).astype(int)
        valid = (
            (sample_x >= 0)
            & (sample_x < image_width)
            & (sample_y >= 0)
            & (sample_y < image_height)
        )
        result = np.zeros(sample_count, dtype=bool)
        result[valid] = board_mask[sample_y[valid], sample_x[valid]] > 0
        return result

    center = inside_at(0.0)
    if not require_boundary_transition:
        positive = inside_at(base_offset)
        negative = inside_at(-base_offset)
        return float(np.mean(center | positive | negative))
    # Closing and convex-hull regularisation intentionally make the region
    # proposal stable, but can place its diagnostic outline a few pixels beyond
    # the actual optical edge. Test several physical bands so a genuine edge is
    # not rejected merely because the proposal has this harmless padding.
    transition_support = 0.0
    for offset_multiplier in (1.0, 2.0, 3.0, 4.0, 5.0):
        offset = base_offset * offset_multiplier
        positive = inside_at(offset)
        negative = inside_at(-offset)
        transition = positive ^ negative
        boundary_presence = center | positive | negative
        transition_support = max(
            transition_support,
            float(np.mean(transition & boundary_presence)),
        )
    return transition_support


def _board_region_corner_bridge_quality(
    intersection: tuple[float, float],
    *,
    end_line: LineSegment,
    longitudinal_line: LineSegment,
    region: _BoardColorRegion,
    side: str,
    direct_join_tolerance: float,
) -> float:
    """Validate an occluded surface junction by projecting inside the board mask.

    A board with a visible main face and side face often has a weak or bevelled
    corner. LSD then stops the longitudinal segment before it reaches the board
    end even though both fitted lines and the board silhouette are correct. The
    mask is used only to bridge that missing optical segment; it never creates a
    line direction by itself.
    """

    image_height, image_width = region.mask.shape[:2]
    minimum_dimension = max(min(image_width, image_height), 1)
    bridge_limit = min(
        max(direct_join_tolerance * 3.0, minimum_dimension * 0.06),
        minimum_dimension * 0.14,
    )
    end_distance = _point_to_segment_distance(intersection, end_line)
    longitudinal_distance = _point_to_segment_distance(
        intersection,
        longitudinal_line,
    )
    join_distance = max(end_distance, longitudinal_distance)
    if join_distance > bridge_limit:
        return 0.0
    if not (
        -direct_join_tolerance <= intersection[0] <= image_width - 1 + direct_join_tolerance
        and -direct_join_tolerance
        <= intersection[1]
        <= image_height - 1 + direct_join_tolerance
    ):
        return 0.0

    outline_distance = min(
        (
            _point_to_segment_distance(
                intersection,
                (
                    start[0],
                    start[1],
                    end[0],
                    end[1],
                ),
            )
            for start, end in zip(
                region.outline,
                [*region.outline[1:], region.outline[0]],
                strict=True,
            )
        ),
        default=math.inf,
    )
    outline_tolerance = max(
        direct_join_tolerance * 2.0,
        minimum_dimension * 0.05,
    )
    if outline_distance > outline_tolerance:
        return 0.0

    dx = longitudinal_line[2] - longitudinal_line[0]
    dy = longitudinal_line[3] - longitudinal_line[1]
    direction_length = math.hypot(dx, dy)
    if direction_length <= 1e-9:
        return 0.0
    dx /= direction_length
    dy /= direction_length
    if (side == "left" and dx < 0.0) or (side == "right" and dx > 0.0):
        dx = -dx
        dy = -dy

    sample_extent = max(
        join_distance + direct_join_tolerance,
        minimum_dimension * 0.08,
    )
    distances = np.linspace(
        max(direct_join_tolerance * 0.2, 2.0),
        sample_extent,
        24,
        dtype=np.float64,
    )
    normal_x = -dy
    normal_y = dx
    normal_offsets = (
        -max(minimum_dimension * 0.004, 1.5),
        0.0,
        max(minimum_dimension * 0.004, 1.5),
    )
    supported = np.zeros(len(distances), dtype=bool)
    for normal_offset in normal_offsets:
        sample_x = np.rint(
            intersection[0] + distances * dx + normal_offset * normal_x
        ).astype(int)
        sample_y = np.rint(
            intersection[1] + distances * dy + normal_offset * normal_y
        ).astype(int)
        valid = (
            (sample_x >= 0)
            & (sample_x < image_width)
            & (sample_y >= 0)
            & (sample_y < image_height)
        )
        inside = np.zeros(len(distances), dtype=bool)
        inside[valid] = region.mask[sample_y[valid], sample_x[valid]] > 0
        supported |= inside
    interior_support = float(np.mean(supported))
    if interior_support < 0.72:
        return 0.0

    distance_quality = max(1.0 - join_distance / max(bridge_limit, 1e-6), 0.0)
    outline_quality = max(
        1.0 - outline_distance / max(outline_tolerance, 1e-6),
        0.0,
    )
    return min(
        max(
            0.45 * distance_quality
            + 0.35 * interior_support
            + 0.20 * outline_quality,
            0.0,
        ),
        1.0,
    )


def _longest_supported_run(row: np.ndarray) -> tuple[int, int] | None:
    supported_indices = np.flatnonzero(row)
    if not len(supported_indices):
        return None
    splits = np.flatnonzero(np.diff(supported_indices) > 1) + 1
    groups = np.split(supported_indices, splits)
    selected = max(
        groups,
        key=lambda group: (
            int(group[-1] - group[0] + 1),
            -int(group[0]),
        ),
    )
    return int(selected[0]), int(selected[-1])


def _slope_segment_from_aligned_span(
    *,
    slope: float,
    center_intercept: float,
    start_x: float,
    end_x: float,
    image_width: int,
    image_height: int,
) -> LineSegment | None:
    center_x = (image_width - 1.0) / 2.0
    start_x = min(max(start_x, 0.0), image_width - 1.0)
    end_x = min(max(end_x, 0.0), image_width - 1.0)
    start_y = slope * (start_x - center_x) + center_intercept
    end_y = slope * (end_x - center_x) + center_intercept
    if not (
        0.0 <= start_y <= image_height - 1.0
        and 0.0 <= end_y <= image_height - 1.0
    ):
        return None
    if math.hypot(end_x - start_x, end_y - start_y) < image_width * 0.30:
        return None
    return (start_x, start_y, end_x, end_y)


def _average_longitudinal_line_separation(
    first: LineSegment,
    second: LineSegment,
    *,
    image_width: int,
) -> float:
    overlap_start = max(
        min(first[0], first[2]),
        min(second[0], second[2]),
    )
    overlap_end = min(
        max(first[0], first[2]),
        max(second[0], second[2]),
    )
    if overlap_end - overlap_start < image_width * 0.08:
        return math.inf
    sample_x = np.linspace(overlap_start, overlap_end, 5, dtype=np.float64)

    def y_at_x(line: LineSegment) -> np.ndarray:
        dx = line[2] - line[0]
        if abs(dx) <= 1e-9:
            return np.full_like(sample_x, (line[1] + line[3]) / 2.0)
        return line[1] + (sample_x - line[0]) * (line[3] - line[1]) / dx

    return float(np.mean(np.abs(y_at_x(first) - y_at_x(second))))


def _matched_surface_boundary_quality(
    line: LineSegment,
    candidates: list[tuple[LineSegment, float]],
    *,
    position_tolerance: float,
    angle_tolerance_degrees: float,
) -> float:
    if not candidates:
        return 0.0
    line_direction = _oriented_line_direction(line)
    line_center = (
        (line[0] + line[2]) / 2.0,
        (line[1] + line[3]) / 2.0,
    )
    angle_tolerance = math.radians(max(angle_tolerance_degrees, 0.1))
    return max(
        (
            quality
            for candidate, quality in candidates
            if _undirected_angle_difference(
                line_direction,
                _oriented_line_direction(candidate),
            )
            <= angle_tolerance
            and _point_to_line_distance(line_center, candidate)
            <= position_tolerance
        ),
        default=0.0,
    )


def _merge_axis_line_segments(
    lines: list[LineSegment],
    *,
    axis: Literal["vertical", "horizontal"],
    position_tolerance: float,
    gap_tolerance: float,
) -> list[LineSegment]:
    if not lines:
        return []

    # Connected-component clustering preserves the previous transitive merge
    # semantics without repeatedly scanning and removing from a Python list.
    # The old implementation became effectively cubic for noisy Hough output.
    parents = list(range(len(lines)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first_index: int, second_index: int) -> None:
        first_root = find(first_index)
        second_root = find(second_index)
        if first_root != second_root:
            parents[second_root] = first_root

    def axis_position(line: LineSegment) -> float:
        if axis == "vertical":
            return (line[0] + line[2]) / 2.0
        return (line[1] + line[3]) / 2.0

    ordered_indices = sorted(range(len(lines)), key=lambda index: axis_position(lines[index]))
    for ordered_offset, first_index in enumerate(ordered_indices):
        first = lines[first_index]
        first_position = axis_position(first)
        for second_offset in range(ordered_offset + 1, len(ordered_indices)):
            second_index = ordered_indices[second_offset]
            if axis_position(lines[second_index]) - first_position > position_tolerance:
                break
            if _axis_segments_are_connected(
                first,
                lines[second_index],
                axis=axis,
                position_tolerance=position_tolerance,
                gap_tolerance=gap_tolerance,
            ):
                union(first_index, second_index)

    clusters_by_root: dict[int, list[LineSegment]] = {}
    for index, line in enumerate(lines):
        clusters_by_root.setdefault(find(index), []).append(line)
    clusters = list(clusters_by_root.values())
    return [_fit_axis_line(cluster, axis=axis) for cluster in clusters]


def _merge_free_oriented_line_segments(
    lines: list[LineSegment],
    *,
    position_tolerance: float,
    gap_tolerance: float,
    angle_tolerance_degrees: float,
) -> list[LineSegment]:
    if not lines:
        return []
    parents = list(range(len(lines)))
    angle_tolerance = math.radians(max(angle_tolerance_degrees, 0.1))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parents[second_root] = first_root

    directions = [_oriented_line_direction(line) for line in lines]
    orientation_angles = [
        math.atan2(direction[1], direction[0]) % math.pi
        for direction in directions
    ]
    centers = [
        (
            (line[0] + line[2]) / 2.0,
            (line[1] + line[3]) / 2.0,
        )
        for line in lines
    ]
    for first_index, first in enumerate(lines):
        first_direction = directions[first_index]
        first_center = centers[first_index]
        first_angle = orientation_angles[first_index]
        for second_index in range(first_index + 1, len(lines)):
            second = lines[second_index]
            angle_difference = abs(
                first_angle - orientation_angles[second_index]
            )
            angle_difference = min(
                angle_difference,
                math.pi - angle_difference,
            )
            if angle_difference > angle_tolerance:
                continue
            second_direction = directions[second_index]
            if (
                first_direction[0] * second_direction[0]
                + first_direction[1] * second_direction[1]
                < 0.0
            ):
                second_direction = (
                    -second_direction[0],
                    -second_direction[1],
                )
            combined_x = first_direction[0] + second_direction[0]
            combined_y = first_direction[1] + second_direction[1]
            combined_length = max(
                math.hypot(combined_x, combined_y),
                1e-12,
            )
            direction = (
                combined_x / combined_length,
                combined_y / combined_length,
            )
            normal = (-direction[1], direction[0])
            second_center = centers[second_index]
            position_difference = abs(
                (second_center[0] - first_center[0]) * normal[0]
                + (second_center[1] - first_center[1]) * normal[1]
            )
            if position_difference > position_tolerance:
                continue

            first_start = first[0] * direction[0] + first[1] * direction[1]
            first_end = first[2] * direction[0] + first[3] * direction[1]
            second_start = (
                second[0] * direction[0] + second[1] * direction[1]
            )
            second_end = (
                second[2] * direction[0] + second[3] * direction[1]
            )
            first_interval = (
                min(first_start, first_end),
                max(first_start, first_end),
            )
            second_interval = (
                min(second_start, second_end),
                max(second_start, second_end),
            )
            interval_gap = max(
                first_interval[0] - second_interval[1],
                second_interval[0] - first_interval[1],
                0.0,
            )
            if interval_gap <= gap_tolerance:
                union(first_index, second_index)

    clusters: dict[int, list[LineSegment]] = {}
    for index, line in enumerate(lines):
        clusters.setdefault(find(index), []).append(line)
    return [
        _fit_oriented_line(
            cluster,
            reference_direction=_oriented_line_direction(cluster[0]),
        )
        for cluster in clusters.values()
    ]


def _refine_lines_from_edge_support(
    lines: list[LineSegment],
    *,
    edge_points: np.ndarray,
    edge_gradients: np.ndarray,
    band_tolerance: float,
    extension_tolerance: float,
    support_gap: float,
) -> list[tuple[LineSegment, float]]:
    if not lines or edge_points.size == 0:
        return []

    def longest_projection_group(
        points: np.ndarray,
        projections: np.ndarray,
    ) -> np.ndarray:
        if len(points) <= 1:
            return points
        order = np.argsort(projections)
        ordered_projections = projections[order]
        split_at = np.flatnonzero(
            np.diff(ordered_projections) > max(support_gap, 1.0)
        ) + 1
        groups = np.split(order, split_at)
        selected_indices = max(
            groups,
            key=lambda indices: (
                float(np.max(projections[indices]) - np.min(projections[indices]))
                if len(indices) > 1
                else 0.0,
                len(indices),
            ),
        )
        return points[selected_indices]

    refined: list[tuple[LineSegment, float]] = []
    gradient_lengths = np.linalg.norm(edge_gradients, axis=1)
    for line in lines:
        reference_direction = np.asarray(
            _oriented_line_direction(line),
            dtype=np.float64,
        )
        reference_normal = np.asarray(
            (-reference_direction[1], reference_direction[0]),
            dtype=np.float64,
        )
        center = np.asarray(
            ((line[0] + line[2]) / 2.0, (line[1] + line[3]) / 2.0),
            dtype=np.float64,
        )
        relative = edge_points - center
        projections = relative @ reference_direction
        perpendicular_offsets = np.abs(relative @ reference_normal)
        half_length = _line_length(line) / 2.0
        gradient_alignment = np.divide(
            np.abs(edge_gradients @ reference_normal),
            gradient_lengths,
            out=np.zeros_like(gradient_lengths),
            where=gradient_lengths > 1e-9,
        )
        nearby_mask = (
            (perpendicular_offsets <= band_tolerance)
            & (projections >= -half_length - extension_tolerance)
            & (projections <= half_length + extension_tolerance)
            & (gradient_alignment >= 0.65)
        )
        nearby_points = edge_points[nearby_mask]
        nearby_projections = projections[nearby_mask]
        if len(nearby_points) < 8:
            continue
        supported_points = longest_projection_group(
            nearby_points,
            nearby_projections,
        )
        if len(supported_points) < 8:
            continue

        fit_center = supported_points.mean(axis=0)
        centered = supported_points - fit_center
        covariance = centered.T @ centered
        eigenvalues, eigenvectors = np.linalg.eigh(covariance)
        fit_direction = eigenvectors[:, int(np.argmax(eigenvalues))]
        if float(fit_direction @ reference_direction) < 0:
            fit_direction = -fit_direction
        fit_normal = np.asarray(
            (-fit_direction[1], fit_direction[0]),
            dtype=np.float64,
        )
        fit_offsets = np.abs((supported_points - fit_center) @ fit_normal)
        inlier_points = supported_points[fit_offsets <= band_tolerance]
        if len(inlier_points) < 8:
            continue
        inlier_projections = (inlier_points - fit_center) @ fit_direction
        inlier_points = longest_projection_group(
            inlier_points,
            inlier_projections,
        )
        if len(inlier_points) < 8:
            continue

        fit_center = inlier_points.mean(axis=0)
        final_projections = (inlier_points - fit_center) @ fit_direction
        projection_min = float(np.min(final_projections))
        projection_max = float(np.max(final_projections))
        span = projection_max - projection_min
        if span <= 1.0:
            continue
        start = fit_center + fit_direction * projection_min
        end = fit_center + fit_direction * projection_max
        occupied_bins = len(
            np.unique(np.floor(final_projections - projection_min).astype(np.int32))
        )
        support_ratio = min(occupied_bins / max(span, 1.0), 1.0)
        refined.append(
            (
                (
                    float(start[0]),
                    float(start[1]),
                    float(end[0]),
                    float(end[1]),
                ),
                float(support_ratio),
            )
        )
    return refined


def _select_longest_line_segments(
    lines: list[LineSegment],
    limit: int,
) -> list[LineSegment]:
    if len(lines) <= limit:
        return lines
    return sorted(
        lines,
        key=lambda line: (
            -((line[2] - line[0]) ** 2 + (line[3] - line[1]) ** 2),
            line,
        ),
    )[:limit]


def _rescale_board_end_detection(
    detection: _BoardEndDetection,
    *,
    processing_scale: float,
    source_width: int,
    source_height: int,
    processing_width: int,
    processing_height: int,
) -> _BoardEndDetection:
    inverse_scale = 1.0 / processing_scale
    source = dict(detection.source)

    def scale_point(values: list[float | int]) -> list[float]:
        return [float(value) * inverse_scale for value in values]

    for key in ("start_edge", "side_edge", "origin"):
        values = source.get(key)
        if isinstance(values, list):
            source[key] = scale_point(values)
    for key in (
        "end_candidates",
        "longitudinal_candidates",
        "calibration_candidates",
        "validated_surface_edges",
    ):
        values = source.get(key)
        if isinstance(values, list):
            source[key] = [
                scale_point(line) if isinstance(line, list) else line
                for line in values
            ]
    board_edges = source.get("board_edges")
    if isinstance(board_edges, dict):
        scaled_board_edges = dict(board_edges)
        for key in ("end", "longitudinal_reference", "corner"):
            values = scaled_board_edges.get(key)
            if isinstance(values, list):
                scaled_board_edges[key] = scale_point(values)
        visible = scaled_board_edges.get("visible_longitudinal")
        if isinstance(visible, list):
            scaled_board_edges["visible_longitudinal"] = [
                scale_point(line) if isinstance(line, list) else line
                for line in visible
            ]
        source["board_edges"] = scaled_board_edges
    color_outline = source.get("board_color_region_outline")
    if isinstance(color_outline, list):
        source["board_color_region_outline"] = [
            scale_point(point) if isinstance(point, list) else point
            for point in color_outline
        ]
    surface_boundaries = source.get("surface_boundary_candidates")
    if isinstance(surface_boundaries, list):
        source["surface_boundary_candidates"] = [
            {
                **candidate,
                "line": scale_point(candidate["line"]),
            }
            if isinstance(candidate, dict) and isinstance(candidate.get("line"), list)
            else candidate
            for candidate in surface_boundaries
        ]
    for key in (
        "join_distance_px",
        "nearest_join_distance_px",
        "maximum_reference_join_gap_px",
        "maximum_region_recovered_join_gap_px",
        "greatest_directional_span_px",
        "required_directional_span_px",
        "configured_hough_line_gap_px",
        "effective_hough_line_gap_px",
    ):
        if isinstance(source.get(key), int | float):
            source[key] = float(source[key]) * inverse_scale
    source.update(
        {
            "processing_scale": processing_scale,
            "processing_width": processing_width,
            "processing_height": processing_height,
            "source_width": source_width,
            "source_height": source_height,
        }
    )
    return _BoardEndDetection(
        origin=(
            detection.origin[0] * inverse_scale,
            detection.origin[1] * inverse_scale,
        ),
        longitudinal=detection.longitudinal,
        confidence=detection.confidence,
        source=source,
    )


def _axis_segments_are_connected(
    first: LineSegment,
    second: LineSegment,
    *,
    axis: Literal["vertical", "horizontal"],
    position_tolerance: float,
    gap_tolerance: float,
) -> bool:
    if axis == "vertical":
        first_position = (first[0] + first[2]) / 2.0
        second_position = (second[0] + second[2]) / 2.0
        first_interval = sorted((first[1], first[3]))
        second_interval = sorted((second[1], second[3]))
    else:
        first_position = (first[1] + first[3]) / 2.0
        second_position = (second[1] + second[3]) / 2.0
        first_interval = sorted((first[0], first[2]))
        second_interval = sorted((second[0], second[2]))
    interval_gap = max(
        first_interval[0] - second_interval[1],
        second_interval[0] - first_interval[1],
        0.0,
    )
    return (
        abs(first_position - second_position) <= position_tolerance
        and interval_gap <= gap_tolerance
    )


def _fit_axis_line(
    lines: list[LineSegment],
    *,
    axis: Literal["vertical", "horizontal"],
) -> LineSegment:
    points = np.asarray(
        [(line[0], line[1]) for line in lines] + [(line[2], line[3]) for line in lines],
        dtype=np.float64,
    )
    if axis == "vertical":
        independent = points[:, 1]
        dependent = points[:, 0]
    else:
        independent = points[:, 0]
        dependent = points[:, 1]
    low = float(np.min(independent))
    high = float(np.max(independent))
    if high - low <= 1e-9:
        fitted_low = fitted_high = float(np.mean(dependent))
    else:
        matrix = np.column_stack((independent, np.ones_like(independent)))
        slope, intercept = np.linalg.lstsq(matrix, dependent, rcond=None)[0]
        fitted_low = float(slope * low + intercept)
        fitted_high = float(slope * high + intercept)
    if axis == "vertical":
        return (fitted_low, low, fitted_high, high)
    return (low, fitted_low, high, fitted_high)


def _line_intersection(
    first: LineSegment,
    second: LineSegment,
) -> tuple[float, float] | None:
    x1, y1, x2, y2 = first
    x3, y3, x4, y4 = second
    denominator = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denominator) <= 1e-9:
        return None
    determinant_first = x1 * y2 - y1 * x2
    determinant_second = x3 * y4 - y3 * x4
    return (
        (determinant_first * (x3 - x4) - (x1 - x2) * determinant_second) / denominator,
        (determinant_first * (y3 - y4) - (y1 - y2) * determinant_second) / denominator,
    )


def _line_projection_fraction(
    line: LineSegment,
    point: tuple[float, float],
) -> float:
    dx = line[2] - line[0]
    dy = line[3] - line[1]
    length_squared = dx * dx + dy * dy
    if length_squared <= 1e-9:
        return 0.0
    return ((point[0] - line[0]) * dx + (point[1] - line[1]) * dy) / length_squared


def _point_to_segment_distance(
    point: tuple[float, float],
    line: LineSegment,
) -> float:
    fraction = max(min(_line_projection_fraction(line, point), 1.0), 0.0)
    closest = (
        line[0] + (line[2] - line[0]) * fraction,
        line[1] + (line[3] - line[1]) * fraction,
    )
    return math.dist(point, closest)


def _point_to_line_distance(
    point: tuple[float, float],
    line: LineSegment,
) -> float:
    dx = line[2] - line[0]
    dy = line[3] - line[1]
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return math.inf
    return abs(
        dy * point[0]
        - dx * point[1]
        + line[2] * line[1]
        - line[3] * line[0]
    ) / length


def _board_reference_edges(
    coordinate: InspectionCoordinateFrame,
) -> ReferenceEdgesPayload:
    source = coordinate.source
    if not bool(source.get("found")):
        return ReferenceEdgesPayload(
            edges=[],
            image_context_id=coordinate.image_context_id,
            metadata={
                "source": "board_end_and_visible_surfaces",
                "found": False,
                "reason_code": source.get("reason_code"),
            },
        )

    start_edge = source.get("start_edge")
    side_edge = source.get("side_edge")
    surface_edges = source.get("validated_surface_edges")
    lines: list[tuple[str, str, list[float]]] = []
    if isinstance(start_edge, list) and len(start_edge) >= 4:
        lines.append(("board_end", "start_edge", start_edge))
    if isinstance(side_edge, list) and len(side_edge) >= 4:
        lines.append(("longitudinal_reference", "side_edge", side_edge))
    if isinstance(surface_edges, list):
        for index, line in enumerate(surface_edges):
            if not isinstance(line, list) or len(line) < 4:
                continue
            if isinstance(side_edge, list) and line[:4] == side_edge[:4]:
                continue
            lines.append(
                (
                    f"visible_surface_{index + 1}",
                    "custom",
                    line,
                )
            )

    edges = [
        ReferenceEdge(
            edge_id=f"{coordinate.frame_id}_{edge_name}"[:160],
            image_context_id=coordinate.image_context_id,
            line=LineGeometry(
                start=InspectionPoint(x=float(line[0]), y=float(line[1])),
                end=InspectionPoint(x=float(line[2]), y=float(line[3])),
            ),
            confidence=coordinate.confidence,
            role=role,  # type: ignore[arg-type]
            source={
                "source": "board_end_and_visible_surfaces",
                "edge_name": edge_name,
                "corner_association": source.get("corner_association"),
                "surface_boundary_consensus_count": source.get(
                    "surface_boundary_consensus_count"
                ),
            },
        )
        for edge_name, role, line in lines
    ]
    return ReferenceEdgesPayload(
        edges=edges,
        image_context_id=coordinate.image_context_id,
        metadata={
            "source": "board_end_and_visible_surfaces",
            "found": True,
            "edge_count": len(edges),
            "board_end_confidence": coordinate.confidence,
        },
    )


def _board_end_debug_overlay(coordinate: InspectionCoordinateFrame) -> OverlayPayload:
    source = coordinate.source
    primitives: list[OverlayPrimitive] = []
    image_context_id = coordinate.image_context_id
    preliminary = isinstance(source.get("calibration_candidates"), list)
    color_outline = source.get("board_color_region_outline")
    if isinstance(color_outline, list) and len(color_outline) >= 3:
        outline_points = [
            point
            for point in color_outline
            if isinstance(point, list) and len(point) >= 2
        ]
        for start, end in zip(
            outline_points,
            [*outline_points[1:], outline_points[0]],
            strict=True,
        ):
            primitives.append(
                OverlayPrimitive(
                    primitive_type="line",
                    layer="board_color_region",
                    label=None,
                    confidence=float(
                        source.get("board_color_region_confidence") or 0.0
                    ),
                    geometry={
                        "start": {"x": start[0], "y": start[1]},
                        "end": {"x": end[0], "y": end[1]},
                        "coordinate_system": "pixel_xy",
                    },
                    image_context_id=image_context_id,
                    style={
                        "stroke": "#7c4dff",
                        "stroke_width": 2.0,
                        "stroke_opacity": 0.9,
                    },
                    metadata={
                        "method": "adaptive_board_color_region",
                    },
                )
            )
    surface_boundaries = source.get("surface_boundary_candidates")
    if isinstance(surface_boundaries, list):
        for candidate in surface_boundaries[:20]:
            if not isinstance(candidate, dict):
                continue
            line = candidate.get("line")
            if not isinstance(line, list) or len(line) < 4:
                continue
            quality = float(candidate.get("quality") or 0.0)
            primitives.append(
                OverlayPrimitive(
                    primitive_type="line",
                    layer="surface_boundary_candidates",
                    label=None,
                    confidence=quality,
                    geometry={
                        "start": {"x": line[0], "y": line[1]},
                        "end": {"x": line[2], "y": line[3]},
                        "coordinate_system": "pixel_xy",
                    },
                    image_context_id=image_context_id,
                    style={
                        "stroke": "#00b8d4",
                        "stroke_width": 1.5,
                        "stroke_opacity": 0.35 + 0.55 * quality,
                    },
                    metadata={
                        "surface_boundary_quality": quality,
                        "method": "cross_surface_contrast_projection",
                    },
                )
            )
    for layer, candidates in (
        ("board_end_candidates", source.get("end_candidates")),
        ("board_axis_candidates", source.get("longitudinal_candidates")),
    ):
        if not isinstance(candidates, list):
            continue
        for candidate in candidates[:20]:
            if not isinstance(candidate, list) or len(candidate) < 4:
                continue
            primitives.append(
                OverlayPrimitive(
                    primitive_type="line",
                    layer=layer,
                    label=None,
                    geometry={
                        "start": {"x": candidate[0], "y": candidate[1]},
                        "end": {"x": candidate[2], "y": candidate[3]},
                        "coordinate_system": "pixel_xy",
                    },
                    image_context_id=image_context_id,
                    style={"stroke": "#8b98a1", "stroke_width": 1.0},
                )
            )
    start_edge = source.get("start_edge")
    side_edge = source.get("side_edge")
    if isinstance(start_edge, list) and len(start_edge) >= 4:
        primitives.append(
            OverlayPrimitive(
                primitive_type="line",
                layer="board_end",
                label=(
                    "Preliminary board-end candidate"
                    if preliminary
                    else "Board end"
                ),
                confidence=coordinate.confidence,
                geometry={
                    "start": {"x": start_edge[0], "y": start_edge[1]},
                    "end": {"x": start_edge[2], "y": start_edge[3]},
                    "coordinate_system": "pixel_xy",
                },
                image_context_id=image_context_id,
                style={
                    "stroke": "#ffab40" if preliminary else "#00e5ff",
                    "stroke_width": 2.0 if preliminary else 3.0,
                },
                metadata=source,
            )
        )
    if isinstance(side_edge, list) and len(side_edge) >= 4:
        primitives.append(
            OverlayPrimitive(
                primitive_type="line",
                layer="board_axis_reference",
                label=(
                    "Preliminary longitudinal candidate"
                    if preliminary
                    else "Longitudinal reference"
                ),
                confidence=coordinate.confidence,
                geometry={
                    "start": {"x": side_edge[0], "y": side_edge[1]},
                    "end": {"x": side_edge[2], "y": side_edge[3]},
                    "coordinate_system": "pixel_xy",
                },
                image_context_id=image_context_id,
                style={
                    "stroke": "#ffab40" if preliminary else "#49c47a",
                    "stroke_width": 2.0 if preliminary else 2.5,
                },
                metadata=source,
            )
        )
    if bool(source.get("found")):
        axis_length = max(float(coordinate.metadata.get("image_width") or 1) * 0.2, 40.0)
        primitives.append(
            OverlayPrimitive(
                primitive_type="arrow",
                layer="board_longitudinal_axis",
                label=(
                    "Preliminary board direction"
                    if preliminary
                    else "Positive board direction"
                ),
                confidence=coordinate.confidence,
                geometry={
                    "start": {"x": coordinate.origin.x, "y": coordinate.origin.y},
                    "end": {
                        "x": coordinate.origin.x + coordinate.longitudinal_axis.x * axis_length,
                        "y": coordinate.origin.y + coordinate.longitudinal_axis.y * axis_length,
                    },
                    "coordinate_system": "pixel_xy",
                },
                image_context_id=image_context_id,
                style={
                    "stroke": "#ffab40" if preliminary else "#49c47a",
                    "stroke_width": 2.0 if preliminary else 3.0,
                },
                metadata=source,
            )
        )
    else:
        reason = str(source.get("reason_code") or "board_end_detection_failed")
        primitives.append(
            OverlayPrimitive(
                primitive_type="line",
                layer="board_geometry_warning",
                label=f"Board end not found: {reason}",
                confidence=0.0,
                geometry={
                    "start": {"x": 12, "y": 24},
                    "end": {"x": 80, "y": 24},
                    "coordinate_system": "pixel_xy",
                },
                image_context_id=image_context_id,
                style={"stroke": "#ff5252", "stroke_width": 3.0, "ttl_ms": 3000},
                metadata={**source, "notification_cooldown_ms": 3000},
            )
        )
    return OverlayPayload(
        primitives=primitives,
        display_context_id=image_context_id,
    )


def _line_length(line: tuple[float, ...]) -> float:
    return math.hypot(line[2] - line[0], line[3] - line[1])


source_synchronized_camera_frames = SynchronizedCameraFramesHandler()
source_single_board_camera_test = SingleBoardCameraTestSourceHandler()
analysis_board_end_coordinate_frame = BoardEndCoordinateFrameHandler()
analysis_board_view_cut_constraints = BoardViewCutConstraintsHandler()
analysis_end_face_cut_constraints = EndFaceCutConstraintsHandler()
analysis_multi_view_board_cut_fusion = MultiViewBoardCutFusionHandler()
analysis_single_view_board_cut_decision = SingleViewBoardCutDecisionHandler()
