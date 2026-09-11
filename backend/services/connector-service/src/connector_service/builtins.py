from __future__ import annotations

from typing import Any, Protocol

from cv_contracts.runtime import (
    RuntimeLiveFrame,
    RuntimeLiveMetrics,
    RuntimeLiveSourceHealth,
    RuntimeLiveViewState,
    RuntimeOverlayPrimitiveState,
)
from cv_core import PlatformSettings
from cv_db import create_audit_event, write_runtime_live_view_state
from cv_db.models import OutputConnector, ResultRecord, RuntimeConnectorDelivery
from cv_runtime import RuntimeConnectorDeliveryError
from cv_status import HealthState, RuntimeObservedState
from cv_utils import utc_now_iso
from sqlalchemy.orm import Session


class OutputConnectorExecutor(Protocol):
    connector_type: str

    def deliver(
        self,
        *,
        settings: PlatformSettings,
        session: Session,
        connector: OutputConnector,
        delivery: RuntimeConnectorDelivery,
        result_record: ResultRecord,
    ) -> dict[str, Any]:
        ...


class OverlayOutputConnector:
    connector_type = "output.overlay"

    def deliver(
        self,
        *,
        settings: PlatformSettings,
        session: Session,
        connector: OutputConnector,
        delivery: RuntimeConnectorDelivery,
        result_record: ResultRecord,
    ) -> dict[str, Any]:
        output = _delivery_output(delivery)
        payload = output.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeConnectorDeliveryError(
                "Overlay connector delivery did not include an overlay payload.",
                code="runtime_connector_payload_invalid",
                retryable=False,
            )
        primitives = _overlay_primitives(payload, connector.config or {})
        frame = _frame_from_result(result_record)
        display_context = frame.image_context if frame is not None else {}
        display_context_id = (
            str(display_context.get("context_id"))
            if isinstance(display_context, dict) and display_context.get("context_id")
            else None
        )
        if result_record.runtime_execution_id:
            state = RuntimeLiveViewState(
                channel_id=result_record.channel_id,
                runtime_instance_id=result_record.runtime_execution_id,
                actual_state=RuntimeObservedState.RUNNING,
                available=bool(frame or primitives),
                stale=False,
                display_context_id=display_context_id,
                display_context=display_context if isinstance(display_context, dict) else {},
                frame=frame,
                overlay=primitives,
                layers_available=sorted({primitive.layer for primitive in primitives}),
                metrics=RuntimeLiveMetrics(
                    frame_count=1,
                    latency_ms=_latency_ms(result_record),
                    last_frame_at=_iso(result_record.source_timestamp)
                    or _iso(result_record.completed_at),
                ),
                source_health=RuntimeLiveSourceHealth(
                    state=HealthState.OK,
                    status_text="Overlay connector delivered",
                    connected=True,
                ),
                updated_at=utc_now_iso(),
            )
            path = write_runtime_live_view_state(settings, state)
            state_ref = str(path)
        else:
            state_ref = None
        return {
            "primitive_count": len(primitives),
            "frame_available": frame is not None,
            "live_view_state_ref": state_ref,
        }


class HttpTriggerResponseOutputConnector:
    connector_type = "output.http_trigger_response"

    def deliver(
        self,
        *,
        settings: PlatformSettings,
        session: Session,
        connector: OutputConnector,
        delivery: RuntimeConnectorDelivery,
        result_record: ResultRecord,
    ) -> dict[str, Any]:
        response = dict(result_record.trigger_response or {})
        if not response:
            output = _delivery_output(delivery)
            payload = output.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
                response = dict(payload["result"])
        return {
            "response_recorded": bool(response),
            "response_field_count": len(response),
            "trigger_endpoint_id": result_record.trigger_endpoint_id,
        }


class PersistedResultOutputConnector:
    connector_type = "output.persisted_result"

    def deliver(
        self,
        *,
        settings: PlatformSettings,
        session: Session,
        connector: OutputConnector,
        delivery: RuntimeConnectorDelivery,
        result_record: ResultRecord,
    ) -> dict[str, Any]:
        return {
            "result_record_id": result_record.id,
            "status": result_record.status,
            "schema_version": result_record.schema_version,
            "completed_at": _iso(result_record.completed_at),
            "retention_class": (connector.config or {}).get("retention_class", "standard"),
        }


