from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AuditEventSummary(BaseModel):
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    sequence_no: int = Field(ge=1)
    source_service: str
    actor_type: str
    actor_id: str
    request_id: str | None = None
    correlation_id: str | None = None
    occurred_at: str


class AuditEventDetail(AuditEventSummary):
    causation_id: str | None = None
    payload_schema_version: int = Field(default=1, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)
