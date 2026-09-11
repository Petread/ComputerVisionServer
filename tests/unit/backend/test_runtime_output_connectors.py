from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from connector_service.builtins import BUILTIN_CONNECTOR_EXECUTORS
from connector_service.executor import drain_connector_work
from cv_contracts.runtime import RuntimeOutputConnectorCreateRequest
from cv_core import generate_prefixed_id
from cv_db import (
    AuditEvent,
    OutputConnector,
    RuntimeChannel,
    RuntimeConnectorDelivery,
    create_output_connector,
    record_runtime_execution_result,
    session_scope,
)
from cv_runtime import RuntimeConnectorDeliveryError, output_connector_type_catalog
from cv_runtime.connectors import validate_output_connector_config
from cv_runtime.payloads import RuntimeExecutionResult
from cv_status import HealthState, RuntimeChannelDesiredState, RuntimeMode, RuntimeObservedState
from sqlalchemy import select
from tests.conftest import build_test_settings, configure_sqlite_factories


def test_connector_registry_and_config_validation() -> None:
    catalog = {item.connector_type: item for item in output_connector_type_catalog()}

    assert set(catalog) >= {
        "output.overlay",
        "output.http_trigger_response",
        "output.persisted_result",
        "output.internal_event",
    }
    assert catalog["output.overlay"].default_delivery_policy.max_attempts == 3
    assert validate_output_connector_config("output.overlay", {"publish_live_view": True})[
        "publish_live_view"
    ] is True
    with pytest.raises(ValueError, match="inline secrets"):
        validate_output_connector_config("output.overlay", {"api_token": "secret"})


def test_builtin_connectors_execute_end_to_end(sqlite_engine, tmp_path: Path) -> None:
    configure_sqlite_factories(sqlite_engine, "connector-service")
    settings = build_test_settings("connector-service", tmp_path)

    with session_scope(settings) as session:
        _seed_channel(session, channel_id="chn_connectors", mode=RuntimeMode.REALTIME)
        result = _execution_result(
            channel_id="chn_connectors",
            runtime_execution_id="rex_connectors",
            include_overlay=True,
            include_http=True,
            include_persisted=True,
            include_event=True,
        )
        record = record_runtime_execution_result(
            session,
            result,
            channel_id="chn_connectors",
            runtime_execution_id="rex_connectors",
            trigger_endpoint_id="tep_connectors",
        )
        deliveries = session.scalars(select(RuntimeConnectorDelivery)).all()
        connectors = session.scalars(select(OutputConnector)).all()

    assert record.id.startswith("rrc_")
    assert {connector.connector_type for connector in connectors} == {
        "output.overlay",
        "output.http_trigger_response",
        "output.persisted_result",
        "output.internal_event",
    }
    assert {delivery.status for delivery in deliveries} == {"queued"}

    stats = drain_connector_work(settings=settings, limit=20)

    assert stats.processed == 4
    assert stats.delivered == 4
    assert stats.failed_retryable == 0
    with session_scope(settings) as session:
        deliveries = session.scalars(select(RuntimeConnectorDelivery)).all()
        connectors = session.scalars(select(OutputConnector)).all()
        audit_events = session.scalars(select(AuditEvent)).all()

    assert {delivery.status for delivery in deliveries} == {"delivered"}
    assert all(connector.status == "active" for connector in connectors)
    assert all(connector.last_delivery_status == "delivered" for connector in connectors)
    assert any(event.event_type == "runtime.connector.test_event.v1" for event in audit_events)
    assert (settings.storage.runtime_overlays_root() / "rex_connectors.latest.json").exists()


def test_result_history_is_opt_in_and_strips_inline_runtime_data(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "connector-service")
    settings = build_test_settings("connector-service", tmp_path)

    with session_scope(settings) as session:
        _seed_channel(session, channel_id="chn_history", mode=RuntimeMode.REALTIME)
        channel = session.get(RuntimeChannel, "chn_history")
        assert channel is not None
        channel.result_history_policy = {
            "enabled": True,
            "retention_hours": 2,
            "sample_interval_seconds": 5,
            "max_records": 100,
        }
        result = _execution_result(
            channel_id="chn_history",
            runtime_execution_id="rex_history",
            include_persisted=True,
        )
        record = record_runtime_execution_result(
            session,
            result,
            channel_id="chn_history",
            runtime_execution_id="rex_history",
        )

        assert record.history_retained is True
        assert record.raw_inference_outputs == []
        assert record.normalized_inference_outputs == []
        assert record.derived_outputs == result.persisted_results
        assert "image_base64" not in str(record.result)


