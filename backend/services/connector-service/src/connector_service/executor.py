from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from cv_core import PlatformSettings
from cv_db import (
    claim_due_connector_deliveries,
    create_audit_event,
    mark_connector_delivery_failed,
    mark_connector_delivery_succeeded,
    record_service_heartbeat,
    session_scope,
)
from cv_db.models import OutputConnector, ResultRecord, RuntimeConnectorDelivery
from cv_runtime import RuntimeConnectorDeliveryError, validate_output_connector_config

from connector_service.builtins import BUILTIN_CONNECTOR_EXECUTORS, OutputConnectorExecutor

LOGGER = logging.getLogger("connector-service")


@dataclass(frozen=True)
class ConnectorDrainStats:
    processed: int = 0
    delivered: int = 0
    failed_retryable: int = 0
    failed_terminal: int = 0
    skipped_no_payload: int = 0

    def add(self, *, processed: int = 0, status: str | None = None) -> ConnectorDrainStats:
        return ConnectorDrainStats(
            processed=self.processed + processed,
            delivered=self.delivered + (1 if status == "delivered" else 0),
            failed_retryable=self.failed_retryable + (1 if status == "failed_retryable" else 0),
            failed_terminal=self.failed_terminal + (1 if status == "failed_terminal" else 0),
            skipped_no_payload=self.skipped_no_payload
            + (1 if status == "skipped_no_payload" else 0),
        )


async def connector_service_loop(
    *,
    settings: PlatformSettings,
    stop_event: asyncio.Event,
    registry: dict[str, OutputConnectorExecutor] | None = None,
) -> None:
    while not stop_event.is_set():
        stats = ConnectorDrainStats()
        try:
            stats = await asyncio.to_thread(
                drain_connector_work,
                settings=settings,
                limit=25,
                registry=registry,
        )
        except Exception:
            LOGGER.exception("Connector service loop failed while draining connector work.")
        await asyncio.to_thread(
            _record_connector_service_heartbeat,
            settings,
            status="running",
            stats=stats,
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
        except TimeoutError:
            continue
    await asyncio.to_thread(
        _record_connector_service_heartbeat,
        settings,
        status="stopping",
        stats=ConnectorDrainStats(),
    )


def drain_connector_work(
    *,
    settings: PlatformSettings,
    limit: int = 20,
    registry: dict[str, OutputConnectorExecutor] | None = None,
) -> ConnectorDrainStats:
    executors = registry or BUILTIN_CONNECTOR_EXECUTORS
    with session_scope(settings) as session:
        rows = claim_due_connector_deliveries(session, limit=limit)
        row_ids = [row.id for row in rows]

    stats = ConnectorDrainStats()
    for row_id in row_ids:
        status = _process_connector_delivery(
            settings=settings,
            connector_delivery_id=row_id,
            registry=executors,
        )
        stats = stats.add(processed=1, status=status)
    return stats


def _record_connector_service_heartbeat(
    settings: PlatformSettings,
    *,
    status: str,
    stats: ConnectorDrainStats,
) -> None:
    with session_scope(settings) as session:
        record_service_heartbeat(
            session,
            service_name="connector-service",
            instance_id="connector-service-main",
            status=status,
            details={
                "processed": stats.processed,
                "delivered": stats.delivered,
                "failed_retryable": stats.failed_retryable,
                "failed_terminal": stats.failed_terminal,
                "skipped_no_payload": stats.skipped_no_payload,
            },
        )


def _process_connector_delivery(
    *,
    settings: PlatformSettings,
    connector_delivery_id: str,
    registry: dict[str, OutputConnectorExecutor],
) -> str:
    with session_scope(settings) as session:
        delivery = session.get(RuntimeConnectorDelivery, connector_delivery_id)
        if delivery is None:
            return "failed_terminal"
        if delivery.status != "processing":
            return delivery.status
        connector = session.get(OutputConnector, delivery.output_connector_id)
        result_record = session.get(ResultRecord, delivery.result_record_id)
        if connector is None:
            _fail(
                session,
                delivery,
                error="The output connector no longer exists.",
                retryable=False,
            )
            return delivery.status
        if result_record is None:
            _fail(
                session,
                delivery,
                error="The runtime result for this connector delivery no longer exists.",
                retryable=False,
            )
            return delivery.status
        if not connector.enabled:
            mark_connector_delivery_succeeded(
                session,
                delivery,
                status="skipped_no_payload",
                response_payload={"skipped": True, "reason": "connector_disabled"},
            )
            _write_delivery_audit(session, connector, delivery)
            return delivery.status
        executor = registry.get(connector.connector_type)
        if executor is None:
            _fail(
                session,
                delivery,
                error=f"No connector executor is registered for {connector.connector_type}.",
                retryable=False,
            )
            return delivery.status
        try:
            validate_output_connector_config(
                connector.connector_type,
                connector.config or {},
                secret_refs=connector.secret_refs or {},
            )
            response_payload = executor.deliver(
                settings=settings,
                session=session,
                connector=connector,
                delivery=delivery,
                result_record=result_record,
            )
            if response_payload.get("event_count") == 0 and connector.connector_type == "output.internal_event":
                mark_connector_delivery_succeeded(
                    session,
                    delivery,
                    status="skipped_no_payload",
                    response_payload=response_payload,
                )
            else:
                mark_connector_delivery_succeeded(
                    session,
                    delivery,
                    response_payload=response_payload,
                )
        except RuntimeConnectorDeliveryError as exc:
            _fail(
                session,
                delivery,
                error=str(exc),
                retryable=exc.retryable,
                response_payload={"code": exc.code, "metadata": exc.metadata},
            )
        except ValueError as exc:
            _fail(session, delivery, error=str(exc), retryable=False)
        except Exception as exc:
            LOGGER.exception(
                "Connector delivery failed.",
                extra={"connector_delivery_id": connector_delivery_id},
            )
            _fail(session, delivery, error=str(exc), retryable=True)
        _write_delivery_audit(session, connector, delivery)
        return delivery.status


def _fail(
    session,
    delivery: RuntimeConnectorDelivery,
    *,
    error: str,
    retryable: bool,
    response_payload: dict[str, Any] | None = None,
) -> None:
    mark_connector_delivery_failed(
        session,
        delivery,
        error=error,
        retryable=retryable,
        response_payload=response_payload,
    )


def _write_delivery_audit(
    session,
    connector: OutputConnector,
    delivery: RuntimeConnectorDelivery,
) -> None:
    create_audit_event(
        session,
        event_type=f"runtime.connector_delivery.{delivery.status}.v1",
        aggregate_type="output_connector",
        aggregate_id=connector.id,
        source_service="connector-service",
        actor_type="service",
        actor_id="connector-service",
        correlation_id=(delivery.request_payload or {}).get("correlation_id"),
        causation_id=delivery.result_record_id,
        payload={
            "connector_delivery_id": delivery.id,
            "channel_id": delivery.channel_id,
            "connector_type": delivery.connector_type,
            "result_record_id": delivery.result_record_id,
            "status": delivery.status,
            "retry_count": delivery.retry_count,
            "last_error": delivery.last_error,
        },
    )
