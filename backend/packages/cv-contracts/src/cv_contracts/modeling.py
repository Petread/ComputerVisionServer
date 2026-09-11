from __future__ import annotations

from typing import Any, Literal

from cv_status import TaskType
from pydantic import BaseModel, Field

from cv_contracts.promotion import PromotionCriteriaPolicy


class TrainingDefaults(BaseModel):
    epochs: int = Field(default=40, ge=1, le=1_000)
    learning_rate: float = Field(default=0.001, gt=0, le=10)
    image_size: int | None = Field(default=640, ge=32, le=4_096, multiple_of=32)
    batch_size: int | None = Field(default=8, ge=1, le=4_096)
    optimizer: str | None = Field(default="auto", min_length=2, max_length=120)
    optimizer_settings: dict[str, Any] = Field(default_factory=dict)
    checkpoint_interval_steps: int = Field(default=25, ge=1, le=100_000)


def training_defaults_from_persisted(payload: dict[str, Any] | None) -> TrainingDefaults:
    """Load legacy JSON without weakening validation for new API inputs."""
    normalized = dict(payload or {})
    image_size = normalized.get("image_size")
    if (
        isinstance(image_size, int)
        and not isinstance(image_size, bool)
        and 32 <= image_size <= 4_096
        and image_size % 32
    ):
        normalized["image_size"] = min(max(((image_size + 16) // 32) * 32, 32), 4_096)
    return TrainingDefaults.model_validate(normalized)


class ModelArchitectureSummary(BaseModel):
    task_type: TaskType
    family: str = Field(min_length=2, max_length=64)
    version: str = Field(min_length=1, max_length=32)
    size: str = Field(min_length=1, max_length=16)
    display_name: str = Field(min_length=3, max_length=160)
    internal_key: str = Field(min_length=3, max_length=160)


class SupportedModelArchitecture(BaseModel):
    architecture: ModelArchitectureSummary
    supported: bool = True
    support_reason: str | None = None
    trainer_backend: str = Field(default="unknown", min_length=2, max_length=120)
    concrete_architecture: str = Field(default="unknown", min_length=2, max_length=160)
    export_support: bool = True
    deployment_support: bool = True
    inference_support: bool = True
    transfer_learning_policy: str = Field(default="auto_pretrained_le_2000", min_length=3)
    status: Literal["supported", "experimental", "hidden"] = "supported"
    status_flags: list[str] = Field(default_factory=list)
    capability_metadata: dict[str, Any] = Field(default_factory=dict)


class ModelTemplateDefaults(BaseModel):
    training: TrainingDefaults = Field(default_factory=TrainingDefaults)
    promotion: PromotionCriteriaPolicy | None = None


class ModelTemplateSummary(BaseModel):
    template_id: str | None = None
    template_key: str
    label: str
    description: str
    task_type: TaskType
    architecture: ModelArchitectureSummary
    display_name_suffix: str
    default_training_profile: str
    defaults: ModelTemplateDefaults = Field(default_factory=ModelTemplateDefaults)
    default_promotion_policy: PromotionCriteriaPolicy
    template_version: int = 1
    recommended: bool = False
    active: bool = True


class ModelTemplateCreateRequest(BaseModel):
    template_key: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,119}$")
    label: str = Field(min_length=3, max_length=160)
    description: str = Field(min_length=3, max_length=500)
    task_type: TaskType
    architecture_family: str = Field(min_length=2, max_length=64)
    architecture_version: str = Field(min_length=1, max_length=32)
    architecture_size: str = Field(min_length=1, max_length=16)
    display_name_suffix: str = Field(min_length=2, max_length=80)
    default_training_profile: str = Field(default="safe-default", min_length=2, max_length=120)
    defaults: ModelTemplateDefaults = Field(default_factory=ModelTemplateDefaults)
    default_promotion_policy: PromotionCriteriaPolicy | None = None
    recommended: bool = False
    active: bool = True


class ModelTemplateUpdateRequest(BaseModel):
    label: str | None = Field(default=None, min_length=3, max_length=160)
    description: str | None = Field(default=None, min_length=3, max_length=500)
    display_name_suffix: str | None = Field(default=None, min_length=2, max_length=80)
    default_training_profile: str | None = Field(default=None, min_length=2, max_length=120)
    defaults: ModelTemplateDefaults | None = None
    default_promotion_policy: PromotionCriteriaPolicy | None = None
    recommended: bool | None = None
    active: bool | None = None
