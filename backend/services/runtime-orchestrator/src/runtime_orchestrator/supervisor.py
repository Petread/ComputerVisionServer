from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from cv_core import PlatformSettings
from cv_db import (
    RuntimeControlCommandRecord,
    RuntimeExecution,
    claim_next_runtime_control_command,
    cleanup_stale_runtime_references,
    create_runtime_control_command,
    create_runtime_execution_instance,
    get_active_runtime_execution,
    list_recoverable_runtime_executions,
    mark_runtime_control_command_failed,
    mark_runtime_control_command_succeeded,
    mark_runtime_execution_running,
    mark_stale_runtime_executions,
    record_runtime_execution_heartbeat,
    record_service_heartbeat,
    request_runtime_execution_stop,
    runtime_execution_matches_channel_binding,
    runtime_execution_matches_loaded_binding,
    session_scope,
    stop_runtime_execution,
    transition_runtime_execution_loading,
    validate_runtime_channel_start_requirements,
)
from cv_db.models import RuntimeChannel
from cv_db.runtime_model_store import lock_model_lifecycle, lock_runtime_use
from cv_deploy.runtime import reconcile_runtime_models
from cv_status import (
    HealthState,
    RuntimeChannelDesiredState,
    RuntimeControlCommand,
    RuntimeExecutionStatus,
    RuntimeMode,
    RuntimeObservedState,
)
from cv_utils import utc_now
from sqlalchemy import select
from realtime_executor.live import (
    InferenceClientFactory,
    RealtimeRuntimeWorker,
    RealtimeSourceFactory,
)

LOGGER = logging.getLogger(__name__)


class RealtimeWorker(Protocol):
    def run(self, stop_event: threading.Event) -> None:
        ...


RealtimeWorkerFactory = Callable[..., RealtimeWorker]


@dataclass
class RuntimeExecutorHandle:
    runtime_execution_id: str
    channel_id: str
    mode: RuntimeMode
    executor_service: str
    stop_event: threading.Event
    thread: threading.Thread


@dataclass(frozen=True)
class RuntimeLaunchRequest:
    runtime_execution_id: str
    channel_id: str
    mode: RuntimeMode


