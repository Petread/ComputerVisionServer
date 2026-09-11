from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from cv_contracts import (
    EdgeCapabilitySnapshot,
    EdgeDeviceAddress,
    EdgeDeviceDetail,
    EdgeDeviceHealth,
    EdgeDeviceIdentity,
    EdgeDeviceRegistrationRequest,
)
from cv_db import create_edge_device, get_edge_device_detail
from runtime_admin_api.edge_fleet_reconciler import EdgeFleetReconciler
from runtime_admin_api.edge_reconciliation import EdgeProbeResult
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from tests.conftest import build_test_settings, configure_sqlite_factories


def test_automatic_reconciler_persists_success_and_survives_device_failure(
    sqlite_engine: Engine, tmp_path: Path
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)
    with Session(sqlite_engine) as session:
        device = create_edge_device(
            session,
            EdgeDeviceRegistrationRequest(
                name="Orin Nano M2",
                address=EdgeDeviceAddress(management_ip="192.168.50.21"),
                allowed_cidr="192.168.50.0/24",
                server_ip="192.168.50.10",
            ),
            created_by="test",
        )
        session.commit()
        device_id = device.edge_device_id

    reconciler = EdgeFleetReconciler(
        settings=settings, interval_seconds=1, probe=_successful_probe
    )
    first = asyncio.run(reconciler.reconcile_once())
    assert first.last_cycle_success_count == 1
    assert first.last_cycle_failure_count == 0
    with Session(sqlite_engine) as session:
        observed = get_edge_device_detail(session, device_id)
        assert observed is not None
        assert observed.connectivity_state == "online"
        assert observed.health is not None
        assert observed.health.runtime is not None

    reconciler.probe = _failed_probe
    second = asyncio.run(reconciler.reconcile_once())
    assert second.cycle_count == 2
    assert second.last_cycle_failure_count == 1
    with Session(sqlite_engine) as session:
        offline = get_edge_device_detail(session, device_id)
        assert offline is not None
        assert offline.connectivity_state == "offline"
        assert offline.health is not None
        assert offline.health.runtime is not None


def _successful_probe(_device: EdgeDeviceDetail) -> EdgeProbeResult:
    now = datetime.now(UTC).isoformat()
    from cv_contracts import EdgeRuntimeSlot, EdgeRuntimeStatus, EdgeStorageQuotaStatus

    runtime = EdgeRuntimeStatus(
        boot_id="boot-m2",
        supervisor_state="idle",
        slots=[
            EdgeRuntimeSlot(name="A", state="empty", updated_at=now),
            EdgeRuntimeSlot(name="B", state="empty", updated_at=now),
        ],
        storage=EdgeStorageQuotaStatus(
            artifact_bytes=0,
            artifact_quota_bytes=1000,
            outbox_bytes=0,
            outbox_quota_bytes=1000,
            outbox_items=0,
            outbox_max_items=100,
            disk_free_bytes=1000,
            minimum_disk_free_bytes=100,
        ),
        observed_at=now,
    )
    return EdgeProbeResult(
        identity=EdgeDeviceIdentity(
            address=EdgeDeviceAddress(management_ip="192.168.50.21"),
            hostname="edge-m2",
            platform="jetson_orin_nano",
            architecture="aarch64",
            agent_version="0.2.0-m2",
            runtime_version="0.2.0-m2",
            boot_id="boot-m2",
        ),
        health=EdgeDeviceHealth(
            state="ok",
            detail="ready",
            boot_id="boot-m2",
            observed_at=now,
            runtime=runtime,
        ),
        capabilities=EdgeCapabilitySnapshot(
            management_ip="192.168.50.21",
            platform="jetson_orin_nano",
            architecture="aarch64",
            os_version="Jetson Linux 39.2",
            ram_total_bytes=8_000_000_000,
            storage_total_bytes=256_000_000_000,
            storage_free_bytes=200_000_000_000,
            agent_version="0.2.0-m2",
            runtime_version="0.2.0-m2",
            observed_at=now,
        ),
    )


def _failed_probe(_device: EdgeDeviceDetail) -> EdgeProbeResult:
    raise ValueError("simulated isolated VLAN timeout")
