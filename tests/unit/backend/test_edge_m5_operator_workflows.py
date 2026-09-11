from __future__ import annotations

from datetime import UTC, datetime
from importlib import util as importlib_util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from cv_contracts import (
    EdgeDeploymentExecuteRequest,
    EdgeDeploymentPreflightRequest,
    EdgeDeploymentState,
    EdgeDeviceAddress,
    EdgeRollbackRequest,
)
from cv_db import (
    EdgeDiagnosticReportRecord,
    EdgeResultCursorRecord,
    ResultRecord,
    RuntimeChannel,
    RuntimeControlCommandRecord,
    get_edge_deployment_attempt,
    get_edge_device_detail,
    record_edge_observation,
    session_scope,
)
from edge_agent.state import EdgeAgentConfig, EdgeAgentState
from runtime_admin_api.edge_deployment_controller import EdgeDeploymentController
from runtime_admin_api.edge_operator_workflows import (
    build_edge_deployment_preflight,
    build_edge_fleet_health_overview,
    execute_edge_deployment_workflow,
    execute_manual_edge_rollback,
    run_edge_diagnostics,
)
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from tests.conftest import build_test_settings, configure_sqlite_factories
from tests.unit.backend.test_edge_m3_deployment import _seed_edge_channel, _StateClient


class _OperatorStateClient(_StateClient):
    def deployment(self, deployment_id: str) -> EdgeDeploymentState:
        return self.state.deployment(deployment_id)


def test_preflight_pins_release_and_manual_rollback_is_idempotent(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path / "server")
    edge_state = EdgeAgentState(
        EdgeAgentConfig(
            address=EdgeDeviceAddress(management_ip="192.168.50.21"),
            data_root=tmp_path / "edge",
            minimum_disk_free_bytes=0,
            supported_node_types=("source.channel_frame", "output.persisted_result"),
        )
    )
    controller = EdgeDeploymentController(
        settings,
        client_factory=lambda _device: _OperatorStateClient(edge_state),
        worker_id="m5-controller",
    )
    try:
        with session_scope(settings) as session:
            channel_id, device_id = _seed_edge_channel(session)
            first_preflight = build_edge_deployment_preflight(
                session,
                settings=settings,
                payload=EdgeDeploymentPreflightRequest(
                    channel_id=channel_id,
                    precision="fp16",
                ),
                created_by="operator",
            )
            assert first_preflight.ready is True
            assert first_preflight.workload_release_id
            device = get_edge_device_detail(session, device_id)
            assert device is not None
            assert device.identity is not None
            assert device.health is not None
            assert device.latest_capability_snapshot is not None
            record_edge_observation(
                session,
                device_id,
                identity=device.identity,
                health=device.health.model_copy(
                    update={"disk_free_bytes": device.health.disk_free_bytes - 4096}
                ),
                capabilities=device.latest_capability_snapshot.capabilities.model_copy(
                    update={
                        "storage_free_bytes": (
                            device.latest_capability_snapshot.capabilities.storage_free_bytes
                            - 4096
                        ),
                        "observed_at": datetime.now(UTC).isoformat(),
                    }
                ),
            )
            response = execute_edge_deployment_workflow(
                session,
                settings=settings,
                payload=EdgeDeploymentExecuteRequest(
                    channel_id=channel_id,
                    workload_release_id=first_preflight.workload_release_id,
                    idempotency_key="m5-deploy-first",
                ),
                requested_by="operator",
            )
            command = session.get(RuntimeControlCommandRecord, response.command_id)
            assert command is not None
            assert command.payload["expected_release_digest"] == first_preflight.release_digest

        assert controller.run_once() is True
        with session_scope(settings) as session:
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None and channel.edge_deployment_id
            first_deployment_id = channel.edge_deployment_id
            second_preflight = build_edge_deployment_preflight(
                session,
                settings=settings,
                payload=EdgeDeploymentPreflightRequest(channel_id=channel_id),
                created_by="operator",
            )
            response = execute_edge_deployment_workflow(
                session,
                settings=settings,
                payload=EdgeDeploymentExecuteRequest(
                    channel_id=channel_id,
                    workload_release_id=second_preflight.workload_release_id or "",
                    idempotency_key="m5-deploy-second",
                ),
                requested_by="operator",
            )
            assert response.command == "restart"

        assert controller.run_once() is True
        with session_scope(settings) as session:
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None and channel.edge_deployment_id
            second_deployment_id = channel.edge_deployment_id
            assert second_deployment_id != first_deployment_id
            first = get_edge_deployment_attempt(session, first_deployment_id)
            assert first is not None and first.status == "standby"

        rollback_payload = EdgeRollbackRequest(
            idempotency_key="m5-rollback-second",
            reason="Operator confirmed rollback after validation",
        )
        result = execute_manual_edge_rollback(
            settings,
            edge_deployment_id=second_deployment_id,
            payload=rollback_payload,
            requested_by="operator",
            client_factory=lambda _device: _OperatorStateClient(edge_state),
        )
        repeated = execute_manual_edge_rollback(
            settings,
            edge_deployment_id=second_deployment_id,
            payload=rollback_payload,
            requested_by="operator",
            client_factory=lambda _device: _OperatorStateClient(edge_state),
        )
        assert result.command.status == "succeeded"
        assert repeated.command.command_id == result.command.command_id
        assert result.restored_deployment.edge_deployment_id == first_deployment_id
        with session_scope(settings) as session:
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            assert channel.edge_device_id == device_id
            assert channel.edge_deployment_id == first_deployment_id
            rolled_back = get_edge_deployment_attempt(session, second_deployment_id)
            restored = get_edge_deployment_attempt(session, first_deployment_id)
            assert rolled_back is not None and rolled_back.status == "rolled_back"
            assert restored is not None and restored.status == "running"
    finally:
        edge_state.close()


