from __future__ import annotations

from pydantic import BaseModel, Field

from cv_utils.timestamps import utc_now_iso


class ActorRef(BaseModel):
    actor_type: str = Field(default="system")
    actor_id: str = Field(default="system")


class AuditContext(BaseModel):
    request_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    occurred_at: str = Field(default_factory=utc_now_iso)
    actor: ActorRef = Field(default_factory=ActorRef)

