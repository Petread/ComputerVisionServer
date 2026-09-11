from __future__ import annotations

from datetime import timedelta
from typing import Any

from cv_contracts.common import ResponseMeta
from cv_contracts.runtime import (
    RuntimeConnectorDeliveryHistoryResponse,
    RuntimeConnectorDeliverySummary,
    RuntimeOutputConnectorCreateRequest,
    RuntimeOutputConnectorDetail,
    RuntimeOutputConnectorListResponse,
    RuntimeOutputConnectorSummary,
    RuntimeOutputConnectorUpdateRequest,
)
from cv_core import generate_prefixed_id
from cv_runtime import (
    connector_display_name,
    normalize_connector_delivery_policy,
    validate_output_connector_config,
)
from cv_runtime.payloads import RuntimeExecutionResult
from cv_status import RuntimeResourceStatus
from cv_utils import utc_now, utc_now_iso
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from cv_db.models import OutputConnector, ResultRecord, RuntimeChannel, RuntimeConnectorDelivery

TERMINAL_CONNECTOR_DELIVERY_STATUSES = {
    "delivered",
    "failed_terminal",
    "skipped_no_payload",
}

DUE_CONNECTOR_DELIVERY_STATUSES = {
    "queued",
    "failed_retryable",
}


def create_output_connector(
    session: Session,
    payload: RuntimeOutputConnectorCreateRequest,
    *,
    created_by: str | None,
) -> RuntimeOutputConnectorDetail:
    _get_channel(session, payload.channel_id)
    config = validate_output_connector_config(
        payload.connector_type,
        payload.config,
        secret_refs=payload.secret_refs,
    )
    policy = normalize_connector_delivery_policy(payload.delivery_policy)
    row = OutputConnector(
        id=generate_prefixed_id("ocn"),
        channel_id=payload.channel_id,
        pipeline_version_id=None,
        connector_type=payload.connector_type,
        name=payload.name,
        source_node_key=payload.source_node_key,
        enabled=payload.enabled,
        config=config,
        secret_refs=dict(payload.secret_refs),
        delivery_policy=policy.model_dump(mode="json"),
        status=(
            RuntimeResourceStatus.ACTIVE.value
            if payload.enabled
            else RuntimeResourceStatus.DISABLED.value
        ),
        created_by=created_by,
        updated_by=created_by,
    )
    session.add(row)
    session.flush()
    return output_connector_to_detail(row)


def update_output_connector(
    session: Session,
    output_connector_id: str,
    payload: RuntimeOutputConnectorUpdateRequest,
    *,
    updated_by: str | None,
) -> RuntimeOutputConnectorDetail:
    row = _get_output_connector(session, output_connector_id)
    fields_set = payload.model_fields_set
    next_config = dict(row.config or {}) if "config" not in fields_set else dict(payload.config or {})
    next_secret_refs = (
        dict(row.secret_refs or {})
        if "secret_refs" not in fields_set
        else dict(payload.secret_refs or {})
    )
    row.config = validate_output_connector_config(
        row.connector_type,
        next_config,
        secret_refs=next_secret_refs,
    )
    row.secret_refs = next_secret_refs
    if payload.name is not None:
        row.name = payload.name
    if "source_node_key" in fields_set:
        row.source_node_key = payload.source_node_key
    if payload.delivery_policy is not None:
        row.delivery_policy = normalize_connector_delivery_policy(payload.delivery_policy).model_dump(
            mode="json"
        )
    if payload.enabled is not None:
        row.enabled = payload.enabled
        row.status = (
            RuntimeResourceStatus.ACTIVE.value
            if payload.enabled
            else RuntimeResourceStatus.DISABLED.value
        )
    row.updated_by = updated_by
    row.updated_at = utc_now()
    session.flush()
    return output_connector_to_detail(row)


def get_output_connector_detail(
    session: Session,
    output_connector_id: str,
) -> RuntimeOutputConnectorDetail | None:
    row = session.get(OutputConnector, output_connector_id)
    if row is None:
        return None
    return output_connector_to_detail(row)


def list_output_connectors(
    session: Session,
    *,
    channel_id: str | None = None,
    connector_type: str | None = None,
) -> list[RuntimeOutputConnectorSummary]:
    query = select(OutputConnector).order_by(
        OutputConnector.channel_id.asc(),
        OutputConnector.connector_type.asc(),
        OutputConnector.name.asc(),
    )
    if channel_id:
        query = query.where(OutputConnector.channel_id == channel_id)
    if connector_type:
        query = query.where(OutputConnector.connector_type == connector_type)
    return [output_connector_to_summary(row) for row in session.scalars(query).all()]


