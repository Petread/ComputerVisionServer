from __future__ import annotations

from typing import Any, Literal

from cv_status import (
    BatchChunkStatus,
    BatchJobStatus,
    DeploymentReleaseStatus,
    TargetPath,
    TaskType,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cv_contracts.common import ResponseMeta
from cv_contracts.modeling import ModelArchitectureSummary


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InferenceTiming(StrictModel):
    received_at: str
    validation_ms: float = Field(ge=0)
    routing_ms: float = Field(ge=0)
    preprocessing_ms: float = Field(ge=0)
    inference_ms: float = Field(ge=0)
    postprocessing_ms: float = Field(ge=0)
    total_ms: float = Field(ge=0)
    shared_memory_transport: bool = False


class LatencyQuantiles(StrictModel):
    sample_count: int = Field(default=0, ge=0)
    p50_ms: float = Field(default=0, ge=0)
    p95_ms: float = Field(default=0, ge=0)
    p99_ms: float = Field(default=0, ge=0)
    throughput_per_second: float = Field(default=0, ge=0)


class ModelRouteRef(StrictModel):
    deployment_release_id: str
    target_path: TargetPath
    release_status: DeploymentReleaseStatus
    model_family_slug: str
    model_version_id: str
    task_type: TaskType
    triton_model_name: str
    triton_model_version: int = Field(ge=1)
    serving_artifact_format: Literal["tensorrt_plan", "onnx"] | None = None
    contract_major: int = Field(ge=1)


class BaseInferenceRequest(StrictModel):
    deployment_target: str = Field(min_length=3, max_length=120)
    image_base64: str = Field(min_length=16)
    deployment_release_id: str | None = Field(default=None, min_length=3, max_length=64)
    timeout_ms: int | None = Field(default=None, ge=50, le=30_000)


class RealtimeClassifyRequest(BaseInferenceRequest):
    top_k: int = Field(default=3, ge=1, le=20)


class RealtimeDetectRequest(BaseInferenceRequest):
    max_detections: int = Field(default=5, ge=1, le=100)
    score_threshold: float | None = Field(default=None, ge=0, le=1)


class RealtimeSegmentRequest(BaseInferenceRequest):
    max_segments: int = Field(default=5, ge=1, le=100)
    score_threshold: float | None = Field(default=None, ge=0, le=1)
    return_masks: bool = False


class RealtimeAnomalyRequest(BaseInferenceRequest):
    threshold: float | None = Field(default=None, ge=0, le=1)
    return_mask: bool = False


class TemporalSequenceFrame(StrictModel):
    """One causal, structured feature row before artifact-defined scaling."""

    frame_index: int = Field(ge=0)
    timestamp_seconds: float = Field(ge=0)
    source_timestamp: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)


class RealtimeTemporalSequenceRequest(StrictModel):
    deployment_target: str = Field(min_length=3, max_length=120)
    deployment_release_id: str | None = Field(default=None, min_length=3, max_length=64)
    timeout_ms: int | None = Field(default=None, ge=50, le=30_000)
    sequence_id: str = Field(min_length=1, max_length=160)
    track_id: str | None = Field(default=None, max_length=160)
    feature_set_id: str = Field(min_length=1, max_length=160)
    feature_schema_version: str = Field(min_length=1, max_length=160)
    rotation_schema_version: str = Field(min_length=1, max_length=160)
    calibration_schema_version: str = Field(min_length=1, max_length=160)
    rotation_calibration_id: str | None = Field(default=None, max_length=160)
    rotation_calibration_hash: str | None = Field(default=None, max_length=160)
    rotation_viewpoint_convention: str = Field(min_length=1, max_length=200)
    positive_rotation_direction: str = Field(min_length=1, max_length=200)
    frames: list[TemporalSequenceFrame] = Field(min_length=1, max_length=4096)
    current_frame_index: int = Field(ge=0)
    decision_frame_index: int | None = Field(default=None, ge=0)
    causal: Literal[True] = True
    post_decision_frames_included: Literal[False] = False
    trace_continuous: bool = True
    calibration_valid: bool = True

    @model_validator(mode="after")
    def validate_causal_prefix(self) -> RealtimeTemporalSequenceRequest:
        indices = [item.frame_index for item in self.frames]
        timestamps = [item.timestamp_seconds for item in self.frames]
        if any(right <= left for left, right in zip(indices, indices[1:], strict=False)):
            raise ValueError("Temporal frame indices must be strictly increasing.")
        if any(
            right < left for left, right in zip(timestamps, timestamps[1:], strict=False)
        ):
            raise ValueError("Temporal frame timestamps must be monotonically increasing.")
        if indices[-1] != self.current_frame_index:
            raise ValueError("current_frame_index must equal the final causal frame index.")
        if self.decision_frame_index is not None and any(
            index > self.decision_frame_index for index in indices
        ):
            raise ValueError("Temporal input must not contain frames after the decision frame.")
        return self