def test_decision_ready_stem_result_uses_existing_history_with_domain_result_type(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "connector-service")
    settings = build_test_settings("connector-service", tmp_path)
    with session_scope(settings) as session:
        _seed_channel(session, channel_id="chn_stem_history", mode=RuntimeMode.REALTIME)
        channel = session.get(RuntimeChannel, "chn_stem_history")
        assert channel is not None
        channel.result_history_policy = {
            "enabled": True,
            "retention_hours": 24,
            "sample_interval_seconds": 60,
            "max_records": 100,
        }
        result = _execution_result(
            channel_id="chn_stem_history",
            runtime_execution_id="rex_stem_history",
            include_persisted=True,
        )
        result.persisted_results[0]["payload"]["result"] = {
            "schema_version": "stem.orientation.v2",
            "session_id": "session-1",
            "track_id": "track-1",
            "decision_ready": True,
            "decision": {"ready": True, "frame_sequence": 12},
            "recommendation": {
                "signed_rotation_deg": -43.0,
                "direction": "counter_clockwise_viewed_from_configured_log_end",
            },
            "compact_curvature_trace": {"samples": []},
            "compact_rotation_trace": {"samples": []},
            "post_rotation_verification": None,
        }
        record = record_runtime_execution_result(
            session,
            result,
            channel_id="chn_stem_history",
            runtime_execution_id="rex_stem_history",
            retain_history=True,
        )

        assert record.history_retained is True
        assert record.result_type == "stem_orientation"
        stored = record.derived_outputs[0]["payload"]["result"]
        assert stored["recommendation"]["signed_rotation_deg"] == -43.0
        assert stored["post_rotation_verification"] is None


def test_shadow_result_can_force_one_semantic_history_row_when_sampling_is_disabled(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "connector-service")
    settings = build_test_settings("connector-service", tmp_path)
    with session_scope(settings) as session:
        _seed_channel(session, channel_id="chn_shadow_history", mode=RuntimeMode.REALTIME)
        channel = session.get(RuntimeChannel, "chn_shadow_history")
        assert channel is not None
        channel.result_history_policy = {
            "enabled": False,
            "retention_hours": 24,
            "sample_interval_seconds": 60,
            "max_records": 100,
        }
        result = _execution_result(
            channel_id="chn_shadow_history",
            runtime_execution_id="rex_shadow_history",
            include_persisted=True,
        )
        result.persisted_results[0]["payload"]["result"] = {
            "schema_version": "stem.orientation.v3",
            "algorithm": "CURVATURE_PHASE_MINIMUM",
            "session_id": "shadow-session-1",
            "track_id": "shadow-track-1",
            "shadow_mode": True,
            "decision_ready": True,
            "decision": "UNCERTAIN",
            "decision_frame_sequence": 42,
        }

        record = record_runtime_execution_result(
            session,
            result,
            channel_id="chn_shadow_history",
            runtime_execution_id="rex_shadow_history",
            retain_history=True,
        )

        assert record.history_retained is True
        assert record.result_type == "stem_orientation"
        assert record.runtime_metadata["shadow_mode"] is True


def test_connector_failure_is_retried_and_isolated(sqlite_engine, tmp_path: Path) -> None:
    configure_sqlite_factories(sqlite_engine, "connector-service")
    settings = build_test_settings("connector-service", tmp_path)

    with session_scope(settings) as session:
        _seed_channel(session, channel_id="chn_retry", mode=RuntimeMode.REALTIME)
        result = _execution_result(
            channel_id="chn_retry",
            runtime_execution_id="rex_retry",
            include_persisted=True,
            include_event=True,
        )
        record_runtime_execution_result(
            session,
            result,
            channel_id="chn_retry",
            runtime_execution_id="rex_retry",
        )

    registry = dict(BUILTIN_CONNECTOR_EXECUTORS)
    registry["output.internal_event"] = _FailingEventConnector()
    stats = drain_connector_work(settings=settings, limit=20, registry=registry)

    assert stats.processed == 2
    assert stats.delivered == 1
    assert stats.failed_retryable == 1
    with session_scope(settings) as session:
        rows = session.scalars(select(RuntimeConnectorDelivery)).all()
        persisted = next(row for row in rows if row.connector_type == "output.persisted_result")
        failed = next(row for row in rows if row.connector_type == "output.internal_event")
        failed_connector = session.get(OutputConnector, failed.output_connector_id)

    assert persisted.status == "delivered"
    assert failed.status == "failed_retryable"
    assert failed.retry_count == 1
    assert failed.next_attempt_at is not None
    assert failed_connector is not None
    assert failed_connector.status == "degraded"
    assert "transient connector failure" in (failed_connector.last_error or "")


