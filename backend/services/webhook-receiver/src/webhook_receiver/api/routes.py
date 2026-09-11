from __future__ import annotations

import hashlib
import ipaddress
import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from redis import Redis
from rq import Queue, Retry

from cv_contracts import (
    ListResponse,
    ResponseMeta,
    WebhookAckResponse,
    WebhookEventDetail,
    WebhookEventSummary,
)
from cv_db import (
    create_webhook_event,
    get_webhook_event_detail,
    list_webhook_events,
    session_scope,
    update_webhook_event,
)
from cv_status import QueueName, WebhookProcessingStatus
from cv_utils import NotFoundError, PlatformError, get_correlation_id, get_request_id, utc_now_iso

router = APIRouter()


def get_settings(request: Request):
    return request.app.state.settings


def build_meta() -> ResponseMeta:
    return ResponseMeta(
        request_id=get_request_id(),
        correlation_id=get_correlation_id(),
        generated_at=utc_now_iso(),
    )


@router.post("/webhooks/label-studio", response_model=WebhookAckResponse, tags=["webhooks"])
async def receive_label_studio_webhook(
    request: Request,
    settings=Depends(get_settings),
) -> WebhookAckResponse:
    raw_body = await request.body()
    headers = {key: value for key, value in request.headers.items()}
    remote_addr = _remote_addr(request)
    payload = _parse_payload(raw_body)
    action = str(payload.get("action") or "unknown")
    project_id = _label_studio_project_id(payload)
    source_event_key = _source_event_key(payload, raw_body)
    signature_valid = _validate_signature(request, settings)
    network_valid = _validate_remote_addr(remote_addr, settings)

    with session_scope(settings) as session:
        event_id, created = create_webhook_event(
            session,
            source="label-studio",
            source_event_key=source_event_key,
            action=action,
            payload=payload,
            headers=headers,
            remote_addr=remote_addr,
            signature_valid=signature_valid,
            label_studio_project_id=project_id,
        )
        if created and (not signature_valid or not network_valid):
            update_webhook_event(
                session,
                event_id,
                processing_status=WebhookProcessingStatus.FAILED,
                processing_error=(
                    "Webhook rejected because the shared secret or source network did not match the configured policy."
                ),
                mark_processed=True,
            )

    if not signature_valid:
        raise PlatformError(
            code="webhook_signature_invalid",
            message="The Label Studio webhook signature could not be verified.",
            detail="Please verify the configured shared secret header in Label Studio and the webhook receiver settings.",
            status_code=403,
        )
    if not network_valid:
        raise PlatformError(
            code="webhook_source_forbidden",
            message="The Label Studio webhook source address is not allowed.",
            detail="The request did not originate from an approved Label Studio network range.",
            status_code=403,
        )

    if created:
        queue = Queue(QueueName.WEBHOOK.value, connection=Redis.from_url(settings.redis.url()))
        queue.enqueue(
            "control_worker.jobs.webhook.process_labelstudio_event",
            event_id=event_id,
            retry=Retry(max=3, interval=[5, 30, 120]),
        )
    return WebhookAckResponse(
        event_id=event_id,
        accepted=True,
        duplicate=not created,
        received_at=utc_now_iso(),
    )


@router.get("/webhook-events", response_model=ListResponse[WebhookEventSummary], tags=["webhooks"])
def get_webhook_event_list(settings=Depends(get_settings)) -> ListResponse[WebhookEventSummary]:
    with session_scope(settings) as session:
        items = list_webhook_events(session)
    return ListResponse(items=items, meta=build_meta())


@router.get("/webhook-events/{event_id}", response_model=WebhookEventDetail, tags=["webhooks"])
def get_webhook_event(event_id: str, settings=Depends(get_settings)) -> WebhookEventDetail:
    with session_scope(settings) as session:
        item = get_webhook_event_detail(session, event_id)
    if item is None:
        raise NotFoundError(
            message="Webhook event not found",
            detail=f"No webhook event exists for id {event_id}.",
        )
    return item


def _parse_payload(raw_body: bytes) -> dict[str, Any]:
    if not raw_body:
        return {}
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlatformError(
            code="webhook_payload_invalid",
            message="The webhook payload is not valid JSON.",
            detail=str(exc),
            status_code=400,
        ) from exc
    if not isinstance(parsed, dict):
        raise PlatformError(
            code="webhook_payload_invalid",
            message="The webhook payload is not a JSON object.",
            detail="Label Studio webhook payloads must be JSON objects.",
            status_code=400,
        )
    return parsed


def _source_event_key(payload: dict[str, Any], raw_body: bytes) -> str:
    action = str(payload.get("action") or "unknown")
    project_id = _label_studio_project_id(payload)
    entity_id = (
        payload.get("event_id")
        or payload.get("id")
        or _nested_id(payload.get("annotation"))
        or _nested_id(payload.get("task"))
        or _nested_id(payload.get("review"))
    )
    if entity_id is not None:
        return f"{action}:{project_id or 'na'}:{entity_id}"
    digest = hashlib.sha256(raw_body).hexdigest()
    return f"{action}:{project_id or 'na'}:{digest}"


def _nested_id(value: Any) -> str | None:
    if isinstance(value, dict) and value.get("id") is not None:
        return str(value["id"])
    return None


def _label_studio_project_id(payload: dict[str, Any]) -> int | None:
    project_value = payload.get("project") or payload.get("project_id")
    if isinstance(project_value, dict):
        project_value = project_value.get("id")
    try:
        return int(project_value) if project_value is not None else None
    except (TypeError, ValueError):
        return None


def _remote_addr(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", maxsplit=1)[0].strip()
    return request.client.host if request.client else None


def _validate_signature(request: Request, settings) -> bool:
    auth_settings = settings.label_studio.webhook_auth
    if not auth_settings.enabled:
        return True
    expected_secret = auth_settings.secret()
    if not expected_secret:
        raise PlatformError(
            code="webhook_auth_misconfigured",
            message="Webhook authentication is enabled but no shared secret is configured.",
            detail="Configure the Label Studio webhook secret file before enabling signature validation.",
            status_code=503,
        )
    header_value = request.headers.get(auth_settings.header_name)
    return header_value == expected_secret


def _validate_remote_addr(remote_addr: str | None, settings) -> bool:
    allowed_cidrs = settings.label_studio.webhook_auth.allowed_cidrs
    if not allowed_cidrs:
        return True
    if not remote_addr:
        return False
    try:
        address = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    return any(address in ipaddress.ip_network(cidr) for cidr in allowed_cidrs)