class ClassificationLabelScore(StrictModel):
    label: str
    score: float = Field(ge=0, le=1)
    rank: int = Field(ge=1)


class DetectionBox(StrictModel):
    x_min: float = Field(ge=0, le=1)
    y_min: float = Field(ge=0, le=1)
    x_max: float = Field(ge=0, le=1)
    y_max: float = Field(ge=0, le=1)


class DetectionPoint(StrictModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class OrientedDetectionBox(StrictModel):
    points: list[DetectionPoint] = Field(min_length=4, max_length=4)
    rotation_degrees: float | None = None


class DetectionPrediction(StrictModel):
    label: str
    score: float = Field(ge=0, le=1)
    box: DetectionBox
    oriented_box: OrientedDetectionBox | None = None


class SegmentationPoint(StrictModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class SegmentationMask(StrictModel):
    format: Literal["png_base64"] = "png_base64"
    data: str = Field(min_length=1)
    width: int = Field(ge=1, le=16384)
    height: int = Field(ge=1, le=16384)
    # Original mask resolution is retained after image-coordinate restoration.
    native_width: int | None = Field(default=None, ge=1)
    native_height: int | None = Field(default=None, ge=1)


class SegmentationPrediction(StrictModel):
    label: str
    score: float = Field(ge=0, le=1)
    polygon: list[SegmentationPoint] = Field(default_factory=list)
    mask: SegmentationMask | None = None


class AnomalyHeatmap(StrictModel):
    format: Literal["png_base64"] = "png_base64"
    data: str = Field(min_length=1)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    min_value: float | None = None
    max_value: float | None = None
    colormap: str = Field(default="turbo", min_length=1, max_length=64)


class AnomalyMask(StrictModel):
    format: Literal["png_base64"] = "png_base64"
    data: str = Field(min_length=1)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    threshold: float = Field(ge=0, le=1)


class InferenceResponseBase(StrictModel):
    schema_version: str
    request_id: str
    correlation_id: str | None = None
    deployment_target: str
    route: ModelRouteRef
    architecture: ModelArchitectureSummary
    warm_model: bool
    timing: InferenceTiming


class ClassificationInferenceResponse(InferenceResponseBase):
    schema_version: Literal["inference.classification.response.v1"] = (
        "inference.classification.response.v1"
    )
    labels: list[ClassificationLabelScore] = Field(default_factory=list)


class DetectionInferenceResponse(InferenceResponseBase):
    schema_version: Literal["inference.detection.response.v1"] = "inference.detection.response.v1"
    detections: list[DetectionPrediction] = Field(default_factory=list)


class SegmentationInferenceResponse(InferenceResponseBase):
    schema_version: Literal["inference.segmentation.response.v1"] = (
        "inference.segmentation.response.v1"
    )
    image_width: int = Field(ge=1)
    image_height: int = Field(ge=1)
    segments: list[SegmentationPrediction] = Field(default_factory=list)


class AnomalyInferenceResponse(InferenceResponseBase):
    schema_version: Literal["inference.anomaly.response.v1"] = "inference.anomaly.response.v1"
    anomaly_score: float = Field(ge=0)
    predicted_label: Literal["normal", "anomalous"]
    is_anomalous: bool
    threshold: float = Field(ge=0, le=1)
    heatmap: AnomalyHeatmap
    mask: AnomalyMask | None = None
    image_width: int = Field(ge=1)
    image_height: int = Field(ge=1)
    model_metadata: dict[str, Any] = Field(default_factory=dict)


class TemporalSequenceInferenceResponse(InferenceResponseBase):
    schema_version: Literal["inference.temporal-sequence.response.v1"] = (
        "inference.temporal-sequence.response.v1"
    )
    sequence_id: str
    track_id: str | None = None
    sequence_length: int = Field(ge=1)
    last_frame_index: int = Field(ge=0)
    decision_frame_index: int | None = Field(default=None, ge=0)
    feature_set_id: str
    feature_schema_digest: str
    scaler_sha256: str
    snapshot_id: str
    feature_schema_version: str
    rotation_schema_version: str
    calibration_schema_version: str
    rotation_calibration_id: str | None = None
    rotation_calibration_hash: str | None = None
    rotation_viewpoint_convention: str
    positive_rotation_direction: str
    bow_relevance_probability: float = Field(ge=0, le=1)
    target_observed_probability: float = Field(ge=0, le=1)
    angle_sin_2e: float = Field(ge=-1, le=1)
    angle_cos_2e: float = Field(ge=-1, le=1)
    signed_correction_deg: float = Field(ge=-90, lt=90)
    raw_angle_confidence: float = Field(ge=0, le=1)
    angle_confidence: float = Field(ge=0, le=1)
    confidence_calibration_valid: bool = False
    final_quality_probabilities: dict[str, float] = Field(default_factory=dict)
    deadband_deg: float = Field(ge=0, lt=90)
    estimate_method: Literal["tcn", "gru"]
    reason_codes: list[str] = Field(default_factory=list)


class RealtimeInferenceResponse(InferenceResponseBase):
    schema_version: Literal["inference.response.v1"] = "inference.response.v1"
    task_type: TaskType
    labels: list[ClassificationLabelScore] = Field(default_factory=list)
    detections: list[DetectionPrediction] = Field(default_factory=list)
    image_width: int | None = Field(default=None, ge=1)
    image_height: int | None = Field(default=None, ge=1)
    segments: list[SegmentationPrediction] = Field(default_factory=list)
    anomaly_score: float | None = Field(default=None, ge=0)
    predicted_label: Literal["normal", "anomalous"] | None = None
    is_anomalous: bool | None = None
    threshold: float | None = Field(default=None, ge=0, le=1)
    heatmap: AnomalyHeatmap | None = None
    mask: AnomalyMask | None = None


RealtimeInferenceRequest = RealtimeClassifyRequest


class BatchInputItem(StrictModel):
    item_id: str = Field(min_length=1, max_length=120)
    image_base64: str = Field(min_length=16)
    external_ref: str | None = Field(default=None, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BatchJobCreateRequest(StrictModel):
    deployment_target: str = Field(min_length=3, max_length=120)
    deployment_release_id: str | None = Field(default=None, min_length=3, max_length=64)
    input_manifest_uri: str | None = Field(default=None, min_length=3, max_length=512)
    inputs: list[BatchInputItem] = Field(default_factory=list)
    chunk_size: int | None = Field(default=None, ge=1, le=1_024)
    timeout_seconds: int | None = Field(default=None, ge=1, le=3_600)
    idempotency_key: str | None = Field(default=None, min_length=3, max_length=200)

    @model_validator(mode="after")
    def validate_input_source(self) -> "BatchJobCreateRequest":
        if not self.input_manifest_uri and not self.inputs:
            raise ValueError("Provide either input_manifest_uri or inputs for the batch job.")
        return self


class BatchJobProgress(StrictModel):
    total_items: int = Field(ge=0)
    completed_items: int = Field(ge=0)
    failed_items: int = Field(ge=0)
    total_chunks: int = Field(ge=0)
    queued_chunks: int = Field(ge=0)
    running_chunks: int = Field(ge=0)
    completed_chunks: int = Field(ge=0)
    failed_chunks: int = Field(ge=0)
    progress_percent: float = Field(ge=0, le=100)


class BatchChunkSummary(StrictModel):
    chunk_id: str
    chunk_no: int = Field(ge=0)
    status: BatchChunkStatus
    retry_count: int = Field(ge=0)
    input_uri: str
    output_uri: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class BatchArtifactSummary(StrictModel):
    manifest_uri: str | None = None
    results_uri: str | None = None
    jsonl_uri: str | None = None
    bundle_uri: str | None = None
    download_path: str | None = None
    recoverable: bool = False
    incomplete_chunk_nos: list[int] = Field(default_factory=list)


class BatchJobSummary(StrictModel):
    schema_version: Literal["inference.batch.job-summary.v1"] = "inference.batch.job-summary.v1"
    batch_job_id: str
    deployment_target: str
    deployment_release_id: str
    status: BatchJobStatus
    created_at: str
    progress: BatchJobProgress


class BatchJobDetail(BatchJobSummary):
    route: ModelRouteRef
    input_manifest_uri: str
    output_manifest_uri: str | None = None
    submitted_by: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    resumable: bool = True
    chunks: list[BatchChunkSummary] = Field(default_factory=list)
    artifacts: BatchArtifactSummary = Field(default_factory=BatchArtifactSummary)
    metrics: LatencyQuantiles = Field(default_factory=LatencyQuantiles)


class BatchResultRecord(StrictModel):
    item_id: str
    external_ref: str | None = None
    status: Literal["completed", "failed"]
    labels: list[ClassificationLabelScore] = Field(default_factory=list)
    detections: list[DetectionPrediction] = Field(default_factory=list)
    image_width: int | None = Field(default=None, ge=1)
    image_height: int | None = Field(default=None, ge=1)
    segments: list[SegmentationPrediction] = Field(default_factory=list)
    anomaly_score: float | None = Field(default=None, ge=0)
    predicted_label: Literal["normal", "anomalous"] | None = None
    is_anomalous: bool | None = None
    threshold: float | None = Field(default=None, ge=0, le=1)
    heatmap: AnomalyHeatmap | None = None
    mask: AnomalyMask | None = None
    timing: InferenceTiming | None = None
    error: str | None = None


class BatchResultResponse(StrictModel):
    schema_version: Literal["inference.batch.results.v1"] = "inference.batch.results.v1"
    batch_job_id: str
    route: ModelRouteRef
    status: BatchJobStatus
    items: list[BatchResultRecord] = Field(default_factory=list)
    artifacts: BatchArtifactSummary = Field(default_factory=BatchArtifactSummary)
    metrics: LatencyQuantiles = Field(default_factory=LatencyQuantiles)
    meta: ResponseMeta


class ModelCapabilitySummary(StrictModel):
    deployment_release_id: str
    target_path: TargetPath
    active: bool
    rollback_candidate: bool
    model_family_slug: str
    model_version_id: str
    task_type: TaskType
    architecture: ModelArchitectureSummary
    contract_major: int = Field(ge=1)
    input_schema_ref: str | None = None
    output_schema_ref: str | None = None
    class_labels: list[str] = Field(default_factory=list)
    compatible_targets: list[TargetPath] = Field(default_factory=list)
    max_batch_size: int = Field(ge=1)
    confidence_threshold_default: float = Field(ge=0, le=1)
    preprocessing: dict[str, Any] = Field(default_factory=dict)
    postprocessing: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    anomaly: dict[str, Any] = Field(default_factory=dict)


class ModelCapabilitiesResponse(StrictModel):
    schema_version: Literal["inference.model-capabilities.v1"] = "inference.model-capabilities.v1"
    items: list[ModelCapabilitySummary] = Field(default_factory=list)
    meta: ResponseMeta


class ModelStatusSummary(StrictModel):
    route: ModelRouteRef
    active: bool
    rollback_candidate: bool
    warm_model: bool
    overloaded: bool
    current_inflight: int = Field(ge=0)
    concurrency_limit: int = Field(ge=1)
    last_request_at: str | None = None
    latency: LatencyQuantiles = Field(default_factory=LatencyQuantiles)
    backend_statistics: dict[str, int | float] = Field(default_factory=dict)


class ModelStatusResponse(StrictModel):
    schema_version: Literal["inference.model-status.v1"] = "inference.model-status.v1"
    items: list[ModelStatusSummary] = Field(default_factory=list)
    meta: ResponseMeta
