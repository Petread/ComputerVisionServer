from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cv_contracts.runtime import (
    RuntimeChannelCreateRequest,
    RuntimeChannelUpdateRequest,
    RuntimeGraphEdge,
    RuntimeLogicalModelRef,
    RuntimeNodeSpec,
    RuntimePipelineDefinitionCreateRequest,
    RuntimePipelineGraph,
    RuntimePipelineVersionCreateRequest,
)
from cv_core import PlatformSettings
from cv_db import (
    RuntimeChannel,
    RuntimeControlCommandRecord,
    RuntimeExecution,
    PipelineVersion,
    TriggerEndpoint,
    build_runtime_operational_summary,
    cleanup_stale_runtime_references,
    create_pipeline_definition,
    create_pipeline_version,
    create_runtime_control_command,
    create_runtime_channel,
    get_ready_runtime_execution_binding,
    get_runtime_channel_detail,
    mark_stale_runtime_executions,
    publish_pipeline_version,
    record_runtime_execution_heartbeat,
    request_runtime_channel_restart,
    request_runtime_channel_start,
    request_runtime_channel_stop,
    runtime_prometheus_metrics_text,
    session_scope,
    update_runtime_channel,
    validate_runtime_channel_start_requirements,
)
from cv_status import (
    HealthState,
    RuntimeControlCommand,
    RuntimeMode,
    RuntimeNodeCategory,
    RuntimeSourceType,
    TargetPath,
    TaskType,
)
import runtime_orchestrator.supervisor as supervisor_module
from runtime_orchestrator.supervisor import (
    RuntimeControlOrchestrator,
    RuntimeExecutorSupervisor,
    runtime_control_loop,
)
from sqlalchemy.engine import Engine
from tests.conftest import build_test_settings, configure_sqlite_factories
from tests.support.runtime_model_seed import seed_deployed_runtime_model


def test_channel_start_creates_supervised_runtime_instance(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings)

    with session_scope(settings) as session:
        channel, command = request_runtime_channel_start(
            session,
            channel_id,
            requested_by="operator",
            reason="start test",
        )
        assert channel.desired_state.value == "start_requested"
        assert command.status.value == "queued"

    try:
        assert orchestrator.process_all_available() == 1
        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            runtime_channel = session.get(RuntimeChannel, channel_id)
            assert runtime_channel is not None
            assert runtime_channel.active_runtime_execution_id == execution.id
            assert runtime_channel.observed_state == "running"
            assert execution.status == "running"
            assert execution.actual_state == "running"
            assert execution.loading_state == "ready"
            assert execution.pipeline_version_id == runtime_channel.pipeline_version_id
            assert execution.runtime_plan["node_count"] == 3
            assert supervisor.has_handle(execution.id)
    finally:
        supervisor.shutdown()


def test_duplicate_pending_start_is_coalesced_for_runtime_control(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)

    with session_scope(settings) as session:
        _, first_command = request_runtime_channel_start(
            session,
            channel_id,
            requested_by="operator",
            reason="first click",
        )
        _, second_command = request_runtime_channel_start(
            session,
            channel_id,
            requested_by="operator",
            reason="duplicate click",
        )

        assert second_command.command_id == first_command.command_id
        assert session.query(RuntimeControlCommandRecord).count() == 1


