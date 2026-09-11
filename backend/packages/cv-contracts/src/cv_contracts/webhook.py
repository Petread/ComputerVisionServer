from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from cv_status import WebhookProcessingStatus


class WebhookAckResponse(BaseModel):
    event_id: str
    accepted: bool
    duplicate: bool = False
    received_at: str


class WebhookEventSummary(BaseModel):
    event_id: str
    source: str
    source_event_key: str
    action: str
    label_studio_project_id: int | None = None
    label_project_id: str | None = None
    signature_valid: bool | None = None
    internal_event_type: str | None = None
    processing_status: WebhookProcessingStatus
    received_at: str
    processed_at: str | None = None


class WebhookEventDetail(WebhookEventSummary):
    remote_addr: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    processing_error: str | None = None
