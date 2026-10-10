from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import util as importlib_util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cv_contracts import (
    EdgeCapabilitySnapshot,
    EdgeDeviceAddress,
    EdgeDeviceHealth,
    EdgeDeviceIdentity,
    EdgeDeviceRegistrationRequest,
    EdgeDeviceUpdateRequest,
)
from cv_contracts.runtime import RuntimeChannelCreateRequest, RuntimeChannelUpdateRequest
from cv_db import (
    EdgeCapabilitySnapshotRecord,
    EdgeDevice,
    EdgeFleetCommandRecord,
    EdgeResultCursorRecord,
    RuntimeChannel,
    advance_edge_result_cursor,
    archive_edge_device,
    create_edge_device,
    create_edge_fleet_command,
    create_runtime_channel,
    finish_edge_fleet_command,
    get_edge_device_detail,
    list_edge_devices,
    record_edge_connectivity_failure,
    record_edge_observation,
    request_runtime_channel_start,
    start_edge_fleet_command,
    update_edge_device,
    update_runtime_channel,
)
from cv_status import RuntimeSourceType
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


def _registration(ip: str = "192.168.50.21") -> EdgeDeviceRegistrationRequest:
    return EdgeDeviceRegistrationRequest(
        name="Orin Nano Press Line",
        technical_place="press-line-1",
        address=EdgeDeviceAddress(management_ip=ip),
        allowed_cidr="192.168.50.0/24",
        server_ip="192.168.50.10",
    )


def test_routed_edge_server_can_be_registered_and_updated(sqlite_engine: Engine) -> None:
    with Session(sqlite_engine) as session:
        created = create_edge_device(
            session,
            EdgeDeviceRegistrationRequest(
                name="Routed Edge Device",
                address=EdgeDeviceAddress(management_ip="192.168.50.21"),
                allowed_cidr="192.168.50.21/32",
                server_ip="10.20.30.40",
            ),
            created_by="admin",
        )
        assert str(created.allowed_cidr) == "192.168.50.21/32"
        assert str(created.server_ip) == "10.20.30.40"
        updated = update_edge_device(
            session,
            created.edge_device_id,
            EdgeDeviceUpdateRequest(server_ip="10.20.30.41"),
            updated_by="admin",
        )
        assert str(updated.server_ip) == "10.20.30.41"


def _observation(
    *,
    boot_id: str,
    runtime_version: str = "0.1.0-m0",
) -> tuple[EdgeDeviceIdentity, EdgeDeviceHealth, EdgeCapabilitySnapshot]:
    observed_at = datetime.now(UTC).isoformat()
    identity = EdgeDeviceIdentity(
        address=EdgeDeviceAddress(management_ip="192.168.50.21"),
        hostname="edge-press-01",
        platform="jetson_orin_nano",
        architecture="aarch64",
        agent_version="0.1.0-m0",
        runtime_version=runtime_version,
        boot_id=boot_id,
    )
    health = EdgeDeviceHealth(
        state="ok",
        detail="Ready",
        boot_id=boot_id,
        observed_at=observed_at,
        disk_free_bytes=1000,
    )
    capabilities = EdgeCapabilitySnapshot(
        management_ip="192.168.50.21",
        platform="jetson_orin_nano",
        architecture="aarch64",
        os_version="Jetson Linux 39.2",
        jetpack_version="7.2",
        l4t_version="39.2",
        cuda_version="13.2",
        tensorrt_version="10.16",
        deepstream_version="9.1",
        gpu_model="Jetson Orin Nano",
        gpu_memory_bytes=8_000_000_000,
        ram_total_bytes=8_000_000_000,
        storage_total_bytes=256_000_000_000,
        storage_free_bytes=200_000_000_000,
        agent_version="0.1.0-m0",
        runtime_version=runtime_version,
        supported_node_types=["inference.detection"],
        observed_at=observed_at,
    )
    return identity, health, capabilities