def test_connector_create_rejects_inline_secret_config(sqlite_engine, tmp_path: Path) -> None:
    configure_sqlite_factories(sqlite_engine, "connector-service")
    settings = build_test_settings("connector-service", tmp_path)
    with session_scope(settings) as session:
        _seed_channel(session, channel_id="chn_secret", mode=RuntimeMode.REALTIME)
        with pytest.raises(ValueError, match="inline secrets"):
            create_output_connector(
                session,
                RuntimeOutputConnectorCreateRequest(
                    channel_id="chn_secret",
                    connector_type="output.overlay",
                    name="Unsafe overlay",
                    config={"password": "not-here"},
                ),
                created_by="tester",
            )


class _FailingEventConnector:
    connector_type = "output.internal_event"

    def deliver(self, **_: Any) -> dict[str, Any]:
        raise RuntimeConnectorDeliveryError("transient connector failure", retryable=True)


def _seed_channel(session, *, channel_id: str, mode: RuntimeMode) -> None:
    session.add(
        RuntimeChannel(
            id=channel_id,
            slug=channel_id,
            name=f"Channel {channel_id}",
            technical_place="test-cell",
            mode=mode.value,
            output_summary={},
            capture_summary={},
            health_summary={},
            performance_summary={},
            desired_state=RuntimeChannelDesiredState.RUNNING.value,
            observed_state=RuntimeObservedState.RUNNING.value,
            health_state=HealthState.OK.value,
            created_by="tester",
        )
    )
    session.flush()


def _execution_result(
    *,
    channel_id: str,
    runtime_execution_id: str,
    include_overlay: bool = False,
    include_http: bool = False,
    include_persisted: bool = False,
    include_event: bool = False,
) -> RuntimeExecutionResult:
    overlay_outputs = [
        {
            "node_key": "overlay",
            "node_type": "output.overlay",
            "node_category": "output",
            "output_port": "overlay",
            "payload_type": "overlay",
            "payload": {
                "payload_type": "overlay",
                "primitives": [
                    {
                        "primitive_type": "box",
                        "label": "defect",
                        "confidence": 0.91,
                        "geometry": {"x_min": 0.1, "y_min": 0.1, "x_max": 0.4, "y_max": 0.4},
                        "style": {},
                        "metadata": {"layer": "detections"},
                    }
                ],
            },
        }
    ] if include_overlay else []
    http_outputs = [
        {
            "node_key": "http_response",
            "node_type": "output.http_trigger_response",
            "node_category": "output",
            "output_port": "response",
            "payload_type": "result",
            "payload": {"payload_type": "result", "result": {"count": 1}, "decision": True},
        }
    ] if include_http else []
    persisted_outputs = [
        {
            "node_key": "persist",
            "node_type": "output.persisted_result",
            "node_category": "output",
            "output_port": "persisted",
            "payload_type": "result",
            "payload": {"payload_type": "result", "result": {"count": 1}, "decision": True},
        }
    ] if include_persisted else []
    events = [
        {
            "node_key": "event",
            "node_type": "output.internal_event",
            "node_category": "output",
            "output_port": "event",
            "event": {
                "event_type": "runtime.connector.test_event.v1",
                "key": "event_1",
                "payload": {"count": 1},
            },
        }
    ] if include_event else []
    return RuntimeExecutionResult(
        status="completed",
        channel_id=channel_id,
        pipeline_version_id="plv_connectors",
        runtime_execution_id=runtime_execution_id,
        correlation_id=generate_prefixed_id("corr"),
        started_at="2026-05-01T10:00:00Z",
        completed_at="2026-05-01T10:00:01Z",
        node_outputs={
            "source": {
                "frame": {
                    "payload_type": "frame",
                    "image_base64": "ZmFrZS1pbWFnZQ==",
                    "mime_type": "image/jpeg",
                    "width": 640,
                    "height": 360,
                    "metadata": {},
                }
            }
        },
        overlay_outputs=overlay_outputs,
        http_trigger_responses=http_outputs,
        persisted_results=persisted_outputs,
        internal_events=events,
        final_response={"count": 1} if include_http else {},
        timing={"total_ms": 12.0},
    )