def list_output_connectors_response(
    session: Session,
    *,
    meta: ResponseMeta,
    channel_id: str | None = None,
    connector_type: str | None = None,
) -> RuntimeOutputConnectorListResponse:
    return RuntimeOutputConnectorListResponse(
        items=list_output_connectors(
            session,
            channel_id=channel_id,
            connector_type=connector_type,
        ),
        meta=meta,
    )


def enqueue_connector_deliveries_for_result(
    session: Session,
    *,
    result_record: ResultRecord,
    execution_result: RuntimeExecutionResult,
) -> list[RuntimeConnectorDelivery]:
    rows: list[RuntimeConnectorDelivery] = []
    for connector_type, output_entry in _iter_result_output_entries(execution_result):
        source_node_key = _entry_source_node_key(output_entry)
        connectors = _matching_connectors(
            session,
            channel_id=result_record.channel_id,
            connector_type=connector_type,
            source_node_key=source_node_key,
        )
        if not connectors:
            connectors = [
                _create_pipeline_output_connector(
                    session,
                    result_record=result_record,
                    connector_type=connector_type,
                    source_node_key=source_node_key,
                )
            ]
        active_connectors = [
            connector
            for connector in connectors
            if bool(connector.enabled)
            and connector.status != RuntimeResourceStatus.DISABLED.value
        ]
        for connector in active_connectors:
            policy = normalize_connector_delivery_policy(connector.delivery_policy)
            delivery = RuntimeConnectorDelivery(
                id=generate_prefixed_id("rcd"),
                output_connector_id=connector.id,
                channel_id=result_record.channel_id,
                result_record_id=result_record.id,
                connector_type=connector.connector_type,
                source_node_key=source_node_key,
                status="queued",
                request_payload=_delivery_request_payload(
                    result_record=result_record,
                    execution_result=execution_result,
                    output_entry=output_entry,
                ),
                response_payload={},
                retry_count=0,
                max_attempts=policy.max_attempts,
                queued_at=utc_now(),
            )
            connector.last_delivery_status = "queued"
            connector.last_error = None
            session.add(delivery)
            rows.append(delivery)
    session.flush()
    return rows


def claim_due_connector_deliveries(
    session: Session,
    *,
    limit: int = 20,
) -> list[RuntimeConnectorDelivery]:
    now = utc_now()
    rows = session.scalars(
        select(RuntimeConnectorDelivery)
        .where(
            RuntimeConnectorDelivery.status.in_(DUE_CONNECTOR_DELIVERY_STATUSES),
            or_(
                RuntimeConnectorDelivery.next_attempt_at.is_(None),
                RuntimeConnectorDelivery.next_attempt_at <= now,
            ),
        )
        .order_by(RuntimeConnectorDelivery.created_at.asc())
        .limit(limit)
    ).all()
    for row in rows:
        row.status = "processing"
        row.claimed_at = now
    session.flush()
    return list(rows)


def mark_connector_delivery_succeeded(
    session: Session,
    delivery: RuntimeConnectorDelivery,
    *,
    response_payload: dict[str, Any],
    status: str = "delivered",
) -> RuntimeConnectorDelivery:
    now = utc_now()
    delivery.status = status
    delivery.response_payload = response_payload
    delivery.delivered_at = now if status == "delivered" else delivery.delivered_at
    delivery.completed_at = now
    delivery.next_attempt_at = None
    delivery.last_error = None
    connector = session.get(OutputConnector, delivery.output_connector_id)
    if connector is not None:
        connector.status = RuntimeResourceStatus.ACTIVE.value if connector.enabled else RuntimeResourceStatus.DISABLED.value
        connector.last_delivery_status = status
        connector.last_delivery_at = now
        if status == "delivered":
            connector.last_success_at = now
        connector.last_error = None
        connector.updated_at = now
    session.flush()
    return delivery


