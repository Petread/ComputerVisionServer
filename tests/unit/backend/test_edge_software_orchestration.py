from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cv_contracts.edge import (
    EdgeArtifactDescriptor,
    EdgeBlobUploadStatus,
    EdgeCapabilitySnapshot,
    EdgeSoftwareCommand,
    EdgeSoftwareRelease,
    EdgeSoftwareRolloutRequest,
    EdgeSoftwareStatus,
)
from cv_db import get_edge_device_detail, session_scope
from cv_db.models import EdgeCapabilitySnapshotRecord, EdgeFleetCommandRecord
from cv_runtime.distribution import runtime_code_digest
from runtime_admin_api import edge_software
from sqlalchemy import select
from sqlalchemy.engine import Engine
from tests.conftest import build_test_settings, configure_sqlite_factories
from tests.unit.backend.test_edge_m3_deployment import _seed_edge_channel


@pytest.mark.parametrize("platform", ["jetson_orin_nano", "jetson_agx_orin"])
def test_runtime_update_compatibility_accepts_probed_patch_versions(platform: str) -> None:
    caps = EdgeCapabilitySnapshot(
        management_ip="172.23.27.245",
        platform=platform,
        architecture="aarch64",
        os_version="Linux",
        ram_total_bytes=8 * 1024**3,
        storage_total_bytes=128 * 1024**3,
        storage_free_bytes=64 * 1024**3,
        agent_version="0.8.0",
        runtime_version="0.1.0",
        observed_at="2026-09-11T13:16:15Z",
        software_update_protocol=1,
        python_version="3.12.3",
        cuda_version="13.2.2",
        tensorrt_version="10.16.2.10",
    )
    release = dict(
        platform=platform,
        architecture="aarch64",
        python_version="3.12",
        cuda_version="13.2",
        tensorrt_version="10.16",
    )
    assert edge_software.compatibility(caps, release) == []
    for field, value in (
        ("platform", "windows_11"),
        ("architecture", "x86_64"),
        ("cuda_version", "13.3.0"),
        ("tensorrt_version", "10.17.0"),
        ("cuda_version", None),
        ("tensorrt_version", None),
    ):
        assert any(
            field in reason
            for reason in edge_software.compatibility(
                caps.model_copy(update={field: value}), release
            )
        )
    assert edge_software.compatibility(caps, {**release, "cuda_version": "13.2.1"})
    assert edge_software.compatibility(caps, {**release, "tensorrt_version": "10.16.2.9"})
    assert edge_software.compatibility(
        caps.model_copy(update={"python_version": "3.11.9"}), release
    )
    assert edge_software.compatibility(
        caps.model_copy(update={"software_update_protocol": 0}), release
    )
    assert edge_software.compatibility(None, release)


def test_software_rollout_is_durable_idempotent_and_serialized_per_device(
    sqlite_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)
    with session_scope(settings) as session:
        _, device_id = _seed_edge_channel(session)
        snapshot = session.scalar(
            select(EdgeCapabilitySnapshotRecord).where(
                EdgeCapabilitySnapshotRecord.edge_device_id == device_id
            )
        )
        snapshot.payload = {
            **snapshot.payload,
            "software_update_protocol": 1,
            "python_version": "3.12.0",
            "cuda_version": "13.2.2",
            "tensorrt_version": "10.16.2.10",
        }
        session.flush()
        device = get_edge_device_detail(session, device_id)
        caps = device.latest_capability_snapshot.capabilities
        release = EdgeSoftwareRelease(
            release_id="software-test",
            platform=caps.platform,
            architecture=caps.architecture,
            python_version="3.12",
            cuda_version="13.2",
            tensorrt_version="10.16",
            runtime_code_digest=runtime_code_digest(),
            wheels={"example.whl": "a" * 64},
        )
        root = edge_software.catalogue_root(settings)
        (root / ("a" * 64 + ".json")).write_text(
            json.dumps(
                {
                    "package_sha256": "a" * 64,
                    "size_bytes": 7,
                    "release": release.model_dump(mode="json"),
                }
            )
        )
        (root / ("a" * 64 + ".zip")).write_bytes(b"package")
        payload = EdgeSoftwareRolloutRequest(
            package_sha256="a" * 64, idempotency_key="software-001"
        )
        command = edge_software.queue_rollout(session, settings, device_id, payload)
        assert (
            edge_software.queue_rollout(session, settings, device_id, payload).command_id
            == command.command_id
        )
        with pytest.raises(ValueError, match="already running"):
            edge_software.queue_rollout(
                session,
                settings,
                device_id,
                payload.model_copy(update={"idempotency_key": "software-002"}),
            )
    calls = []

    class Client:
        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def software_status(self) -> EdgeSoftwareStatus:
            if not calls:
                return EdgeSoftwareStatus(protocol_version=1)
            return EdgeSoftwareStatus(
                protocol_version=1,
                request_id=command.command_id,
                phase="succeeded",
                active_release="a" * 64,
                previous_release="bootstrap",
            )

        def upload_blob_file(self, path: Path, **kwargs: object) -> object:
            assert path.read_bytes() == b"package"
            return kwargs["descriptor"]

        def software_command(self, request: EdgeSoftwareCommand) -> EdgeSoftwareStatus:
            calls.append(request.request_id)
            return EdgeSoftwareStatus(
                protocol_version=1, request_id=request.request_id, phase="queued"
            )

    monkeypatch.setattr(edge_software, "EdgeAgentClient", Client)
    edge_software.process_software_command(settings)
    edge_software.process_software_command(settings)
    with session_scope(settings) as session:
        stored = session.get(EdgeFleetCommandRecord, command.command_id)
        assert stored.status == "succeeded"
        overview = edge_software.software_overview(session, settings)
        assert overview.devices[0].releases[0].reasons == []
        assert overview.devices[0].commands[0].software.previous_release == "bootstrap"
    assert calls == [command.command_id]


def test_package_transfer_yields_and_resumes_before_queueing_software(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient
    from tests.integration.test_edge_agent_m0 import _client, _edge_app, _transport

    monkeypatch.setenv("YBB_EDGE_SOFTWARE_LAUNCHER", "1")
    content = b"package-transfer-fixture"
    path = tmp_path / "package.zip"
    path.write_bytes(content)
    descriptor = EdgeArtifactDescriptor(
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        file_name=path.name,
        kind="runtime_plugin",
    )
    with TestClient(_edge_app(tmp_path)) as http, _client(_transport(http)) as client:
        partial = client.upload_blob_file(
            path, descriptor=descriptor, idempotency_key="transfer-001", chunk_size=4, max_chunks=2
        )
        assert isinstance(partial, EdgeBlobUploadStatus)
        assert partial.received_bytes == 8
        assert not partial.complete
        complete = client.upload_blob_file(
            path, descriptor=descriptor, idempotency_key="transfer-001", chunk_size=4
        )
        assert complete == descriptor
        command = EdgeSoftwareCommand(request_id="software-001", package_sha256=descriptor.sha256)
        assert client.software_command(command).phase == "queued"
        assert client.software_command(command).request_id == "software-001"
        assert http.app.state.edge_agent.capabilities().software_update_protocol == 1
