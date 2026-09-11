from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EventEnvelope(BaseModel):
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
    causation_id: str | None = None
    payload_schema_version: int = Field(default=1, ge=1)
    occurred_at: str
    payload: dict[str, Any] = Field(default_factory=dict)


class StateChangePayload(BaseModel):
    previous_state: str | None = None
    current_state: str
    reason: str | None = None


class WebhookReceivedPayload(BaseModel):
    source: str
    action: str
    source_event_key: str

