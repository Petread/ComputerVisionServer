from __future__ import annotations

import json
import socket
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest
from cv_contracts.edge import (
    EdgeCapabilitySnapshot,
    EdgeDeviceAddress,
    EdgeDeviceHealth,
    EdgeDeviceIdentity,
    EdgeDeviceRegistrationRequest,
    EdgeSoftwareRolloutRequest,
    EdgeSoftwareStatus,
    EdgeSoftwareTargetManifest,
)
from cv_db import (
    create_edge_device,
    create_edge_fleet_command,
    finish_edge_fleet_command,
    record_edge_observation,
    session_scope,
)
from cv_db.models import EdgeDevice
from cv_runtime.distribution import runtime_code_digest
from runtime_admin_api import edge_reconciliation, edge_software
from scripts.release import build_central_edge_updates as central
from scripts.release import build_edge_runtime_update as exporter
from sqlalchemy.engine import Engine
from tests.conftest import build_test_settings, configure_sqlite_factories

ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.usefixtures("edge_system_package_cache")


def _wheel(directory: Path, name: str, *, source: Path | None = None, requires: str = "") -> Path:
    path = directory / f"{name}-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{name}-1.0.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n{requires}",
        )
        if source:
            for item in source.rglob("*.py"):
                archive.write(item, name + "/" + item.relative_to(source).as_posix())
    return path


