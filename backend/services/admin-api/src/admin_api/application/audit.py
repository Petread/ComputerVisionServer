from __future__ import annotations

from fastapi import Request

from cv_contracts import SessionUser
from cv_db import create_audit_event, session_scope
from cv_utils import utc_now_iso


def write_admin_audit_event(
    *,
    settings,
    request: Request,
    current_user: SessionUser,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict[str, object] | None = None,
) -> None:
    enriched_payload = dict(payload or {})
    required_permission = getattr(request.state, "required_permission", None)
    if required_permission:
        enriched_payload.setdefault("required_permission", required_permission)
    enriched_payload.setdefault("method", request.method.upper())
    enriched_payload.setdefault("path", request.url.path)
    enriched_payload.setdefault("recorded_at", utc_now_iso())

    with session_scope(settings) as session:
        create_audit_event(
            session,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            source_service="admin-api",
            actor_type="user",
            actor_id=current_user.username,
            payload=enriched_payload,
        )
