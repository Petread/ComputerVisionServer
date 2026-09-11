from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cv_contracts.modeling import ModelArchitectureSummary
from cv_status import BatchJobStatus, TaskType


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicEnvelope(StrictModel):
    api_version: Literal["v1"] = "v1"
    request_id: str
    correlation_id: str | None = None


class PublicInferenceWarning(StrictModel):
    code: str
    message: str


class PublicImageMetadata(StrictModel):
    file_name: str | None = None
    content_type: str
    size_bytes: int = Field(ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    sha256: str


class PublicInferenceTiming(StrictModel):
    received_at: str
    completed_at: str
    validation_ms: float = Field(ge=0)
    routing_ms: float = Field(ge=0)
    preprocessing_ms: float = Field(ge=0)
    inference_ms: float = Field(ge=0)
    postprocessing_ms: float = Field(ge=0)
    total_ms: float = Field(ge=0)


class PublicParameterSpec(StrictModel):
    name: str
    parameter_type: Literal["integer", "number", "string", "boolean"]
    required: bool = False
    default: Any | None = None
    minimum: float | None = None
    maximum: float | None = None
    description: str


class PublicInputConstraints(StrictModel):
    canonical_form: Literal["multipart/form-data"] = "multipart/form-data"
    supported_forms: list[str] = Field(default_factory=lambda: ["multipart/form-data"])
    supported_content_types: list[str] = Field(default_factory=list)
    max_images_per_request: int = Field(default=1, ge=1)
    max_image_bytes: int = Field(ge=1)
    max_request_body_bytes: int = Field(ge=1)


class PublicResponseSchema(StrictModel):
    schema_id: str
    content_type: Literal["application/json"] = "application/json"


class PublicRealtimeAvailability(StrictModel):
    available: bool
    timeout_default_ms: int | None = Field(default=None, ge=1)
    timeout_max_ms: int | None = Field(default=None, ge=1)
    queueing: Literal["none"] = "none"
    overload_behavior: Literal["reject"] = "reject"


class PublicBatchAvailability(StrictModel):
    available: bool
    timeout_default_seconds: int | None = Field(default=None, ge=1)
    timeout_max_seconds: int | None = Field(default=None, ge=1)
    idempotency_required: bool = True
    resumable: bool = True


class PublicModelAvailability(StrictModel):
    realtime: PublicRealtimeAvailability
    batch: PublicBatchAvailability


class PublicModelSummary(StrictModel):
    model_id: str
    display_name: str
    description: str | None = None
    model_family: str
    task_type: TaskType
    architecture: ModelArchitectureSummary
    model_version: int | None = Field(default=None, ge=1)
    availability: PublicModelAvailability


class PublicModelDetail(PublicModelSummary):
    input_constraints: PublicInputConstraints
    response_schema: PublicResponseSchema
    supported_parameters: list[PublicParameterSpec] = Field(default_factory=list)
    class_labels: list[str] = Field(default_factory=list)


class PublicModelListResponse(PublicEnvelope):
    schema_version: Literal["inference.public.models.v1"] = "inference.public.models.v1"
    items: list[PublicModelSummary] = Field(default_factory=list)


class PublicModelDetailResponse(PublicEnvelope):
    schema_version: Literal["inference.public.model-detail.v1"] = (
        "inference.public.model-detail.v1"
    )
    item: PublicModelDetail


class PublicClassificationPrediction(StrictModel):
    label: str
    score: float = Field(ge=0, le=1)


class PublicBoundingBox(StrictModel):
    x_min: float = Field(ge=0, le=1)
    y_min: float = Field(ge=0, le=1)
    x_max: float = Field(ge=0, le=1)
    y_max: float = Field(ge=0, le=1)


class PublicOrientedPoint(StrictModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class PublicOrientedBoundingBox(StrictModel):
    points: list[PublicOrientedPoint] = Field(min_length=4, max_length=4)
    rotation_degrees: float | None = None


class PublicCoordinateSystem(StrictModel):
    name: Literal["normalized_xyxy"] = "normalized_xyxy"
    origin: Literal["top_left"] = "top_left"
    normalized: bool = True
    description: str = (
        "Bounding box coordinates use x_min,y_min,x_max,y_max normalized to [0,1]. "
        "Oriented detections additionally include four normalized corner points."
    )


class PublicDetection(StrictModel):
    label: str
    class_id: int | None = Field(default=None, ge=0)
    confidence: float = Field(ge=0, le=1)
    bounding_box: PublicBoundingBox
    oriented_box: PublicOrientedBoundingBox | None = None


class PublicSegmentationPoint(StrictModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class PublicSegmentation(StrictModel):
    label: str
    class_id: int | None = Field(default=None, ge=0)
    confidence: float = Field(ge=0, le=1)
    polygon: list[PublicSegmentationPoint] = Field(default_factory=list)


class PublicInferenceResponseBase(PublicEnvelope):
    model_id: str
    model_family: str
    model_version: int = Field(ge=1)
    task_type: TaskType
    architecture: ModelArchitectureSummary
    input: PublicImageMetadata
    timing: PublicInferenceTiming
    warnings: list[PublicInferenceWarning] = Field(default_factory=list)


class PublicClassificationResponse(PublicInferenceResponseBase):
    schema_version: Literal["inference.public.classification-response.v1"] = (
        "inference.public.classification-response.v1"
    )
    predictions: list[PublicClassificationPrediction] = Field(default_factory=list)


class PublicDetectionResponse(PublicInferenceResponseBase):
    schema_version: Literal["inference.public.detection-response.v1"] = (
        "inference.public.detection-response.v1"
    )
    coordinate_system: PublicCoordinateSystem = Field(default_factory=PublicCoordinateSystem)
    detections: list[PublicDetection] = Field(default_factory=list)


class PublicSegmentationResponse(PublicInferenceResponseBase):
    schema_version: Literal["inference.public.segmentation-response.v1"] = (
        "inference.public.segmentation-response.v1"
    )
    image_width: int = Field(ge=1)
    image_height: int = Field(ge=1)
    segments: list[PublicSegmentation] = Field(default_factory=list)


class PublicAnomalyHeatmap(StrictModel):
    format: Literal["png_base64"] = "png_base64"
    data: str = Field(min_length=1)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    min_value: float | None = None
    max_value: float | None = None
    colormap: str = "turbo"


class PublicAnomalyMask(StrictModel):
    format: Literal["png_base64"] = "png_base64"
    data: str = Field(min_length=1)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    threshold: float | None = None


class PublicAnomalyResponse(PublicInferenceResponseBase):
    schema_version: Literal["inference.public.anomaly-response.v1"] = (
        "inference.public.anomaly-response.v1"
    )
    anomaly_score: float = Field(ge=0)
    predicted_label: Literal["normal", "anomalous"]
    is_anomalous: bool
    threshold: float | None = None
    heatmap: PublicAnomalyHeatmap
    mask: PublicAnomalyMask | None = None
    image_width: int = Field(ge=1)
    image_height: int = Field(ge=1)


class PublicBatchInputItem(StrictModel):
    item_id: str = Field(min_length=1, max_length=120)
    image_base64: str = Field(min_length=16)
    external_ref: str | None = Field(default=None, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PublicBatchJobCreateRequest(StrictModel):
    model_id: str = Field(min_length=3, max_length=120)
    inputs: list[PublicBatchInputItem] = Field(default_factory=list)
    input_manifest_uri: str | None = Field(default=None, min_length=3, max_length=512)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)

    @model_validator(mode="after")
    def validate_input_source(self) -> "PublicBatchJobCreateRequest":
        if not self.inputs and not self.input_manifest_uri:
            raise ValueError("Provide either inputs or input_manifest_uri.")
        return self


class PublicBatchProgress(StrictModel):
    total_items: int = Field(ge=0)
    completed_items: int = Field(ge=0)
    failed_items: int = Field(ge=0)
    total_chunks: int = Field(ge=0)
    queued_chunks: int = Field(ge=0)
    running_chunks: int = Field(ge=0)
    completed_chunks: int = Field(ge=0)
    failed_chunks: int = Field(ge=0)
    progress_percent: float = Field(ge=0, le=100)


class PublicBatchChunkSummary(StrictModel):
    chunk_no: int = Field(ge=0)
    status: str
    retry_count: int = Field(ge=0)
    started_at: str | None = None
    finished_at: str | None = None


class PublicBatchResultPackage(StrictModel):
    manifest_uri: str | None = None
    jsonl_uri: str | None = None
    bundle_uri: str | None = None
    result_schema: str = "inference.public.batch-results.v1"
    recoverable: bool = False
    incomplete_chunk_nos: list[int] = Field(default_factory=list)


class PublicBatchJobSummary(PublicEnvelope):
    schema_version: Literal["inference.public.batch-job.v1"] = "inference.public.batch-job.v1"
    job_id: str
    model_id: str
    model_family: str
    model_version: int = Field(ge=1)
    task_type: TaskType
    status: BatchJobStatus
    submitted_at: str
    progress: PublicBatchProgress
    result_package: PublicBatchResultPackage = Field(default_factory=PublicBatchResultPackage)


class PublicBatchJobDetail(PublicBatchJobSummary):
    started_at: str | None = None
    finished_at: str | None = None
    resumable: bool = True
    chunks: list[PublicBatchChunkSummary] = Field(default_factory=list)


class PublicBatchResultItem(StrictModel):
    item_id: str
    external_ref: str | None = None
    status: Literal["completed", "failed"]
    predictions: list[PublicClassificationPrediction] = Field(default_factory=list)
    detections: list[PublicDetection] = Field(default_factory=list)
    image_width: int | None = Field(default=None, ge=1)
    image_height: int | None = Field(default=None, ge=1)
    segments: list[PublicSegmentation] = Field(default_factory=list)
    anomaly_score: float | None = Field(default=None, ge=0)
    predicted_label: Literal["normal", "anomalous"] | None = None
    is_anomalous: bool | None = None
    threshold: float | None = None
    heatmap: PublicAnomalyHeatmap | None = None
    mask: PublicAnomalyMask | None = None
    error_code: str | None = None
    error_message: str | None = None
    timing: PublicInferenceTiming | None = None


class PublicBatchResultsResponse(PublicEnvelope):
    schema_version: Literal["inference.public.batch-results.v1"] = (
        "inference.public.batch-results.v1"
    )
    job_id: str
    model_id: str
    model_family: str
    model_version: int = Field(ge=1)
    task_type: TaskType
    status: BatchJobStatus
    progress: PublicBatchProgress
    result_package: PublicBatchResultPackage = Field(default_factory=PublicBatchResultPackage)
    items: list[PublicBatchResultItem] = Field(default_factory=list)


class PublicBatchJobMutationResponse(PublicEnvelope):
    schema_version: Literal["inference.public.batch-job-mutation.v1"] = (
        "inference.public.batch-job-mutation.v1"
    )
    job_id: str
    action: Literal["canceled", "cleared"]
    status: BatchJobStatus
    detail: str
