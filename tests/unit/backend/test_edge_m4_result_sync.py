from __future__ import annotations

from importlib import util as importlib_util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cv_contracts import (
    EdgeDeploymentManifest,
    EdgeDeviceAddress,
    EdgeMediaLeaseRequest,
    EdgeMediaLeaseState,
    EdgePreviewFrame,
    EdgeResultAck,
    EdgeResultBatch,
    EdgeResultEnvelope,
    EdgeResultStreamList,
    EdgeResultStreamSummary,
    EdgeViewerLeaseRequest,
)
from cv_db import (
    EdgeResultReceiptRecord,
    EdgeViewerLeaseRecord,
    ResultRecord,
    RuntimeChannel,
    active_edge_viewer_leases,
    edge_result_cursor_sequences,
    get_runtime_live_view_response,
    persist_edge_result_batch,
    session_scope,
    upsert_edge_viewer_lease,
)
from edge_agent.state import EdgeAgentConfig, EdgeAgentState, EdgeStateError
from runtime_admin_api.edge_result_sync import EdgeResultSynchronizer
from runtime_admin_api.edge_workload_builder import build_edge_workload_release
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from tests.conftest import build_test_settings, configure_sqlite_factories
from tests.unit.backend.test_edge_m3_deployment import _seed_edge_channel


def test_result_stream_contract_rejects_inconsistent_watermarks() -> None:
    with pytest.raises(ValueError, match="acknowledged_sequence"):
        EdgeResultStreamSummary(
            boot_id="boot-invalid",
            earliest_sequence=0,
            highest_sequence=1,
            acknowledged_sequence=2,
            pending_count=0,
        )


def test_edge_outbox_discovers_old_boots_and_compacts_after_ack(tmp_path: Path) -> None:
    state = EdgeAgentState(
        EdgeAgentConfig(
            address=EdgeDeviceAddress(management_ip="192.168.50.21"),
            data_root=tmp_path,
            minimum_disk_free_bytes=0,
            engine_builder_mode="simulated",
        )
    )
    try:
        first_boot = state.boot_id
        first = state.enqueue_result(
            channel_id="channel-m4",
            workload_release_id="release-m4",
            payload={"value": 1},
        )
        state.enqueue_result(
            channel_id="channel-m4",
            workload_release_id="release-m4",
            payload={"value": 2},
        )
        second_boot = state.restart()
        state.enqueue_result(
            channel_id="channel-m4",
            workload_release_id="release-m4",
            payload={"value": 3},
        )

        streams = state.result_streams()
        assert {item.boot_id for item in streams.items} == {first_boot, second_boot}
        assert sum(item.pending_count for item in streams.items) == 3
        state.acknowledge(
            _ack(state, boot_id=first_boot, sequence=2, key="ack-old-boot")
        )
        assert state.store.outbox_usage()[0] == 1
        streams_after_ack = {
            item.boot_id: item for item in state.result_streams().items
        }
        assert set(streams_after_ack) == {first_boot, second_boot}
        assert streams_after_ack[first_boot].pending_count == 0
        assert streams_after_ack[first_boot].acknowledged_sequence == 2
        assert streams_after_ack[first_boot].highest_sequence == 2
        assert first.sequence == 1
    finally:
        state.close()


def test_edge_preview_requires_running_channel_and_active_lease(tmp_path: Path) -> None:
    state = EdgeAgentState(
        EdgeAgentConfig(
            address=EdgeDeviceAddress(management_ip="192.168.50.21"),
            data_root=tmp_path,
            minimum_disk_free_bytes=0,
            engine_builder_mode="simulated",
        )
    )
    manifest = _runtime_manifest("deployment-m4", "release-m4", "channel-m4")
    try:
        assert state._preview_requested("channel-m4") is False
        with pytest.raises(EdgeStateError):
            state.acquire_media_lease(
                EdgeMediaLeaseRequest(lease_id="lease-m4", channel_id="channel-m4")
            )
        state.prepare(manifest)
        state.commit(manifest.deployment_id, "commit-m4")
        lease = state.acquire_media_lease(
            EdgeMediaLeaseRequest(
                lease_id="lease-m4",
                channel_id="channel-m4",
                max_fps=2,
                max_width=640,
            )
        )
        assert lease.channel_id == "channel-m4"
        assert state._preview_requested("channel-m4") is True
        frame = state.publish_preview_bytes(
            channel_id="channel-m4",
            workload_release_id="release-m4",
            image_bytes=b"jpeg-preview",
            width=320,
            height=180,
        )
        assert state.preview_frame(
            lease_id="lease-m4", channel_id="channel-m4", after_sequence=0
        ).image_base64 == frame.image_base64
        assert not state.preview_frame(
            lease_id="lease-m4",
            channel_id="channel-m4",
            after_sequence=frame.sequence,
        ).available
        state.release_media_lease("lease-m4")
        assert state._preview_requested("channel-m4") is False
        with pytest.raises(EdgeStateError):
            state.preview_frame(
                lease_id="lease-m4", channel_id="channel-m4", after_sequence=0
            )
    finally:
        state.close()