class RuntimeExecutorSupervisor:
    def __init__(
        self,
        *,
        settings: PlatformSettings,
        orchestrator_instance_id: str,
        realtime_source_factory: RealtimeSourceFactory | None = None,
        realtime_inference_client_factory: InferenceClientFactory | None = None,
        realtime_worker_factory: RealtimeWorkerFactory | None = None,
    ) -> None:
        self.settings = settings
        self.orchestrator_instance_id = orchestrator_instance_id
        self.realtime_source_factory = realtime_source_factory
        self.realtime_inference_client_factory = realtime_inference_client_factory
        self.realtime_worker_factory = realtime_worker_factory
        self._handles: dict[str, RuntimeExecutorHandle] = {}
        self._lock = threading.Lock()

    def has_handle(self, runtime_execution_id: str) -> bool:
        with self._lock:
            handle = self._handles.get(runtime_execution_id)
            if handle is None:
                return False
            if handle.thread.is_alive():
                return True
            self._handles.pop(runtime_execution_id, None)
            return False

    def launch(self, *, runtime_execution_id: str, channel_id: str, mode: RuntimeMode) -> None:
        with self._lock:
            if runtime_execution_id in self._handles:
                return
            stop_event = threading.Event()
            executor_service = "trigger-executor" if mode == RuntimeMode.TRIGGER else "realtime-executor"
            target: Callable[..., None]
            kwargs: dict[str, Any]
            if mode == RuntimeMode.REALTIME:
                worker_factory = self.realtime_worker_factory or RealtimeRuntimeWorker
                worker = worker_factory(
                    settings=self.settings,
                    runtime_execution_id=runtime_execution_id,
                    channel_id=channel_id,
                    executor_instance_id=self.orchestrator_instance_id,
                    source_factory=self.realtime_source_factory,
                    inference_client_factory=self.realtime_inference_client_factory,
                )
                target = worker.run
                kwargs = {"stop_event": stop_event}
            else:
                target = self._heartbeat_loop
                kwargs = {
                    "runtime_execution_id": runtime_execution_id,
                    "channel_id": channel_id,
                    "mode": mode,
                    "executor_service": executor_service,
                    "stop_event": stop_event,
                }
            thread = threading.Thread(
                target=target,
                name=f"runtime-{runtime_execution_id}",
                kwargs=kwargs,
                daemon=True,
            )
            self._handles[runtime_execution_id] = RuntimeExecutorHandle(
                runtime_execution_id=runtime_execution_id,
                channel_id=channel_id,
                mode=mode,
                executor_service=executor_service,
                stop_event=stop_event,
                thread=thread,
            )
            thread.start()

    def stop(self, runtime_execution_id: str, *, timeout_seconds: float = 5.0) -> bool:
        with self._lock:
            handle = self._handles.get(runtime_execution_id)
        if handle is None:
            return True
        handle.stop_event.set()
        handle.thread.join(timeout=timeout_seconds)
        stopped = not handle.thread.is_alive()
        with self._lock:
            if stopped:
                self._handles.pop(runtime_execution_id, None)
        return stopped

    def active_runtime_execution_ids(self) -> set[str]:
        with self._lock:
            for runtime_execution_id, handle in list(self._handles.items()):
                if not handle.thread.is_alive():
                    self._handles.pop(runtime_execution_id, None)
            return set(self._handles)

    def shutdown(self) -> None:
        for runtime_execution_id in list(self.active_runtime_execution_ids()):
            self.stop(runtime_execution_id, timeout_seconds=2.0)

    def _heartbeat_loop(
        self,
        *,
        runtime_execution_id: str,
        channel_id: str,
        mode: RuntimeMode,
        executor_service: str,
        stop_event: threading.Event,
    ) -> None:
        interval = max(float(self.settings.runtime.executor_heartbeat_interval_seconds), 1.0)
        while not stop_event.wait(interval):
            with session_scope(self.settings) as session:
                execution = session.get(RuntimeExecution, runtime_execution_id)
                if execution is None or execution.desired_state == RuntimeChannelDesiredState.STOPPED.value:
                    return
                if execution.status not in {
                    RuntimeExecutionStatus.RUNNING.value,
                    RuntimeExecutionStatus.FAILED_RETRYABLE.value,
                }:
                    continue
                health_summary = {
                    "state": HealthState.OK.value,
                    "status_text": (
                        "Trigger endpoint ready"
                        if mode == RuntimeMode.TRIGGER
                        else "Realtime executor supervised"
                    ),
                    "executor_service": executor_service,
                    "endpoint_ready": mode == RuntimeMode.TRIGGER,
                }
                metrics_summary = {
                    "runtime_execution_id": runtime_execution_id,
                    "metrics_source": "executor_heartbeat",
                    "execution_mode": mode.value,
                    "frame_count": int(execution.frame_count or 0),
                    "drop_count": int(execution.drop_count or 0),
                    "trigger_count": int(execution.trigger_count or 0),
                }
                record_runtime_execution_heartbeat(
                    session,
                    runtime_execution_id=runtime_execution_id,
                    executor_instance_id=self.orchestrator_instance_id,
                    health_summary=health_summary,
                    metrics_summary=metrics_summary,
                )


