from __future__ import annotations

import base64
import io
import math
from typing import Any, Literal

from cv_contracts import (
    AnomalyHeatmap,
    AnomalyMask,
    ClassificationLabelScore,
    DetectionBox,
    DetectionPrediction,
    RuntimeStemRotationViewModel,
    SegmentationPrediction,
)
from cv_contracts.bark import BarkSideMeasurement, EndfaceReference, StemCsvMeasurement
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)


class RuntimePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload_type: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProvenanceLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: Literal[
        "image_context",
        "object",
        "roi",
        "node",
        "model",
        "operator_config",
    ]
    source_id: str
    relationship: str = Field(min_length=1, max_length=120)
    node_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeImageTransform(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transform_type: Literal[
        "identity",
        "resize",
        "crop",
        "roi_crop",
        "mask",
        "normalize",
        "undistort",
        "perspective_rectify",
        "masked_crop",
        "unknown",
    ] = "identity"
    parameters: dict[str, Any] = Field(default_factory=dict)
    coordinate_projection: Literal[
        "identity",
        "scale",
        "crop_offset",
        "affine",
        "homography",
        "not_projectable",
        "unknown",
    ] = "identity"


class RuntimeImageContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_id: str = Field(min_length=3, max_length=160)
    kind: Literal[
        "original",
        "crop",
        "roi",
        "transformed",
        "masked",
        "processed",
    ] = "original"
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    source_uri: str | None = None
    source_node_key: str | None = None
    source_image_context_id: str | None = None
    parent_image_context_id: str | None = None
    source_object_id: str | None = None
    transform: RuntimeImageTransform = Field(default_factory=RuntimeImageTransform)
    provenance: list[ProvenanceLink] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeObjectReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_id: str = Field(min_length=3, max_length=160)
    object_type: Literal["detection", "segment", "region", "tracked_object", "anomaly"] = (
        "detection"
    )
    source_image_context_id: str
    geometry: dict[str, Any] = Field(default_factory=dict)
    coordinate_system: str = "normalized_xyxy"
    label: str | None = None
    score: float | None = Field(default=None, ge=0, le=1)
    class_id: int | None = None
    track_id: str | None = None
    producer_node_key: str | None = None
    provenance: list[ProvenanceLink] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeObjectAttribute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=160)
    value: float | int | str | bool | dict[str, Any] | list[Any]
    score: float | None = Field(default=None, ge=0, le=1)
    label: str | None = None
    source_node_key: str | None = None
    source_image_context_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeObjectEnrichment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_object_id: str
    source_object_id: str | None = None
    source_image_context_id: str | None = None
    derived_image_context_id: str | None = None
    enrichment_type: Literal[
        "classification",
        "segmentation_measurement",
        "measurement",
        "score",
        "anomaly_score",
        "label",
        "custom",
    ]
    attributes: list[RuntimeObjectAttribute] = Field(default_factory=list)
    producer_node_key: str | None = None
    provenance: list[ProvenanceLink] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FramePayload(RuntimePayload):
    payload_type: Literal["frame"] = "frame"
    image_base64: str
    mime_type: str = "image/jpeg"
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    source_uri: str | None = None
    timestamp: str | None = None
    image_context: RuntimeImageContext | None = None
    _image_bytes_cache: bytes | None = PrivateAttr(default=None)
    _image_object_cache: Any | None = PrivateAttr(default=None)
    _cv_bgr_cache: Any | None = PrivateAttr(default=None)
    _image_format: str = PrivateAttr(default="JPEG")
    _jpeg_quality: int = PrivateAttr(default=90)

    @classmethod
    def from_bytes(
        cls,
        image_bytes: bytes,
        *,
        mime_type: str = "image/jpeg",
        width: int | None = None,
        height: int | None = None,
        source_uri: str | None = None,
        timestamp: str | None = None,
        image_context: RuntimeImageContext | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "FramePayload":
        payload = cls(
            image_base64="",
            mime_type=mime_type,
            width=width,
            height=height,
            source_uri=source_uri,
            timestamp=timestamp,
            image_context=image_context,
            metadata=metadata or {},
        )
        payload._image_bytes_cache = image_bytes
        return payload

    @classmethod
    def from_image(
        cls,
        image: Any,
        *,
        image_format: str = "JPEG",
        mime_type: str = "image/jpeg",
        jpeg_quality: int = 80,
        source_uri: str | None = None,
        timestamp: str | None = None,
        image_context: RuntimeImageContext | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "FramePayload":
        payload = cls(
            image_base64="",
            mime_type=mime_type,
            width=getattr(image, "width", None),
            height=getattr(image, "height", None),
            source_uri=source_uri,
            timestamp=timestamp,
            image_context=image_context,
            metadata=metadata or {},
        )
        payload._image_object_cache = image
        payload._image_format = image_format
        payload._jpeg_quality = max(int(jpeg_quality), 1)
        return payload

    @classmethod
    def from_cv2_frame(
        cls,
        frame: Any,
        *,
        mime_type: str = "image/jpeg",
        jpeg_quality: int = 80,
        source_uri: str | None = None,
        timestamp: str | None = None,
        image_context: RuntimeImageContext | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "FramePayload":
        shape = getattr(frame, "shape", None)
        if not isinstance(shape, tuple) or len(shape) < 2:
            raise ValueError("OpenCV frame must expose a two-dimensional image shape.")
        payload = cls(
            image_base64="",
            mime_type=mime_type,
            width=max(int(shape[1]), 1),
            height=max(int(shape[0]), 1),
            source_uri=source_uri,
            timestamp=timestamp,
            image_context=image_context,
            metadata=metadata or {},
        )
        payload._cv_bgr_cache = frame
        payload._jpeg_quality = min(max(int(jpeg_quality), 1), 100)
        return payload

    def image_bytes(self) -> bytes:
        if self._image_bytes_cache is not None:
            return self._image_bytes_cache
        if self.image_base64:
            self._image_bytes_cache = base64.b64decode(self.image_base64.encode("ascii"))
            return self._image_bytes_cache
        if self._cv_bgr_cache is not None:
            try:
                import cv2
                import numpy as np

                frame = np.asarray(self._cv_bgr_cache)
                if frame.ndim == 2:
                    frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
                elif frame.ndim == 3 and frame.shape[2] == 4:
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                if frame.ndim != 3 or frame.shape[2] != 3:
                    raise ValueError("OpenCV frame must contain one, three, or four channels.")
                success, encoded = cv2.imencode(
                    ".jpg",
                    np.ascontiguousarray(frame),
                    [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality],
                )
                if not success:
                    raise ValueError("OpenCV did not encode the frame.")
                self._image_bytes_cache = encoded.tobytes()
                return self._image_bytes_cache
            except Exception as exc:
                raise RuntimeError("Could not encode the cached OpenCV frame.") from exc
        if self._image_object_cache is not None:
            buffer = io.BytesIO()
            image = self._image_object_cache
            image_format = self._image_format.upper()
            if image_format == "JPEG" and getattr(image, "mode", "RGB") not in {"RGB", "L"}:
                image = image.convert("RGB")
            if image_format == "JPEG":
                image.save(buffer, format=image_format, quality=self._jpeg_quality)
            else:
                image.save(buffer, format=image_format)
            self._image_bytes_cache = buffer.getvalue()
            return self._image_bytes_cache
        raise RuntimeError("Frame payload has no materialized image bytes.")

    def image_base64_value(self) -> str:
        if not self.image_base64:
            self.image_base64 = base64.b64encode(self.image_bytes()).decode("ascii")
        return self.image_base64

    def cached_image(self) -> Any | None:
        return self._image_object_cache

    def cached_cv2_frame(self) -> Any | None:
        return self._cv_bgr_cache

    def jpeg_quality(self) -> int:
        return self._jpeg_quality

    def set_cached_image(self, image: Any) -> None:
        self._image_object_cache = image

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        self.image_base64_value()
        return handler(self)


class SynchronizedCameraFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["upper", "lower", "end_face"]
    channel_id: str
    frame: FramePayload = Field(exclude=True)
    captured_at: str | None = None
    capture_offset_ms: float = Field(default=0, ge=0)


class SynchronizedFrameSetPayload(RuntimePayload):
    payload_type: Literal["synchronized_frame_set"] = "synchronized_frame_set"
    workpiece_id: str
    trigger_id: str | None = None
    frames: list[SynchronizedCameraFrame] = Field(default_factory=list)
    capture_skew_ms: float = Field(default=0, ge=0)
    max_capture_skew_ms: float = Field(default=50, ge=0)
    valid: bool = True
    status: Literal[
        "captured",
        "camera_missing",
        "capture_not_synchronized",
        "capture_timeout",
    ] = "captured"
    reason_code: str | None = None


class TriggerPayload(RuntimePayload):
    payload_type: Literal["trigger"] = "trigger"
    trigger_id: str | None = None
    body: dict[str, Any] = Field(default_factory=dict)


class DetectionPayload(RuntimePayload):
    payload_type: Literal["detections"] = "detections"
    detections: list[DetectionPrediction] = Field(default_factory=list)
    image_context_id: str | None = None
    image_width: int | None = Field(default=None, ge=1)
    image_height: int | None = Field(default=None, ge=1)
    objects: list[RuntimeObjectReference] = Field(default_factory=list)
    route: dict[str, Any] = Field(default_factory=dict)


class ClassificationPayload(RuntimePayload):
    payload_type: Literal["classifications"] = "classifications"
    labels: list[ClassificationLabelScore] = Field(default_factory=list)
    image_context_id: str | None = None
    source_object_id: str | None = None
    enrichments: list[RuntimeObjectEnrichment] = Field(default_factory=list)
    route: dict[str, Any] = Field(default_factory=dict)


class SegmentPayload(RuntimePayload):
    payload_type: Literal["segments"] = "segments"
    segments: list[SegmentationPrediction] = Field(default_factory=list)
    image_width: int | None = Field(default=None, ge=1)
    image_height: int | None = Field(default=None, ge=1)
    image_context_id: str | None = None
    objects: list[RuntimeObjectReference] = Field(default_factory=list)
    route: dict[str, Any] = Field(default_factory=dict)


class StemGeometryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float
    y: float


class StemGeometryReferenceAxis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: StemGeometryPoint
    end: StemGeometryPoint
    direction: StemGeometryPoint
    normal: StemGeometryPoint
    length_px: float = Field(ge=0)


class StemGeometryCrossSectionIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    normalized_s: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=120)
    observed_width_px: float | None = Field(default=None, ge=0)
    expected_width_px: float | None = Field(default=None, ge=0)


class StemGeometryReason(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=500)


class StemGeometryPayload(RuntimePayload):
    """Typed, pixel-domain observation for one selected segmented stem."""

    payload_type: Literal["stem_geometry"] = "stem_geometry"
    schema_version: Literal["stem.geometry.v1"] = "stem.geometry.v1"
    feature_schema_version: Literal["stem.geometry.v1"] = "stem.geometry.v1"
    valid: bool
    track_id: str | None = None
    source_object_id: str | None = None
    source_label: str | None = None
    segmentation_score: float | None = Field(default=None, ge=0, le=1)
    image_context_id: str | None = None
    image_width: int | None = Field(default=None, ge=1)
    image_height: int | None = Field(default=None, ge=1)
    frame_sequence: int | None = Field(default=None, ge=0)
    timestamp: str | None = None
    reference_axis: StemGeometryReferenceAxis | None = None
    visible_endpoints: tuple[StemGeometryPoint, StemGeometryPoint] | None = None
    normalized_s: list[float] = Field(default_factory=list)
    centerline_image_xy: list[StemGeometryPoint] = Field(default_factory=list)
    residual_profile_px: list[float] = Field(default_factory=list)
    residual_profile_normalized: list[float] = Field(default_factory=list)
    residual_valid_mask: list[bool] = Field(default_factory=list)
    width_normalized_s: list[float] = Field(default_factory=list)
    width_profile_px: list[float] = Field(default_factory=list)
    width_valid_mask: list[bool] = Field(default_factory=list)
    signed_curvature: float | None = None
    signed_bow_px: float | None = None
    signed_bow_normalized: float | None = None
    rms_residual_px: float | None = Field(default=None, ge=0)
    max_abs_residual_px: float | None = Field(default=None, ge=0)
    peak_to_peak_px: float | None = Field(default=None, ge=0)
    median_width_px: float | None = Field(default=None, ge=0)
    rms_residual_normalized: float | None = Field(default=None, ge=0)
    visible_fraction: float = Field(default=0, ge=0, le=1)
    valid_cross_section_fraction: float = Field(default=0, ge=0, le=1)
    shape_class: Literal["straight", "c_bow", "s_bow_or_complex", "uncertain"] = "uncertain"
    quality_score: float = Field(default=0, ge=0, le=1)
    quality_flags: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    reasons: list[StemGeometryReason] = Field(default_factory=list)
    rejected_cross_sections: list[StemGeometryCrossSectionIssue] = Field(default_factory=list)
    uncertain_cross_sections: list[StemGeometryCrossSectionIssue] = Field(default_factory=list)
    selected_component_area_px: int = Field(default=0, ge=0)
    meaningful_component_count: int = Field(default=0, ge=0)
    selected_component_fraction: float = Field(default=0, ge=0, le=1)
    segmentation_route: dict[str, Any] = Field(default_factory=dict)
    provenance: list[ProvenanceLink] = Field(default_factory=list)


class StemHorizontalCurvatureProfilePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x_mm: float
    x_normalized: float = Field(ge=0, le=1)
    y_center_mm: float
    y_reference_mm: float
    residual_mm: float
    residual_normalized_length: float
    residual_normalized_width: float | None = None
    weight: float = Field(ge=0, le=1)


class StemHorizontalWidthPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x_mm: float
    x_normalized: float = Field(ge=0, le=1)
    width_mm: float | None = Field(default=None, ge=0)
    width_normalized: float | None = Field(default=None, ge=0)
    valid: bool
    exclusion_reason: str | None = Field(default=None, max_length=120)


class StemHorizontalCommonSegmentDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["INNER_VISIBLE_LENGTH_FRACTION_V1"] = (
        "INNER_VISIBLE_LENGTH_FRACTION_V1"
    )
    x_start_mm: float | None = None
    x_end_mm: float | None = None
    length_mm: float = Field(default=0, ge=0)
    visible_x_start_mm: float | None = None
    visible_x_end_mm: float | None = None
    visible_length_mm: float = Field(default=0, ge=0)
    configured_inner_fraction: float = Field(default=0, ge=0, le=1)
    common_segment_ratio: float = Field(default=0, ge=0, le=1)
    minimum_common_segment_ratio: float = Field(default=0, ge=0, le=1)
    comparable: bool = False


class StemHorizontalCurvaturePayload(RuntimePayload):
    """Metric horizontal projected curvature for one active stem and frame."""

    payload_type: Literal["stem_horizontal_curvature"] = "stem_horizontal_curvature"
    schema_version: Literal["stem.horizontal-curvature.v1"] = (
        "stem.horizontal-curvature.v1"
    )
    algorithm_version: Literal["stem-horizontal-curvature.robust-inner-chord.v1"] = (
        "stem-horizontal-curvature.robust-inner-chord.v1"
    )
    reference_method: Literal["ROBUST_INNER_ENDPOINT_CHORD_V1"] = (
        "ROBUST_INNER_ENDPOINT_CHORD_V1"
    )
    valid: bool
    observation_state: Literal[
        "accepted",
        "outside_curvature_roi",
        "curvature_frozen",
        "insufficient_common_segment",
        "fragmented",
        "invalid",
    ] = "invalid"
    track_id: str | None = None
    source_object_id: str | None = None
    image_context_id: str | None = None
    frame_sequence: int | None = Field(default=None, ge=0)
    timestamp: str | None = None
    calibration_id: str | None = None
    process_calibration_hash: str | None = None
    coordinate_unit: Literal["mm"] = "mm"
    signed_bow_mm: float | None = None
    curvature_rms_mm: float | None = Field(default=None, ge=0)
    curvature_maxabs_mm: float | None = Field(default=None, ge=0)
    curvature_peak_to_peak_mm: float | None = Field(default=None, ge=0)
    signed_bow_normalized_length: float | None = None
    signed_bow_normalized_width: float | None = None
    curvature_rms_normalized_length: float | None = Field(default=None, ge=0)
    curvature_rms_normalized_width: float | None = Field(default=None, ge=0)
    curvature_maxabs_normalized_length: float | None = Field(default=None, ge=0)
    curvature_maxabs_normalized_width: float | None = Field(default=None, ge=0)
    curvature_peak_to_peak_normalized_length: float | None = Field(default=None, ge=0)
    curvature_peak_to_peak_normalized_width: float | None = Field(default=None, ge=0)
    centerline_profile: list[StemHorizontalCurvatureProfilePoint] = Field(
        default_factory=list
    )
    width_profile: list[StemHorizontalWidthPoint] = Field(default_factory=list)
    visible_length_ratio: float = Field(default=0, ge=0, le=1)
    valid_station_ratio: float = Field(default=0, ge=0, le=1)
    common_segment_descriptor: StemHorizontalCommonSegmentDescriptor = Field(
        default_factory=StemHorizontalCommonSegmentDescriptor
    )
    median_width_mm: float | None = Field(default=None, ge=0)
    geometry_confidence: float = Field(default=0, ge=0, le=1)
    bow_shape_hint: Literal[
        "straight",
        "positive_c_bow",
        "negative_c_bow",
        "s_bow",
        "complex",
        "uncertain",
    ] = "uncertain"
    fragment_status: Literal[
        "single_component",
        "minor_noise_removed",
        "multiple_meaningful_fragments",
        "empty",
    ] = "empty"
    meaningful_component_count: int = Field(default=0, ge=0)
    selected_component_fraction: float = Field(default=0, ge=0, le=1)
    curvature_roi_clipped: bool = False
    touches_curvature_roi_boundary: bool = False
    quality_flags: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    provenance: list[ProvenanceLink] = Field(default_factory=list)


class StemRotationTranslationComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x_px: float = 0.0
    y_px: float = 0.0
    magnitude_px: float = Field(default=0.0, ge=0)
    cumulative_x_px: float = 0.0
    cumulative_y_px: float = 0.0
    cumulative_magnitude_px: float = Field(default=0.0, ge=0)
    x_mm: float | None = None
    y_mm: float | None = None
    magnitude_mm: float | None = Field(default=None, ge=0)
    cumulative_x_mm: float | None = None
    cumulative_y_mm: float | None = None
    cumulative_magnitude_mm: float | None = Field(default=None, ge=0)


class StemRotationFeatureQuality(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detected_feature_count: int = Field(default=0, ge=0)
    matched_feature_count: int = Field(default=0, ge=0)
    inlier_feature_count: int = Field(default=0, ge=0)
    inlier_ratio: float = Field(default=0.0, ge=0, le=1)
    median_tracking_error_px: float | None = Field(default=None, ge=0)
    previous_blur_score: float | None = Field(default=None, ge=0)
    current_blur_score: float | None = Field(default=None, ge=0)
    eroded_mask_fraction: float = Field(default=0.0, ge=0, le=1)
    quality_score: float = Field(default=0.0, ge=0, le=1)


class StemRotationPayload(RuntimePayload):
    """Calibrated relative surface-rotation observation for one active stem track."""

    payload_type: Literal["stem_rotation"] = "stem_rotation"
    schema_version: Literal["stem.rotation.v1"] = "stem.rotation.v1"
    valid: bool
    track_id: str | None = None
    source_object_id: str | None = None
    image_context_id: str | None = None
    frame_sequence: int | None = Field(default=None, ge=0)
    timestamp: str | None = None
    delta_rotation_deg: float | None = None
    cumulative_rotation_deg_unwrapped: float | None = None
    rotation_direction: Literal["positive", "negative", "stationary", "unknown"] = "unknown"
    rotation_confidence: float = Field(default=0.0, ge=0, le=1)
    detected_feature_count: int = Field(default=0, ge=0)
    matched_feature_count: int = Field(default=0, ge=0)
    inlier_feature_count: int = Field(default=0, ge=0)
    inlier_ratio: float = Field(default=0.0, ge=0, le=1)
    feature_quality: StemRotationFeatureQuality = Field(default_factory=StemRotationFeatureQuality)
    translation_component: StemRotationTranslationComponent = Field(
        default_factory=StemRotationTranslationComponent
    )
    compensated_axis_rotation_deg: float = 0.0
    trace_discontinuity: bool = False
    trace_generation: int = Field(default=0, ge=0)
    calibration_valid: bool = False
    calibration_id: str | None = None
    calibration_version: str | None = None
    calibration_method: str | None = None
    calibration_hash: str | None = None
    intrinsic_calibration_hash: str | None = None
    rotation_viewpoint_convention: str | None = None
    positive_rotation_direction: str | None = None
    algorithm_version: str
    reason_codes: list[str] = Field(default_factory=list)
    provenance: list[ProvenanceLink] = Field(default_factory=list)


class StemSurfacePhasePayload(RuntimePayload):
    """Raw and, only with a valid profile, calibrated roll phase for one stem."""

    payload_type: Literal["stem_surface_phase"] = "stem_surface_phase"
    schema_version: Literal["stem.surface-phase.v1"] = "stem.surface-phase.v1"
    algorithm_version: Literal[
        "stem.surface-phase.lk-keyframe-relock.v1",
        "stem.surface-phase.cylindrical-high-rate.v2",
    ] = "stem.surface-phase.lk-keyframe-relock.v1"
    phase_estimator_version: Literal[
        "LK_KEYFRAME_RELOCK_V1",
        "CYLINDRICAL_SURFACE_V2",
    ] = "LK_KEYFRAME_RELOCK_V1"
    execution_mode: Literal["SYNCHRONOUS", "HIGH_RATE_SEEDED"] = "SYNCHRONOUS"
    raw_phase_valid: bool = False
    calibrated_phase_valid: bool = False
    phase_locked: bool = False
    track_id: str | None = None
    source_object_id: str | None = None
    image_context_id: str | None = None
    frame_sequence: int | None = Field(default=None, ge=0)
    timestamp: str | None = None
    delta_raw_phase: float | None = None
    cumulative_raw_phase: float | None = None
    last_safe_raw_phase: float | None = None
    delta_phase_deg: float | None = None
    cumulative_phase_deg: float | None = None
    direction: Literal["positive", "negative", "stationary", "unknown"] = "unknown"
    tracking_mode: Literal[
        "INITIALIZING",
        "INCREMENTAL",
        "HOLDING",
        "KEYFRAME_RELOCK",
        "LOST",
        "RESET",
    ] = "LOST"
    relock_status: Literal[
        "NOT_REQUIRED",
        "NOT_ATTEMPTED",
        "RELOCKED",
        "AMBIGUOUS",
        "FAILED",
    ] = "NOT_ATTEMPTED"
    relock_keyframe_id: str | None = None
    keyframe_count: int = Field(default=0, ge=0)
    detected_feature_count: int = Field(default=0, ge=0)
    matched_feature_count: int = Field(default=0, ge=0)
    inlier_feature_count: int = Field(default=0, ge=0)
    inlier_ratio: float = Field(default=0, ge=0, le=1)
    median_tracking_error_px: float | None = Field(default=None, ge=0)
    forward_backward_error_px: float | None = Field(default=None, ge=0)
    feature_span_ratio: float = Field(default=0.0, ge=0, le=1)
    blur_score: float | None = Field(default=None, ge=0)
    translation_component: StemRotationTranslationComponent = Field(
        default_factory=StemRotationTranslationComponent
    )
    compensated_axis_rotation_deg: float = 0.0
    confidence: float = Field(default=0, ge=0, le=1)
    trace_generation: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)
    source_phase_fps: float | None = Field(default=None, ge=0)
    processed_phase_fps: float | None = Field(default=None, ge=0)
    source_frame_gap: int | None = Field(default=None, ge=0)
    seed_age_ms: float | None = Field(default=None, ge=0)
    fast_path_latency_ms: float | None = Field(default=None, ge=0)
    fast_path_dropped_frames: int = Field(default=0, ge=0)
    seed_status: Literal[
        "NOT_USED",
        "WAITING",
        "APPLIED",
        "PROPAGATED",
        "STALE",
        "TRACK_CHANGED",
    ] = "NOT_USED"
    calibration_id: str | None = None
    calibration_version: str | None = None
    calibration_hash: str | None = None
    calibration_fit_model: str | None = None
    calibration_mean_abs_error_deg: float | None = Field(default=None, ge=0)
    calibration_rms_error_deg: float | None = Field(default=None, ge=0)
    calibration_maximum_abs_error_deg: float | None = Field(default=None, ge=0)
    intrinsic_calibration_hash: str | None = None
    process_calibration_hash: str | None = None
    rotation_reference_view: str | None = None
    positive_rotation: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    provenance: list[ProvenanceLink] = Field(default_factory=list)

    @model_validator(mode="after")
    def prohibit_uncalibrated_degrees(self) -> StemSurfacePhasePayload:
        if not self.calibrated_phase_valid and (
            self.delta_phase_deg is not None or self.cumulative_phase_deg is not None
        ):
            raise ValueError("degree phase values require a valid runtime calibration")
        if self.phase_locked and not self.raw_phase_valid:
            raise ValueError("a locked phase must have a valid raw phase")
        return self


class StemSliceGeometryPayload(BaseModel):
    """One y-z ellipse in canonical machine coordinates.

    Invalid stations intentionally carry no fitted center, axes, or angle. This keeps a
    missing measurement visibly different from an estimated physical cross-section.
    """

    model_config = ConfigDict(extra="forbid")

    x_mm: float
    cy_mm: float | None = None
    cz_mm: float | None = None
    a_mm: float | None = Field(default=None, gt=0)
    b_mm: float | None = Field(default=None, gt=0)
    phi_rad: float | None = None
    quality: float = Field(default=0, ge=0, le=1)
    valid: bool = False
    interpolated: bool = False
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_geometry_presence(self) -> StemSliceGeometryPayload:
        geometry = (self.cy_mm, self.cz_mm, self.a_mm, self.b_mm, self.phi_rad)
        if self.valid and any(value is None for value in geometry):
            raise ValueError("valid slice stations require complete fitted geometry")
        if not self.valid and any(value is not None for value in geometry):
            raise ValueError("invalid slice stations must not contain invented geometry")
        if self.valid and self.a_mm is not None and self.b_mm is not None:
            if self.a_mm < self.b_mm:
                raise ValueError("slice axes must satisfy a_mm >= b_mm")
        return self


class StemSliceFitDiagnosticsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x_mm: float
    valid: bool
    observation_count: int = Field(ge=0)
    accepted_observation_count: int = Field(ge=0)
    unique_phase_count: int = Field(ge=0)
    phase_span_deg: float = Field(ge=0)
    phase_coverage_ratio: float = Field(ge=0, le=1)
    center_residual_rms_mm: float | None = Field(default=None, ge=0)
    width_residual_rms_mm: float | None = Field(default=None, ge=0)
    center_condition_number: float | None = Field(default=None, ge=0)
    width_condition_number: float | None = Field(default=None, ge=0)
    center_inlier_weight_ratio: float = Field(default=0, ge=0, le=1)
    width_inlier_weight_ratio: float = Field(default=0, ge=0, le=1)
    mean_observation_quality: float = Field(default=0, ge=0, le=1)
    fit_quality: float = Field(default=0, ge=0, le=1)
    near_circle: bool = False
    phi_informative: bool = False
    neighbor_consistency: float = Field(default=1, ge=0, le=1)
    reason_codes: list[str] = Field(default_factory=list)


class StemProjectionObservationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x_mm: float
    phase_deg: float
    projected_center_mm: float
    visible_width_mm: float = Field(gt=0)
    quality: float = Field(ge=0, le=1)
    frame_sequence: int | None = Field(default=None, ge=0)
    timestamp: str | None = None


class StemSliceModelPayload(RuntimePayload):
    """Versioned adapter from calibrated camera projections to a pure slice model."""

    payload_type: Literal["stem_slice_model"] = "stem_slice_model"
    schema_version: Literal["stem.slice-model.v1"] = "stem.slice-model.v1"
    algorithm_version: Literal[
        "stem-slice-measurement.robust-projection-fit.v1",
        "stem-slice-measurement.robust-projection-fit.v2",
    ] = "stem-slice-measurement.robust-projection-fit.v2"
    preprocessing_version: Literal["stem-slice-preprocessing.robust-grid.v1"] = (
        "stem-slice-preprocessing.robust-grid.v1"
    )
    projection_model: Literal["CALIBRATED_REFERENCE_PLANE_ORTHOGRAPHIC_SUPPORT_V1"] = (
        "CALIBRATED_REFERENCE_PLANE_ORTHOGRAPHIC_SUPPORT_V1"
    )
    coordinate_system: Literal["MACHINE_X_Y_Z_RIGHT_HANDED_V1"] = (
        "MACHINE_X_Y_Z_RIGHT_HANDED_V1"
    )
    coordinate_unit: Literal["mm"] = "mm"
    state: Literal["RESET", "COLLECTING", "READY", "DEGRADED", "FROZEN", "INVALID"]
    valid: bool = False
    frozen: bool = False
    session_id: str | None = None
    track_id: str | None = None
    frame_sequence: int | None = Field(default=None, ge=0)
    timestamp: str | None = None
    current_pose_phase_deg: float | None = None
    rotation_reference_view: str | None = None
    positive_rotation: str | None = None
    trace_generation: int | None = Field(default=None, ge=0)
    process_calibration_hash: str | None = None
    phase_calibration_id: str | None = None
    phase_calibration_hash: str | None = None
    model_hash: str | None = None
    model_quality: float = Field(default=0, ge=0, le=1)
    requested_station_count: int = Field(default=0, ge=0)
    valid_raw_slice_count: int = Field(default=0, ge=0)
    valid_processed_slice_count: int = Field(default=0, ge=0)
    valid_raw_slice_ratio: float = Field(default=0, ge=0, le=1)
    maximum_invalid_gap_mm: float = Field(default=0, ge=0)
    observation_frame_count: int = Field(default=0, ge=0)
    accepted_observation_count: int = Field(default=0, ge=0)
    rejected_observation_frame_count: int = Field(default=0, ge=0)
    raw_slices: list[StemSliceGeometryPayload] = Field(default_factory=list)
    processed_slices: list[StemSliceGeometryPayload] = Field(default_factory=list)
    fit_diagnostics: list[StemSliceFitDiagnosticsPayload] = Field(default_factory=list)
    raw_observations: list[StemProjectionObservationPayload] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    provenance: list[ProvenanceLink] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_model_state(self) -> StemSliceModelPayload:
        if self.valid and (
            self.current_pose_phase_deg is None
            or not self.processed_slices
            or not any(item.valid for item in self.processed_slices)
            or self.rotation_reference_view is None
            or self.positive_rotation is None
        ):
            raise ValueError("valid slice models require a calibrated pose and geometry")
        if self.frozen and not self.valid:
            raise ValueError("only a valid slice model may be frozen")
        if self.model_hash is not None and not self.valid:
            raise ValueError("invalid slice models must not claim a model hash")
        return self


class StemRotationPriorityPayload(RuntimePayload):
    """Resolved priority consumed by the optimizer, independent of its source adapter."""

    payload_type: Literal["stem_rotation_priority"] = "stem_rotation_priority"
    schema_version: Literal["stem.rotation-priority.v1"] = "stem.rotation-priority.v1"
    coordinate_system: Literal["MACHINE_X_Y_Z_RIGHT_HANDED_V1"] = (
        "MACHINE_X_Y_Z_RIGHT_HANDED_V1"
    )
    rotation_axis: Literal["POSITIVE_MACHINE_X"] = "POSITIVE_MACHINE_X"
    priority_source: Literal["FRONTEND", "PLC"] = "FRONTEND"
    priority_mode: Literal[
        "NONE",
        "VORSCHNITT_KRITISCH",
        "NACHSCHNITT_KRITISCH",
    ] = "NONE"
    priority_axis: Literal["NONE", "VERTICAL_Z", "HORIZONTAL_Y"] = "NONE"
    optimization_enable: bool = True
    revision: int = Field(default=0, ge=0)
    valid: bool = False
    updated_at: str | None = None
    updated_by: str | None = None
    reason_code: str = "INVALID_PRIORITY_MODE"


class StemRotationAngleScorePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    angle_deg: float
    score_mm: float | None = None
    limiting_x_mm: float | None = None
    valid: bool
    reason_code: str


class StemRotationReplaySnapshotPayload(BaseModel):
    """Bounded, self-contained optimizer input retained for deterministic replay."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stem.rotation-optimization-replay.v1"] = (
        "stem.rotation-optimization-replay.v1"
    )
    model_schema_version: str
    model_hash: str | None = None
    current_pose_phase_deg: float | None = None
    rotation_reference_view: str | None = None
    positive_rotation: str | None = None
    trace_generation: int | None = Field(default=None, ge=0)
    process_calibration_hash: str | None = None
    phase_calibration_id: str | None = None
    phase_calibration_hash: str | None = None
    raw_slices: list[StemSliceGeometryPayload] = Field(default_factory=list)
    processed_slices: list[StemSliceGeometryPayload] = Field(default_factory=list)
    fit_diagnostics: list[StemSliceFitDiagnosticsPayload] = Field(default_factory=list)
    raw_observations: list[StemProjectionObservationPayload] = Field(default_factory=list)
    model_quality: float = Field(ge=0, le=1)
    model_reason_codes: list[str] = Field(default_factory=list)
    priority_control: dict[str, Any]
    optimizer_config: dict[str, Any]
    rotation_constraints: dict[str, float]


class StemRotationOptimizationPayload(BaseModel):
    """Versioned, fail-closed runtime result for geometric stem rotation optimization."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stem.rotation-optimization.v1"] = (
        "stem.rotation-optimization.v1"
    )
    algorithm_version: str
    geometry_version: str
    preprocessing_version: str
    coordinate_system: Literal["MACHINE_X_Y_Z_RIGHT_HANDED_V1"] = (
        "MACHINE_X_Y_Z_RIGHT_HANDED_V1"
    )
    coordinate_unit: Literal["mm"] = "mm"
    rotation_axis: Literal["POSITIVE_MACHINE_X"] = "POSITIVE_MACHINE_X"
    rotation_reference_view: str | None = None
    positive_rotation: str | None = None
    state: Literal[
        "IDLE",
        "MEASURING",
        "MODEL_BUILDING",
        "OPTIMIZING",
        "RESULT_READY",
        "DONE",
        "APPLIED",
        "INVALID",
        "ERROR",
    ]
    result_ready: bool = False
    result_valid: bool = False
    shadow_mode: Literal[True] = True
    plc_writes_suppressed: Literal[True] = True
    timestamp: str
    stem_id: str | None = None
    session_id: str | None = None
    track_id: str | None = None
    model_hash: str | None = None
    configuration_hash: str
    priority_source: Literal["FRONTEND", "PLC"]
    priority_mode: Literal[
        "NONE",
        "VORSCHNITT_KRITISCH",
        "NACHSCHNITT_KRITISCH",
    ]
    priority_axis: Literal["NONE", "VERTICAL_Z", "HORIZONTAL_Y"]
    priority_revision: int = Field(ge=0)
    optimization_enable: bool
    should_rotate: bool = False
    rotation_delta_deg: float | None = None
    delta_angle_deg: float | None = None
    gain_mm: float | None = None
    model_quality: float = Field(default=0, ge=0, le=1)
    reason_code: str
    reason_codes: list[str] = Field(default_factory=list)
    limiting_x_current_mm: float | None = None
    limiting_x_optimal_mm: float | None = None
    score_current_mm: float | None = None
    score_optimal_mm: float | None = None
    optimal_candidate_angle_deg: float | None = None
    plateau_width_deg: float | None = None
    valid_slice_count: int = Field(default=0, ge=0)
    evaluated_slice_count: int = Field(default=0, ge=0)
    score_curve: list[StemRotationAngleScorePayload] = Field(default_factory=list)
    replay: StemRotationReplaySnapshotPayload
    view_model: RuntimeStemRotationViewModel | None = None

    @model_validator(mode="after")
    def validate_safe_decision(self) -> StemRotationOptimizationPayload:
        if self.rotation_delta_deg != self.delta_angle_deg:
            raise ValueError("rotation_delta_deg and delta_angle_deg must be identical")
        if self.should_rotate and (
            not self.result_ready
            or not self.result_valid
            or self.rotation_delta_deg is None
            or self.gain_mm is None
        ):
            raise ValueError("rotation commands require a ready valid result and geometry gain")
        if not self.result_valid and (
            self.should_rotate
            or self.rotation_delta_deg is not None
            or self.delta_angle_deg is not None
        ):
            raise ValueError("invalid results must not expose a rotation recommendation")
        return self


class StemCurvaturePhaseHarmonicFit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offset: float
    cosine: float
    sine: float
    residual_rms: float = Field(ge=0)
    residual_ratio: float = Field(ge=0)
    condition_number: float = Field(ge=0)
    inlier_weight_ratio: float = Field(ge=0, le=1)
    stable: bool


class StemCurvaturePhaseDiagnosticPoint(BaseModel):
    """One auditable input candidate for the curvature-over-phase fit."""

    model_config = ConfigDict(extra="forbid")

    frame_id: str
    frame_sequence: int | None = Field(default=None, ge=0)
    timestamp: str | None = None
    cumulative_phase_deg: float | None = None
    curvature_rms_mm: float | None = Field(default=None, ge=0)
    signed_bow_mm: float | None = None
    geometry_confidence: float = Field(default=0, ge=0, le=1)
    phase_confidence: float = Field(default=0, ge=0, le=1)
    common_coverage: float = Field(default=0, ge=0, le=1)
    base_weight: float = Field(default=0, ge=0, le=1)
    fit_weight: float = Field(default=0, ge=0, le=1)
    status: Literal["weighted", "downweighted", "ignored"]
    included: bool
    reason_codes: list[str] = Field(default_factory=list)


class StemCurvaturePhaseDiagnosticCurvePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cumulative_phase_deg: float
    fitted_curvature_mm: float = Field(ge=0)


class StemCurvaturePhaseDiagnosticGraph(BaseModel):
    """UI-independent graph contract used by live preview and result history."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stem.curvature-phase-diagnostic.v1"] = (
        "stem.curvature-phase-diagnostic.v1"
    )
    x_axis_label: Literal["cumulative roll phase"] = "cumulative roll phase"
    x_axis_unit: Literal["deg"] = "deg"
    y_axis_label: Literal["horizontal curvature"] = "horizontal curvature"
    y_axis_unit: Literal["mm"] = "mm"
    points: list[StemCurvaturePhaseDiagnosticPoint] = Field(
        default_factory=list, max_length=2000
    )
    fit_curve: list[StemCurvaturePhaseDiagnosticCurvePoint] = Field(
        default_factory=list, max_length=361
    )
    observed_phase_span_deg: float = Field(default=0, ge=0, le=180)
    minimum_phase_deg: float | None = None
    minimum_curvature_mm: float | None = Field(default=None, ge=0)
    maximum_phase_deg: float | None = None
    maximum_curvature_mm: float | None = Field(default=None, ge=0)
    current_phase_deg: float | None = None
    decision_phase_deg: float | None = None
    curvature_frozen: bool = False
    fit_available: bool = False
    reason_codes: list[str] = Field(default_factory=list)


class StemOrientationResultV3(BaseModel):
    """Immutable pre-entry result for the curvature-phase minimum algorithm."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["stem.orientation.v3"] = "stem.orientation.v3"
    contract: Literal["stem.orientation.v3"] = "stem.orientation.v3"
    algorithm: Literal["CURVATURE_PHASE_MINIMUM"] = "CURVATURE_PHASE_MINIMUM"
    algorithm_version: Literal["curvature-phase-minimum.v1"] = (
        "curvature-phase-minimum.v1"
    )
    shadow_mode: bool = False
    plc_writes_suppressed: bool = False
    shadow_evaluation_approved: bool = False
    minimum_principle_validation_id: str | None = None
    minimum_principle_validation_version: str | None = None
    minimum_principle_validation_hash: str | None = None
    minimum_principle_validation_status: Literal[
        "VALIDATION_PASSED", "VALIDATION_FAILED", "INSUFFICIENT_DATA"
    ] | None = None
    session_id: str
    track_id: str
    session_state: Literal["RESULT_READY", "DONE"] = "RESULT_READY"
    decision_ready: Literal[True] = True
    decision: Literal["ROTATE", "NO_ROTATION", "UNCERTAIN"]
    signed_correction_deg: float | None = Field(default=None, ge=-90, lt=90)
    recommended_rotation_deg_signed: float | None = Field(default=None, ge=-90, lt=90)
    recommended_rotation_deg_abs: float | None = Field(default=None, ge=0, le=90)
    direction: Literal["CLOCKWISE", "COUNTER_CLOCKWISE", "NONE", "UNKNOWN"]
    rotation_reference_view: str | None = None
    positive_rotation: str | None = None
    curvature_min_mm: float | None = Field(default=None, ge=0)
    curvature_max_mm: float | None = Field(default=None, ge=0)
    curvature_span_mm: float | None = Field(default=None, ge=0)
    curvature_min_normalized: float | None = Field(default=None, ge=0)
    curvature_max_normalized: float | None = Field(default=None, ge=0)
    target_phase_deg: float | None = Field(default=None, ge=0, lt=180)
    maximum_phase_deg: float | None = Field(default=None, ge=0, lt=180)
    current_phase_deg: float | None = None
    phase_distance_to_target_deg_signed: float | None = Field(
        default=None, ge=-90, lt=90
    )
    observed_rotation_span_deg: float = Field(default=0, ge=0, le=180)
    target_observed: bool = False
    phase_relocked_after_impact: bool = False
    relock_status: str
    fit_residual: float | None = Field(default=None, ge=0)
    fit_residual_mm2: float | None = Field(default=None, ge=0)
    active_stem_confidence: float = Field(default=0, ge=0, le=1)
    geometry_confidence: float = Field(default=0, ge=0, le=1)
    phase_confidence: float = Field(default=0, ge=0, le=1)
    fit_confidence: float = Field(default=0, ge=0, le=1)
    overall_confidence: float = Field(default=0, ge=0, le=1)
    calibration_id: str | None = None
    process_calibration_hash: str | None = None
    rotation_calibration_id: str | None = None
    rotation_calibration_hash: str | None = None
    curvature_algorithm_version: str
    phase_algorithm_version: str
    solver_algorithm_version: str
    observation_count: int = Field(default=0, ge=0)
    common_segment_coverage: float = Field(default=0, ge=0, le=1)
    used_frame_sequences: list[int] = Field(default_factory=list, max_length=2000)
    used_phase_points_deg: list[float] = Field(default_factory=list, max_length=2000)
    fit_parameters: dict[str, Any] = Field(default_factory=dict)
    diagnostic_graph: StemCurvaturePhaseDiagnosticGraph | None = None
    session_started_timestamp: str | None = None
    session_started_frame_sequence: int | None = Field(default=None, ge=0)
    decision_timestamp: str
    decision_frame_sequence: int | None = Field(default=None, ge=0)
    decision_frame: dict[str, Any] = Field(default_factory=dict)
    curvature_freeze_timestamp: str | None = None
    curvature_freeze_frame_sequence: int | None = Field(default=None, ge=0)
    decision_margin_mm: float | None = Field(default=None, ge=0)
    decision_margin_entered: bool = False
    remaining_distance_to_decision_gate_mm: float | None = Field(default=None, ge=0)
    remaining_distance_to_first_roller_mm: float | None = Field(default=None, ge=0)
    remaining_time_to_first_roller_ms: float | None = Field(default=None, ge=0)
    result_expiry: str
    deadline_met: bool
    result_frozen: Literal[True] = True
    reason_codes: list[str] = Field(default_factory=list)
    component_versions: dict[str, str] = Field(default_factory=dict)
    asset_references: dict[str, Any] = Field(
        default_factory=lambda: {"status": "not_recorded"}
    )

    @model_validator(mode="after")
    def validate_decision_angle(self) -> StemOrientationResultV3:
        signed = self.recommended_rotation_deg_signed
        absolute = self.recommended_rotation_deg_abs
        if self.signed_correction_deg != signed:
            raise ValueError("signed correction alias must match the recommended rotation")
        if self.decision == "ROTATE":
            if signed is None or absolute is None or absolute <= 0:
                raise ValueError("ROTATE requires a non-zero signed correction angle")
            if self.direction not in {"CLOCKWISE", "COUNTER_CLOCKWISE"}:
                raise ValueError("ROTATE requires a configured physical direction")
            if not math.isclose(abs(signed), absolute, abs_tol=1.0e-6):
                raise ValueError("absolute correction must match the signed correction")
            if self.target_phase_deg is None or self.current_phase_deg is None:
                raise ValueError("ROTATE requires target and current phase")
        elif self.decision == "NO_ROTATION":
            if signed != 0 or absolute != 0 or self.direction != "NONE":
                raise ValueError("NO_ROTATION requires zero angle and direction NONE")
        elif signed is not None or absolute is not None or self.direction != "UNKNOWN":
            raise ValueError("UNCERTAIN must not publish an angle or direction")
        return self


class StemCurvaturePhaseSolutionPayload(RuntimePayload):
    """Track-local robust fit of curvature over calibrated roll phase."""

    payload_type: Literal["stem_curvature_phase_solution"] = (
        "stem_curvature_phase_solution"
    )
    schema_version: Literal["stem.curvature-phase-solution.v1"] = (
        "stem.curvature-phase-solution.v1"
    )
    algorithm_version: Literal["stem-curvature-phase.robust-harmonic.v1"] = (
        "stem-curvature-phase.robust-harmonic.v1"
    )
    session_id: str | None = None
    track_id: str | None = None
    frame_sequence: int | None = Field(default=None, ge=0)
    timestamp: str | None = None
    current_phase_deg: float | None = None
    observation_count: int = Field(default=0, ge=0)
    accepted_new_observation: bool = False
    observed_phase_span_deg: float = Field(default=0, ge=0, le=180)
    curvature_frozen: bool = False
    decision_gate_reached: bool = False
    session_state: Literal[
        "WAITING",
        "CANDIDATE",
        "OBSERVING_CURVATURE",
        "TRACKING_PHASE",
        "READY_TO_DECIDE",
        "RESULT_READY",
        "TRACKING_TO_TARGET",
        "DONE",
    ] = "WAITING"
    phase_settled: bool = False
    result_frozen: bool = False
    orientation_result: StemOrientationResultV3 | None = None
    state: Literal[
        "RESET",
        "COLLECTING",
        "INSUFFICIENT_ROTATION",
        "FIT_UNSTABLE",
        "AMBIGUOUS",
        "BOW_NOT_RELEVANT",
        "SOLVED",
        "FROZEN",
        "INVALID",
    ] = "COLLECTING"
    observed_min_curvature_mm: float | None = Field(default=None, ge=0)
    observed_min_phase_deg: float | None = Field(default=None, ge=0, lt=180)
    observed_max_curvature_mm: float | None = Field(default=None, ge=0)
    observed_max_phase_deg: float | None = Field(default=None, ge=0, lt=180)
    observed_min_signed_bow_mm: float | None = None
    observed_max_signed_bow_mm: float | None = None
    fitted_min_curvature_mm: float | None = Field(default=None, ge=0)
    fitted_max_curvature_mm: float | None = Field(default=None, ge=0)
    fitted_min_signed_bow_mm: float | None = None
    fitted_max_signed_bow_mm: float | None = None
    curvature_span_mm: float | None = Field(default=None, ge=0)
    target_phase_deg: float | None = Field(default=None, ge=0, lt=180)
    maximum_phase_deg: float | None = Field(default=None, ge=0, lt=180)
    target_phase_source: Literal["observed", "extrapolated", "unavailable"] = (
        "unavailable"
    )
    target_extrapolation_deg: float | None = Field(default=None, ge=0)
    minimum_unique: bool = False
    bow_relevant: bool | None = None
    s_bow_residual_minimum: bool = False
    signed_fit: StemCurvaturePhaseHarmonicFit | None = None
    energy_fit: StemCurvaturePhaseHarmonicFit | None = None
    signed_fit_residual_rms_mm: float | None = Field(default=None, ge=0)
    energy_fit_residual_rms_mm2: float | None = Field(default=None, ge=0)
    diagnostic_graph: StemCurvaturePhaseDiagnosticGraph = Field(
        default_factory=StemCurvaturePhaseDiagnosticGraph
    )
    confidence: float = Field(default=0, ge=0, le=1)
    process_calibration_id: str | None = None
    process_calibration_hash: str | None = None
    phase_calibration_id: str | None = None
    phase_calibration_hash: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    reason_code_history: list[str] = Field(default_factory=list)
    provenance: list[ProvenanceLink] = Field(default_factory=list)

    @model_validator(mode="after")
    def prohibit_ambiguous_target_phase(self) -> StemCurvaturePhaseSolutionPayload:
        has_target = self.target_phase_deg is not None or self.maximum_phase_deg is not None
        if has_target and (
            self.target_phase_deg is None
            or self.maximum_phase_deg is None
            or self.state not in {"SOLVED", "FROZEN"}
            or not self.minimum_unique
            or self.bow_relevant is not True
        ):
            raise ValueError(
                "target and maximum phase require a unique, bow-relevant solved fit"
            )
        if self.target_phase_source == "unavailable" and has_target:
            raise ValueError("an available target phase requires its source classification")
        if self.target_phase_source != "unavailable" and not has_target:
            raise ValueError("target phase source requires target and maximum phase")
        if self.result_frozen != (self.orientation_result is not None):
            raise ValueError("a frozen session result requires its immutable v3 contract")
        if self.orientation_result is not None and self.session_state not in {
            "RESULT_READY",
            "TRACKING_TO_TARGET",
            "DONE",
        }:
            raise ValueError("a v3 result requires a terminal session state")
        return self


class TemporalFeatureFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_index: int = Field(ge=0)
    timestamp_seconds: float = Field(ge=0)
    source_timestamp: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)