def test_fleet_observations_are_durable_versioned_and_keep_health_when_offline(
    sqlite_engine: Engine,
) -> None:
    with Session(sqlite_engine) as session:
        created = create_edge_device(session, _registration(), created_by="admin")
        session.commit()
        device_id = created.edge_device_id

    with Session(sqlite_engine) as session:
        identity, health, capabilities = _observation(boot_id="boot-a")
        first = record_edge_observation(
            session,
            device_id,
            identity=identity,
            health=health,
            capabilities=capabilities,
        )
        session.commit()
        first_snapshot_id = first.latest_capability_snapshot.snapshot_id  # type: ignore[union-attr]

    with Session(sqlite_engine) as session:
        identity, health, capabilities = _observation(boot_id="boot-a")
        capabilities = capabilities.model_copy(
            update={
                "storage_free_bytes": capabilities.storage_free_bytes - 4096,
                "observed_at": datetime.now(UTC).isoformat(),
            }
        )
        repeated = record_edge_observation(
            session,
            device_id,
            identity=identity,
            health=health,
            capabilities=capabilities,
        )
        assert repeated.latest_capability_snapshot is not None
        assert repeated.latest_capability_snapshot.snapshot_id == first_snapshot_id
        assert (
            repeated.latest_capability_snapshot.capabilities.storage_free_bytes
            == capabilities.storage_free_bytes
        )
        assert session.scalar(select(func.count(EdgeCapabilitySnapshotRecord.id))) == 1

        changed_identity, changed_health, changed_capabilities = _observation(
            boot_id="boot-b",
            runtime_version="0.2.0-m1",
        )
        changed = record_edge_observation(
            session,
            device_id,
            identity=changed_identity,
            health=changed_health,
            capabilities=changed_capabilities,
        )
        assert changed.identity_generation == 2
        assert changed.deployment_revalidation_required is True
        assert session.scalar(select(func.count(EdgeCapabilitySnapshotRecord.id))) == 2

        offline = record_edge_connectivity_failure(
            session,
            device_id,
            message="simulated LAN timeout",
        )
        session.commit()
        assert offline.connectivity_state == "offline"
        assert offline.reported_health_state == "ok"
        assert offline.health is not None
        assert offline.health.boot_id == "boot-b"

        device_row = session.get(EdgeDevice, device_id)
        assert device_row is not None
        device_row.connectivity_state = "online"
        device_row.last_seen_at = datetime.now(UTC) - timedelta(seconds=91)
        assert list_edge_devices(session)[0].connectivity_state == "stale"

    with Session(sqlite_engine) as session:
        persisted = get_edge_device_detail(session, device_id)
        assert persisted is not None
        assert persisted.connectivity_state == "offline"
        assert len(list_edge_devices(session)) == 1


def test_fleet_command_idempotency_and_result_cursor_monotonicity(
    sqlite_engine: Engine,
) -> None:
    with Session(sqlite_engine) as session:
        device = create_edge_device(session, _registration(), created_by="admin")
        command = create_edge_fleet_command(
            session,
            device.edge_device_id,
            command_type="reconcile",
            idempotency_key="reconcile-001",
            request_payload={"edge_device_id": device.edge_device_id},
        )
        repeated = create_edge_fleet_command(
            session,
            device.edge_device_id,
            command_type="reconcile",
            idempotency_key="reconcile-001",
            request_payload={"edge_device_id": device.edge_device_id},
        )
        assert repeated.command_id == command.command_id
        running = start_edge_fleet_command(session, command.command_id)
        finished = finish_edge_fleet_command(
            session,
            command.command_id,
            succeeded=True,
            response_payload={"health_state": "ok"},
        )
        assert running.attempt_count == 1
        assert finished.status == "succeeded"
        assert session.scalar(select(func.count(EdgeFleetCommandRecord.id))) == 1

        with pytest.raises(ValueError, match="another request"):
            create_edge_fleet_command(
                session,
                device.edge_device_id,
                command_type="deployment_commit",
                idempotency_key="reconcile-001",
                request_payload={},
            )

        cursor = advance_edge_result_cursor(
            session,
            device.edge_device_id,
            boot_id="boot-a",
            persisted_sequence=5,
            acknowledged_sequence=4,
        )
        assert cursor.last_persisted_sequence == 5
        assert session.scalar(select(func.count(EdgeResultCursorRecord.id))) == 1
        with pytest.raises(ValueError, match="backwards"):
            advance_edge_result_cursor(
                session,
                device.edge_device_id,
                boot_id="boot-a",
                persisted_sequence=4,
                acknowledged_sequence=4,
            )
        with pytest.raises(ValueError, match="cannot exceed"):
            advance_edge_result_cursor(
                session,
                device.edge_device_id,
                boot_id="boot-a",
                persisted_sequence=6,
                acknowledged_sequence=7,
            )


