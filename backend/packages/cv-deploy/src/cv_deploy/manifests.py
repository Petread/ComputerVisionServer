from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from cv_status import TargetPath


class ModelTensorSchema(BaseModel):
    name: str
    data_type: str
    dims: list[int | str] = Field(default_factory=list)
    layout: str | None = None
    description: str | None = None


class ModelIoSchema(BaseModel):
    input_tensors: list[ModelTensorSchema] = Field(default_factory=list)
    output_tensors: list[ModelTensorSchema] = Field(default_factory=list)
    input_schema_ref: str | None = None
    output_schema_ref: str | None = None
    contract_major: int = 1
    schema_digest: str


class NmsConfig(BaseModel):
    enabled: bool = False
    iou_threshold: float | None = None
    max_detections: int | None = None
    score_threshold: float | None = None


class HardwareCompatibility(BaseModel):
    accelerator: Literal["nvidia_gpu"] = "nvidia_gpu"
    target_gpu_profile: str
    min_compute_capability: str
    cuda_version: str
    tensorrt_version: str
    precision: str
    max_batch_size: dict[str, int] = Field(default_factory=dict)
    validated_devices: list[str] = Field(default_factory=list)


class ExportFormatArtifact(BaseModel):
    uri: str
    sha256: str
    size_bytes: int
    format: str
    file_name: str


class ExportManifest(BaseModel):
    training_job_id: str
    model_family_slug: str
    task_type: str
    recipe_name: str
    recipe_version: str
    image_size: int | None = None
    generated_at: str
    source_snapshot_id: str
    source_snapshot_manifest_uri: str | None = None
    class_map_version: str | None = None
    class_map_sha256: str | None = None
    class_count: int = 0
    confidence_threshold_default: float | None = None
    preprocessing: dict[str, Any] = Field(default_factory=dict)
    postprocessing: dict[str, Any] = Field(default_factory=dict)
    nms: NmsConfig = Field(default_factory=NmsConfig)
    io_schema: ModelIoSchema
    hardware_compatibility: HardwareCompatibility
    onnx: ExportFormatArtifact
    tensorrt: ExportFormatArtifact | None = None
    build_manifest: dict[str, Any] = Field(default_factory=dict)
    artifact_validation: dict[str, Any] = Field(default_factory=dict)
    temporal: dict[str, Any] = Field(default_factory=dict)


class ServingTargetVariant(BaseModel):
    target_path: TargetPath
    purpose: str
    max_batch_size: int
    artifact_file: str
    artifact_format: str = "tensorrt_plan"
    artifact_sha256: str
    artifact_size_bytes: int
    dynamic_batching: bool = False
    preferred_batch_sizes: list[int] = Field(default_factory=list)
    max_queue_delay_microseconds: int | None = None
    instance_group_count: int = 1
    config_sha256: str
    config_path: str


class CompatibilityCheck(BaseModel):
    code: str
    level: Literal["info", "warning", "error"]
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class CompatibilityReport(BaseModel):
    compatible: bool
    checked_at: str
    target_path: TargetPath
    model_version_id: str
    checks: list[CompatibilityCheck] = Field(default_factory=list)


class SmokeTestCheck(BaseModel):
    name: str
    passed: bool
    detail: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class SmokeTestReport(BaseModel):
    deployment_release_id: str
    target_path: TargetPath
    checked_at: str
    passed: bool
    checks: list[SmokeTestCheck] = Field(default_factory=list)


class DeploymentPackageManifest(BaseModel):
    model_version_id: str
    model_family_id: str
    model_family_slug: str
    version_no: int
    task_type: str
    contract_major: int
    built_at: str
    training_artifact_manifest_uri: str | None = None
    training_export_manifest_uri: str | None = None
    package_root: str
    package_digest: str
    immutable_metadata_sha256: str | None = None
    class_map_version: str
    class_map_sha256: str
    class_count: int
    confidence_threshold_default: float
    preprocessing: dict[str, Any] = Field(default_factory=dict)
    postprocessing: dict[str, Any] = Field(default_factory=dict)
    anomaly: dict[str, Any] = Field(default_factory=dict)
    temporal: dict[str, Any] = Field(default_factory=dict)
    nms: NmsConfig = Field(default_factory=NmsConfig)
    io_schema: ModelIoSchema
    hardware_compatibility: HardwareCompatibility
    compatible_targets: list[TargetPath] = Field(default_factory=list)
    variants: list[ServingTargetVariant] = Field(default_factory=list)
    source_manifests: dict[str, Any] = Field(default_factory=dict)


class DeploymentPackageStatusPayload(BaseModel):
    status: str
    built_at: str | None = None
    manifest_uri: str | None = None
    error: str | None = None