class TemporalSequencePayload(RuntimePayload):
    """Bounded causal prefix of structured, pre-scaler temporal features."""

    payload_type: Literal["temporal_sequence"] = "temporal_sequence"
    schema_version: Literal["runtime.temporal-sequence.v1"] = "runtime.temporal-sequence.v1"
    sequence_id: str
    group_id: str | None = None
    track_id: str | None = None
    feature_set_id: str
    feature_schema_version: str
    rotation_schema_version: str
    calibration_schema_version: str
    rotation_calibration_id: str | None = None
    rotation_calibration_hash: str | None = None
    rotation_viewpoint_convention: str
    positive_rotation_direction: str
    frames: list[TemporalFeatureFrame] = Field(default_factory=list, max_length=4096)
    current_frame_index: int | None = Field(default=None, ge=0)
    decision_frame_index: int | None = Field(default=None, ge=0)
    decision_ready: bool = False
    causal: Literal[True] = True
    post_decision_frames_included: Literal[False] = False
    trace_continuous: bool = True
    calibration_valid: bool = True
    reason_codes: list[str] = Field(default_factory=list)
    provenance: list[ProvenanceLink] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_causal_prefix(self) -> TemporalSequencePayload:
        if not self.frames:
            if self.current_frame_index is not None or self.decision_ready:
                raise ValueError("An indexed temporal sequence requires at least one frame.")
            return self
        indices = [item.frame_index for item in self.frames]
        timestamps = [item.timestamp_seconds for item in self.frames]
        if any(
            right <= left for left, right in zip(indices, indices[1:], strict=False)
        ):
            raise ValueError("Temporal frame indices must be strictly increasing.")
        if any(
            right < left for left, right in zip(timestamps, timestamps[1:], strict=False)
        ):
            raise ValueError("Temporal frame timestamps must be monotonically increasing.")
        if self.current_frame_index != indices[-1]:
            raise ValueError("current_frame_index must equal the final causal frame index.")
        if self.decision_frame_index is not None and any(
            index > self.decision_frame_index for index in indices
        ):
            raise ValueError("Temporal input must not contain frames after the decision frame.")
        if self.decision_ready and self.decision_frame_index != self.current_frame_index:
            raise ValueError("A ready temporal decision must freeze at the final causal frame.")
        return self


