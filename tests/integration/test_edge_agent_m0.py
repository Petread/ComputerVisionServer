from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from cv_contracts import EdgeDeploymentManifest, EdgeDeviceAddress
from cv_edge import (
    EdgeAgentClient,
    EdgeClientError,
    EdgeNetworkPolicy,
    EdgeResultReplayTracker,
)
from edge_agent.app import create_app
from edge_agent.state import EdgeAgentConfig, EdgeAgentState
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _edge_app(tmp_path: Path) -> FastAPI:
    return create_app(
        EdgeAgentConfig(
            address=EdgeDeviceAddress(management_ip="192.168.50.21"),
            data_root=tmp_path / "edge-state",
        )
    )


def _network_policy() -> EdgeNetworkPolicy:
    return EdgeNetworkPolicy.from_strings(
        allowed_cidrs=["192.168.50.0/24"],
        registered_addresses=["192.168.50.21"],
    )


def _transport(
    test_client: TestClient,
    before_request: Callable[[httpx.Request], None] | None = None,
    after_response: Callable[[httpx.Request], None] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if before_request is not None:
            before_request(request)
        response = test_client.request(
            request.method,
            request.url.path,
            params=dict(request.url.params),
            headers=dict(request.headers),
            content=request.content,
        )
        if after_response is not None:
            after_response(request)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=response.content,
            request=request,
        )

    return httpx.MockTransport(handler)


def _client(transport: httpx.BaseTransport) -> EdgeAgentClient:
    return EdgeAgentClient(
        address=EdgeDeviceAddress(management_ip="192.168.50.21"),
        network_policy=_network_policy(),
        transport=transport,
    )


def test_identity_health_and_capability_roundtrip(tmp_path: Path) -> None:
    app = _edge_app(tmp_path)
    with TestClient(app) as edge_http, _client(_transport(edge_http)) as client:
        identity = client.identity()
        health = client.health()
        capabilities = client.capabilities()
        runtime = client.runtime_status()

    assert str(identity.address.management_ip) == "192.168.50.21"
    assert identity.platform == "jetson_orin_nano"
    assert health.state == "ok"
    assert capabilities.jetpack_version == "7.2"
    assert "inference.detection" in capabilities.supported_node_types
    assert runtime.database_journal_mode == "wal"
    assert [slot.name for slot in runtime.slots] == ["A", "B"]


def test_validation_errors_use_the_versioned_edge_error_contract(tmp_path: Path) -> None:
    app = _edge_app(tmp_path)

    with TestClient(app) as edge_http:
        invalid_digest = edge_http.put(
            "/edge/v1/blobs/not-a-sha256",
            content=b"",
            headers={"X-Upload-Offset": "0", "X-Total-Size": "0"},
        )
        invalid_limit = edge_http.get("/edge/v1/results", params={"limit": 0})

    assert invalid_digest.status_code == 422
    assert invalid_digest.json()["schema_version"] == "edge.control.error.v1"
    assert invalid_digest.json()["code"] == "request_validation_error"
    assert invalid_limit.status_code == 422
    assert invalid_limit.json()["code"] == "request_validation_error"


def test_simulated_degraded_disk_full_and_offline_faults(tmp_path: Path) -> None:
    app = _edge_app(tmp_path)
    state: EdgeAgentState = app.state.edge_agent

    with TestClient(app) as edge_http:
        state.faults.degraded = True
        degraded = edge_http.get("/edge/v1/health")

        state.faults.degraded = False
        state.faults.disk_full = True
        disk_full = edge_http.put(
            f"/edge/v1/blobs/{hashlib.sha256(b'x').hexdigest()}",
            content=b"x",
            headers={
                "X-Upload-Offset": "0",
                "X-Total-Size": "1",
                "X-Idempotency-Key": "fault-upload-1",
            },
        )

        state.faults.disk_full = False
        state.faults.offline = True
        offline = edge_http.get("/edge/v1/health")

    assert degraded.json()["state"] == "degraded"
    assert disk_full.status_code == 507
    assert disk_full.json()["code"] == "disk_full"
    assert disk_full.json()["retryable"] is True
    assert offline.status_code == 503
    assert offline.json()["code"] == "simulated_offline"