class InternalEventOutputConnector:
    connector_type = "output.internal_event"

    def deliver(
        self,
        *,
        settings: PlatformSettings,
        session: Session,
        connector: OutputConnector,
        delivery: RuntimeConnectorDelivery,
        result_record: ResultRecord,
    ) -> dict[str, Any]:
        output = _delivery_output(delivery)
        raw_event = output.get("event")
        if not isinstance(raw_event, dict):
            return {"event_written": False, "event_count": 0}
        event_type = str(raw_event.get("event_type") or "runtime.output.internal_event.v1")
        event_key = str(raw_event.get("key") or delivery.id)
        payload = raw_event.get("payload") if isinstance(raw_event.get("payload"), dict) else {}
        audit_id = create_audit_event(
            session,
            event_type=event_type,
            aggregate_type="runtime_channel",
            aggregate_id=result_record.channel_id,
            source_service="connector-service",
            actor_type="service",
            actor_id="connector-service",
            correlation_id=result_record.correlation_id,
            causation_id=result_record.id,
            payload={
                "event_key": event_key,
                "output_connector_id": connector.id,
                "connector_delivery_id": delivery.id,
                "result_record_id": result_record.id,
                "payload": payload,
            },
        )
        return {"event_written": True, "event_count": 1, "audit_event_id": audit_id}


BUILTIN_CONNECTOR_EXECUTORS: dict[str, OutputConnectorExecutor] = {
    executor.connector_type: executor
    for executor in (
        OverlayOutputConnector(),
        HttpTriggerResponseOutputConnector(),
        PersistedResultOutputConnector(),
        InternalEventOutputConnector(),
    )
}


def _delivery_output(delivery: RuntimeConnectorDelivery) -> dict[str, Any]:
    request_payload = delivery.request_payload or {}
    output = request_payload.get("output")
    return output if isinstance(output, dict) else {}


def _overlay_primitives(
    payload: dict[str, Any],
    config: dict[str, Any],
) -> list[RuntimeOverlayPrimitiveState]:
    raw_primitives = payload.get("primitives")
    if not isinstance(raw_primitives, list):
        return []
    layer_prefix = str(config.get("layer_prefix") or "").strip()
    primitives: list[RuntimeOverlayPrimitiveState] = []
    for item in raw_primitives:
        if not isinstance(item, dict):
            continue
        primitive_type = str(item.get("primitive_type") or "")
        if primitive_type not in {
            "box",
            "polygon",
            "point",
            "label",
            "measurement",
            "roi",
            "zone",
            "motion",
            "heatmap",
            "mask",
        }:
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        raw_layer = str(metadata.get("layer") or "overlay")
        layer = f"{layer_prefix}{raw_layer}" if layer_prefix else raw_layer
        primitives.append(
            RuntimeOverlayPrimitiveState(
                primitive_type=primitive_type,  # type: ignore[arg-type]
                layer=layer,
                label=item.get("label"),
                confidence=item.get("confidence"),
                geometry=item.get("geometry") if isinstance(item.get("geometry"), dict) else {},
                image_context_id=item.get("image_context_id"),
                display_context_id=item.get("display_context_id"),
                object_id=item.get("object_id"),
                source_object_id=item.get("source_object_id"),
                projected_from_context_id=item.get("projected_from_context_id"),
                style=item.get("style") if isinstance(item.get("style"), dict) else {},
                metadata=metadata,
            )
        )
    return primitives


def _frame_from_result(result_record: ResultRecord) -> RuntimeLiveFrame | None:
    result = result_record.result or {}
    node_outputs = result.get("node_outputs") if isinstance(result, dict) else None
    if not isinstance(node_outputs, dict):
        return None
    for outputs in node_outputs.values():
        if not isinstance(outputs, dict):
            continue
        for payload in outputs.values():
            if not isinstance(payload, dict) or payload.get("payload_type") != "display_output":
                continue
            frame = payload.get("frame")
            if isinstance(frame, dict):
                live_frame = _live_frame_from_payload(frame, result_record)
                if live_frame is not None:
                    return live_frame
    for outputs in node_outputs.values():
        if not isinstance(outputs, dict):
            continue
        for payload in outputs.values():
            if not isinstance(payload, dict) or payload.get("payload_type") != "frame":
                continue
            live_frame = _live_frame_from_payload(payload, result_record)
            if live_frame is not None:
                return live_frame
    return None


def _live_frame_from_payload(
    payload: dict[str, Any],
    result_record: ResultRecord,
) -> RuntimeLiveFrame | None:
    image_base64 = payload.get("image_base64")
    if not image_base64:
        return None
    return RuntimeLiveFrame(
        image_base64=str(image_base64),
        mime_type=str(payload.get("mime_type") or "image/jpeg"),
        width=payload.get("width"),
        height=payload.get("height"),
        source_timestamp=payload.get("timestamp"),
        captured_at=_iso(result_record.completed_at),
        sequence=0,
        image_context=payload.get("image_context") if isinstance(payload.get("image_context"), dict) else {},
        metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
    )


def _latency_ms(result_record: ResultRecord) -> float | None:
    timing = result_record.timing or {}
    raw = timing.get("total_ms") if isinstance(timing, dict) else None
    return float(raw) if isinstance(raw, int | float) else None


def _iso(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat()).replace("+00:00", "Z")
    return str(value)