@pytest.fixture()
def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, Path, Path]:
    bundle = tmp_path / "base"
    wheels = bundle / "wheelhouse"
    wheels.mkdir(parents=True)
    app_archive = tmp_path / "app.tar.gz"
    with tarfile.open(app_archive, "w:gz") as archive:
        for name in central.INTERNAL:
            project = (
                "services/edge-agent"
                if name == "edge_agent"
                else f"packages/{name.replace('_', '-')}"
            )
            relative = Path(f"backend/{project}/src/{name}")
            _wheel(wheels, name, source=ROOT / relative)
            archive.add(ROOT / relative, arcname=str(relative).replace("\\", "/"))
    _wheel(wheels, "pypylon")
    _wheel(wheels, "uvloop")
    profile = {
        "platform": "jetson_orin_nano",
        "architecture": "aarch64",
        "python_tag": "312",
        "python_abi": "cp312",
        "pip_platforms": ["manylinux2014_aarch64"],
        "cuda_version": "13.2",
        "tensorrt_version": "10.16",
        "required_distributions": list(central.INTERNAL),
    }
    (bundle / "platform-profile.json").write_text(json.dumps(profile))
    key = tmp_path / "private.pem"
    public = tmp_path / "public.pem"
    openssl = exporter._resolve_openssl("openssl")
    subprocess.run(
        [
            openssl,
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(key),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [openssl, "pkey", "-in", str(key), "-pubout", "-out", str(public)],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("YBB_EDGE_TRUSTED_RELEASE_KEY", str(public))
    monkeypatch.setenv("YBB_EDGE_OPENSSL_EXECUTABLE", openssl)
    monkeypatch.setattr(central.builder, "_refresh_internal_wheels", lambda *args: None)
    monkeypatch.setattr(
        socket, "create_connection", lambda *args, **kwargs: pytest.fail("Network used")
    )
    return (
        {"bundles": [str(bundle)], "signing_key": str(key), "openssl": openssl},
        app_archive,
        bundle,
    )


def test_build_import_repeat_and_release_rollback_without_network(
    prepared: tuple[dict, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, app_archive, _ = prepared
    package_dir = tmp_path / "offline/edge-runtime"
    manifest = central.build_updates(config, package_dir, "central-001", app_archive)
    assert manifest["runtime_code_digest"] == runtime_code_digest()
    assert (
        "analysis.bark_radial_thickness"
        in manifest["packages"][0]["release"]["node_contract_versions"]
    )
    package = package_dir / (manifest["packages"][0]["package_sha256"] + ".zip")
    original = package.read_bytes()
    settings = build_test_settings("runtime-admin-api", tmp_path)
    destination = tmp_path / "release-one/deployment/edge-runtime-targets.json"
    original_replace = edge_software.os.replace

    def same_filesystem_replace(source: Path, target: Path) -> None:
        assert Path(source).parent == Path(target).parent
        original_replace(source, target)

    monkeypatch.setattr(edge_software.os, "replace", same_filesystem_replace)
    for _ in range(2):
        edge_software.import_bundle_targets(
            settings, package_dir, destination, central_release_id="central-001"
        )
        assert package.read_bytes() == original
    second_dir = tmp_path / "offline-two/edge-runtime"
    second = central.build_updates(config, second_dir, "central-002", app_archive)
    assert (
        second["packages"] == manifest["packages"]
    )  # Central-only changes reuse the Edge artifact.
    destination_two = tmp_path / "release-two/deployment/edge-runtime-targets.json"
    edge_software.import_bundle_targets(
        settings, second_dir, destination_two, central_release_id="central-002"
    )
    assert edge_software.read_targets(destination).central_release_id == "central-001"
    assert edge_software.read_targets(destination_two).central_release_id == "central-002"


@pytest.mark.parametrize("failure", ["checksum", "signature", "release", "missing", "mapping"])
def test_invalid_bundle_does_not_publish_targets(
    prepared: tuple[dict, Path, Path], tmp_path: Path, failure: str
) -> None:
    config, app_archive, _ = prepared
    directory = tmp_path / "edge-runtime"
    manifest = central.build_updates(config, directory, "central-001", app_archive)
    package = directory / (manifest["packages"][0]["package_sha256"] + ".zip")
    if failure == "checksum":
        raw = bytearray(package.read_bytes())
        raw[-1] ^= 1
        package.write_bytes(raw)
    elif failure == "signature":
        import hashlib

        with zipfile.ZipFile(package) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        entries["release.sig"] = b"invalid"
        with zipfile.ZipFile(package, "w") as archive:
            for name, raw in entries.items():
                archive.writestr(name, raw)
        digest = hashlib.sha256(package.read_bytes()).hexdigest()
        manifest["packages"][0].update(package_sha256=digest, size_bytes=package.stat().st_size)
        package.rename(directory / f"{digest}.zip")
    elif failure == "release":
        manifest["central_release_id"] = "other-release"
    elif failure == "mapping":
        manifest["packages"][0]["release"]["release_id"] = "wrong-signed-release"
    else:
        package.unlink()
    (directory / "targets.json").write_text(json.dumps(manifest))
    settings = build_test_settings("runtime-admin-api", tmp_path)
    destination = tmp_path / "candidate/deployment/edge-runtime-targets.json"
    with pytest.raises((ValueError, OSError)):
        edge_software.import_bundle_targets(
            settings, directory, destination, central_release_id="central-001"
        )
    assert not destination.exists()


def test_build_rejects_code_changed_after_central_archive(
    prepared: tuple[dict, Path, Path], tmp_path: Path
) -> None:
    config, app_archive, bundle = prepared
    with zipfile.ZipFile(bundle / "wheelhouse/edge_agent-1.0-py3-none-any.whl", "a") as archive:
        archive.writestr("edge_agent/changed_after_archive.py", "changed = True\n")
    with pytest.raises(ValueError, match="changed after central app archive"):
        central.build_updates(config, tmp_path / "output", "central-001", app_archive)
    assert not (tmp_path / "output/targets.json").exists()


def test_target_dependency_check_uses_linux_markers_and_rejects_missing_dependencies(
    tmp_path: Path,
) -> None:
    profile = {
        "platform": "jetson_orin_nano",
        "python_tag": "312",
        "pip_platforms": ["manylinux2014_aarch64"],
    }
    _wheel(tmp_path, "test_package", requires='Requires-Dist: uvloop; sys_platform == "linux"\n')
    with pytest.raises(ValueError, match="uvloop"):
        central.validate_target_wheels(tmp_path, profile)
    _wheel(tmp_path, "uvloop")
    central.validate_target_wheels(tmp_path, profile)


def _caps() -> EdgeCapabilitySnapshot:
    return EdgeCapabilitySnapshot(
        management_ip="192.168.50.21",
        platform="jetson_orin_nano",
        architecture="aarch64",
        os_version="Linux",
        ram_total_bytes=8 * 1024**3,
        storage_total_bytes=128 * 1024**3,
        storage_free_bytes=100 * 1024**3,
        agent_version="0.8.0",
        runtime_version="0.1.0",
        observed_at="2026-09-21T12:00:00Z",
        software_update_protocol=2,
        python_version="3.12.3",
        cuda_version="13.2.2",
        tensorrt_version="10.16.2.10",
        runtime_code_digest=runtime_code_digest(),
    )


def test_target_wheel_rejects_incompatible_python_abi(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path, "test_package")
    wheel.rename(tmp_path / "test_package-1.0-cp312-cp312t-manylinux2014_aarch64.whl")
    with pytest.raises(ValueError, match="does not match target Python/platform"):
        central.validate_target_wheels(
            tmp_path,
            {
                "platform": "jetson_orin_nano",
                "python_tag": "312",
                "python_abi": "cp312",
                "pip_platforms": ["manylinux2014_aarch64"],
            },
        )


def test_agent_only_update_uses_observed_package_and_exact_release_target(
    prepared: tuple[dict, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sqlite_engine: Engine,
) -> None:
    config, app_archive, _ = prepared
    directory = tmp_path / "packages"
    manifest = central.build_updates(config, directory, "central-001", app_archive)
    settings = build_test_settings("runtime-admin-api", tmp_path)
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    targets_path = tmp_path / "release/deployment/edge-runtime-targets.json"
    edge_software.import_bundle_targets(
        settings, directory, targets_path, central_release_id="central-001"
    )
    monkeypatch.setenv("YBB_EDGE_RUNTIME_TARGET_MANIFEST", str(targets_path))
    target = manifest["packages"][0]["package_sha256"]
    with session_scope(settings) as session:
        device = create_edge_device(
            session,
            EdgeDeviceRegistrationRequest(
                name="Edge under test",
                address=EdgeDeviceAddress(management_ip="192.168.50.21"),
                allowed_cidr="192.168.50.0/24",
                server_ip="192.168.50.10",
            ),
            created_by=None,
        )
        identity = EdgeDeviceIdentity(
            address=device.address,
            hostname="edge",
            platform="jetson_orin_nano",
            architecture="aarch64",
            agent_version="0.8.0",
            runtime_version="0.1.0",
            boot_id="boot-1",
        )
        health = EdgeDeviceHealth(
            state="ok",
            detail="Ready",
            boot_id="boot-1",
            observed_at="2026-09-21T12:00:00Z",
        )
        # The full installer can update the launcher while an older runtime slot remains active.
        older_caps = _caps().model_copy(update={"software_update_protocol": 1})
        record_edge_observation(
            session, device.edge_device_id, identity=identity, health=health, capabilities=older_caps
        )
        blocked = edge_software.software_overview(session, settings).devices[0]
        assert blocked.recommended_package_sha256 is None
        assert "Offline-Installer" in ";".join(blocked.releases[0].reasons)

        def observe_software(active: str, request_id: str) -> None:
            command = create_edge_fleet_command(
                session,
                device.edge_device_id,
                command_type="reconcile",
                idempotency_key=request_id,
                request_payload={},
            )
            result = edge_reconciliation.EdgeProbeResult(
                identity, health, older_caps, EdgeSoftwareStatus(active_release=active, protocol_version=2)
            )
            finish_edge_fleet_command(
                session,
                command.command_id,
                succeeded=True,
                response_payload={"boot_id": identity.boot_id, **result.software_observation()},
            )

        observe_software("a" * 64, "probe-before")
        overview = edge_software.software_overview(session, settings)
        assert overview.targets_managed
        assert overview.devices[0].requires_update
        assert overview.devices[0].recommended_package_sha256 == target
        assert overview.devices[0].active_package_sha256 == "a" * 64
        queued = edge_software.queue_rollout(
            session, settings, device.edge_device_id,
            EdgeSoftwareRolloutRequest(package_sha256=target, idempotency_key="new-launcher-rollout"),
        )
        assert queued.status == "queued"
        observe_software(target, "probe-after")
        assert not edge_software.software_overview(session, settings).devices[0].requires_update
        # Rollback to an older central contract must still be able to read health JSON.
        assert "software" not in session.get(EdgeDevice, device.edge_device_id).health_payload
        record_edge_observation(
            session,
            device.edge_device_id,
            identity=identity.model_copy(update={"boot_id": "boot-2"}),
            health=health.model_copy(update={"boot_id": "boot-2"}),
            capabilities=older_caps,
        )
        assert (
            edge_software.software_overview(session, settings).devices[0].active_package_sha256
            is None
        )
        assert edge_software.software_overview(session, settings).devices[0].update_protocol == 1
        targets_path.write_text("broken manifest")
        overview = edge_software.software_overview(session, settings)
        assert overview.target_error
        assert overview.devices[0].recommended_package_sha256 is None


def test_ambiguous_legacy_packages_are_not_automatically_selected(
    prepared: tuple[dict, Path, Path], tmp_path: Path
) -> None:
    config, app_archive, _ = prepared
    manifest = central.build_updates(config, tmp_path / "packages", "central-001", app_archive)
    current = manifest["packages"][0]
    older_agent = {**current, "package_sha256": "0" * 64}
    assert edge_software.recommended_package(_caps(), [older_agent, current], None, None)[0] is None
    targets = EdgeSoftwareTargetManifest.model_validate(manifest)
    assert (
        edge_software.recommended_package(_caps(), [older_agent, current], targets, None)[0]
        == current["package_sha256"]
    )


def test_missing_bundled_artifact_allows_valid_manual_rollout_but_blocks_missing_file(
    prepared: tuple[dict, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sqlite_engine: Engine,
) -> None:
    config, app_archive, bundle = prepared
    directory = tmp_path / "packages"
    manifest = central.build_updates(config, directory, "central-001", app_archive)
    settings = build_test_settings("runtime-admin-api", tmp_path)
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    targets_path = tmp_path / "release/deployment/edge-runtime-targets.json"
    edge_software.import_bundle_targets(
        settings, directory, targets_path, central_release_id="central-001"
    )
    monkeypatch.setenv("YBB_EDGE_RUNTIME_TARGET_MANIFEST", str(targets_path))
    missing = manifest["packages"][0]["package_sha256"]
    (edge_software.catalogue_root(settings) / f"{missing}.zip").unlink()
    manual = tmp_path / "manual.zip"
    exporter.export_runtime_update(
        bundle,
        manual,
        release_id="edge-runtime-manual",
        signing_key=Path(config["signing_key"]),
        openssl_executable=config["openssl"],
    )
    imported = edge_software.import_package(settings, manual)
    with session_scope(settings) as session:
        device = create_edge_device(
            session,
            EdgeDeviceRegistrationRequest(
                name="Jetson manual update",
                address=EdgeDeviceAddress(management_ip="192.168.50.21"),
                allowed_cidr="192.168.50.0/24",
                server_ip="192.168.50.10",
            ),
            created_by=None,
        )
        record_edge_observation(
            session,
            device.edge_device_id,
            identity=EdgeDeviceIdentity(
                address=device.address,
                hostname="edge",
                platform="jetson_orin_nano",
                architecture="aarch64",
                agent_version="0.8.0",
                runtime_version="0.1.0",
                boot_id="boot-manual",
            ),
            health=EdgeDeviceHealth(
                state="ok", detail="Ready", boot_id="boot-manual", observed_at=_caps().observed_at
            ),
            capabilities=_caps(),
        )
        overview = edge_software.software_overview(session, settings)
        assert missing[:12] in overview.target_error
        assert overview.devices[0].recommended_package_sha256 is None
        reasons = {item.package_sha256: item.reasons for item in overview.devices[0].releases}
        assert reasons[imported["package_sha256"]] == []
        assert reasons[missing]
        with pytest.raises(ValueError, match="Paketdatei fehlt"):
            edge_software.queue_rollout(
                session,
                settings,
                device.edge_device_id,
                EdgeSoftwareRolloutRequest(package_sha256=missing, idempotency_key="missing-001"),
            )
        command = edge_software.queue_rollout(
            session,
            settings,
            device.edge_device_id,
            EdgeSoftwareRolloutRequest(
                package_sha256=imported["package_sha256"], idempotency_key="manual-001"
            ),
        )
        assert command.status == "queued"
        assert edge_software.read_targets(targets_path).packages[0].package_sha256 == missing


def test_probe_reads_existing_software_endpoint_for_older_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    caps = _caps()
    health = EdgeDeviceHealth(
        state="ok", detail="Ready", boot_id="boot-1", observed_at=caps.observed_at
    )
    software = EdgeSoftwareStatus(active_release="d" * 64)

    class Client:
        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def identity(self) -> str:
            return "identity"

        def health(self) -> EdgeDeviceHealth:
            return health

        def capabilities(self) -> EdgeCapabilitySnapshot:
            return caps

        def software_status(self) -> EdgeSoftwareStatus:
            return software

    monkeypatch.setattr(edge_reconciliation, "EdgeAgentClient", Client)
    device = SimpleNamespace(
        allowed_cidr="192.168.50.0/24", address=EdgeDeviceAddress(management_ip=caps.management_ip)
    )
    result = edge_reconciliation.probe_edge_device(device)
    assert result.software == software
    assert "software" not in result.health.model_dump()
def test_legacy_catalogue_without_system_packages_field_remains_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sqlite_engine: Engine
) -> None:
    from cv_contracts.edge import EdgeSoftwarePackageSummary, EdgeSoftwareRelease

    settings = build_test_settings("runtime-admin-api", tmp_path)
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    release = EdgeSoftwareRelease(
        release_id="legacy-package", platform="jetson_orin_nano", architecture="aarch64",
        python_version="3.12", runtime_code_digest=runtime_code_digest(),
        wheels={"edge_agent.whl": "b" * 64},
    )
    descriptor = EdgeSoftwarePackageSummary(
        package_sha256="a" * 64, size_bytes=1, release=release
    ).model_dump(mode="json")
    del descriptor["release"]["system_packages"]
    catalogue = edge_software.catalogue_root(settings)
    (catalogue / ("a" * 64 + ".json")).write_text(json.dumps(descriptor))
    (catalogue / ("a" * 64 + ".zip")).write_bytes(b"x")
    targets = EdgeSoftwareTargetManifest(
        central_release_id="legacy-central", runtime_code_digest=runtime_code_digest(),
        packages=[descriptor],
    )
    target_path = tmp_path / "targets.json"
    target_path.write_text(targets.model_dump_json())
    monkeypatch.setenv("YBB_EDGE_RUNTIME_TARGET_MANIFEST", str(target_path))
    with session_scope(settings) as session:
        overview = edge_software.software_overview(session, settings)
        assert overview.target_error is None
        assert len(overview.releases) == 1