class TemporalPredictionPayload(RuntimePayload):
    payload_type: Literal["temporal_prediction"] = "temporal_prediction"
    schema_version: Literal["runtime.temporal-prediction.v1"] = (
        "runtime.temporal-prediction.v1"
    )
    valid: bool = False
    sequence_id: str
    track_id: str | None = None
    sequence_length: int = Field(default=0, ge=0)
    last_frame_index: int | None = Field(default=None, ge=0)
    decision_frame_index: int | None = Field(default=None, ge=0)
    feature_set_id: str | None = None
    feature_schema_digest: str | None = None
    scaler_sha256: str | None = None
    snapshot_id: str | None = None
    feature_schema_version: str | None = None
    rotation_schema_version: str | None = None
    calibration_schema_version: str | None = None
    rotation_calibration_id: str | None = None
    rotation_calibration_hash: str | None = None
    rotation_viewpoint_convention: str | None = None
    positive_rotation_direction: str | None = None
    bow_relevance_probability: float | None = Field(default=None, ge=0, le=1)
    target_observed_probability: float | None = Field(default=None, ge=0, le=1)
    angle_sin_2e: float | None = Field(default=None, ge=-1, le=1)
    angle_cos_2e: float | None = Field(default=None, ge=-1, le=1)
    signed_correction_deg: float | None = Field(default=None, ge=-90, lt=90)
    raw_angle_confidence: float | None = Field(default=None, ge=0, le=1)
    angle_confidence: float | None = Field(default=None, ge=0, le=1)
    confidence_calibration_valid: bool = False
    final_quality_probabilities: dict[str, float] = Field(default_factory=dict)
    deadband_deg: float | None = Field(default=None, ge=0, lt=90)
    estimate_method: Literal["tcn", "gru"] | None = None
    model_family_slug: str | None = None
    model_version_id: str | None = None
    deployment_release_id: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    timing: dict[str, Any] = Field(default_factory=dict)
    provenance: list[ProvenanceLink] = Field(default_factory=list)


