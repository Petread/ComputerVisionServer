from typing import Annotated, Literal

from cv_contracts import ResponseMeta
from cv_contracts.runtime import (
    RuntimeBoundaryResponse,
    RuntimeCaptureDrainRequest,
    RuntimeCaptureDrainResponse,
    RuntimeCaptureHistoryResponse,
)
from cv_core import PlatformSettings, parse_machine_token
from cv_db import authenticate_machine_identity, list_runtime_capture_history, session_scope
from cv_runtime import runtime_service_boundary
from cv_utils import AuthenticationError, get_correlation_id, get_request_id, utc_now_iso
from fastapi import APIRouter, Depends, Query, Request

from capture_service import SERVICE_NAME
from capture_service.executor import drain_capture_work

router = APIRouter(tags=["runtime-boundary"])


def get_settings(request: Request) -> PlatformSettings:
    return request.app.state.settings


def require_capture_drain_access(
    request: Request,
    settings: Annotated[PlatformSettings, Depends(get_settings)],
) -> None:
    if not settings.api_keys.enabled:
        return None
    machine_token = parse_machine_token(
        authorization_header=request.headers.get(settings.api_keys.authorization_header_name),
        legacy_api_key_header=request.headers.get(settings.api_keys.legacy_header_name),
    )
    if machine_token is None:
        raise AuthenticationError("Machine authentication is required for capture drain APIs.")
    key_id, secret = machine_token
    authenticate_machine_identity(
        settings,
        key_id=key_id,
        secret=secret,
        required_scopes={"runtime.capture.drain"},
        allowed_subject_types={"service"},
    )
    return None


@router.get("/runtime/boundary", response_model=RuntimeBoundaryResponse)
def get_runtime_boundary() -> RuntimeBoundaryResponse:
    boundary = runtime_service_boundary(SERVICE_NAME)
    return RuntimeBoundaryResponse(
        service_name=boundary.service_name,
        responsibilities=list(boundary.responsibilities),
        non_responsibilities=list(boundary.non_responsibilities),
    )


@router.get("/runtime/captures", response_model=RuntimeCaptureHistoryResponse)
def list_capture_history(
    settings: Annotated[PlatformSettings, Depends(get_settings)],
    channel_id: str | None = Query(default=None, min_length=3, max_length=64),
    source_type: Literal["manual", "pipeline", "temporal_sample"] | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> RuntimeCaptureHistoryResponse:
    with session_scope(settings) as session:
        items, total = list_runtime_capture_history(
            session,
            channel_id=channel_id,
            source_type=source_type,
            limit=limit,
        )
    return RuntimeCaptureHistoryResponse(items=items, total=total, meta=_meta())


@router.post("/runtime/captures:drain", response_model=RuntimeCaptureDrainResponse)
def drain_captures(
    payload: RuntimeCaptureDrainRequest,
    settings: Annotated[PlatformSettings, Depends(get_settings)],
    _access: Annotated[None, Depends(require_capture_drain_access)],
) -> RuntimeCaptureDrainResponse:
    stats = drain_capture_work(settings=settings, limit=payload.limit)
    return RuntimeCaptureDrainResponse(
        processed=stats.processed,
        delivered=stats.delivered,
        failed_retryable=stats.failed_retryable,
        failed_terminal=stats.failed_terminal,
        skipped_duplicate=stats.skipped_duplicate,
        rate_limited=stats.rate_limited,
        needs_attention=stats.needs_attention,
        enqueued_interval_captures=stats.enqueued_interval_captures,
        meta=_meta(),
    )


def _meta() -> ResponseMeta:
    return ResponseMeta(
        request_id=get_request_id(),
        correlation_id=get_correlation_id(),
        generated_at=utc_now_iso(),
    )
