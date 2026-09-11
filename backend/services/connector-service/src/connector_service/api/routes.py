from typing import Annotated

from cv_contracts import ResponseMeta
from cv_contracts.runtime import (
    RuntimeBoundaryResponse,
    RuntimeConnectorDeliveryHistoryResponse,
    RuntimeConnectorDrainRequest,
    RuntimeConnectorDrainResponse,
    RuntimeOutputConnectorListResponse,
    RuntimeOutputConnectorTypeCatalogResponse,
)
from cv_core import PlatformSettings, parse_machine_token
from cv_db import (
    authenticate_machine_identity,
    connector_delivery_history_response,
    list_output_connectors_response,
    session_scope,
)
from cv_runtime import runtime_service_boundary
from cv_runtime.connectors import output_connector_type_catalog
from cv_utils import AuthenticationError, get_correlation_id, get_request_id, utc_now_iso
from fastapi import APIRouter, Depends, Query, Request

from connector_service import SERVICE_NAME
from connector_service.executor import drain_connector_work

router = APIRouter(tags=["runtime-boundary"])


def get_settings(request: Request) -> PlatformSettings:
    return request.app.state.settings


def require_connector_drain_access(
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
        raise AuthenticationError("Machine authentication is required for connector drain APIs.")
    key_id, secret = machine_token
    authenticate_machine_identity(
        settings,
        key_id=key_id,
        secret=secret,
        required_scopes={"runtime.connector.drain"},
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


@router.get(
    "/runtime/output-connectors/types",
    response_model=RuntimeOutputConnectorTypeCatalogResponse,
)
def list_connector_types() -> RuntimeOutputConnectorTypeCatalogResponse:
    return RuntimeOutputConnectorTypeCatalogResponse(
        items=output_connector_type_catalog(),
        meta=_meta(),
    )


@router.get("/runtime/output-connectors", response_model=RuntimeOutputConnectorListResponse)
def list_output_connectors(
    settings: Annotated[PlatformSettings, Depends(get_settings)],
    channel_id: str | None = Query(default=None, min_length=3, max_length=64),
    connector_type: str | None = Query(default=None, min_length=3, max_length=120),
) -> RuntimeOutputConnectorListResponse:
    with session_scope(settings) as session:
        return list_output_connectors_response(
            session,
            channel_id=channel_id,
            connector_type=connector_type,
            meta=_meta(),
        )


@router.get(
    "/runtime/connector-deliveries",
    response_model=RuntimeConnectorDeliveryHistoryResponse,
)
def list_connector_deliveries(
    settings: Annotated[PlatformSettings, Depends(get_settings)],
    channel_id: str | None = Query(default=None, min_length=3, max_length=64),
    output_connector_id: str | None = Query(default=None, min_length=3, max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
) -> RuntimeConnectorDeliveryHistoryResponse:
    with session_scope(settings) as session:
        return connector_delivery_history_response(
            session,
            channel_id=channel_id,
            output_connector_id=output_connector_id,
            limit=limit,
            meta=_meta(),
        )


@router.post("/runtime/connector-deliveries:drain", response_model=RuntimeConnectorDrainResponse)
def drain_connector_deliveries(
    payload: RuntimeConnectorDrainRequest,
    settings: Annotated[PlatformSettings, Depends(get_settings)],
    _access: Annotated[None, Depends(require_connector_drain_access)],
) -> RuntimeConnectorDrainResponse:
    stats = drain_connector_work(settings=settings, limit=payload.limit)
    return RuntimeConnectorDrainResponse(
        processed=stats.processed,
        delivered=stats.delivered,
        failed_retryable=stats.failed_retryable,
        failed_terminal=stats.failed_terminal,
        skipped_no_payload=stats.skipped_no_payload,
        meta=_meta(),
    )


def _meta() -> ResponseMeta:
    return ResponseMeta(
        request_id=get_request_id(),
        correlation_id=get_correlation_id(),
        generated_at=utc_now_iso(),
    )