def mark_connector_delivery_failed(
    session: Session,
    delivery: RuntimeConnectorDelivery,
    *,
    error: str,
    retryable: bool,
    response_payload: dict[str, Any] | None = None,
) -> RuntimeConnectorDelivery:
    now = utc_now()
    attempts = int(delivery.retry_count or 0) + 1
    connector = session.get(OutputConnector, delivery.output_connector_id)
    policy = normalize_connector_delivery_policy(
        connector.delivery_policy if connector is not None else None
    )
    delivery.retry_count = attempts
    if retryable and attempts < int(delivery.max_attempts or 3):
        delivery.status = "failed_retryable"
        delivery.next_attempt_at = now + timedelta(
            seconds=_retry_delay_seconds(policy.model_dump(mode="json"), attempts)
        )
    else:
        delivery.status = "failed_terminal"
        delivery.next_attempt_at = None
        delivery.completed_at = now
    delivery.last_error = error
    delivery.response_payload = response_payload or {}
    if connector is not None:
        connector.status = (
            RuntimeResourceStatus.DEGRADED.value
            if delivery.status == "failed_retryable"
            else RuntimeResourceStatus.FAILED.value
        )
        connector.last_delivery_status = delivery.status
        connector.last_delivery_at = now
        connector.last_error = error
        connector.updated_at = now
    session.flush()
    return delivery


def list_connector_delivery_history(
    session: Session,
    *,
    channel_id: str | None = None,
    output_connector_id: str | None = None,
    limit: int = 50,
) -> tuple[list[RuntimeConnectorDeliverySummary], int]:
    filters = []
    if channel_id:
        filters.append(RuntimeConnectorDelivery.channel_id == channel_id)
    if output_connector_id:
        filters.append(RuntimeConnectorDelivery.output_connector_id == output_connector_id)
    total_query = select(func.count(RuntimeConnectorDelivery.id))
    query = (
        select(RuntimeConnectorDelivery)
        .order_by(RuntimeConnectorDelivery.created_at.desc())
        .limit(limit)
    )
    if filters:
        total_query = total_query.where(*filters)
        query = query.where(*filters)
    total = int(session.scalar(total_query) or 0)
    rows = session.scalars(query).all()
    connectors = {
        connector.id: connector
        for connector in session.scalars(
            select(OutputConnector).where(
                OutputConnector.id.in_({row.output_connector_id for row in rows})
            )
        ).all()
    } if rows else {}
    return [
        connector_delivery_to_summary(row, connectors.get(row.output_connector_id))
        for row in rows
    ], total


def connector_delivery_history_response(
    session: Session,
    *,
    meta: ResponseMeta,
    channel_id: str | None = None,
    output_connector_id: str | None = None,
    limit: int = 50,
) -> RuntimeConnectorDeliveryHistoryResponse:
    items, total = list_connector_delivery_history(
        session,
        channel_id=channel_id,
        output_connector_id=output_connector_id,
        limit=limit,
    )
    return RuntimeConnectorDeliveryHistoryResponse(items=items, total=total, meta=meta)