def test_m4_result_persistence_rejects_gaps_and_deduplicates(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path / "server")
    with session_scope(settings) as session:
        channel_id, device_id = _seed_edge_channel(session)
        release = build_edge_workload_release(
            session,
            settings=settings,
            channel_id=channel_id,
            created_by="tester",
        )
        channel = session.get(RuntimeChannel, channel_id)
        assert channel is not None
        channel.result_history_policy = {
            "enabled": True,
            "retention_hours": 24,
            "sample_interval_seconds": 5,
            "max_records": 100,
        }
        gap = EdgeResultBatch(
            management_ip="192.168.50.21",
            boot_id="boot-gap",
            after_sequence=0,
            items=[_envelope(channel_id, release.workload_release_id, "boot-gap", 2)],
            highest_sequence=2,
        )
        with pytest.raises(ValueError, match="gap"):
            persist_edge_result_batch(session, edge_device_id=device_id, batch=gap)

    batch = EdgeResultBatch(
        management_ip="192.168.50.21",
        boot_id="boot-m4",
        after_sequence=0,
        items=[
            _envelope(channel_id, release.workload_release_id, "boot-m4", 1),
            _envelope(channel_id, release.workload_release_id, "boot-m4", 2),
        ],
        highest_sequence=2,
    )
    with session_scope(settings) as session:
        outcome = persist_edge_result_batch(session, edge_device_id=device_id, batch=batch)
        assert outcome.inserted_count == 2
    with session_scope(settings) as session:
        replay = persist_edge_result_batch(session, edge_device_id=device_id, batch=batch)
        assert replay.inserted_count == 0
        assert replay.duplicate_count == 2
        records = sorted(
            session.scalars(select(ResultRecord)).all(),
            key=lambda record: int(record.runtime_metadata["edge_sequence"]),
        )
        assert len(records) == 2
        assert [record.history_retained for record in records] == [True, False]
        assert all("image_base64" not in record.result for record in records)
        assert len(session.scalars(select(EdgeResultReceiptRecord)).all()) == 2


def test_result_synchronizer_recovers_ack_and_relays_preview(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path / "server-sync")
    edge_state = EdgeAgentState(
        EdgeAgentConfig(
            address=EdgeDeviceAddress(management_ip="192.168.50.21"),
            data_root=tmp_path / "edge-sync",
            minimum_disk_free_bytes=0,
            engine_builder_mode="simulated",
        )
    )
    try:
        with session_scope(settings) as session:
            channel_id, device_id = _seed_edge_channel(session)
            release = build_edge_workload_release(
                session,
                settings=settings,
                channel_id=channel_id,
                created_by="tester",
            )
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            channel.edge_workload_release_id = release.workload_release_id
            channel.edge_deployment_id = "deployment-m4-sync"
            channel.desired_state = "running"
            channel.observed_state = "running"
            upsert_edge_viewer_lease(
                session,
                channel_id=channel_id,
                payload=EdgeViewerLeaseRequest(max_fps=2, max_width=640),
            )
            assert len(active_edge_viewer_leases(session, edge_device_id=device_id)) == 1

        manifest = _runtime_manifest(
            "deployment-m4-sync", release.workload_release_id, channel_id
        )
        edge_state.prepare(manifest)
        edge_state.commit(manifest.deployment_id, "commit-sync")
        first_boot = edge_state.boot_id
        edge_state.enqueue_result(
            channel_id=channel_id,
            workload_release_id=release.workload_release_id,
            payload={"status": "completed", "derived_outputs": [{"value": 1}]},
        )
        second_boot = edge_state.restart()
        edge_state.enqueue_result(
            channel_id=channel_id,
            workload_release_id=release.workload_release_id,
            payload={"status": "completed", "derived_outputs": [{"value": 2}]},
        )
        edge_state.publish_preview_bytes(
            channel_id=channel_id,
            workload_release_id=release.workload_release_id,
            image_bytes=b"m4-preview",
            width=640,
            height=360,
        )
        client = _StateClient(edge_state, fail_ack_once=True)
        synchronizer = EdgeResultSynchronizer(
            settings,
            client_factory=lambda _device: client,
            interval_seconds=1,
        )

        first_status = synchronizer.sync_once()
        assert first_status.last_error is not None
        assert first_status.persisted_results == 1
        assert first_status.acknowledged_results == 0
        with session_scope(settings) as session:
            assert len(session.scalars(select(ResultRecord)).all()) == 1
            cursors = [
                edge_result_cursor_sequences(
                    session, edge_device_id=device_id, boot_id=boot_id
                )
                for boot_id in (first_boot, second_boot)
            ]
            assert (1, 0) in cursors

        second_status = synchronizer.sync_once()
        assert second_status.last_error is None
        assert second_status.persisted_results == 2
        assert second_status.acknowledged_results == 2
        assert edge_state.store.outbox_usage() == (0, 0)
        with session_scope(settings) as session:
            assert len(session.scalars(select(ResultRecord)).all()) == 2
            assert len(session.scalars(select(EdgeResultReceiptRecord)).all()) == 2
            assert edge_result_cursor_sequences(
                session, edge_device_id=device_id, boot_id=first_boot
            ) == (1, 1)
            assert edge_result_cursor_sequences(
                session, edge_device_id=device_id, boot_id=second_boot
            ) == (1, 1)
            live = get_runtime_live_view_response(
                session,
                settings,
                channel_id=channel_id,
            )
            assert live.state.available is True
            assert live.state.frame is not None
            assert live.state.frame.image_base64 is not None
    finally:
        edge_state.close()


