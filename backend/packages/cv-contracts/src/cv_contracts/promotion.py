from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PromotionCriterion(BaseModel):
    metric_name: str = Field(min_length=2, max_length=120)
    comparison: Literal["gte", "lte"]
    threshold: float
    phase: Literal["validation", "evaluation"] = "evaluation"


class PromotionCriterionResult(BaseModel):
    metric_name: str
    comparison: Literal["gte", "lte"]
    threshold: float
    phase: Literal["validation", "evaluation"]
    actual_value: float | None = None
    passed: bool = False


class PromotionCriteriaPolicy(BaseModel):
    criteria: list[PromotionCriterion] = Field(default_factory=list, min_length=1)
    allow_model_override: bool = True