def test_blob_transfer_resumes_after_connection_loss(tmp_path: Path) -> None:
    app = _edge_app(tmp_path)
    put_count = 0
    fail_next_second_put = True

    def interrupt_second_put(request: httpx.Request) -> None:
        nonlocal put_count, fail_next_second_put
        if request.method == "PUT":
            put_count += 1
            if fail_next_second_put and put_count == 2:
                fail_next_second_put = False
                raise httpx.ConnectError("simulated LAN interruption", request=request)

    data = b"0123456789abcdef"
    with (
        TestClient(app) as edge_http,
        _client(_transport(edge_http, interrupt_second_put)) as client,
    ):
        with pytest.raises(EdgeClientError) as exc_info:
            client.upload_blob(
                data,
                kind="onnx_model",
                file_name="model.onnx",
                idempotency_key="upload-model-1",
                chunk_size=4,
            )
        assert exc_info.value.retryable is True

        artifact = client.upload_blob(
            data,
            kind="onnx_model",
            file_name="model.onnx",
            idempotency_key="upload-model-1",
            chunk_size=4,
        )
        already_present = client.upload_blob(
            data,
            kind="onnx_model",
            file_name="model.onnx",
            idempotency_key="upload-model-1",
            chunk_size=4,
        )

    assert artifact.sha256 == hashlib.sha256(data).hexdigest()
    assert already_present == artifact
    assert put_count == 5
    final_path = app.state.edge_agent.blob_root / artifact.sha256[:2] / artifact.sha256
    assert final_path.read_bytes() == data


def test_hash_mismatch_discards_partial_artifact(tmp_path: Path) -> None:
    app = _edge_app(tmp_path)
    expected_digest = hashlib.sha256(b"expected").hexdigest()

    with TestClient(app) as edge_http:
        response = edge_http.put(
            f"/edge/v1/blobs/{expected_digest}",
            content=b"different",
            headers={
                "X-Upload-Offset": "0",
                "X-Total-Size": str(len(b"different")),
                "X-Idempotency-Key": "upload-corrupt-1",
            },
        )
        status = edge_http.head(f"/edge/v1/blobs/{expected_digest}")

    assert response.status_code == 422
    assert response.json()["code"] == "blob_hash_mismatch"
    assert status.headers["x-blob-offset"] == "0"
    assert status.headers["x-blob-complete"] == "false"


def test_blob_transfer_rejects_wrong_offset_and_size(tmp_path: Path) -> None:
    app = _edge_app(tmp_path)
    payload = b"abcdef"
    digest = hashlib.sha256(payload).hexdigest()

    with TestClient(app) as edge_http:
        partial = edge_http.put(
            f"/edge/v1/blobs/{digest}",
            content=payload[:3],
            headers={
                "X-Upload-Offset": "0",
                "X-Total-Size": str(len(payload)),
                "X-Idempotency-Key": "offset-test-1",
            },
        )
        wrong_offset = edge_http.put(
            f"/edge/v1/blobs/{digest}",
            content=payload[3:],
            headers={
                "X-Upload-Offset": "2",
                "X-Total-Size": str(len(payload)),
                "X-Idempotency-Key": "offset-test-1",
            },
        )
        wrong_size = edge_http.put(
            f"/edge/v1/blobs/{digest}",
            content=payload[3:],
            headers={
                "X-Upload-Offset": "3",
                "X-Total-Size": "5",
                "X-Idempotency-Key": "offset-test-1",
            },
        )

    assert partial.status_code == 200
    assert partial.json()["received_bytes"] == 3
    assert wrong_offset.status_code == 409
    assert wrong_offset.json()["code"] == "blob_offset_conflict"
    assert wrong_size.status_code == 409
    assert wrong_size.json()["code"] == "invalid_blob_range"