def test_m4_models_and_migration(tmp_path: Path) -> None:
    assert EdgeResultReceiptRecord.__tablename__ == "edge_result_receipts"
    assert EdgeViewerLeaseRecord.__tablename__ == "edge_viewer_leases"
    engine = create_engine(f"sqlite:///{tmp_path / 'm4-migration.db'}")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        operations = Operations(context)
        path = Path(
            "backend/migrations/versions/20260721_0030_edge_result_sync_and_media_leases.py"
        )
        spec = importlib_util.spec_from_file_location("edge_m4_migration", path)
        assert spec is not None and spec.loader is not None
        module = importlib_util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.op = operations
        module.upgrade()
        assert {"edge_result_receipts", "edge_viewer_leases"} <= set(
            inspect(connection).get_table_names()
        )
        receipt_indexes = {
            item["name"]
            for item in inspect(connection).get_indexes("edge_result_receipts")
        }
        assert "ix_edge_result_receipts_channel_created" in receipt_indexes
        connection.execute(text("SELECT 1"))


class _StateClient:
    def __init__(self, state: EdgeAgentState, *, fail_ack_once: bool = False) -> None:
        self.state = state
        self.fail_ack_once = fail_ack_once

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def result_streams(self) -> EdgeResultStreamList:
        return self.state.result_streams()

    def results(
        self, *, after_sequence: int, limit: int, boot_id: str
    ) -> EdgeResultBatch:
        return self.state.results(
            boot_id=boot_id, after_sequence=after_sequence, limit=limit
        )

    def acknowledge_results(self, ack: EdgeResultAck) -> EdgeResultAck:
        confirmed = self.state.acknowledge(ack)
        if self.fail_ack_once:
            self.fail_ack_once = False
            raise OSError("simulated disconnect after remote acknowledgement")
        return confirmed

    def acquire_media_lease(self, request: EdgeMediaLeaseRequest) -> EdgeMediaLeaseState:
        return self.state.acquire_media_lease(request)

    def release_media_lease(self, lease_id: str) -> None:
        self.state.release_media_lease(lease_id)

    def preview_frame(
        self, *, lease_id: str, channel_id: str, after_sequence: int
    ) -> EdgePreviewFrame:
        return self.state.preview_frame(
            lease_id=lease_id,
            channel_id=channel_id,
            after_sequence=after_sequence,
        )


def _runtime_manifest(
    deployment_id: str,
    workload_release_id: str,
    channel_id: str,
) -> EdgeDeploymentManifest:
    return EdgeDeploymentManifest(
        deployment_id=deployment_id,
        workload_release_id=workload_release_id,
        channel_id=channel_id,
        pipeline_version_id="pipeline-m4",
        idempotency_key=f"prepare-{deployment_id}",
        runtime_requirements={
            "source": {"type": "passive", "interval_ms": 60_000},
            "emit_source_results": False,
        },
        created_at="2026-07-21T12:00:00+00:00",
    )


def _envelope(
    channel_id: str,
    workload_release_id: str,
    boot_id: str,
    sequence: int,
) -> EdgeResultEnvelope:
    return EdgeResultEnvelope(
        management_ip="192.168.50.21",
        boot_id=boot_id,
        sequence=sequence,
        channel_id=channel_id,
        workload_release_id=workload_release_id,
        correlation_id=f"edge-m4-{boot_id}-{sequence}",
        occurred_at="2026-07-21T12:00:00+00:00",
        payload={"status": "completed", "value": sequence, "image_base64": "not-durable"},
    )


def _ack(
    state: EdgeAgentState, *, boot_id: str, sequence: int, key: str
) -> EdgeResultAck:
    return EdgeResultAck(
        management_ip=state.config.address.management_ip,
        boot_id=boot_id,
        acknowledged_through_sequence=sequence,
        idempotency_key=key,
    )