class RuntimeControlOrchestrator:
    def __init__(
        self,
        *,
        settings: PlatformSettings,
        orchestrator_instance_id: str,
        supervisor: RuntimeExecutorSupervisor,
    ) -> None:
        self.settings = settings
        self.orchestrator_instance_id = orchestrator_instance_id
        self.supervisor = supervisor

    def process_one(self) -> RuntimeControlCommandRecord | None:
        with session_scope(self.settings) as session:
            command = claim_next_runtime_control_command(
                session,
                worker_id=self.orchestrator_instance_id,
            )
            if command is None:
                return None
            command_id = command.id

        with session_scope(self.settings) as session:
            command = session.get(RuntimeControlCommandRecord, command_id)
            if command is None:
                return None
            launch_request: RuntimeLaunchRequest | None = None
            try:
                runtime_execution_id, launch_request = self._process_claimed(session, command)
            except Exception as exc:
                if command.runtime_execution_id and command.command != RuntimeControlCommand.STOP.value:
                    stop_runtime_execution(session, runtime_execution_id=command.runtime_execution_id,
                                           error=str(exc), failed=True)
                mark_runtime_control_command_failed(
                    session,
                    command.id,
                    error=str(exc),
                    runtime_execution_id=command.runtime_execution_id,
                )
            else:
                mark_runtime_control_command_succeeded(
                    session,
                    command.id,
                    runtime_execution_id=runtime_execution_id,
                )
            processed = command
        if launch_request is not None:
            self.supervisor.launch(
                runtime_execution_id=launch_request.runtime_execution_id,
                channel_id=launch_request.channel_id,
                mode=launch_request.mode,
            )
        self._reconcile_models()
        return processed

    def _reconcile_models(self) -> None:
        try:
            with session_scope(self.settings) as session:
                reconcile_runtime_models(session, self.settings)
        except Exception:
            LOGGER.exception("Runtime model reconciliation will be retried.")

    def process_all_available(self, *, limit: int = 100) -> int:
        count = 0
        while count < limit and self.process_one() is not None:
            count += 1
        return count

    def reconcile(self) -> int:
        changed = 0
        launch_requests: list[RuntimeLaunchRequest] = []
        with session_scope(self.settings) as session:
            changed += self._reconcile_stopping_runtime_handles(session)
            stale = mark_stale_runtime_executions(
                session,
                stale_after_seconds=self.settings.runtime.executor_stale_after_seconds,
                stop_after_seconds=self.settings.runtime.executor_stop_grace_period_seconds,
            )
            changed += len(stale)
            changed += cleanup_stale_runtime_references(session)
            recoverable = list_recoverable_runtime_executions(session)
            active_handles = self.supervisor.active_runtime_execution_ids()
            for execution in recoverable:
                channel = session.get(RuntimeChannel, execution.channel_id)
                if (
                    channel is not None
                    and channel.pipeline_version_id
                    and not runtime_execution_matches_channel_binding(session, execution)
                ):
                    request_runtime_execution_stop(
                        session,
                        runtime_execution_id=execution.id,
                        update_channel=False,
                    )
                    if (
                        channel.active_runtime_execution_id == execution.id
                        and RuntimeChannelDesiredState(channel.desired_state)
                        in {
                            RuntimeChannelDesiredState.RUNNING,
                            RuntimeChannelDesiredState.START_REQUESTED,
                        }
                    ):
                        create_runtime_control_command(
                            session,
                            channel_id=channel.id,
                            command=RuntimeControlCommand.RESTART,
                            requested_by=self.orchestrator_instance_id,
                            reason=(
                                "Runtime reconcile requested a restart because the channel "
                                "pipeline assignment changed while an older runtime instance "
                                "was still active."
                            ),
                        )
                    changed += 1
                    continue
                execution_status = RuntimeExecutionStatus(execution.status)
                if execution.id in active_handles:
                    if execution_status != RuntimeExecutionStatus.FAILED_RETRYABLE:
                        continue
                    LOGGER.warning(
                        "Stopping stale retryable runtime executor before relaunch: "
                        "runtime_execution_id=%s channel_id=%s",
                        execution.id,
                        execution.channel_id,
                    )
                    if not self.supervisor.stop(execution.id, timeout_seconds=2.0):
                        continue
                    active_handles.discard(execution.id)
                if execution_status in {
                    RuntimeExecutionStatus.RUNNING,
                    RuntimeExecutionStatus.FAILED_RETRYABLE,
                }:
                    lock_model_lifecycle(session)
                    reconcile_runtime_models(session, self.settings, validate_local=True)
                    mark_runtime_execution_running(
                        session,
                        runtime_execution_id=execution.id,
                        executor_instance_id=self.orchestrator_instance_id,
                        assigned_runtime_worker=self.orchestrator_instance_id,
                    )
                    launch_requests.append(
                        RuntimeLaunchRequest(
                            runtime_execution_id=execution.id,
                            channel_id=execution.channel_id,
                            mode=RuntimeMode(execution.mode),
                        )
                    )
                    changed += 1
            changed += self._request_missing_desired_runtime_starts(session)
        for launch_request in launch_requests:
            self.supervisor.launch(
                runtime_execution_id=launch_request.runtime_execution_id,
                channel_id=launch_request.channel_id,
                mode=launch_request.mode,
            )
        self._reconcile_models()
        return changed

    def _reconcile_stopping_runtime_handles(self, session) -> int:
        changed = 0
        stopping = session.scalars(
            select(RuntimeExecution)
            .where(RuntimeExecution.status == RuntimeExecutionStatus.DRAINING.value)
            .order_by(RuntimeExecution.heartbeat_at.asc(), RuntimeExecution.created_at.asc())
        ).all()
        for execution in stopping:
            if not self.supervisor.stop(execution.id, timeout_seconds=0.1):
                continue
            if not lock_runtime_use(session, execution.id, exclusive=True):
                continue
            stop_runtime_execution(
                session,
                runtime_execution_id=execution.id,
            )
            changed += 1
        return changed

    def _request_missing_desired_runtime_starts(self, session) -> int:
        changed = 0
        channels = session.scalars(
            select(RuntimeChannel)
            .where(
                RuntimeChannel.deleted_at.is_(None),
                RuntimeChannel.desired_state == RuntimeChannelDesiredState.RUNNING.value,
            )
            .order_by(RuntimeChannel.updated_at.asc(), RuntimeChannel.created_at.asc())
        ).all()
        for channel in channels:
            active_execution = (
                session.get(RuntimeExecution, channel.active_runtime_execution_id)
                if channel.active_runtime_execution_id
                else None
            )
            if active_execution is not None and active_execution.status not in {
                RuntimeExecutionStatus.STOPPED.value,
                RuntimeExecutionStatus.FAILED_TERMINAL.value,
                RuntimeExecutionStatus.RECOVERED.value,
            }:
                if (
                    channel.pipeline_version_id
                    and not runtime_execution_matches_channel_binding(session, active_execution)
                ):
                    create_runtime_control_command(
                        session,
                        channel_id=channel.id,
                        command=RuntimeControlCommand.RESTART,
                        requested_by=self.orchestrator_instance_id,
                        reason=(
                            "Runtime reconcile requested a restart because the channel "
                            "pipeline assignment changed while an older runtime instance "
                            "was still active."
                        ),
                    )
                    changed += 1
                continue
            create_runtime_control_command(
                session,
                channel_id=channel.id,
                command=RuntimeControlCommand.START,
                requested_by=self.orchestrator_instance_id,
                reason=(
                    "Runtime reconcile requested a fresh executor because the channel "
                    "is desired running but has no active runtime instance."
                ),
                idempotency_key=(
                    f"runtime-reconcile:{channel.id}:"
                    f"{channel.active_runtime_execution_id or 'missing'}"
                ),
            )
            changed += 1
        return changed

    def _process_claimed(
        self,
        session,
        command: RuntimeControlCommandRecord,
    ) -> tuple[str | None, RuntimeLaunchRequest | None]:
        command_type = RuntimeControlCommand(command.command)
        if command_type == RuntimeControlCommand.START:
            return self._start(session, command)
        if command_type == RuntimeControlCommand.STOP:
            return self._stop(session, command), None
        if command_type == RuntimeControlCommand.RESTART:
            self._stop(session, command)
            return self._start(session, command)
        if command_type == RuntimeControlCommand.RECONCILE:
            return None, None
        raise ValueError(f"Unsupported runtime-control command: {command.command}")

    def _start(self, session, command: RuntimeControlCommandRecord) -> tuple[str, RuntimeLaunchRequest | None]:
        lock_model_lifecycle(session)
        loaded = validate_runtime_channel_start_requirements(session, channel_id=command.channel_id)
        active = get_active_runtime_execution(session, channel_id=command.channel_id)
        if (
            active is not None
            and active.status == RuntimeExecutionStatus.RUNNING.value
            and active.actual_state == RuntimeObservedState.RUNNING.value
            and runtime_execution_matches_loaded_binding(session, active, loaded)
        ):
            reconcile_runtime_models(
                session, self.settings, validate_local=not self.supervisor.has_handle(active.id)
            )
            mark_runtime_execution_running(
                session,
                runtime_execution_id=active.id,
                executor_instance_id=self.orchestrator_instance_id,
                assigned_runtime_worker=self.orchestrator_instance_id,
            )
            command.runtime_execution_id = active.id
            if self.supervisor.has_handle(active.id):
                return active.id, None
            return active.id, RuntimeLaunchRequest(
                runtime_execution_id=active.id,
                channel_id=loaded.channel.id,
                mode=RuntimeMode(loaded.version.mode),
            )

        if active is not None:
            request_runtime_execution_stop(session, runtime_execution_id=active.id)
            if not self.supervisor.stop(active.id):
                raise TimeoutError(
                    f"Runtime executor {active.id} did not stop within the timeout."
                )
            if not lock_runtime_use(session, active.id, exclusive=True):
                raise ValueError("The previous runtime is still completing an invocation; retry restart after it drains.")
            stop_runtime_execution(session, runtime_execution_id=active.id)

        execution = create_runtime_execution_instance(
            session,
            loaded=loaded,
            command_id=command.id,
            executor_instance_id=self.orchestrator_instance_id,
            assigned_runtime_worker=self.orchestrator_instance_id,
        )
        command.runtime_execution_id = execution.id
        transition_runtime_execution_loading(
            session,
            runtime_execution_id=execution.id,
            status=RuntimeExecutionStatus.LOADING_PIPELINE,
            actual_state=RuntimeObservedState.LOADING_PIPELINE,
            loading_state="loading_pipeline",
            health_text="Loading published pipeline",
        )
        reconcile_runtime_models(session, self.settings, validate_local=True)
        transition_runtime_execution_loading(
            session,
            runtime_execution_id=execution.id,
            status=RuntimeExecutionStatus.STARTING,
            actual_state=RuntimeObservedState.STARTING,
            loading_state="starting_executor",
            health_text="Starting executor",
        )
        mark_runtime_execution_running(
            session,
            runtime_execution_id=execution.id,
            executor_instance_id=self.orchestrator_instance_id,
            assigned_runtime_worker=self.orchestrator_instance_id,
        )
        return execution.id, RuntimeLaunchRequest(
            runtime_execution_id=execution.id,
            channel_id=loaded.channel.id,
            mode=RuntimeMode(loaded.version.mode),
        )

    def _stop(self, session, command: RuntimeControlCommandRecord) -> str | None:
        active = get_active_runtime_execution(session, channel_id=command.channel_id)
        if active is None:
            channel = session.get(RuntimeChannel, command.channel_id)
            if channel is not None:
                channel.desired_state = RuntimeChannelDesiredState.STOPPED.value
                channel.observed_state = RuntimeObservedState.STOPPED.value
                channel.health_state = HealthState.DOWN.value
                channel.active_runtime_execution_id = None
                channel.last_activity_at = utc_now()
                channel.health_summary = {
                    "state": HealthState.DOWN.value,
                    "status_text": "Stopped",
                }
            return None
        request_runtime_execution_stop(session, runtime_execution_id=active.id)
        force = bool(dict(command.payload or {}).get("force"))
        executor_stopped = self.supervisor.stop(
            active.id,
            timeout_seconds=0.25 if force else 5.0,
        )
        if not executor_stopped and not force:
            raise TimeoutError(
                f"Runtime executor {active.id} did not stop within the timeout."
            )
        if not lock_runtime_use(session, active.id, exclusive=True):
            return active.id
        lock_model_lifecycle(session)
        stopped = stop_runtime_execution(session, runtime_execution_id=active.id)
        if force and not executor_stopped:
            LOGGER.warning(
                "Force-finalized unresponsive runtime executor: "
                "runtime_execution_id=%s channel_id=%s",
                active.id,
                command.channel_id,
            )
            stopped.health_summary = {
                **(stopped.health_summary or {}),
                "status_text": "Runtime force-stopped after executor timeout",
                "forced_stop_recovery": True,
                "recovery_hint": "The unresponsive executor was detached from the channel.",
            }
            channel = session.get(RuntimeChannel, command.channel_id)
            if channel is not None:
                channel.health_summary = {
                    **(channel.health_summary or {}),
                    "status_text": "Force-stopped after executor timeout",
                    "forced_stop_recovery": True,
                }
        command.runtime_execution_id = active.id
        return active.id