class AnomalyPayload(RuntimePayload):
    payload_type: Literal["anomaly"] = "anomaly"
    anomaly_score: float = Field(ge=0)
    predicted_label: Literal["normal", "anomalous"]
    is_anomalous: bool
    threshold: float = Field(ge=0, le=1)
    heatmap: AnomalyHeatmap
    mask: AnomalyMask | None = None
    image_width: int = Field(ge=1)
    image_height: int = Field(ge=1)
    image_context_id: str | None = None
    route: dict[str, Any] = Field(default_factory=dict)


class Region(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str | None = None
    label: str | None = None
    score: float | None = Field(default=None, ge=0, le=1)
    box: DetectionBox
    source_image_context_id: str | None = None
    source_object_id: str | None = None
    source_node_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RegionSetPayload(RuntimePayload):
    payload_type: Literal["region_set"] = "region_set"
    regions: list[Region] = Field(default_factory=list)


class RoiPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roi_key: str
    frame: FramePayload
    box: DetectionBox
    label: str | None = None
    source_region_key: str | None = None
    source_image_context_id: str | None = None
    source_object_id: str | None = None
    image_context: RuntimeImageContext | None = None
    provenance: list[ProvenanceLink] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RoiSetPayload(RuntimePayload):
    payload_type: Literal["roi_set"] = "roi_set"
    rois: list[RoiPayload] = Field(default_factory=list)


class PositionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str | None = None
    x: float
    y: float
    box: DetectionBox | None = None
    source_image_context_id: str | None = None
    source_object_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PositionsPayload(RuntimePayload):
    payload_type: Literal["positions"] = "positions"
    positions: list[PositionPayload] = Field(default_factory=list)


class MeasurementPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    value: float | int | str | bool | dict[str, Any]
    unit: str | None = None
    label: str | None = None
    source_image_context_id: str | None = None
    source_object_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MeasurementsPayload(RuntimePayload):
    payload_type: Literal["measurements"] = "measurements"
    measurements: list[MeasurementPayload] = Field(default_factory=list)


class InspectionPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float
    y: float


class InspectionVector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float
    y: float


class LineGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: InspectionPoint
    end: InspectionPoint
    coordinate_system: Literal["pixel_xy"] = "pixel_xy"


class ReferenceEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edge_id: str = Field(min_length=3, max_length=160)
    image_context_id: str
    line: LineGeometry
    confidence: float = Field(ge=0, le=1)
    role: Literal["start_edge", "side_edge", "left_edge", "right_edge", "custom"]
    source: dict[str, Any] = Field(default_factory=dict)
    provenance: list[ProvenanceLink] = Field(default_factory=list)


class ReferenceEdgesPayload(RuntimePayload):
    payload_type: Literal["reference_edges"] = "reference_edges"
    edges: list[ReferenceEdge] = Field(default_factory=list)
    image_context_id: str | None = None


class CvEdgeContour(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contour_id: str = Field(min_length=3, max_length=160)
    points: list[InspectionPoint] = Field(default_factory=list)
    bounding_box: dict[str, float] = Field(default_factory=dict)
    area_px: float = Field(default=0, ge=0)
    length_px: float = Field(default=0, ge=0)
    score: float | None = Field(default=None, ge=0, le=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CvLineCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_id: str = Field(min_length=3, max_length=160)
    line: LineGeometry
    length_px: float = Field(ge=0)
    angle_degrees: float
    score: float = Field(ge=0, le=1)
    method: str = Field(default="hough_line_segments", max_length=120)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CvEdgesPayload(RuntimePayload):
    payload_type: Literal["cv_edges"] = "cv_edges"
    image_context_id: str
    source_image_context_id: str | None = None
    edge_mask_frame: FramePayload | None = None
    contours: list[CvEdgeContour] = Field(default_factory=list)
    line_candidates: list[CvLineCandidate] = Field(default_factory=list)
    edge_pixel_count: int = Field(default=0, ge=0)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    algorithm: str = Field(default="canny", max_length=80)
    provenance: list[ProvenanceLink] = Field(default_factory=list)


class InspectionCoordinateFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_id: str = Field(min_length=3, max_length=160)
    image_context_id: str
    origin: InspectionPoint
    longitudinal_axis: InspectionVector
    transverse_axis: InspectionVector
    confidence: float = Field(ge=0, le=1)
    source: dict[str, Any] = Field(default_factory=dict)
    provenance: list[ProvenanceLink] = Field(default_factory=list)
    inspected_object_id: str | None = None
    camera_id: str | None = None
    view_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CoordinateFramePayload(RuntimePayload):
    payload_type: Literal["coordinate_frame"] = "coordinate_frame"
    frame: InspectionCoordinateFrame


class FeatureMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feature_id: str = Field(min_length=1, max_length=160)
    feature_type: str = Field(min_length=1, max_length=160)
    measurement_name: str = Field(min_length=1, max_length=160)
    value_px: float = Field(ge=0)
    value_mm: float | None = Field(default=None, ge=0)
    basis: Literal[
        "bbox_width",
        "bbox_height",
        "approximate_diameter",
        "polygon_length",
        "mask_area",
        "axis_projected_width",
        "axis_projected_length",
    ]
    method_metadata: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0, le=1)
    source_image_context_id: str | None = None
    source_object_id: str | None = None
    provenance: list[ProvenanceLink] = Field(default_factory=list)


class FeatureMeasurementsPayload(RuntimePayload):
    payload_type: Literal["feature_measurements"] = "feature_measurements"
    measurements: list[FeatureMeasurement] = Field(default_factory=list)
    features: list[dict[str, Any]] = Field(default_factory=list)
    image_context_id: str | None = None


class ClearanceZone(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zone_id: str = Field(min_length=3, max_length=160)
    feature_id: str
    feature_type: str
    interval_start_px: float
    interval_end_px: float
    interval_start_mm: float | None = None
    interval_end_mm: float | None = None
    clearance_px: float = Field(ge=0)
    clearance_mm: float | None = Field(default=None, ge=0)
    rule_metadata: dict[str, Any] = Field(default_factory=dict)
    severity: str = Field(default="info", max_length=80)
    category: str = Field(default="clearance", max_length=80)


class ClearanceZonesPayload(RuntimePayload):
    payload_type: Literal["clearance_zones"] = "clearance_zones"
    zones: list[ClearanceZone] = Field(default_factory=list)
    image_context_id: str | None = None
    coordinate_frame_id: str | None = None


class FreeRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    found: bool
    start_position_px: float | None = None
    end_position_px: float | None = None
    distance_from_origin_px: float | None = None
    start_position_mm: float | None = None
    end_position_mm: float | None = None
    distance_from_origin_mm: float | None = None
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FreeRegionPayload(RuntimePayload):
    payload_type: Literal["free_region"] = "free_region"
    free_region: FreeRegion
    image_context_id: str | None = None
    coordinate_frame_id: str | None = None


class BoardFeatureConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    constraint_id: str = Field(min_length=1, max_length=200)
    view_role: Literal["upper", "lower", "end_face"]
    surface_role: Literal["top", "bottom", "side_a", "side_b", "end_face"]
    feature_id: str = Field(min_length=1, max_length=200)
    class_id: int = Field(ge=0)
    label_snapshot: str
    confidence: float = Field(ge=0, le=1)
    measurement_basis: Literal[
        "max_feret_diameter",
        "equivalent_diameter",
        "longitudinal_extent",
        "transverse_extent",
    ]
    feature_start_mm: float | None = None
    feature_end_mm: float | None = None
    feature_size_mm: float = Field(ge=0)
    clearance_mm: float = Field(ge=0)
    uncertainty_mm: float = Field(ge=0)
    required_cut_mm: float = Field(ge=0)
    clipped: bool = False
    source_image_context_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BoardCutConstraintsPayload(RuntimePayload):
    payload_type: Literal["board_cut_constraints"] = "board_cut_constraints"
    workpiece_id: str
    view_role: Literal["upper", "lower", "end_face"]
    valid: bool = True
    status: Literal[
        "evaluated",
        "board_end_not_found",
        "calibration_invalid",
        "feature_clipped",
        "safe_cut_outside_inspection_range",
    ] = "evaluated"
    reason_code: str | None = None
    inspection_length_mm: float = Field(default=300, gt=0)
    calibrated_limit_mm: float = Field(gt=0)
    calibration_rms_mm: float = Field(default=0, ge=0)
    uncertainty_mm: float = Field(default=0, ge=0)
    constraints: list[BoardFeatureConstraint] = Field(default_factory=list)
    image_context_id: str | None = None


class BoardCutDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["board.cut-decision.v1"] = "board.cut-decision.v1"
    workpiece_id: str
    valid: bool
    status: Literal[
        "no_cut_required",
        "cut_required",
        "camera_missing",
        "capture_not_synchronized",
        "capture_timeout",
        "board_end_not_found",
        "calibration_invalid",
        "feature_clipped",
        "safe_cut_outside_inspection_range",
    ]
    cut_position_mm: float | None = Field(default=None, ge=0)
    longitudinal_shift_mm: float | None = Field(default=None, ge=0)
    inspection_length_mm: float = Field(default=300, gt=0)
    capture_skew_ms: float = Field(default=0, ge=0)
    views_used: list[Literal["upper", "lower", "end_face"]] = Field(default_factory=list)
    uncertainty_mm: float = Field(default=0, ge=0)
    dominant_constraint: BoardFeatureConstraint | None = None
    reason_code: str | None = None


class BoardCutDecisionPayload(RuntimePayload):
    payload_type: Literal["board_cut_decision"] = "board_cut_decision"
    decision: BoardCutDecision


class MeasurementDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["pass", "fail", "warning"]
    result_label: str
    evaluated_rules: list[dict[str, Any]] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    selected_output_values: dict[str, Any] = Field(default_factory=dict)


class QualityDecisionPayload(RuntimePayload):
    payload_type: Literal["quality_decision"] = "quality_decision"
    decision: MeasurementDecision


class EventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: str
    key: str
    payload: dict[str, Any] = Field(default_factory=dict)


class EventsPayload(RuntimePayload):
    payload_type: Literal["events"] = "events"
    events: list[EventPayload] = Field(default_factory=list)


class ResultPayload(RuntimePayload):
    payload_type: Literal["result"] = "result"
    result: dict[str, Any] = Field(default_factory=dict)
    decision: bool | None = None


class SiemensSpsWriteStatusPayload(RuntimePayload):
    payload_type: Literal["siemens_sps_write_status"] = "siemens_sps_write_status"
    target: dict[str, Any] = Field(default_factory=dict)
    writes: list[dict[str, Any]] = Field(default_factory=list)
    enabled: bool = False
    dry_run: bool = True
    write_attempted: bool = False
    success: bool = False
    status: Literal["disabled", "dry_run", "success", "failed", "skipped"] = "dry_run"
    last_error: str | None = None
    stem_orientation_command: dict[str, Any] = Field(default_factory=dict)
    stem_orientation_gate: dict[str, Any] = Field(default_factory=dict)
    phase_control: dict[str, Any] = Field(default_factory=dict)


class CaptureIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capture_mode: Literal[
        "full_frame",
        "roi",
        "conditional",
        "multi_project",
        "temporal_sequence",
    ]
    target_project_ids: list[str] = Field(default_factory=list)
    asset: dict[str, Any] = Field(default_factory=dict)
    condition: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CaptureIntentsPayload(RuntimePayload):
    payload_type: Literal["capture_intents"] = "capture_intents"
    intents: list[CaptureIntent] = Field(default_factory=list)


class OverlayPrimitive(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primitive_type: Literal[
        "box",
        "polygon",
        "point",
        "label",
        "measurement",
        "heatmap",
        "mask",
        "line",
        "axis",
        "zone",
        "arrow",
    ]
    layer: str = "default"
    label: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    geometry: dict[str, Any] = Field(default_factory=dict)
    image_context_id: str | None = None
    display_context_id: str | None = None
    object_id: str | None = None
    source_object_id: str | None = None
    projected_from_context_id: str | None = None
    style: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class OverlayPayload(RuntimePayload):
    payload_type: Literal["overlay"] = "overlay"
    primitives: list[OverlayPrimitive] = Field(default_factory=list)
    display_context_id: str | None = None
    projection_policy: Literal["same_context_only", "lineage_projection_required"] = (
        "same_context_only"
    )


class DisplayOutputPayload(RuntimePayload):
    payload_type: Literal["display_output"] = "display_output"
    display_context_id: str
    display_context: RuntimeImageContext
    frame: FramePayload | None = None
    layer_policy: dict[str, Any] = Field(default_factory=dict)
    object_layer_keys: list[str] = Field(default_factory=list)
    overlay_layer_keys: list[str] = Field(default_factory=list)
    render_enriched_labels: bool = True


class StemCsvPayload(RuntimePayload):
    payload_type: Literal["stem_csv"] = "stem_csv"
    measurement: StemCsvMeasurement
    side_mapping_override: Literal["left_first", "left_last"] | None = None


class EndfaceReferencePayload(RuntimePayload):
    payload_type: Literal["endface_reference"] = "endface_reference"
    reference: EndfaceReference


class EndfaceCalibrationPayload(RuntimePayload):
    payload_type: Literal["endface_calibration"] = "endface_calibration"
    reference: EndfaceReference
    valid: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    image_context_id: str | None = None
    image_to_mm: list[float] = Field(default_factory=list)
    image_to_view: list[float] = Field(default_factory=list)
    origin_mm: tuple[float, float] = (0, 0)
    center_mm: tuple[float, float] = (0, 0)
    mm_per_pixel: float = 0.25
    outer_mask: dict[str, Any] | None = None
    registration_hash: str | None = None
    error_mm: float | None = None
    stability_mm: float | None = None
    effective_source_mm_per_pixel: float | None = None
    alternative_view_to_mm: list[list[float]] = Field(default_factory=list)


class BarkMeasurementPayload(RuntimePayload):
    payload_type: Literal["bark_measurement"] = "bark_measurement"
    measurement: BarkSideMeasurement


AnyRuntimePayload = (
    FramePayload
    | SynchronizedFrameSetPayload
    | TriggerPayload
    | DetectionPayload
    | ClassificationPayload
    | SegmentPayload
    | StemGeometryPayload
    | StemHorizontalCurvaturePayload
    | StemRotationPayload
    | StemSurfacePhasePayload
    | StemSliceModelPayload
    | StemRotationPriorityPayload
    | StemCurvaturePhaseSolutionPayload
    | TemporalSequencePayload
    | TemporalPredictionPayload
    | AnomalyPayload
    | RegionSetPayload
    | RoiSetPayload
    | PositionsPayload
    | MeasurementsPayload
    | ReferenceEdgesPayload
    | CvEdgesPayload
    | CoordinateFramePayload
    | FeatureMeasurementsPayload
    | ClearanceZonesPayload
    | FreeRegionPayload
    | BoardCutConstraintsPayload
    | BoardCutDecisionPayload
    | QualityDecisionPayload
    | EventsPayload
    | ResultPayload
    | SiemensSpsWriteStatusPayload
    | CaptureIntentsPayload
    | OverlayPayload
    | DisplayOutputPayload
    | StemCsvPayload
    | EndfaceReferencePayload
    | EndfaceCalibrationPayload
    | BarkMeasurementPayload
)


class RuntimeExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    _recording_frame: FramePayload | None = PrivateAttr(default=None)

    schema_version: Literal["runtime.execution.result.v1"] = "runtime.execution.result.v1"
    status: Literal["completed", "failed", "skipped"]
    channel_id: str | None = None
    pipeline_version_id: str
    runtime_execution_id: str | None = None
    correlation_id: str
    started_at: str
    completed_at: str
    node_outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    raw_inference_outputs: list[dict[str, Any]] = Field(default_factory=list)
    normalized_inference_outputs: list[dict[str, Any]] = Field(default_factory=list)
    derived_outputs: list[dict[str, Any]] = Field(default_factory=list)
    image_contexts: list[dict[str, Any]] = Field(default_factory=list)
    object_sets: list[dict[str, Any]] = Field(default_factory=list)
    enriched_object_annotations: list[dict[str, Any]] = Field(default_factory=list)
    reference_edges: list[dict[str, Any]] = Field(default_factory=list)
    cv_edges: list[dict[str, Any]] = Field(default_factory=list)
    coordinate_frames: list[dict[str, Any]] = Field(default_factory=list)
    feature_measurements: list[dict[str, Any]] = Field(default_factory=list)
    clearance_zones: list[dict[str, Any]] = Field(default_factory=list)
    free_regions: list[dict[str, Any]] = Field(default_factory=list)
    synchronized_frame_sets: list[dict[str, Any]] = Field(default_factory=list)
    board_cut_constraints: list[dict[str, Any]] = Field(default_factory=list)
    board_cut_decisions: list[dict[str, Any]] = Field(default_factory=list)
    quality_decisions: list[dict[str, Any]] = Field(default_factory=list)
    overlay_primitives: list[dict[str, Any]] = Field(default_factory=list)
    overlay_outputs: list[dict[str, Any]] = Field(default_factory=list)
    display_outputs: list[dict[str, Any]] = Field(default_factory=list)
    capture_intents: list[dict[str, Any]] = Field(default_factory=list)
    http_trigger_responses: list[dict[str, Any]] = Field(default_factory=list)
    persisted_results: list[dict[str, Any]] = Field(default_factory=list)
    internal_events: list[dict[str, Any]] = Field(default_factory=list)
    plc_write_outputs: list[dict[str, Any]] = Field(default_factory=list)
    projected_results: list[dict[str, Any]] = Field(default_factory=list)
    final_response: dict[str, Any] = Field(default_factory=dict)
    timing: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)

    def set_recording_frame(self, frame: FramePayload | None) -> None:
        """Attach an in-process frame without leaking image bytes into result JSON."""

        self._recording_frame = frame

    def recording_frame(self) -> FramePayload | None:
        return self._recording_frame
