from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

from cv_status import HealthState, QueueName, ServiceName

T = TypeVar("T")


class ValidationErrorDetail(BaseModel):
    field: str | None = None
    location: list[str | int] = Field(default_factory=list)
    message: str
    error_type: str | None = None


class ApiError(BaseModel):
    code: str
    message: str
    detail: str | None = None
    status_code: int
    correlation_id: str | None = None
    retryable: bool = False
    validation_errors: list[ValidationErrorDetail] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResponseMeta(BaseModel):
    request_id: str | None = None
    correlation_id: str | None = None
    generated_at: str


class HealthCheck(BaseModel):
    name: str
    state: HealthState
    detail: str
    observed_at: str


class HealthResponse(BaseModel):
    service: ServiceName | str
    state: HealthState
    checks: list[HealthCheck]
    meta: ResponseMeta


class ServiceSummary(BaseModel):
    name: ServiceName | str
    state: HealthState
    detail: str
    last_heartbeat_at: str | None = None


class ListResponse(BaseModel, Generic[T]):
    items: list[T]
    meta: ResponseMeta


class AcceptedJob(BaseModel):
    job_id: str
    queue: QueueName
    status: str
    accepted_at: str


class StatusCard(BaseModel):
    key: str
    label: str
    value: str
    state: HealthState = HealthState.OK
