from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import Callable
from pathlib import Path

from cv_contracts import EdgeDeploymentControllerStatus, EdgeDeviceDetail, EdgeWorkloadReleaseDetail
from cv_core import PlatformSettings
from cv_db import (
    claim_next_edge_runtime_control_command,
    complete_edge_runtime_command,
    create_edge_deployment_attempt,
    fail_edge_runtime_command,
    get_edge_device_detail,
    get_edge_workload_release,
    get_edge_workload_release_row,
    latest_edge_deployment_row,
    mark_edge_deployment_validated,
    mark_previous_edge_deployment_standby,
    session_scope,
    update_edge_deployment_attempt,
)
from cv_db.models import RuntimeChannel
from cv_edge import EdgeAgentClient, EdgeNetworkPolicy
from cv_status import RuntimeControlCommand
from cv_utils import utc_now_iso
from sqlalchemy.orm import Session

from runtime_admin_api.edge_workload_builder import (
    build_edge_workload_release,
    materialize_edge_deployment_manifest,
)

ClientFactory = Callable[[EdgeDeviceDetail], EdgeAgentClient]


class EdgeDeploymentController:
    """Consumes channel control commands and applies immutable releases to Edge devices."""

    def __init__(
        self,
        settings: PlatformSettings,
        *,
        interval_seconds: float = 1.0,
        client_factory: ClientFactory | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.interval_seconds = interval_seconds
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:12]}"
        self.client_factory = client_factory or _edge_client
        self.processed_commands = 0
        self.failed_commands = 0
        self.active_command_id: str | None = None
        self.last_error: str | None = None
        self.running = False

    @classmethod
    def from_environment(cls, settings: PlatformSettings) -> EdgeDeploymentController:
        return cls(
            settings,
            interval_seconds=float(os.getenv("YBB_EDGE_DEPLOYMENT_INTERVAL_SECONDS", "1")),
        )

    def status(self) -> EdgeDeploymentControllerStatus:
        return EdgeDeploymentControllerStatus(
            running=self.running,
            worker_id=self.worker_id,
            interval_seconds=self.interval_seconds,
            processed_commands=self.processed_commands,
            failed_commands=self.failed_commands,
            active_command_id=self.active_command_id,
            last_error=self.last_error,
        )

    async def run(self, stop_event: asyncio.Event) -> None:
        self.running = True
        try:
            while not stop_event.is_set():
                handled = await asyncio.to_thread(self.run_once)
                if handled:
                    continue
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
                except TimeoutError:
                    pass
        finally:
            self.running = False

    def run_once(self) -> bool:
        from runtime_admin_api.edge_software import process_software_command

        process_software_command(self.settings)
        with session_scope(self.settings) as session:
            command = claim_next_edge_runtime_control_command(session, worker_id=self.worker_id)
            if command is None:
                return False
            command_id = command.id
            command_type = RuntimeControlCommand(command.command)
            channel_id = command.channel_id
            requested_by = command.requested_by
            command_payload = dict(command.payload or {})
        self.active_command_id = command_id
        try:
            if command_type == RuntimeControlCommand.STOP:
                self._stop(command_id=command_id, channel_id=channel_id)
            else:
                self._deploy(
                    command_id=command_id,
                    channel_id=channel_id,
                    requested_by=requested_by,
                    command_payload=command_payload,
                )
        except Exception as exc:  # noqa: BLE001 - durable worker failure boundary
            self.failed_commands += 1
            self.last_error = str(exc)
            with session_scope(self.settings) as session:
                fail_edge_runtime_command(session, command_id=command_id, error=str(exc))
        else:
            self.processed_commands += 1
            self.last_error = None
        finally:
            self.active_command_id = None
        return True

    def _deploy(
        self,
        *,
        command_id: str,
        channel_id: str,
        requested_by: str | None,
        command_payload: dict[str, object],
    ) -> None:
        with session_scope(self.settings) as session:
            expected_release_id = str(
                command_payload.get("expected_edge_workload_release_id") or ""
            ).strip()
            if expected_release_id:
                release = get_edge_workload_release(session, expected_release_id)
                if release is None:
                    raise RuntimeError("The deployment wizard's pinned release no longer exists.")
                _validate_pinned_release(
                    session,
                    release,
                    channel_id=channel_id,
                    expected_release_digest=str(
                        command_payload.get("expected_release_digest") or ""
                    ),
                )
            else:
                release = build_edge_workload_release(
                    session,
                    settings=self.settings,
                    channel_id=channel_id,
                    created_by=requested_by,
                )
            attempt = create_edge_deployment_attempt(
                session,
                command_id=command_id,
                channel_id=channel_id,
                edge_device_id=release.edge_device_id,
                workload_release_id=release.workload_release_id,
            )
            attempt_id = attempt.edge_deployment_id
            if release.status != "admitted":
                messages = [
                    item.message
                    for item in release.admission_report.checks
                    if item.outcome == "fail"
                ]
                message = "; ".join(messages) or "Edge admission rejected the release."
                update_edge_deployment_attempt(
                    session, attempt_id, status="admission_failed", last_error=message
                )
                session.commit()
                raise RuntimeError(message)
            device = get_edge_device_detail(session, release.edge_device_id)
            row = get_edge_workload_release_row(session, release.workload_release_id)
            if device is None or row is None:
                raise RuntimeError("Edge release lost its registered target or artifact metadata.")
            sources = {
                str(item["sha256"]): Path(str(item["source_path"])) for item in row.artifact_sources
            }
            manifest = materialize_edge_deployment_manifest(session, release.manifest).model_copy(
                update={
                    "deployment_id": attempt_id,
                    "idempotency_key": f"prepare:{command_id}:{release.release_digest}",
                    "created_at": utc_now_iso(),
                }
            )
            update_edge_deployment_attempt(
                session,
                attempt_id,
                status="transferring",
                progress={"artifacts_total": len(manifest.artifacts), "artifacts_uploaded": 0},
            )

        prepared = False
        try:
            with self.client_factory(device) as client:
                for index, descriptor in enumerate(manifest.artifacts, start=1):
                    source = sources.get(descriptor.sha256)
                    if source is None:
                        raise RuntimeError(f"No local source exists for {descriptor.sha256}.")
                    client.upload_blob_file(
                        source,
                        descriptor=descriptor,
                        idempotency_key=f"blob:{command_id}:{descriptor.sha256}",
                    )
                    with session_scope(self.settings) as session:
                        update_edge_deployment_attempt(
                            session,
                            attempt_id,
                            status="transferring",
                            progress={
                                "artifacts_total": len(manifest.artifacts),
                                "artifacts_uploaded": index,
                            },
                        )
                with session_scope(self.settings) as session:
                    update_edge_deployment_attempt(session, attempt_id, status="preparing")
                state = client.prepare_deployment(manifest)
                prepared = True
                with session_scope(self.settings) as session:
                    update_edge_deployment_attempt(
                        session,
                        attempt_id,
                        status="activating",
                        remote_status=state.status,
                        progress={
                            "artifacts_total": len(manifest.artifacts),
                            "artifacts_uploaded": len(manifest.artifacts),
                            "local_admission": state.admission_report,
                            "engine_builds": state.engine_builds,
                        },
                    )
                state = client.commit_deployment(attempt_id, idempotency_key=f"commit:{command_id}")
        except Exception as exc:
            rolled_back = False
            if prepared:
                try:
                    with self.client_factory(device) as rollback_client:
                        rollback_client.rollback_deployment(
                            attempt_id, idempotency_key=f"rollback:{command_id}"
                        )
                    rolled_back = True
                except Exception:  # noqa: BLE001 - original deployment error stays authoritative
                    pass
            with session_scope(self.settings) as session:
                update_edge_deployment_attempt(
                    session,
                    attempt_id,
                    status="rolled_back" if rolled_back else "failed",
                    last_error=str(exc),
                )
            raise

        with session_scope(self.settings) as session:
            update_edge_deployment_attempt(
                session,
                attempt_id,
                status="running",
                remote_status=state.status,
                progress={
                    "artifacts_total": len(manifest.artifacts),
                    "artifacts_uploaded": len(manifest.artifacts),
                    "runtime_slot": state.runtime_slot,
                    "local_admission": state.admission_report,
                    "engine_builds": state.engine_builds,
                },
            )
            mark_previous_edge_deployment_standby(session, attempt_id)
            mark_edge_deployment_validated(session, release.edge_device_id)
            complete_edge_runtime_command(
                session,
                command_id=command_id,
                deployment_id=attempt_id,
                workload_release_id=release.workload_release_id,
                running=True,
            )

    def _stop(self, *, command_id: str, channel_id: str) -> None:
        with session_scope(self.settings) as session:
            channel = session.get(RuntimeChannel, channel_id)
            if channel is None or not channel.edge_device_id:
                raise RuntimeError("Edge channel or device assignment no longer exists.")
            device = get_edge_device_detail(session, channel.edge_device_id)
            deployment = latest_edge_deployment_row(session, channel_id)
            if device is None:
                raise RuntimeError("Assigned Edge device no longer exists.")
            deployment_id = channel.edge_deployment_id or (
                deployment.id if deployment is not None and deployment.status == "running" else None
            )
            workload_release_id = channel.edge_workload_release_id
            if deployment_id is not None:
                update_edge_deployment_attempt(session, deployment_id, status="stopping")
        if deployment_id is not None:
            with self.client_factory(device) as client:
                state = client.stop_deployment(deployment_id, idempotency_key=f"stop:{command_id}")
            with session_scope(self.settings) as session:
                update_edge_deployment_attempt(
                    session,
                    deployment_id,
                    status="stopped",
                    remote_status=state.status,
                )
        with session_scope(self.settings) as session:
            complete_edge_runtime_command(
                session,
                command_id=command_id,
                deployment_id=None,
                workload_release_id=workload_release_id,
                running=False,
            )


