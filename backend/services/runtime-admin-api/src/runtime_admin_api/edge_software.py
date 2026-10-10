"""Runtime release catalogue and durable orchestration in the existing fleet command store."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

from cv_contracts.edge import (
    EdgeArtifactDescriptor,
    EdgeBlobUploadStatus,
    EdgeCapabilitySnapshot,
    EdgeDeviceIdentity,
    EdgeFleetCommandSummary,
    EdgeSoftwareCommand,
    EdgeSoftwareOverview,
    EdgeSoftwarePackageSummary,
    EdgeSoftwareRolloutRequest,
    EdgeSoftwareStatus,
    EdgeSoftwareTargetManifest,
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

_DEFAULT_TARGET_MANIFEST_PATH = (
    Path(__file__).resolve().parents[5] / "deployment/edge-runtime-targets.json"
)


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
    _write_json(root / f"{digest}.json", descriptor)
    return descriptor


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def target_manifest_path() -> Path:
    # Bound to the loaded application release, so a central rollback restores its targets.
    return Path(
        os.getenv(
            "YBB_EDGE_RUNTIME_TARGET_MANIFEST",
            str(_DEFAULT_TARGET_MANIFEST_PATH),
        )
    )


def read_targets(path: Path) -> EdgeSoftwareTargetManifest:
    targets = EdgeSoftwareTargetManifest.model_validate_json(path.read_bytes())
    if targets.runtime_code_digest != runtime_code_digest():
        raise ValueError("Edge-Zielpakete passen nicht zum Runtime-Code der Zentrale.")
    identities = set()
    for package in targets.packages:
        release = package.release
        if release.runtime_code_digest != targets.runtime_code_digest:
            raise ValueError("Edge-Paket und zentrales Release enthalten unterschiedlichen Code.")
        identity = (
            release.platform,
            release.architecture,
            release.python_version,
            release.cuda_version,
            release.tensorrt_version,
        )
        if identity in identities:
            raise ValueError("Mehrere Edge-Zielpakete für dasselbe Plattformprofil.")
        identities.add(identity)
    return targets


def import_bundle_targets(
    settings: PlatformSettings,
    bundle_directory: Path,
    destination: Path,
    *,
    central_release_id: str,
) -> EdgeSoftwareTargetManifest:
    """Stage verified packages before activation; never consume the offline source bundle."""
    targets = read_targets(bundle_directory / "targets.json")
    if targets.central_release_id != central_release_id:
        raise ValueError("Edge-Zuordnung gehört zu einem anderen zentralen Release.")
    for package in targets.packages:
        # File names are derived only from validated SHA256 values, never arbitrary manifest paths.
        source = bundle_directory / f"{package.package_sha256}.zip"
        if source.stat().st_size != package.size_bytes:
            raise ValueError("Mitgeliefertes Edge-Paket hat eine abweichende Größe.")
        with tempfile.NamedTemporaryFile(dir=catalogue_root(settings), delete=False) as stream:
            staging = Path(stream.name)
        try:
            shutil.copyfile(source, staging)
            with staging.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != package.package_sha256:
                    raise ValueError("Mitgeliefertes Edge-Paket hat eine abweichende Prüfsumme.")
            descriptor = import_package(settings, staging)
            if descriptor != package.model_dump(mode="json"):
                raise ValueError("Signiertes Edge-Paket weicht von der zentralen Zuordnung ab.")
        finally:
            staging.unlink(missing_ok=True)
    # Only publish the complete mapping. The destination belongs to the candidate release.
    _write_json(destination, targets.model_dump(mode="json"))
    return targets


def recommended_package(
    caps: EdgeCapabilitySnapshot | None,
    releases: list[dict],
    targets: EdgeSoftwareTargetManifest | None,
    target_error: str | None,
) -> tuple[str | None, list[str]]:
    if target_error:
        return None, [target_error]
    if caps is None:
        return None, ["Gerätestatus zuerst abrufen."]
    expected = (
        {p.package_sha256: p.model_dump(mode="json") for p in targets.packages} if targets else None
    )
    candidates = [
        r
        for r in releases
        if r["release"]["runtime_code_digest"] == runtime_code_digest()
        and not compatibility(caps, r["release"])
        and (expected is None or expected.get(r["package_sha256"]) == r)
    ]
    if len(candidates) == 1:
        return candidates[0]["package_sha256"], []
    if len(candidates) > 1:
        return None, [
            "Mehrere passende Pakete: eindeutige zentrale Zuordnung "
            "oder manuelle Auswahl erforderlich."
        ]
    return None, [
        "Kein freigegebenes Zielpaket für dieses Geräteprofil vorhanden. "
        "Profil im zentralen Export ergänzen; Python-/CUDA-Basis prüfen."
    ]


def compatibility(capabilities: EdgeCapabilitySnapshot | None, release: dict) -> list[str]:
    if capabilities is None:
        return ["Gerätestatus zuerst abrufen."]
    reasons = []
    if capabilities.software_update_protocol < 1:
        reasons.append("Einmalige Installation des Runtime-Launchers erforderlich.")
    elif release.get("system_packages") and capabilities.software_update_protocol < 2:
        reasons.append(
            "Dieses Paket enthält Ubuntu-Systempakete. Einmal den aktuellen Edge-Offline-Installer "
            "ausführen, um den privilegierten Systempaket-Installer einzurichten."
        )
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


def package_availability(settings: PlatformSettings, descriptor: dict) -> list[str]:
    artifact = catalogue_root(settings) / f"{descriptor['package_sha256']}.zip"
    if not artifact.is_file() or artifact.stat().st_size != descriptor["size_bytes"]:
        return [
            "Die Paketdatei fehlt oder ist unvollständig. Dieses Runtime-Paket erneut importieren."
        ]
    return []


def software_overview(session: Session, settings: PlatformSettings) -> EdgeSoftwareOverview:
    releases = [
        EdgeSoftwarePackageSummary.model_validate_json(p.read_bytes()).model_dump(mode="json")
        for p in sorted(catalogue_root(settings).glob("*.json"))
    ]
    unavailable = {r["package_sha256"]: package_availability(settings, r) for r in releases}
    path = target_manifest_path()
    targets = None
    target_error = None
    managed = path.exists() or bool(os.getenv("YBB_EDGE_RUNTIME_TARGET_MANIFEST"))
    if managed:
        try:
            targets = read_targets(path)
            for package in targets.packages:
                descriptor = package.model_dump(mode="json")
                if package_availability(settings, descriptor) or descriptor not in releases:
                    raise ValueError(
                        f"Mitgeliefertes Edge-Paket {package.release.release_id} "
                        f"({package.package_sha256[:12]}) fehlt im Katalog oder ist unvollständig; "
                        "dieses Paket erneut importieren oder das zentrale Update erneut anwenden."
                    )
        except (ValueError, OSError) as exc:
            target_error = str(exc)
    devices = []
    for summary in list_edge_devices(session):
        detail = get_edge_device_detail(session, summary.edge_device_id)
        caps = (
            detail.latest_capability_snapshot.capabilities
            if detail.latest_capability_snapshot
            else None
        )
        observation = software_observation(session, summary.edge_device_id, detail.identity)
        software = (
            EdgeSoftwareStatus.model_validate(observation["software"])
            if observation.get("software")
            else None
        )
        caps = launcher_capabilities(caps, software)
        recommended, recommendation_reasons = recommended_package(
            caps,
            [r for r in releases if not unavailable[r["package_sha256"]]],
            targets,
            target_error,
        )
        active = (
            software.active_release if software and software.active_release != "bootstrap" else None
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
                "requires_update": bool(
                    caps
                    and (
                        caps.runtime_code_digest != runtime_code_digest()
                        or (recommended is not None and active != recommended)
                    )
                ),
                "active_package_sha256": active,
                "recommended_package_sha256": recommended,
                "recommendation_reasons": recommendation_reasons,
                "software_observed_at": observation.get("software_observed_at")
                if software
                else None,
                "releases": [
                    {
                        "package_sha256": r["package_sha256"],
                        "reasons": compatibility(caps, r["release"])
                        + unavailable[r["package_sha256"]],
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
        server_runtime_digest=runtime_code_digest(),
        releases=releases,
        devices=devices,
        central_release_id=targets.central_release_id if targets else None,
        targets_managed=managed,
        target_error=target_error,
    )


def software_observation(session: Session, device_id: str, identity: EdgeDeviceIdentity | None) -> dict:
    probe = session.scalar(
        select(EdgeFleetCommandRecord)
        .where(
            EdgeFleetCommandRecord.edge_device_id == device_id,
            EdgeFleetCommandRecord.command_type == "reconcile",
            EdgeFleetCommandRecord.status == "succeeded",
        )
        .order_by(EdgeFleetCommandRecord.created_at.desc())
        .limit(1)
    )
    observation = probe.response_payload if probe else {}
    return observation if identity and observation.get("boot_id") == identity.boot_id else {}


def launcher_capabilities(
    caps: EdgeCapabilitySnapshot | None, software: EdgeSoftwareStatus | None
) -> EdgeCapabilitySnapshot | None:
    # A full installer updates the launcher even if a previously activated runtime
    # slot still reports the older capability format. Use the same-boot observation.
    if caps and software:
        return caps.model_copy(
            update={
                "software_update_protocol": max(
                    caps.software_update_protocol, software.protocol_version
                )
            }
        )
    return caps


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
    observation = software_observation(session, device_id, device.identity)
    caps = launcher_capabilities(
        caps,
        EdgeSoftwareStatus.model_validate(observation["software"])
        if observation.get("software")
        else None,
    )
    if caps is None or caps.software_update_protocol < 1:
        raise ValueError("Install the runtime launcher once before using central software updates.")
    if payload.action == "update":
        if not payload.package_sha256:
            raise ValueError("Select a signed runtime package.")
        path = catalogue_root(settings) / f"{payload.package_sha256}.json"
        if not path.is_file():
            raise ValueError("Runtime package is not in the verified catalogue.")
        descriptor = json.loads(path.read_text())
        reasons = compatibility(caps, descriptor["release"]) + package_availability(
            settings, descriptor
        )
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


def main() -> None:
    import argparse

    from cv_core import load_platform_settings

    parser = argparse.ArgumentParser(
        description="Import bundled Edge runtime packages without network access."
    )
    parser.add_argument("--bundle-directory", required=True, type=Path)
    parser.add_argument("--release-root", required=True, type=Path)
    parser.add_argument("--central-release-id", required=True)
    arguments = parser.parse_args()
    settings = load_platform_settings("runtime-admin-api", root_dir=arguments.release_root)
    if settings.env != "prod":
        parser.error("Vor dem zentralen Edge-Import die Produktionsumgebung aus runtime.env laden.")
    imported = import_bundle_targets(
        settings,
        arguments.bundle_directory,
        arguments.release_root / "deployment/edge-runtime-targets.json",
        central_release_id=arguments.central_release_id,
    )
    print(
        f"Edge-Pakete für {imported.central_release_id}: "
        f"{len(imported.packages)} geprüft und importiert."
    )


if __name__ == "__main__":
    main()