def test_force_stop_escalation_is_not_coalesced_with_pending_normal_stop(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_unassigned_channel(sqlite_engine, tmp_path)

    with session_scope(settings) as session:
        _, normal_stop = request_runtime_channel_stop(
            session,
            channel_id,
            requested_by="operator",
        )
        _, force_stop = request_runtime_channel_stop(
            session,
            channel_id,
            requested_by="operator",
            command_payload={"force": True},
        )

        assert force_stop.command_id != normal_stop.command_id
        assert session.query(RuntimeControlCommandRecord).count() == 2
        force_record = session.get(RuntimeControlCommandRecord, force_stop.command_id)
        assert force_record is not None
        assert force_record.payload == {"force": True}


def test_duplicate_start_against_running_runtime_restores_running_channel_state(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings)

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            assert channel.desired_state == "start_requested"

        assert orchestrator.process_all_available() == 1
        with session_scope(settings) as session:
            executions = session.query(RuntimeExecution).all()
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            assert len(executions) == 1
            assert channel.desired_state == "running"
            assert channel.observed_state == "running"
            assert channel.health_state == "ok"
    finally:
        supervisor.shutdown()


def test_channel_stop_stops_runtime_and_persists_state(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings)

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1
        with session_scope(settings) as session:
            request_runtime_channel_stop(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            channel = session.get(RuntimeChannel, channel_id)
            assert execution.status == "stopped"
            assert execution.actual_state == "stopped"
            assert execution.stopped_at is not None
            assert channel is not None
            assert channel.desired_state == "stopped"
            assert channel.observed_state == "stopped"
            assert channel.active_runtime_execution_id is None
    finally:
        supervisor.shutdown()


def test_stop_request_is_not_overwritten_by_late_source_heartbeat(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings)

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            request_runtime_channel_stop(session, channel_id, requested_by="operator")
            record_runtime_execution_heartbeat(
                session,
                runtime_execution_id=execution.id,
                health_summary={
                    "state": HealthState.OK.value,
                    "status_text": "RTSP source connected",
                    "last_error": None,
                },
                metrics_summary={"frame_count": 42, "drop_count": 0},
            )
            channel = session.get(RuntimeChannel, channel_id)
            summary = get_runtime_channel_detail(session, channel_id)

            assert channel is not None
            assert summary is not None
            assert channel.desired_state == "stop_requested"
            assert channel.observed_state == "stopping"
            assert channel.health_summary["status_text"] == "Stop requested"
            assert summary.health.status_text == "Stop requested"
            assert summary.runtime_state.actual_state.value == "stopping"
            assert summary.runtime_state.health_summary["status_text"] == "Stop requested"
    finally:
        supervisor.shutdown()


def test_channel_restart_stops_old_instance_and_starts_new_one(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings)

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1
        with session_scope(settings) as session:
            first_execution_id = session.query(RuntimeExecution).one().id
            request_runtime_channel_restart(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            executions = session.query(RuntimeExecution).order_by(RuntimeExecution.created_at.asc()).all()
            assert [item.status for item in executions] == ["stopped", "running"]
            assert executions[0].id == first_execution_id
            assert executions[1].id != first_execution_id
    finally:
        supervisor.shutdown()


def test_force_stop_finalizes_executor_that_ignores_normal_stop(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_unassigned_channel(sqlite_engine, tmp_path)
    supervisor, orchestrator = _orchestrator(settings)
    original_stop = supervisor.stop

    try:
        with session_scope(settings) as session:
            execution = RuntimeExecution(
                id="rex_force_stop_test",
                channel_id=channel_id,
                pipeline_version_id="plv_force_stop_test",
                mode=RuntimeMode.REALTIME.value,
                executor_service="realtime-executor",
                status="running",
                actual_state="running",
                loading_state="ready",
                desired_state="running",
                started_at=datetime.now(tz=UTC),
                heartbeat_at=datetime.now(tz=UTC),
            )
            session.add(execution)
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            channel.active_runtime_execution_id = execution.id
            channel.desired_state = "running"
            channel.observed_state = "running"
            request_runtime_channel_stop(
                session,
                channel_id,
                requested_by="operator",
                command_payload={"force": True},
            )

        supervisor.stop = lambda _runtime_execution_id, *, timeout_seconds=5.0: False
        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            execution = session.get(RuntimeExecution, "rex_force_stop_test")
            channel = session.get(RuntimeChannel, channel_id)
            assert execution is not None
            assert channel is not None
            assert execution.status == "stopped"
            assert execution.health_summary["forced_stop_recovery"] is True
            assert channel.active_runtime_execution_id is None
            assert channel.desired_state == "stopped"
            assert channel.observed_state == "stopped"
            assert channel.health_summary["forced_stop_recovery"] is True
    finally:
        supervisor.stop = original_stop
        supervisor.shutdown()


def test_running_heartbeat_repairs_channel_stuck_in_start_requested(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_unassigned_channel(sqlite_engine, tmp_path)

    with session_scope(settings) as session:
        execution = RuntimeExecution(
            id="rex_start_requested_repair",
            channel_id=channel_id,
            pipeline_version_id="plv_start_requested_repair",
            mode=RuntimeMode.REALTIME.value,
            executor_service="realtime-executor",
            status="running",
            actual_state="running",
            loading_state="ready",
            desired_state="running",
            started_at=datetime.now(tz=UTC),
            heartbeat_at=datetime.now(tz=UTC),
        )
        session.add(execution)
        channel = session.get(RuntimeChannel, channel_id)
        assert channel is not None
        channel.active_runtime_execution_id = execution.id
        channel.desired_state = "start_requested"
        channel.observed_state = "starting"

        record_runtime_execution_heartbeat(
            session,
            runtime_execution_id=execution.id,
            health_summary={
                "state": HealthState.OK.value,
                "status_text": "Realtime executor processing frames",
                "last_error": None,
            },
            metrics_summary={"frame_count": 42, "drop_count": 3, "fps": 1.0},
        )

        assert channel.desired_state == "running"
        assert channel.observed_state == "running"
        assert channel.health_state == "ok"
        assert channel.performance_summary["frame_count"] == 42


def test_running_channel_source_config_update_restarts_with_new_runtime_plan(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings)

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            first_execution = session.query(RuntimeExecution).one()
            first_execution_id = first_execution.id
            assert first_execution.runtime_plan["source_config"] == {
                "url": "rtsp://camera.local/stream"
            }
            update_runtime_channel(
                session,
                channel_id,
                RuntimeChannelUpdateRequest(
                    source_type=RuntimeSourceType.RTSP,
                    source_config={
                        "url": "rtsp://camera.local/stream",
                        "max_fps": 12,
                    },
                ),
                updated_by="operator",
            )
            assert (
                session.query(RuntimeControlCommandRecord)
                .filter(
                    RuntimeControlCommandRecord.command == "restart",
                    RuntimeControlCommandRecord.status == "queued",
                )
                .count()
                == 1
            )

        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            executions = session.query(RuntimeExecution).order_by(RuntimeExecution.created_at.asc()).all()
            assert [item.status for item in executions] == ["stopped", "running"]
            assert executions[0].id == first_execution_id
            assert executions[1].id != first_execution_id
            assert executions[1].runtime_plan["source_config"]["max_fps"] == 12.0
            assert executions[1].runtime_plan["binding_signature"]["source_config"]["max_fps"] == 12.0
    finally:
        supervisor.shutdown()


def test_publish_rebinds_stale_channel_version_and_restart_loads_new_pipeline(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings)

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            first_execution_id = str(channel.active_runtime_execution_id)
            first_version_id = str(channel.pipeline_version_id)
            first_version = session.get(PipelineVersion, first_version_id)
            assert first_version is not None
            definition_id = first_version.pipeline_definition_id

            _publish_realtime_version_for_definition(
                session,
                definition_id=definition_id,
                output_node_key="display_output",
            )

            channel.pipeline_version_id = first_version_id
            channel.active_runtime_execution_id = first_execution_id
            channel.desired_state = "running"
            channel.observed_state = "running"

            latest_version_id = _publish_realtime_version_for_definition(
                session,
                definition_id=definition_id,
                output_node_key="persist_new",
            )

            first_execution = session.get(RuntimeExecution, first_execution_id)
            assert first_execution is not None
            assert first_execution.status == "draining"
            assert first_execution.actual_state == "stopping"
            assert first_execution.desired_state == "stopped"
            assert channel.pipeline_version_id == latest_version_id
            assert channel.desired_state == "start_requested"
            assert channel.observed_state == "starting"
            assert (
                session.query(RuntimeControlCommandRecord)
                .filter(
                    RuntimeControlCommandRecord.command == "restart",
                    RuntimeControlCommandRecord.status == "queued",
                )
                .count()
                == 1
            )

        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            active = session.get(RuntimeExecution, channel.active_runtime_execution_id)
            assert active is not None
            first_execution = session.get(RuntimeExecution, first_execution_id)
            assert first_execution is not None
            assert first_execution.status == "stopped"
            assert active.status == "running"
            assert active.pipeline_version_id == latest_version_id
            node_keys = {node["node_key"] for node in active.runtime_plan["nodes"]}
            assert "persist_new" in node_keys
            assert "display_output" not in node_keys
            assert channel.pipeline_version_id == latest_version_id
    finally:
        supervisor.shutdown()


def test_ready_runtime_binding_uses_execution_version_after_channel_rebind(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings)

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            execution = session.get(RuntimeExecution, channel.active_runtime_execution_id)
            assert execution is not None
            old_execution_id = execution.id
            old_version_id = execution.pipeline_version_id
            old_version = session.get(PipelineVersion, old_version_id)
            assert old_version is not None

            new_version_id = _publish_realtime_version_for_definition(
                session,
                definition_id=old_version.pipeline_definition_id,
                output_node_key="persist_after_rebind",
            )

            execution.status = "running"
            execution.actual_state = "running"
            execution.desired_state = "running"
            channel.active_runtime_execution_id = old_execution_id
            channel.pipeline_version_id = new_version_id
            channel.desired_state = "running"
            channel.observed_state = "running"

            bound_execution, loaded = get_ready_runtime_execution_binding(
                session,
                channel_id=channel_id,
                expected_mode=RuntimeMode.REALTIME,
            )

            assert bound_execution.id == old_execution_id
            assert loaded.version.id == old_version_id
            assert loaded.version.status == "superseded"
    finally:
        supervisor.shutdown()


def test_reconcile_restarts_runtime_when_active_model_release_changes(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(
        sqlite_engine,
        tmp_path,
        mode=RuntimeMode.REALTIME,
        model_selection_policy="active_production",
    )
    supervisor, orchestrator = _orchestrator(settings)

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            first_execution = session.get(RuntimeExecution, channel.active_runtime_execution_id)
            assert first_execution is not None
            first_execution_id = first_execution.id
            first_bindings = first_execution.runtime_plan["binding_signature"]["model_bindings"]
            assert first_bindings[0]["deployment_release_id"] == "rel_detection"

            seed_deployed_runtime_model(
                session,
                release_id="rel_detection_v2",
                model_family_id="mf_defect_detector",
                model_family_slug="defect-detector",
                model_version_id="mvr_detection_v2",
                version_no=2,
                active_release=True,
            )

        assert orchestrator.reconcile() >= 1

        with session_scope(settings) as session:
            first_execution = session.get(RuntimeExecution, first_execution_id)
            assert first_execution is not None
            assert first_execution.status == "draining"
            assert (
                session.query(RuntimeControlCommandRecord)
                .filter(
                    RuntimeControlCommandRecord.command == "restart",
                    RuntimeControlCommandRecord.status == "queued",
                )
                .count()
                == 1
            )

        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            active = session.get(RuntimeExecution, channel.active_runtime_execution_id)
            assert active is not None
            assert active.id != first_execution_id
            assert active.status == "running"
            bindings = active.runtime_plan["binding_signature"]["model_bindings"]
            assert bindings[0]["deployment_release_id"] == "rel_detection_v2"
            assert bindings[0]["triton_model_version"] == 2
    finally:
        supervisor.shutdown()


def test_runtime_operational_summary_aggregates_metrics_and_stale_health(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings)

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            record_runtime_execution_heartbeat(
                session,
                runtime_execution_id=execution.id,
                executor_instance_id="realtime-executor-a",
                health_summary={"state": HealthState.OK.value, "status_text": "Running"},
                metrics_summary={
                    "processed_fps": 14.5,
                    "latency_p95_ms": 42.0,
                    "drop_rate_percent": 2.5,
                    "frame_count": 100,
                    "drop_count": 10,
                },
            )
            execution.heartbeat_at = datetime.now(tz=UTC) - timedelta(seconds=600)

        with session_scope(settings) as session:
            summary = build_runtime_operational_summary(session, settings)
            metrics_text = runtime_prometheus_metrics_text(session, settings)

        assert summary.state == HealthState.DEGRADED
        assert summary.total_channels == 1
        assert summary.active_runtime_instances == 1
        assert summary.stale_runtime_instances == 1
        assert summary.performance.realtime_fps == 14.5
        assert summary.performance.realtime_latency_p95_ms == 42.0
        assert summary.performance.frame_count == 100
        assert summary.performance.drop_count == 10
        assert summary.attention[0].channel_id == channel_id
        assert "Executor heartbeat is stale" in str(summary.attention[0].recovery_hint)
        assert "cv_runtime_realtime_fps" in metrics_text
        assert "cv_runtime_executor_heartbeat_age_seconds" in metrics_text
        assert "cv_runtime_trigger_timeouts" in metrics_text
        assert 'mode="realtime"' in metrics_text
    finally:
        supervisor.shutdown()


def test_cleanup_stale_runtime_references_clears_terminal_active_pointer(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings)

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            channel.active_runtime_execution_id = execution.id
            channel.desired_state = "stopped"
            execution.status = "stopped"
            execution.actual_state = "stopped"

            assert cleanup_stale_runtime_references(session) == 1
            assert channel.active_runtime_execution_id is None
            assert channel.observed_state == "stopped"
            assert channel.health_summary["recovery_hint"].startswith("Start the channel")
    finally:
        supervisor.shutdown()


def test_stale_draining_runtime_is_finalized_after_stop_grace_period(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings)

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            supervisor.stop(execution.id)
            execution.status = "draining"
            execution.actual_state = "stopping"
            execution.desired_state = "stopped"
            execution.heartbeat_at = datetime.now(tz=UTC) - timedelta(seconds=30)
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            channel.desired_state = "stop_requested"
            channel.observed_state = "stopping"
            channel.active_runtime_execution_id = execution.id

        with session_scope(settings) as session:
            changed = mark_stale_runtime_executions(
                session,
                stale_after_seconds=5,
                stop_after_seconds=10,
            )
            assert len(changed) == 1
            assert changed[0].status == "stopped"
            assert changed[0].actual_state == "stopped"
            assert changed[0].health_summary["forced_stop_recovery"] is True
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            assert channel.desired_state == "stopped"
            assert channel.observed_state == "stopped"
            assert channel.active_runtime_execution_id is None
            assert channel.health_summary["forced_stop_recovery"] is True
    finally:
        supervisor.shutdown()


def test_stale_heartbeat_transitions_to_degraded_then_failed(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings)

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            supervisor.stop(execution.id)
            execution.heartbeat_at = datetime.now(tz=UTC) - timedelta(seconds=20)
        with session_scope(settings) as session:
            changed = mark_stale_runtime_executions(
                session,
                stale_after_seconds=5,
                fail_after_seconds=60,
            )
            assert changed[0].actual_state == "degraded"
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            assert channel.observed_state == "degraded"

        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            execution.status = "running"
            execution.actual_state = "running"
            execution.heartbeat_at = datetime.now(tz=UTC) - timedelta(seconds=120)
        with session_scope(settings) as session:
            changed = mark_stale_runtime_executions(
                session,
                stale_after_seconds=5,
                fail_after_seconds=60,
            )
            assert changed[0].actual_state == "failed"
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            assert channel.observed_state == "failed"
    finally:
        supervisor.shutdown()


def test_retryable_stale_heartbeat_transitions_to_terminal_after_failure_window(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings)

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            supervisor.stop(execution.id)
            execution.status = "failed_retryable"
            execution.actual_state = "degraded"
            execution.loading_state = "heartbeat_stale"
            execution.last_error = "Runtime executor heartbeat is stale."
            execution.heartbeat_at = datetime.now(tz=UTC) - timedelta(seconds=120)

        with session_scope(settings) as session:
            changed = mark_stale_runtime_executions(
                session,
                stale_after_seconds=5,
                fail_after_seconds=60,
            )
            assert len(changed) == 1
            assert changed[0].status == "failed_terminal"
            assert changed[0].actual_state == "failed"
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            assert channel.observed_state == "failed"
    finally:
        supervisor.shutdown()


def test_retryable_runtime_recovers_when_heartbeat_returns(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings)

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            execution.status = "failed_retryable"
            execution.actual_state = "degraded"
            execution.loading_state = "heartbeat_stale"
            execution.last_error = "Runtime executor heartbeat is stale."
            record_runtime_execution_heartbeat(
                session,
                runtime_execution_id=execution.id,
                health_summary={
                    "state": HealthState.OK.value,
                    "status_text": "RTSP source connected",
                    "last_error": None,
                },
                metrics_summary={"frame_count": 5, "drop_count": 0},
            )
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            assert execution.status == "running"
            assert execution.actual_state == "running"
            assert execution.loading_state == "ready"
            assert execution.last_error is None
            assert channel.observed_state == "running"
            assert channel.health_state == "ok"
            assert channel.last_error is None
    finally:
        supervisor.shutdown()


def test_retryable_runtime_stays_retryable_on_degraded_heartbeat(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings)

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            execution.status = "failed_retryable"
            execution.actual_state = "degraded"
            execution.loading_state = "executor_failed"
            execution.last_error = "worker exited"
            record_runtime_execution_heartbeat(
                session,
                runtime_execution_id=execution.id,
                health_summary={
                    "state": HealthState.DEGRADED.value,
                    "status_text": "Realtime executor loop reported an error",
                    "last_error": "worker exited",
                },
                metrics_summary={"frame_count": 0, "drop_count": 1},
            )
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            assert execution.status == "failed_retryable"
            assert execution.actual_state == "degraded"
            assert execution.loading_state == "executor_failed"
            assert channel.observed_state == "degraded"
            assert channel.health_state == "degraded"
            assert channel.last_error == "worker exited"
    finally:
        supervisor.shutdown()


def test_reconcile_relaunches_retryable_stale_execution_with_active_handle(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    settings.runtime.executor_stale_after_seconds = 5
    supervisor, orchestrator = _orchestrator(settings, instance_id="runtime-orchestrator-a")

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            execution_id = execution.id
            execution.heartbeat_at = datetime.now(tz=UTC) - timedelta(seconds=7)

        assert orchestrator.reconcile() >= 1
        assert supervisor.has_handle(execution_id)

        with session_scope(settings) as session:
            execution = session.get(RuntimeExecution, execution_id)
            channel = session.get(RuntimeChannel, channel_id)
            assert execution is not None
            assert channel is not None
            assert execution.status == "running"
            assert execution.actual_state == "running"
            assert execution.assigned_runtime_worker == "runtime-orchestrator-a"
            assert channel.active_runtime_execution_id == execution_id
            assert channel.observed_state == "running"
            assert channel.health_state == "ok"
    finally:
        supervisor.shutdown()


def test_reconcile_recovers_running_instance_after_orchestrator_restart(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings, instance_id="runtime-orchestrator-a")
    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1
        with session_scope(settings) as session:
            runtime_execution_id = session.query(RuntimeExecution).one().id
    finally:
        supervisor.shutdown()

    supervisor_b, orchestrator_b = _orchestrator(settings, instance_id="runtime-orchestrator-b")
    try:
        assert orchestrator_b.reconcile() >= 1
        assert supervisor_b.has_handle(runtime_execution_id)
        with session_scope(settings) as session:
            execution = session.get(RuntimeExecution, runtime_execution_id)
            assert execution is not None
            assert execution.actual_state == "running"
            assert execution.assigned_runtime_worker == "runtime-orchestrator-b"
    finally:
        supervisor_b.shutdown()


def test_start_command_relaunches_existing_running_instance_without_local_handle(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor_a, orchestrator_a = _orchestrator(settings, instance_id="runtime-orchestrator-a")
    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator_a.process_all_available() == 1
        with session_scope(settings) as session:
            runtime_execution_id = session.query(RuntimeExecution).one().id
    finally:
        supervisor_a.shutdown()

    supervisor_b, orchestrator_b = _orchestrator(settings, instance_id="runtime-orchestrator-b")
    try:
        with session_scope(settings) as session:
            create_runtime_control_command(
                session,
                channel_id=channel_id,
                command=RuntimeControlCommand.START,
                requested_by="operator",
                reason="operator retried start after orchestrator restart",
            )

        assert orchestrator_b.process_one() is not None
        assert supervisor_b.has_handle(runtime_execution_id)
        with session_scope(settings) as session:
            execution = session.get(RuntimeExecution, runtime_execution_id)
            assert execution is not None
            assert execution.status == "running"
            assert execution.actual_state == "running"
            assert execution.assigned_runtime_worker == "runtime-orchestrator-b"
    finally:
        supervisor_b.shutdown()


def test_reconcile_restarts_desired_running_channel_after_terminal_executor_failure(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    supervisor, orchestrator = _orchestrator(settings, instance_id="runtime-orchestrator-a")
    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1
        with session_scope(settings) as session:
            failed_execution = session.query(RuntimeExecution).one()
            failed_execution.status = "failed_terminal"
            failed_execution.actual_state = "failed"
            failed_execution.desired_state = "running"
            failed_execution.last_error = "Runtime executor heartbeat is stale; the instance is treated as failed."
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            channel.desired_state = "running"
            channel.observed_state = "failed"
            channel.health_state = "down"
            channel.active_runtime_execution_id = failed_execution.id
            failed_execution_id = failed_execution.id
    finally:
        supervisor.shutdown()

    supervisor_b, orchestrator_b = _orchestrator(settings, instance_id="runtime-orchestrator-b")
    try:
        assert orchestrator_b.reconcile() >= 1
        assert orchestrator_b.process_all_available() == 1
        with session_scope(settings) as session:
            executions = session.query(RuntimeExecution).order_by(RuntimeExecution.created_at.asc()).all()
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            assert len(executions) == 2
            assert executions[0].id == failed_execution_id
            assert executions[0].status == "failed_terminal"
            assert executions[1].status == "running"
            assert executions[1].actual_state == "running"
            assert executions[1].assigned_runtime_worker == "runtime-orchestrator-b"
            assert channel.active_runtime_execution_id == executions[1].id
            assert channel.observed_state == "running"
            assert channel.health_state == "ok"
        assert supervisor_b.has_handle(executions[1].id)
    finally:
        supervisor_b.shutdown()


def test_live_runtime_start_does_not_create_or_own_pipeline_trigger_endpoints(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    with session_scope(settings) as session:
        trigger_version_id = _create_published_pipeline(
            session,
            mode=RuntimeMode.TRIGGER,
            channel_id=channel_id,
        )
        endpoint = session.query(TriggerEndpoint).one()
        assert endpoint.derived_from_pipeline is True
        assert endpoint.pipeline_version_id == trigger_version_id

    supervisor, orchestrator = _orchestrator(settings)

    try:
        with session_scope(settings) as session:
            request_runtime_channel_start(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1

        with session_scope(settings) as session:
            execution = session.query(RuntimeExecution).one()
            endpoint = session.query(TriggerEndpoint).one()
            assert execution.mode == "realtime"
            assert execution.executor_service == "realtime-executor"
            assert endpoint.channel_id == channel_id
            assert endpoint.pipeline_version_id == trigger_version_id
            assert endpoint.status == "degraded"
            record_runtime_execution_heartbeat(
                session,
                runtime_execution_id=execution.id,
                metrics_summary={"trigger_count": 0},
            )
            assert endpoint.status == "degraded"

        with session_scope(settings) as session:
            request_runtime_channel_stop(session, channel_id, requested_by="operator")
        assert orchestrator.process_all_available() == 1
        with session_scope(settings) as session:
            endpoint = session.query(TriggerEndpoint).one()
            assert endpoint.status == "degraded"
    finally:
        supervisor.shutdown()


def test_realtime_channel_can_bind_trigger_pipeline_without_live_start(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings, channel_id = _seed_channel(sqlite_engine, tmp_path, mode=RuntimeMode.REALTIME)
    with session_scope(settings) as session:
        trigger_version_id = _create_published_pipeline(
            session,
            mode=RuntimeMode.TRIGGER,
            channel_id=channel_id,
            source_node_type="source.channel_frame",
        )
        channel = update_runtime_channel(
            session,
            channel_id,
            RuntimeChannelUpdateRequest(pipeline_version_id=trigger_version_id),
            updated_by="operator",
        )

        assert channel.mode == RuntimeMode.REALTIME
        assert channel.pipeline_version_id == trigger_version_id
        endpoint = session.query(TriggerEndpoint).one()
        assert endpoint.channel_id == channel_id
        assert endpoint.pipeline_version_id == trigger_version_id
        assert endpoint.derived_from_pipeline is True

        try:
            validate_runtime_channel_start_requirements(session, channel_id=channel_id)
        except ValueError as exc:
            assert "triggerable pipelines run through trigger endpoints" in str(exc)
        else:
            raise AssertionError("Trigger pipeline assignment must not start as live runtime.")


def _orchestrator(
    settings: PlatformSettings,
    *,
    instance_id: str = "runtime-orchestrator-test",
) -> tuple[RuntimeExecutorSupervisor, RuntimeControlOrchestrator]:
    supervisor = RuntimeExecutorSupervisor(
        settings=settings,
        orchestrator_instance_id=instance_id,
        realtime_worker_factory=_IdleRealtimeWorker,
    )
    orchestrator = RuntimeControlOrchestrator(
        settings=settings,
        orchestrator_instance_id=instance_id,
        supervisor=supervisor,
    )
    return supervisor, orchestrator


class _IdleRealtimeWorker:
    def __init__(self, **_kwargs) -> None:
        pass

    def run(self, stop_event: threading.Event) -> None:
        stop_event.wait()


def test_runtime_control_loop_keeps_running_after_iteration_exception(monkeypatch) -> None:
    heartbeats: list[tuple[str, int, int]] = []

    async def run_loop() -> int:
        stop_event = asyncio.Event()

        class FailOnceOrchestrator:
            calls = 0

            def process_all_available(self, *, limit: int = 100) -> int:
                del limit
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("synthetic control-loop failure")
                stop_event.set()
                return 0

            def reconcile(self) -> int:
                return 0

        async def fast_wait(awaitable, timeout: float):  # noqa: ANN001
            del timeout
            if stop_event.is_set():
                return await awaitable
            awaitable.close()
            raise TimeoutError

        def record_heartbeat(
            _orchestrator,
            *,
            status: str,
            processed: int,
            reconciled: int,
        ) -> None:
            heartbeats.append((status, processed, reconciled))

        monkeypatch.setattr(
            supervisor_module,
            "_record_orchestrator_heartbeat",
            record_heartbeat,
        )
        monkeypatch.setattr(supervisor_module.asyncio, "wait_for", fast_wait)

        orchestrator = FailOnceOrchestrator()
        await runtime_control_loop(orchestrator, stop_event=stop_event)  # type: ignore[arg-type]
        return orchestrator.calls

    calls = asyncio.run(run_loop())

    assert calls == 2
    assert heartbeats[0] == ("degraded", 0, 0)
    assert ("running", 0, 0) in heartbeats
    assert heartbeats[-1] == ("stopping", 0, 0)


def test_runtime_control_loop_reconciles_after_processing_commands(monkeypatch) -> None:
    heartbeats: list[tuple[str, int, int]] = []

    async def run_loop() -> None:
        stop_event = asyncio.Event()

        class CommandThenStopOrchestrator:
            def process_all_available(self, *, limit: int = 100) -> int:
                del limit
                stop_event.set()
                return 1

            def reconcile(self) -> int:
                return 3

        async def fast_wait(awaitable, timeout: float):  # noqa: ANN001
            del timeout
            return await awaitable

        def record_heartbeat(
            _orchestrator,
            *,
            status: str,
            processed: int,
            reconciled: int,
        ) -> None:
            heartbeats.append((status, processed, reconciled))

        monkeypatch.setattr(
            supervisor_module,
            "_record_orchestrator_heartbeat",
            record_heartbeat,
        )
        monkeypatch.setattr(supervisor_module.asyncio, "wait_for", fast_wait)

        await runtime_control_loop(CommandThenStopOrchestrator(), stop_event=stop_event)  # type: ignore[arg-type]

    asyncio.run(run_loop())

    assert ("running", 1, 3) in heartbeats
    assert heartbeats[-1] == ("stopping", 0, 0)


def _seed_channel(
    sqlite_engine: Engine,
    tmp_path: Path,
    *,
    mode: RuntimeMode,
    model_selection_policy: str = "explicit_release",
) -> tuple[PlatformSettings, str]:
    configure_sqlite_factories(sqlite_engine, "runtime-orchestrator")
    settings = build_test_settings("runtime-orchestrator", tmp_path)
    with session_scope(settings) as session:
        seed_deployed_runtime_model(session)
        version_id = _create_published_pipeline(
            session,
            mode=mode,
            model_selection_policy=model_selection_policy,
        )
        channel = create_runtime_channel(
            session,
            RuntimeChannelCreateRequest(
                name="Lifecycle channel",
                technical_place="press-line-1",
                source_type=(
                    RuntimeSourceType.TRIGGER_CAPTURE
                    if mode == RuntimeMode.TRIGGER
                    else RuntimeSourceType.RTSP
                ),
                source_config=(
                    {"camera": "reject-camera"}
                    if mode == RuntimeMode.TRIGGER
                    else {"url": "rtsp://camera.local/stream"}
                ),
                mode=mode,
                assigned_pipeline="Lifecycle pipeline",
                pipeline_version_id=version_id,
            ),
            created_by="tester",
        )
    return settings, channel.channel_id


def _seed_unassigned_channel(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> tuple[PlatformSettings, str]:
    configure_sqlite_factories(sqlite_engine, "runtime-orchestrator")
    settings = build_test_settings("runtime-orchestrator", tmp_path)
    with session_scope(settings) as session:
        channel = create_runtime_channel(
            session,
            RuntimeChannelCreateRequest(
                name="Force-stop lifecycle channel",
                technical_place="press-line-force-stop",
                source_type=RuntimeSourceType.RTSP,
                source_config={"url": "rtsp://camera.local/stream"},
                mode=RuntimeMode.REALTIME,
            ),
            created_by="tester",
        )
    return settings, channel.channel_id


def _create_published_pipeline(
    session,
    *,
    mode: RuntimeMode,
    channel_id: str | None = None,
    source_node_type: str = "source.channel_snapshot",
    model_selection_policy: str = "explicit_release",
) -> str:
    definition = create_pipeline_definition(
        session,
        RuntimePipelineDefinitionCreateRequest(name="Lifecycle pipeline"),
        created_by="tester",
    )
    if mode == RuntimeMode.TRIGGER:
        if channel_id is None:
            raise ValueError("channel_id is required for triggerable pipeline tests")
        trigger_node = _node("http_trigger", "trigger.http_put")
        source_node = _node(
            "channel_snapshot",
            source_node_type,
            config={"channel_id": channel_id} if source_node_type == "source.channel_snapshot" else {},
        )
        output_node = _node(
            "http_response",
            "output.http_trigger_response",
            config={"response_fields": [{"source_field": "decision", "response_key": "decision"}]},
        )
        nodes = [
            trigger_node,
            source_node,
            _inference_node("detect", selection_policy=model_selection_policy),
            output_node,
        ]
        edges = [
            RuntimeGraphEdge(
                from_node_key=trigger_node.node_key,
                from_port="trigger",
                to_node_key=source_node.node_key,
                to_port="trigger",
            ),
            RuntimeGraphEdge(
                from_node_key=source_node.node_key,
                from_port="frame",
                to_node_key="detect",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="detect",
                from_port="detections",
                to_node_key=output_node.node_key,
                to_port="result",
            ),
        ]
    else:
        source_node = _node("channel_input", "source.channel_frame")
        output_node = _node("persist", "output.persisted_result")
        nodes = [
            source_node,
            _inference_node("detect", selection_policy=model_selection_policy),
            output_node,
        ]
        edges = [
            RuntimeGraphEdge(
                from_node_key=source_node.node_key,
                from_port="frame",
                to_node_key="detect",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="detect",
                from_port="detections",
                to_node_key=output_node.node_key,
                to_port="result",
            ),
        ]
    version = create_pipeline_version(
        session,
        definition.pipeline_definition_id,
        RuntimePipelineVersionCreateRequest(
            mode=mode,
            graph=RuntimePipelineGraph(
                nodes=nodes,
                edges=edges,
            ),
        ),
        created_by="tester",
    )
    published = publish_pipeline_version(session, version.pipeline_version_id, published_by="tester")
    return published.pipeline_version_id


def _publish_realtime_version_for_definition(
    session,
    *,
    definition_id: str,
    output_node_key: str,
) -> str:
    source_node = _node("channel_input", "source.channel_frame")
    output_node = _node(output_node_key, "output.persisted_result")
    version = create_pipeline_version(
        session,
        definition_id,
        RuntimePipelineVersionCreateRequest(
            mode=RuntimeMode.REALTIME,
            graph=RuntimePipelineGraph(
                nodes=[source_node, _inference_node("detect"), output_node],
                edges=[
                    RuntimeGraphEdge(
                        from_node_key=source_node.node_key,
                        from_port="frame",
                        to_node_key="detect",
                        to_port="frame",
                    ),
                    RuntimeGraphEdge(
                        from_node_key="detect",
                        from_port="detections",
                        to_node_key=output_node.node_key,
                        to_port="result",
                    ),
                ],
            ),
        ),
        created_by="tester",
    )
    published = publish_pipeline_version(session, version.pipeline_version_id, published_by="tester")
    return published.pipeline_version_id


def _node(
    node_key: str,
    node_type: str,
    *,
    config: dict[str, object] | None = None,
) -> RuntimeNodeSpec:
    if node_type.startswith("source."):
        category = RuntimeNodeCategory.SOURCE
    elif node_type.startswith("trigger."):
        category = RuntimeNodeCategory.TRIGGER
    else:
        category = RuntimeNodeCategory.OUTPUT
    return RuntimeNodeSpec(
        node_key=node_key,
        node_type=node_type,
        node_category=category,
        config=config or {},
    )


def _inference_node(
    node_key: str,
    *,
    selection_policy: str = "explicit_release",
) -> RuntimeNodeSpec:
    return RuntimeNodeSpec(
        node_key=node_key,
        node_type="inference.detection",
        node_category=RuntimeNodeCategory.INFERENCE,
        logical_model_ref=RuntimeLogicalModelRef(
            model_family_slug="defect-detector",
            target_path=TargetPath.REALTIME,
            required_task_type=TaskType.DETECTION,
            selection_policy=selection_policy,
            deployment_release_id=(
                "rel_detection" if selection_policy == "explicit_release" else None
            ),
        ),
    )