def test_deployment_is_idempotent_and_rolls_back_activation_failure(tmp_path: Path) -> None:
    app = _edge_app(tmp_path)
    data = b"portable-onnx-model"

    with TestClient(app) as edge_http, _client(_transport(edge_http)) as client:
        artifact = client.upload_blob(
            data,
            kind="onnx_model",
            file_name="model.onnx",
            idempotency_key="upload-deployment-1",
            chunk_size=5,
        )
        manifest = EdgeDeploymentManifest(
            deployment_id="deployment-001",
            workload_release_id="workload-001",
            channel_id="channel-001",
            pipeline_version_id="pipeline-version-001",
            idempotency_key="prepare-001",
            artifacts=[artifact],
            required_platforms=["jetson_orin_nano"],
            required_node_types=["inference.detection"],
            created_at="2026-07-21T12:00:00Z",
        )
        first = client.prepare_deployment(manifest)
        repeated = client.prepare_deployment(manifest)
        running = client.commit_deployment("deployment-001", idempotency_key="commit-001")
        running_repeated = client.commit_deployment("deployment-001", idempotency_key="commit-001")

        failing_manifest = manifest.model_copy(
            update={
                "deployment_id": "deployment-002",
                "workload_release_id": "workload-002",
                "idempotency_key": "prepare-002",
                "simulate_activation_failure": True,
            }
        )
        client.prepare_deployment(failing_manifest)
        with pytest.raises(EdgeClientError, match="activation failure"):
            client.commit_deployment("deployment-002", idempotency_key="commit-002")
        failed = client.deployment("deployment-002")
        rolled_back = client.rollback_deployment("deployment-002", idempotency_key="rollback-002")

    assert first == repeated
    assert first.status == "prepared"
    assert running == running_repeated
    assert running.status == "running"
    assert failed.status == "activation_failed"
    assert rolled_back.status == "rolled_back"


def test_result_replay_deduplicates_after_lost_ack_and_handles_new_boot(
    tmp_path: Path,
) -> None:
    app = _edge_app(tmp_path)
    state: EdgeAgentState = app.state.edge_agent
    first_boot = state.boot_id
    state.enqueue_result(
        channel_id="channel-001",
        workload_release_id="workload-001",
        payload={"decision": "ok"},
    )
    state.enqueue_result(
        channel_id="channel-001",
        workload_release_id="workload-001",
        payload={"decision": "reject"},
    )
    fail_first_ack = True

    def interrupt_first_ack(request: httpx.Request) -> None:
        nonlocal fail_first_ack
        if request.url.path.endswith("/results/ack") and fail_first_ack:
            fail_first_ack = False
            raise httpx.ConnectError("ack response lost", request=request)

    persisted = []
    tracker = EdgeResultReplayTracker()
    with (
        TestClient(app) as edge_http,
        _client(_transport(edge_http, after_response=interrupt_first_ack)) as client,
    ):
        with pytest.raises(EdgeClientError):
            tracker.sync_once(
                client,
                persisted.append,
                boot_id=first_boot,
                idempotency_key="ack-first-boot",
            )
        assert len(persisted) == 2

        replayed = tracker.sync_once(
            client,
            persisted.append,
            boot_id=first_boot,
            idempotency_key="ack-first-boot",
        )
        assert replayed == []
        assert len(persisted) == 2
        assert tracker.confirmed[("192.168.50.21", first_boot)] == 2

        second_boot = state.restart()
        state.enqueue_result(
            channel_id="channel-001",
            workload_release_id="workload-001",
            payload={"decision": "ok-after-reboot"},
        )
        new_items = tracker.sync_once(
            client,
            persisted.append,
            boot_id=second_boot,
            idempotency_key="ack-second-boot",
        )

    assert len(new_items) == 1
    assert new_items[0].sequence == 1
    assert new_items[0].boot_id == second_boot
    assert len(persisted) == 3


def test_result_batches_are_limited_by_count_and_serialized_size(tmp_path: Path) -> None:
    app = create_app(
        EdgeAgentConfig(
            address=EdgeDeviceAddress(management_ip="192.168.50.21"),
            data_root=tmp_path / "edge-state",
            max_result_batch_bytes=700,
        )
    )
    state: EdgeAgentState = app.state.edge_agent
    for index in range(3):
        state.enqueue_result(
            channel_id="channel-001",
            workload_release_id="workload-001",
            payload={"index": index, "detail": "x" * 250},
        )

    count_limited = state.results(boot_id=state.boot_id, after_sequence=0, limit=1)
    size_limited = state.results(boot_id=state.boot_id, after_sequence=0, limit=100)

    assert len(count_limited.items) == 1
    assert count_limited.has_more is True
    assert len(size_limited.items) == 1
    assert size_limited.has_more is True