def test_channel_placement_requires_registered_enabled_edge_and_requires_published_pipeline(
    sqlite_engine: Engine,
) -> None:
    with Session(sqlite_engine) as session:
        device = create_edge_device(session, _registration(), created_by="admin")
        channel = create_runtime_channel(
            session,
            RuntimeChannelCreateRequest(
                name="Edge camera",
                technical_place="press-line-1",
                source_type=RuntimeSourceType.RTSP,
                source_config={"url": "rtsp://camera.local/live"},
                placement={
                    "target": "edge_device",
                    "edge_device_id": device.edge_device_id,
                },
            ),
            created_by="admin",
        )
        assert channel.placement.target == "edge_device"
        assert channel.placement.edge_management_ip == "192.168.50.21"
        with pytest.raises(ValueError, match="no published channel pipeline"):
            request_runtime_channel_start(session, channel.channel_id, requested_by="admin")
        with pytest.raises(ValueError, match="channels are assigned"):
            archive_edge_device(session, device.edge_device_id, archived_by="admin")

        channel_row = session.get(RuntimeChannel, channel.channel_id)
        assert channel_row is not None
        channel_row.desired_state = "running"
        channel_row.observed_state = "running"
        with pytest.raises(ValueError, match="Stop the channel"):
            update_runtime_channel(
                session,
                channel.channel_id,
                RuntimeChannelUpdateRequest(placement={"target": "server"}),
                updated_by="admin",
            )
        channel_row.desired_state = "stopped"
        channel_row.observed_state = "stopped"

        server_channel = update_runtime_channel(
            session,
            channel.channel_id,
            RuntimeChannelUpdateRequest(placement={"target": "server"}),
            updated_by="admin",
        )
        assert server_channel.placement.target == "server"
        assert server_channel.placement.edge_device_id is None
        archived = archive_edge_device(session, device.edge_device_id, archived_by="admin")
        assert archived.edge_device_id == device.edge_device_id
        replacement = create_edge_device(session, _registration(), created_by="admin")
        assert replacement.edge_device_id != archived.edge_device_id
        assert replacement.address.management_ip == archived.address.management_ip
        assert session.get(EdgeDevice, archived.edge_device_id).deleted_at is not None
        with pytest.raises(ValueError, match="active record"):
            create_edge_device(session, _registration(), created_by="admin")

        disabled = create_edge_device(
            session,
            _registration("192.168.50.22"),
            created_by="admin",
        )
        update_edge_device(
            session,
            disabled.edge_device_id,
            EdgeDeviceUpdateRequest(enabled=False),
            updated_by="admin",
        )
        with pytest.raises(ValueError, match="disabled"):
            create_runtime_channel(
                session,
                RuntimeChannelCreateRequest(
                    name="Disabled edge camera",
                    technical_place="press-line-2",
                    source_config={"url": "rtsp://camera.local/disabled"},
                    placement={
                        "target": "edge_device",
                        "edge_device_id": disabled.edge_device_id,
                    },
                ),
                created_by="admin",
            )


def test_m1_tables_and_channel_columns_are_registered() -> None:
    assert EdgeDevice.__tablename__ == "edge_devices"
    assert EdgeFleetCommandRecord.__tablename__ == "edge_fleet_commands"
    assert EdgeResultCursorRecord.__tablename__ == "edge_result_cursors"
    assert "execution_target" in RuntimeChannel.__table__.columns
    assert "edge_device_id" in RuntimeChannel.__table__.columns


def test_m1_migration_upgrades_existing_runtime_channel_schema(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'edge-m1-migration.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE runtime_channels (id VARCHAR(64) PRIMARY KEY)"))
        context = MigrationContext.configure(connection)
        operations = Operations(context)
        migration_path = Path("backend/migrations/versions/20260721_0028_edge_fleet_foundations.py")
        spec = importlib_util.spec_from_file_location("edge_m1_migration", migration_path)
        assert spec is not None and spec.loader is not None
        module = importlib_util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.op = operations
        module.upgrade()

        schema = inspect(connection)
        assert {
            "edge_devices",
            "edge_capability_snapshots",
            "edge_fleet_commands",
            "edge_result_cursors",
        } <= set(schema.get_table_names())
        channel_columns = {
            column["name"]: column for column in schema.get_columns("runtime_channels")
        }
        assert channel_columns["execution_target"]["nullable"] is False
        assert channel_columns["execution_target"]["default"] == "'server'"
        assert "edge_device_id" in channel_columns
