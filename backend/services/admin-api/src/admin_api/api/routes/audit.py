from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from admin_api.dependencies import build_meta, get_settings, require_permission
from cv_contracts import AuditEventDetail, AuditEventSummary, ListResponse, WebhookEventDetail, WebhookEventSummary
from cv_db import (
    get_audit_event_detail,
    get_webhook_event_detail,
    list_audit_events,
    list_webhook_events,
    session_scope,
)
from cv_contracts import SessionUser
from cv_status import UserRole
from cv_utils import NotFoundError

router = APIRouter(tags=["audit"])

AuditReader = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "audit.read",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
            UserRole.LABEL_MANAGER,
            UserRole.VIEWER,
        )
    ),
]


@router.get("/audit-events", response_model=ListResponse[AuditEventSummary])
def get_audit_events(
    _current_user: AuditReader,
    aggregate_type: str | None = None,
    aggregate_id: str | None = None,
    event_type: str | None = None,
    source_service: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    settings=Depends(get_settings),
) -> ListResponse[AuditEventSummary]:
    with session_scope(settings) as session:
        items = list_audit_events(
            session,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            source_service=source_service,
            limit=limit,
        )
    return ListResponse(items=items, meta=build_meta())


@router.get("/audit-events/{event_id}", response_model=AuditEventDetail)
def get_audit_event(
    event_id: str,
    _current_user: AuditReader,
    settings=Depends(get_settings),
) -> AuditEventDetail:
    with session_scope(settings) as session:
        item = get_audit_event_detail(session, event_id)
    if item is None:
        raise NotFoundError(
            message="Audit event not found",
            detail=f"No audit event exists for id {event_id}.",
        )
    return item


@router.get("/webhook-events", response_model=ListResponse[WebhookEventSummary])
def get_webhook_events(
    _current_user: AuditReader,
    settings=Depends(get_settings),
) -> ListResponse[WebhookEventSummary]:
    with session_scope(settings) as session:
        items = list_webhook_events(session)
    return ListResponse(items=items, meta=build_meta())


@router.get("/webhook-events/{event_id}", response_model=WebhookEventDetail)
def get_webhook_event(
    event_id: str,
    _current_user: AuditReader,
    settings=Depends(get_settings),
) -> WebhookEventDetail:
    with session_scope(settings) as session:
        item = get_webhook_event_detail(session, event_id)
    if item is None:
        raise NotFoundError(
            message="Webhook event not found",
            detail=f"No webhook event exists for id {event_id}.",
        )
    return item
