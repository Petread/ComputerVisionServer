"""Runtime release catalogue and durable orchestration in the existing fleet command store."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path

from cv_contracts.edge import (
    EdgeArtifactDescriptor,
    EdgeBlobUploadStatus,
    EdgeCapabilitySnapshot,
    EdgeFleetCommandSummary,
    EdgeSoftwareCommand,
    EdgeSoftwareOverview,
    EdgeSoftwareRolloutRequest,
)
from cv_core import PlatformSettings
from cv_db import (
    create_edge_fleet_command,
    finish_edge_fleet_command,
    get_edge_device_detail,
    list_edge_devices,
    session_scope,
    start_edge_fleet_command,
)
from cv_db.models import EdgeDevice, EdgeFleetCommandRecord
from cv_edge import EdgeAgentClient, EdgeNetworkPolicy
from cv_edge.software import runtime_platform_version_matches, verify_software_package
from cv_runtime.distribution import runtime_code_digest
from cv_utils import utc_now
from sqlalchemy import select
from sqlalchemy.orm import Session


def catalogue_root(settings: PlatformSettings) -> Path:
    root = settings.storage.edge_workload_blob_root() / "software"
    root.mkdir(parents=True, exist_ok=True)
    return root


def import_package(settings: PlatformSettings, path: Path) -> dict:
    key = Path(
        os.getenv("YBB_EDGE_TRUSTED_RELEASE_KEY", "/srv/cv-platform/config/edge-release-public.pem")
    )
    release = verify_software_package(path, key)
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    root = catalogue_root(settings)
    descriptor = {
        "package_sha256": digest,
        "size_bytes": path.stat().st_size,
        "release": release.model_dump(mode="json"),
    }
    os.replace(path, root / f"{digest}.zip")
    temporary = root / f"{digest}.tmp"
    temporary.write_text(json.dumps(descriptor), encoding="utf-8")
    os.replace(temporary, root / f"{digest}.json")
    return descriptor


def compatibility(capabilities: EdgeCapabilitySnapshot | None, release: dict) -> list[str]:
    if capabilities is None:
        return ["Gerätestatus zuerst abrufen."]
    reasons = []
    if capabilities.software_update_protocol < 1:
        reasons.append("Einmalige Installation des Runtime-Launchers erforderlich.")
    for name in ("platform", "architecture"):
        expected = release.get(name)
        if expected and expected != getattr(capabilities, name):
            reasons.append(
                f"{name}: Paket {expected}, Gerät {getattr(capabilities, name) or 'unbekannt'}"
            )
    for name in ("cuda_version", "tensorrt_version"):
        expected = release.get(name)
        installed = getattr(capabilities, name)
        if not runtime_platform_version_matches(expected, installed):
            reasons.append(f"{name}: Paket {expected}, Gerät {installed or 'unbekannt'}")
    installed_python = ".".join(str(capabilities.python_version or "").split(".")[:2])
    if release["python_version"] != installed_python:
        reasons.append("Python-Basisversion stimmt nicht mit dem Paket überein.")
    return reasons


def software_overview(session: Session, settings: PlatformSettings) -> EdgeSoftwareOverview:
    releases = [json.loads(p.read_text()) for p in sorted(catalogue_root(settings).glob("*.json"))]
    devices = []
    for summary in list_edge_devices(session):
        detail = get_edge_device_detail(session, summary.edge_device_id)
        caps = (
            detail.latest_capability_snapshot.capabilities
            if detail.latest_capability_snapshot
            else None
        )
        commands = session.scalars(
            select(EdgeFleetCommandRecord)
            .where(
                EdgeFleetCommandRecord.edge_device_id == summary.edge_device_id,
                EdgeFleetCommandRecord.command_type.in_(["software_update", "software_rollback"]),
            )
            .order_by(EdgeFleetCommandRecord.created_at.desc())
            .limit(10)
        ).all()
        devices.append(
            {
                "edge_device_id": summary.edge_device_id,
                "runtime_code_digest": caps.runtime_code_digest if caps else None,
                "update_protocol": caps.software_update_protocol if caps else 0,
                "platform": caps.platform if caps else None,
                "runtime_version": caps.runtime_version if caps else None,
                "node_count": len(caps.supported_node_types) if caps else 0,
                "requires_update": bool(caps and caps.runtime_code_digest != runtime_code_digest()),
                "releases": [
                    {
                        "package_sha256": r["package_sha256"],
                        "reasons": compatibility(caps, r["release"]),
                    }
                    for r in releases
                ],
                "commands": [
                    {
                        "command_id": c.id,
                        "status": c.status,
                        "last_error": c.last_error,
                        "action": c.request_payload.get("action"),
                        "package_sha256": c.request_payload.get("package_sha256"),
                        "software": c.response_payload,
                        "created_at": c.created_at.isoformat(),
                    }
                    for c in commands
                ],
            }
        )
    return EdgeSoftwareOverview(
        server_runtime_digest=runtime_code_digest(), releases=releases, devices=devices
    )


def queue_rollout(
    session: Session,
    settings: PlatformSettings,
    device_id: str,
    payload: EdgeSoftwareRolloutRequest,
) -> EdgeFleetCommandSummary:
    # Serialize all software commands for a device, including requests with different keys.
    session.scalar(select(EdgeDevice).where(EdgeDevice.id == device_id).with_for_update())
    device = get_edge_device_detail(session, device_id)
    if device is None or not device.enabled:
        raise ValueError("Enabled Edge device required.")
    existing = session.scalar(
        select(EdgeFleetCommandRecord).where(
            EdgeFleetCommandRecord.edge_device_id == device_id,
            EdgeFleetCommandRecord.idempotency_key == payload.idempotency_key,
        )
    )
    if (
        existing is None
        and session.scalar(
            select(EdgeFleetCommandRecord).where(
                EdgeFleetCommandRecord.edge_device_id == device_id,
                EdgeFleetCommandRecord.command_type.in_(["software_update", "software_rollback"]),
                EdgeFleetCommandRecord.status.in_(["queued", "running"]),
            )
        )
        is not None
    ):
        raise ValueError("An Edge software operation is already running.")
    caps = (
        device.latest_capability_snapshot.capabilities
        if device.latest_capability_snapshot
        else None
    )
    if caps is None or caps.software_update_protocol < 1:
        raise ValueError("Install the runtime launcher once before using central software updates.")
    if payload.action == "update":
        if not payload.package_sha256:
            raise ValueError("Select a signed runtime package.")
        path = catalogue_root(settings) / f"{payload.package_sha256}.json"
        if not path.is_file():
            raise ValueError("Runtime package is not in the verified catalogue.")
        reasons = compatibility(caps, json.loads(path.read_text())["release"])
        if reasons:
            raise ValueError("; ".join(reasons))
    return create_edge_fleet_command(
        session,
        device_id,
        command_type="software_update" if payload.action == "update" else "software_rollback",
        idempotency_key=payload.idempotency_key,
        request_payload=payload.model_dump(mode="json"),
    )


def process_software_command(settings: PlatformSettings) -> bool:
    # One resumable step per controller tick. Row locking also works across controller processes.
    with session_scope(settings) as session:
        command = session.scalar(
            select(EdgeFleetCommandRecord)
            .where(
                EdgeFleetCommandRecord.command_type.in_(["software_update", "software_rollback"]),
                EdgeFleetCommandRecord.status.in_(["queued", "running"]),
            )
            .order_by(EdgeFleetCommandRecord.attempt_count, EdgeFleetCommandRecord.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if command is None:
            return False
        device = get_edge_device_detail(session, command.edge_device_id)
        if device is None or not device.enabled:
            finish_edge_fleet_command(
                session, command.id, succeeded=False, last_error="Device disabled."
            )
            return False
        if command.started_at and utc_now() - command.started_at.replace(
            tzinfo=utc_now().tzinfo
        ) > timedelta(hours=2):
            finish_edge_fleet_command(
                session,
                command.id,
                succeeded=False,
                last_error="Software update exceeded its two-hour deadline.",
            )
            return False
        start_edge_fleet_command(session, command.id)
        payload = EdgeSoftwareRolloutRequest.model_validate(command.request_payload)
        try:
            policy = EdgeNetworkPolicy.from_strings(
                allowed_cidrs=[str(device.allowed_cidr)],
                registered_addresses=[str(device.address.management_ip)],
            )
            with EdgeAgentClient(
                address=device.address, network_policy=policy, transfer_timeout_seconds=300
            ) as client:
                status = client.software_status()
                if status.request_id != command.id:
                    if payload.action == "update":
                        package = catalogue_root(settings) / f"{payload.package_sha256}.zip"
                        command.response_payload = {"phase": "transferring"}
                        transfer = client.upload_blob_file(
                            package,
                            descriptor=EdgeArtifactDescriptor(
                                sha256=payload.package_sha256,
                                size_bytes=package.stat().st_size,
                                kind="runtime_plugin",
                                file_name="runtime-update.zip",
                            ),
                            idempotency_key=command.id,
                            max_chunks=8,
                        )
                        if isinstance(transfer, EdgeBlobUploadStatus) and not transfer.complete:
                            command.response_payload = {
                                "request_id": command.id,
                                "phase": "transferring",
                                "transfer_bytes": transfer.received_bytes,
                                "transfer_total_bytes": transfer.size_bytes,
                            }
                            command.last_error = None
                            session.flush()
                            return False
                    status = client.software_command(
                        EdgeSoftwareCommand(
                            request_id=command.id,
                            action=payload.action,
                            package_sha256=payload.package_sha256,
                        )
                    )
                command.response_payload = status.model_dump(mode="json")
                command.last_error = None
                if status.phase in {"succeeded", "rolled_back", "failed"}:
                    finish_edge_fleet_command(
                        session,
                        command.id,
                        succeeded=status.phase != "failed",
                        response_payload=command.response_payload,
                        last_error=status.last_error,
                    )
        except Exception as exc:
            # Network interruptions and process switches are retried using the same durable key.
            command.last_error = str(exc)[:2000]
        session.flush()
    return False  # Keep servicing channel commands and sleep at the normal controller cadence.
