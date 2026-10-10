from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cv_contracts import EdgeDeviceAddress, EdgeDeviceRegistrationRequest
from cv_contracts.runtime import RuntimeChannelCreateRequest
from cv_db import (
    RuntimeChannel,
    RuntimeExecution,
    create_edge_device,
    create_runtime_channel,
    get_runtime_channel_detail,
    session_scope,
)
from cv_status import RuntimeSourceType
from sqlalchemy.engine import Engine
from tests.conftest import build_test_settings, configure_sqlite_factories


def test_edge_channel_summary_ignores_stale_central_runtime_execution(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        device = create_edge_device(
            session,
            EdgeDeviceRegistrationRequest(
                name="Orin Nano status test",
                address=EdgeDeviceAddress(management_ip="192.168.50.21"),
                allowed_cidr="192.168.50.0/24",
                server_ip="192.168.50.10",
            ),
            created_by="operator",
        )
        created = create_runtime_channel(
            session,
            RuntimeChannelCreateRequest(
                name="Edge status camera",
                technical_place="line-edge-status",
                source_type=RuntimeSourceType.RTSP,
                source_config={"url": "rtsp://camera.local/live"},
                placement={
                    "target": "edge_device",
                    "edge_device_id": device.edge_device_id,
                },
            ),
            created_by="operator",
        )
        session.add(
            RuntimeExecution(
                id="rex_stale_central_edge_channel",
                channel_id=created.channel_id,
                pipeline_version_id="plv_old_server_pipeline",
                mode="realtime",
                executor_service="realtime-executor",
                executor_instance_id="central-runtime-worker",
                status="stopped",
                actual_state="stopped",
                desired_state="stopped",
                frame_count=123,
                stopped_at=datetime.now(UTC),
            )
        )
        channel = session.get(RuntimeChannel, created.channel_id)
        assert channel is not None
        channel.desired_state = "running"
        channel.observed_state = "running"
        channel.health_state = "ok"
        channel.health_summary = {
            "state": "ok",
            "status_text": "Edge workload is running",
        }
        channel_id = channel.id

    with session_scope(settings) as session:
        summary = get_runtime_channel_detail(session, channel_id)

    assert summary is not None
    assert summary.placement.target == "edge_device"
    assert summary.observed_state.value == "running"
    assert summary.runtime_state is not None
    assert summary.runtime_state.actual_state.value == "running"
    assert summary.runtime_state.runtime_instance_id is None
    assert summary.runtime_state.assigned_runtime_worker is None
    assert summary.performance.frame_count == 0