def _edge_client(device: EdgeDeviceDetail) -> EdgeAgentClient:
    policy = EdgeNetworkPolicy.from_strings(
        allowed_cidrs=[str(device.allowed_cidr)],
        registered_addresses=[str(device.address.management_ip)],
    )
    return EdgeAgentClient(
        address=device.address,
        network_policy=policy,
        connect_timeout_seconds=3.0,
        read_timeout_seconds=float(os.getenv("YBB_EDGE_DEPLOYMENT_TIMEOUT_SECONDS", "1200")),
        transfer_timeout_seconds=float(os.getenv("YBB_EDGE_TRANSFER_TIMEOUT_SECONDS", "120")),
    )


def _validate_pinned_release(
    session: Session,
    release: EdgeWorkloadReleaseDetail,
    *,
    channel_id: str,
    expected_release_digest: str,
) -> None:
    channel = session.get(RuntimeChannel, channel_id)
    device = get_edge_device_detail(session, release.edge_device_id)
    if channel is None or channel.deleted_at is not None:
        raise RuntimeError("The deployment wizard channel no longer exists.")
    if (
        channel.execution_target != "edge_device"
        or channel.edge_device_id != release.edge_device_id
        or channel.pipeline_version_id != release.pipeline_version_id
    ):
        raise RuntimeError("Channel placement or pipeline changed after deployment preflight.")
    if release.status != "admitted":
        raise RuntimeError("The pinned workload release is not admitted.")
    if not expected_release_digest or release.release_digest != expected_release_digest:
        raise RuntimeError("The pinned workload release digest does not match preflight.")
    if device is None or not device.enabled or device.connectivity_state != "online":
        raise RuntimeError("The pinned Edge target is not online and enabled.")
    snapshot = device.latest_capability_snapshot
    if snapshot is None or snapshot.digest != release.capability_digest:
        raise RuntimeError("Edge capabilities changed after deployment preflight.")
