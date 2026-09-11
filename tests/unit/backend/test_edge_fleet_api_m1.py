from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from admin_api.main import app as admin_app
from cv_contracts import (
    EdgeCapabilitySnapshot,
    EdgeDeviceDetail,
    EdgeDeviceHealth,
    EdgeDeviceIdentity,
)
from cv_core import PlatformSettings
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from runtime_admin_api.api import edge_routes
from runtime_admin_api.edge_reconciliation import EdgeProbeResult
from runtime_admin_api.main import app as runtime_admin_app
from sqlalchemy.engine import Engine
from tests.conftest import build_test_settings, configure_sqlite_factories


def test_edge_fleet_api_registration_reconcile_and_channel_placement(
    sqlite_engine: Engine,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings
    monkeypatch.setattr(edge_routes, "probe_edge_device", _probe)

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)
        headers = {"X-CSRF-Token": csrf_token}

        registered = runtime_client.post(
            "/api/v1/runtime/edge-devices",
            headers=headers,
            json={
                "name": "Orin Nano Press Line",
                "technical_place": "press-line-1",
                "address": {
                    "management_ip": "192.168.50.21",
                    "control_port": 9443,
                    "plant_api_port": 9444,
                    "rtsp_port": 8554,
                },
                "allowed_cidr": "192.168.50.0/24",
                "server_ip": "192.168.50.10",
            },
        )
        assert registered.status_code == 201
        device_id = registered.json()["edge_device_id"]

        duplicate = runtime_client.post(
            "/api/v1/runtime/edge-devices",
            headers=headers,
            json={
                "name": "Duplicate Device",
                "address": {"management_ip": "192.168.50.21"},
                "allowed_cidr": "192.168.50.0/24",
                "server_ip": "192.168.50.10",
            },
        )
        assert duplicate.status_code == 422

        reconciled = runtime_client.post(
            f"/api/v1/runtime/edge-devices/{device_id}:reconcile",
            headers=headers,
            json={"idempotency_key": "fleet-reconcile-api-001"},
        )
        assert reconciled.status_code == 200
        assert reconciled.json()["command"]["status"] == "succeeded"
        assert reconciled.json()["device"]["connectivity_state"] == "online"
        assert (
            reconciled.json()["device"]["latest_capability_snapshot"]["capabilities"]["platform"]
            == "jetson_orin_nano"
        )

        repeated = runtime_client.post(
            f"/api/v1/runtime/edge-devices/{device_id}:reconcile",
            headers=headers,
            json={"idempotency_key": "fleet-reconcile-api-001"},
        )
        assert repeated.status_code == 200
        assert (
            repeated.json()["command"]["command_id"] == reconciled.json()["command"]["command_id"]
        )

        monkeypatch.setattr(edge_routes, "probe_edge_device", _offline_probe)
        offline = runtime_client.post(
            f"/api/v1/runtime/edge-devices/{device_id}:reconcile",
            headers=headers,
            json={"idempotency_key": "fleet-reconcile-api-offline-001"},
        )
        assert offline.status_code == 200
        assert offline.json()["command"]["status"] == "failed"
        assert offline.json()["device"]["connectivity_state"] == "offline"
        assert offline.json()["device"]["reported_health_state"] == "ok"

        channel = runtime_client.post(
            "/api/v1/runtime/channels",
            headers=headers,
            json={
                "name": "Edge camera",
                "technical_place": "press-line-1",
                "source_type": "rtsp",
                "source_config": {"url": "rtsp://camera.local/live"},
                "placement": {"target": "edge_device", "edge_device_id": device_id},
            },
        )
        assert channel.status_code == 201
        assert channel.json()["placement"]["target"] == "edge_device"
        assert channel.json()["placement"]["edge_management_ip"] == "192.168.50.21"

        fleet = runtime_client.get("/api/v1/runtime/edge-devices")
        assert fleet.status_code == 200
        assert fleet.json()["items"][0]["assigned_channel_count"] == 1

        filtered_channels = runtime_client.get(
            "/api/v1/runtime/channels",
            params={"execution_target": "edge_device"},
        )
        assert filtered_channels.status_code == 200
        assert [item["channel_id"] for item in filtered_channels.json()["items"]] == [
            channel.json()["channel_id"]
        ]

        archive = runtime_client.delete(
            f"/api/v1/runtime/edge-devices/{device_id}",
            headers=headers,
        )
        assert archive.status_code == 422

        audit_events = admin_client.get("/api/v1/audit-events")
        assert audit_events.status_code == 200
        audit_types = {item["event_type"] for item in audit_events.json()["items"]}
        assert "edge.device.registered.v1" in audit_types
        assert "edge.device.reconciled.v1" in audit_types


def _probe(_device: EdgeDeviceDetail) -> EdgeProbeResult:
    observed_at = datetime.now(UTC).isoformat()
    identity = EdgeDeviceIdentity(
        address={"management_ip": "192.168.50.21"},
        hostname="edge-press-01",
        platform="jetson_orin_nano",
        architecture="aarch64",
        agent_version="0.1.0-m0",
        runtime_version="0.1.0-m0",
        boot_id="boot-api-1",
    )
    return EdgeProbeResult(
        identity=identity,
        health=EdgeDeviceHealth(
            state="ok",
            detail="Ready",
            boot_id=identity.boot_id,
            observed_at=observed_at,
            disk_free_bytes=1000,
        ),
        capabilities=EdgeCapabilitySnapshot(
            management_ip="192.168.50.21",
            platform="jetson_orin_nano",
            architecture="aarch64",
            os_version="Jetson Linux 39.2",
            ram_total_bytes=8_000_000_000,
            storage_total_bytes=256_000_000_000,
            storage_free_bytes=200_000_000_000,
            agent_version="0.1.0-m0",
            runtime_version="0.1.0-m0",
            observed_at=observed_at,
        ),
    )


def _offline_probe(_device: EdgeDeviceDetail) -> EdgeProbeResult:
    raise ValueError("simulated isolated VLAN timeout")


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "adminadmin"},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def _copy_auth_cookies(
    source_client: TestClient,
    target_client: TestClient,
    settings: PlatformSettings,
) -> None:
    target_client.cookies.set(
        settings.session.cookie_name,
        source_client.cookies.get(settings.session.cookie_name),
    )
    target_client.cookies.set(
        settings.session.csrf_cookie_name,
        source_client.cookies.get(settings.session.csrf_cookie_name),
    )