def test_pinned_release_digest_change_fails_before_transfer(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path / "server-digest")
    with session_scope(settings) as session:
        channel_id, _device_id = _seed_edge_channel(session)
        preflight = build_edge_deployment_preflight(
            session,
            settings=settings,
            payload=EdgeDeploymentPreflightRequest(channel_id=channel_id),
            created_by="operator",
        )
        response = execute_edge_deployment_workflow(
            session,
            settings=settings,
            payload=EdgeDeploymentExecuteRequest(
                channel_id=channel_id,
                workload_release_id=preflight.workload_release_id or "",
                idempotency_key="m5-bad-digest",
            ),
            requested_by="operator",
        )
        command = session.get(RuntimeControlCommandRecord, response.command_id)
        assert command is not None
        command.payload = {**command.payload, "expected_release_digest": "0" * 64}

    controller = EdgeDeploymentController(settings, worker_id="m5-digest-controller")
    assert controller.run_once() is True
    assert controller.failed_commands == 1
    assert "digest does not match" in (controller.last_error or "")


def test_diagnostics_persist_slo_and_fleet_alerts(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path / "server-diagnostics")
    with session_scope(settings) as session:
        channel_id, device_id = _seed_edge_channel(session)
        session.add(
            ResultRecord(
                id="rrc_m5_slow",
                channel_id=channel_id,
                pipeline_version_id="pver_edge_m3",
                correlation_id="m5-slow",
                completed_at=datetime.now(UTC),
                mode="realtime",
                status="completed",
                schema_version="runtime.result.v1",
                timing={"total_ms": 240.0},
            )
        )
        session.add(
            EdgeResultCursorRecord(
                id="erc_m5_lag",
                edge_device_id=device_id,
                management_ip="192.168.50.21",
                boot_id="boot-m5-lag",
                last_persisted_sequence=20,
                last_acknowledged_sequence=15,
            )
        )
        report = run_edge_diagnostics(
            session,
            edge_device_id=device_id,
            created_by="operator",
        )
        assert report.status == "critical"
        capability_check = next(item for item in report.checks if item.key == "capability.snapshot")
        assert capability_check.outcome == "warning"
        assert "successful commit clears revalidation" in (
            capability_check.remediation_hint or ""
        )
        channel = report.channels[0]
        assert channel.latency_p95_ms == 240.0
        assert channel.latency_slo_ms == 100.0
        assert channel.status == "critical"
        assert session.get(EdgeDiagnosticReportRecord, report.diagnostic_report_id) is not None

        overview = build_edge_fleet_health_overview(session)
        assert overview.critical_devices == 1
        assert any(item.check_key == f"slo.channel.{channel_id}" for item in overview.alerts)


def test_m5_diagnostic_migration_creates_report_store(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'edge-m5-migration.db'}")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        operations = Operations(context)
        path = Path("backend/migrations/versions/20260722_0031_edge_operator_diagnostics.py")
        spec = importlib_util.spec_from_file_location("edge_m5_migration", path)
        assert spec is not None and spec.loader is not None
        module = importlib_util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.op = operations
        module.upgrade()

        schema = inspect(connection)
        assert "edge_diagnostic_reports" in schema.get_table_names()
        columns = {item["name"] for item in schema.get_columns("edge_diagnostic_reports")}
        assert {"edge_device_id", "status", "checks", "channels", "generated_at"} <= columns
