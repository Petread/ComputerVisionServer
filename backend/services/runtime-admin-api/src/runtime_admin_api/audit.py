from __future__ import annotations

from cv_contracts import SessionUser
from cv_core import MachineIdentity, PlatformSettings
from cv_db import create_audit_event, session_scope
from cv_utils import utc_now_iso
from fastapi import Request


def write_runtime_audit_event(
    *,
    settings: PlatformSettings,
    request: Request,
    current_user: SessionUser | MachineIdentity,
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
            source_service="runtime-admin-api",
            actor_type=_audit_actor_type(current_user),
            actor_id=_audit_actor_id(current_user),
            payload=enriched_payload,
        )


def _audit_actor_type(principal: SessionUser | MachineIdentity) -> str:
    if isinstance(principal, MachineIdentity):
        return principal.subject_type
    return "user"


def _audit_actor_id(principal: SessionUser | MachineIdentity) -> str:
    if isinstance(principal, MachineIdentity):
        return principal.subject_id or principal.key_id
    return principal.username