def output_connector_to_summary(row: OutputConnector) -> RuntimeOutputConnectorSummary:
    policy = normalize_connector_delivery_policy(row.delivery_policy)
    return RuntimeOutputConnectorSummary(
        output_connector_id=row.id,
        channel_id=row.channel_id,
        pipeline_version_id=row.pipeline_version_id,
        connector_type=row.connector_type,
        name=row.name,
        source_node_key=row.source_node_key,
        enabled=bool(row.enabled),
        config=dict(row.config or {}),
        secret_refs=dict(row.secret_refs or {}),
        delivery_policy=policy,
        status=RuntimeResourceStatus(row.status),
        last_delivery_status=_delivery_status(row.last_delivery_status),
        last_delivery_at=_iso(row.last_delivery_at),
        last_success_at=_iso(row.last_success_at),
        last_error=row.last_error,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def output_connector_to_detail(row: OutputConnector) -> RuntimeOutputConnectorDetail:
    return RuntimeOutputConnectorDetail(
        **output_connector_to_summary(row).model_dump(mode="json"),
        created_by=row.created_by,
        updated_by=row.updated_by,
    )


def connector_delivery_to_summary(
    row: RuntimeConnectorDelivery,
    connector: OutputConnector | None = None,
) -> RuntimeConnectorDeliverySummary:
    return RuntimeConnectorDeliverySummary(
        connector_delivery_id=row.id,
        output_connector_id=row.output_connector_id,
        channel_id=row.channel_id,
        result_record_id=row.result_record_id,
        connector_type=row.connector_type,
        connector_name=connector.name if connector is not None else None,
        source_node_key=row.source_node_key,
        status=_delivery_status(row.status) or "queued",
        retry_count=int(row.retry_count or 0),
        max_attempts=int(row.max_attempts or 3),
        queued_at=_iso(row.queued_at),
        claimed_at=_iso(row.claimed_at),
        delivered_at=_iso(row.delivered_at),
        completed_at=_iso(row.completed_at),
        next_attempt_at=_iso(row.next_attempt_at),
        request_payload=dict(row.request_payload or {}),
        response_payload=dict(row.response_payload or {}),
        last_error=row.last_error,
        created_at=_iso(row.created_at),
    )


def _matching_connectors(
    session: Session,
    *,
    channel_id: str,
    connector_type: str,
    source_node_key: str | None,
) -> list[OutputConnector]:
    rows = session.scalars(
        select(OutputConnector).where(
            OutputConnector.channel_id == channel_id,
            OutputConnector.connector_type == connector_type,
            or_(
                OutputConnector.source_node_key.is_(None),
                OutputConnector.source_node_key == source_node_key,
            ),
        )
    ).all()
    return list(rows)


def _create_pipeline_output_connector(
    session: Session,
    *,
    result_record: ResultRecord,
    connector_type: str,
    source_node_key: str | None,
) -> OutputConnector:
    config = validate_output_connector_config(connector_type, {}, secret_refs={})
    policy = normalize_connector_delivery_policy(None)
    node_suffix = f" ({source_node_key})" if source_node_key else ""
    row = OutputConnector(
        id=generate_prefixed_id("ocn"),
        channel_id=result_record.channel_id,
        pipeline_version_id=result_record.pipeline_version_id,
        connector_type=connector_type,
        name=f"{connector_display_name(connector_type)}{node_suffix}",
        source_node_key=source_node_key,
        enabled=True,
        config=config,
        secret_refs={},
        delivery_policy=policy.model_dump(mode="json"),
        status=RuntimeResourceStatus.ACTIVE.value,
        created_by="runtime",
        updated_by="runtime",
    )
    session.add(row)
    session.flush()
    return row


def _iter_result_output_entries(
    result: RuntimeExecutionResult,
) -> list[tuple[str, dict[str, Any]]]:
    entries: list[tuple[str, dict[str, Any]]] = []
    for connector_type, section in (
        ("output.overlay", result.overlay_outputs),
        ("output.http_trigger_response", result.http_trigger_responses),
        ("output.persisted_result", result.persisted_results),
        ("output.internal_event", result.internal_events),
    ):
        for entry in section:
            if isinstance(entry, dict):
                entries.append((connector_type, entry))
    return entries


def _entry_source_node_key(entry: dict[str, Any]) -> str | None:
    raw = entry.get("node_key")
    return str(raw) if raw else None


def _delivery_request_payload(
    *,
    result_record: ResultRecord,
    execution_result: RuntimeExecutionResult,
    output_entry: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "runtime.connector.delivery-request.v1",
        "channel_id": result_record.channel_id,
        "result_record_id": result_record.id,
        "pipeline_version_id": result_record.pipeline_version_id,
        "runtime_execution_id": result_record.runtime_execution_id,
        "trigger_endpoint_id": result_record.trigger_endpoint_id,
        "correlation_id": result_record.correlation_id or execution_result.correlation_id,
        "completed_at": _iso(result_record.completed_at) or utc_now_iso(),
        "output": output_entry,
    }


def _retry_delay_seconds(policy: object, attempt: int) -> int:
    normalized = normalize_connector_delivery_policy(policy if isinstance(policy, dict) else None)
    policy_values = normalized.retry_backoff_seconds or [5, 30, 120]
    return policy_values[min(max(attempt - 1, 0), len(policy_values) - 1)]


def _delivery_status(value: str | None) -> str | None:
    if value in {
        "queued",
        "processing",
        "delivered",
        "failed_retryable",
        "failed_terminal",
        "skipped_no_payload",
    }:
        return value
    return None


def _get_channel(session: Session, channel_id: str) -> RuntimeChannel:
    row = session.get(RuntimeChannel, channel_id)
    if row is None or row.deleted_at is not None:
        raise ValueError(f"Runtime channel {channel_id} does not exist.")
    return row


def _get_output_connector(session: Session, output_connector_id: str) -> OutputConnector:
    row = session.get(OutputConnector, output_connector_id)
    if row is None:
        raise ValueError(f"Output connector {output_connector_id} does not exist.")
    return row


def _iso(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat()).replace("+00:00", "Z")
    return str(value)