async def runtime_control_loop(
    orchestrator: RuntimeControlOrchestrator,
    *,
    stop_event: asyncio.Event,
) -> None:
    heartbeat_status = "running"
    while not stop_event.is_set():
        processed = 0
        changed = 0
        try:
            processed = await asyncio.to_thread(orchestrator.process_all_available, limit=25)
            changed = await asyncio.to_thread(orchestrator.reconcile)
            heartbeat_status = "running"
        except Exception:
            heartbeat_status = "degraded"
            LOGGER.exception("Runtime control loop iteration failed; retrying.")
        try:
            await asyncio.to_thread(
                _record_orchestrator_heartbeat,
                orchestrator,
                status=heartbeat_status,
                processed=processed,
                reconciled=changed,
            )
        except Exception:
            LOGGER.exception("Runtime control loop heartbeat failed.")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
        except TimeoutError:
            continue
    await asyncio.to_thread(
        _record_orchestrator_heartbeat,
        orchestrator,
        status="stopping",
        processed=0,
        reconciled=0,
    )


def _record_orchestrator_heartbeat(
    orchestrator: RuntimeControlOrchestrator,
    *,
    status: str,
    processed: int,
    reconciled: int,
) -> None:
    with session_scope(orchestrator.settings) as session:
        record_service_heartbeat(
            session,
            service_name="runtime-orchestrator",
            instance_id=orchestrator.orchestrator_instance_id,
            status=status,
            details={
                "active_runtime_handles": len(
                    orchestrator.supervisor.active_runtime_execution_ids()
                ),
                "processed_commands": processed,
                "reconciled_runtime_records": reconciled,
                "executor_stale_after_seconds": (
                    orchestrator.settings.runtime.executor_stale_after_seconds
                ),
            },
        )
