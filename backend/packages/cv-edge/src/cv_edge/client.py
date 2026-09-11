from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
from cv_contracts import (
    EdgeArtifactDescriptor,
    EdgeBlobUploadStatus,
    EdgeCapabilitySnapshot,
    EdgeControlError,
    EdgeDeploymentCommandRequest,
    EdgeDeploymentManifest,
    EdgeDeploymentState,
    EdgeDeviceAddress,
    EdgeDeviceHealth,
    EdgeDeviceIdentity,
    EdgeMediaLeaseRequest,
    EdgeMediaLeaseState,
    EdgePreviewFrame,
    EdgeResultAck,
    EdgeResultBatch,
    EdgeResultStreamList,
    EdgeRuntimeStatus,
)
from cv_contracts.edge import EdgeSoftwareCommand, EdgeSoftwareStatus

from cv_edge.network import EdgeNetworkPolicy


class EdgeClientErrorCode(StrEnum):
    TIMEOUT = "timeout"
    OFFLINE = "offline"
    REMOTE_ERROR = "remote_error"
    INCOMPATIBLE = "incompatible"
    HASH_MISMATCH = "hash_mismatch"
    PROTOCOL_ERROR = "protocol_error"


class EdgeClientError(RuntimeError):
    def __init__(
        self,
        code: EdgeClientErrorCode,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code


class EdgeAgentClient:
    def __init__(
        self,
        *,
        address: EdgeDeviceAddress,
        network_policy: EdgeNetworkPolicy,
        connect_timeout_seconds: float = 2.0,
        read_timeout_seconds: float = 10.0,
        transfer_timeout_seconds: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        validated = network_policy.validate(address.management_ip)
        self.address = address.model_copy(update={"management_ip": validated})
        self.base_url = f"http://{validated}:{address.control_port}"
        self.transfer_timeout_seconds = transfer_timeout_seconds
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                connect=connect_timeout_seconds,
                read=read_timeout_seconds,
                write=transfer_timeout_seconds,
                pool=connect_timeout_seconds,
            ),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> EdgeAgentClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def identity(self) -> EdgeDeviceIdentity:
        return EdgeDeviceIdentity.model_validate(self._request("GET", "/edge/v1/identity").json())

    def health(self) -> EdgeDeviceHealth:
        return EdgeDeviceHealth.model_validate(self._request("GET", "/edge/v1/health").json())

    def capabilities(self) -> EdgeCapabilitySnapshot:
        response = self._request("GET", "/edge/v1/capabilities")
        return EdgeCapabilitySnapshot.model_validate(response.json())

    def runtime_status(self) -> EdgeRuntimeStatus:
        response = self._request("GET", "/edge/v1/runtime")
        return EdgeRuntimeStatus.model_validate(response.json())

    def software_status(self) -> EdgeSoftwareStatus:
        return EdgeSoftwareStatus.model_validate(self._request("GET", "/edge/v1/software").json())

    def software_command(self, command: EdgeSoftwareCommand) -> EdgeSoftwareStatus:
        response = self._request(
            "POST", "/edge/v1/software/commands", json=command.model_dump(mode="json")
        )
        return EdgeSoftwareStatus.model_validate(response.json())

    def upload_blob(
        self,
        data: bytes,
        *,
        kind: str,
        file_name: str,
        idempotency_key: str,
        chunk_size: int = 1024 * 1024,
        media_type: str = "application/octet-stream",
    ) -> EdgeArtifactDescriptor:
        if chunk_size < 1:
            raise ValueError("chunk_size must be at least one byte")
        digest = hashlib.sha256(data).hexdigest()
        descriptor = EdgeArtifactDescriptor(
            sha256=digest,
            size_bytes=len(data),
            kind=kind,
            file_name=file_name,
            media_type=media_type,
        )
        head = self._request("HEAD", f"/edge/v1/blobs/{digest}")
        offset = _integer_header(head, "x-blob-offset")
        complete = head.headers.get("x-blob-complete", "false").lower() == "true"
        if complete:
            if _integer_header(head, "x-blob-size") != len(data):
                raise EdgeClientError(
                    EdgeClientErrorCode.PROTOCOL_ERROR,
                    "Edge reports an existing blob with an unexpected size.",
                    retryable=False,
                )
            return descriptor
        if offset > len(data):
            raise EdgeClientError(
                EdgeClientErrorCode.PROTOCOL_ERROR,
                "Edge upload offset is beyond the local artifact size.",
                retryable=False,
            )
        status = EdgeBlobUploadStatus(
            sha256=digest,
            size_bytes=len(data),
            received_bytes=offset,
            complete=False,
            idempotency_key=idempotency_key,
        )
        if not data:
            response = self._request(
                "PUT",
                f"/edge/v1/blobs/{digest}",
                content=b"",
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Upload-Offset": "0",
                    "X-Total-Size": "0",
                    "X-Idempotency-Key": idempotency_key,
                },
                timeout=self.transfer_timeout_seconds,
            )
            status = EdgeBlobUploadStatus.model_validate(response.json())
        while offset < len(data):
            chunk = data[offset : offset + chunk_size]
            response = self._request(
                "PUT",
                f"/edge/v1/blobs/{digest}",
                content=chunk,
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Upload-Offset": str(offset),
                    "X-Total-Size": str(len(data)),
                    "X-Idempotency-Key": idempotency_key,
                },
                timeout=self.transfer_timeout_seconds,
            )
            status = EdgeBlobUploadStatus.model_validate(response.json())
            if status.received_bytes <= offset:
                raise EdgeClientError(
                    EdgeClientErrorCode.PROTOCOL_ERROR,
                    "Edge did not advance the artifact upload offset.",
                    retryable=True,
                )
            offset = status.received_bytes
        if not status.complete:
            raise EdgeClientError(
                EdgeClientErrorCode.PROTOCOL_ERROR,
                "Edge received all bytes but did not finalize the blob.",
                retryable=True,
            )
        return descriptor

    def upload_blob_file(
        self,
        path: Path,
        *,
        descriptor: EdgeArtifactDescriptor,
        idempotency_key: str,
        chunk_size: int = 1024 * 1024,
        max_chunks: int | None = None,
    ) -> EdgeArtifactDescriptor | EdgeBlobUploadStatus:
        if chunk_size < 1:
            raise ValueError("chunk_size must be at least one byte")
        if max_chunks is not None and max_chunks < 1:
            raise ValueError("max_chunks must be positive")
        if not path.is_file() or path.stat().st_size != descriptor.size_bytes:
            raise EdgeClientError(
                EdgeClientErrorCode.PROTOCOL_ERROR,
                f"Local artifact {path} is missing or has an unexpected size.",
                retryable=False,
            )
        head = self._request(
            "HEAD", f"/edge/v1/blobs/{descriptor.sha256}", timeout=self.transfer_timeout_seconds
        )
        offset = _integer_header(head, "x-blob-offset")
        complete = head.headers.get("x-blob-complete", "false").lower() == "true"
        if complete:
            if _integer_header(head, "x-blob-size") != descriptor.size_bytes:
                raise EdgeClientError(
                    EdgeClientErrorCode.PROTOCOL_ERROR,
                    "Edge reports an existing blob with an unexpected size.",
                    retryable=False,
                )
            return descriptor
        if offset > descriptor.size_bytes:
            raise EdgeClientError(
                EdgeClientErrorCode.PROTOCOL_ERROR,
                "Edge upload offset is beyond the local artifact size.",
                retryable=False,
            )
        status = EdgeBlobUploadStatus(
            sha256=descriptor.sha256,
            size_bytes=descriptor.size_bytes,
            received_bytes=offset,
            complete=False,
            idempotency_key=idempotency_key,
        )
        if descriptor.size_bytes == 0:
            response = self._request(
                "PUT",
                f"/edge/v1/blobs/{descriptor.sha256}",
                content=b"",
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Upload-Offset": "0",
                    "X-Total-Size": "0",
                    "X-Idempotency-Key": idempotency_key,
                },
                timeout=self.transfer_timeout_seconds,
            )
            status = EdgeBlobUploadStatus.model_validate(response.json())
        with path.open("rb") as source:
            source.seek(offset)
            sent_chunks = 0
            while offset < descriptor.size_bytes:
                chunk = source.read(min(chunk_size, descriptor.size_bytes - offset))
                response = self._request(
                    "PUT",
                    f"/edge/v1/blobs/{descriptor.sha256}",
                    content=chunk,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-Upload-Offset": str(offset),
                        "X-Total-Size": str(descriptor.size_bytes),
                        "X-Idempotency-Key": idempotency_key,
                    },
                    timeout=self.transfer_timeout_seconds,
                )
                status = EdgeBlobUploadStatus.model_validate(response.json())
                if status.received_bytes <= offset:
                    raise EdgeClientError(
                        EdgeClientErrorCode.PROTOCOL_ERROR,
                        "Edge did not advance the artifact upload offset.",
                        retryable=True,
                    )
                offset = status.received_bytes
                sent_chunks += 1
                if max_chunks is not None and sent_chunks >= max_chunks and not status.complete:
                    return status
        if not status.complete:
            raise EdgeClientError(
                EdgeClientErrorCode.PROTOCOL_ERROR,
                "Edge received all bytes but did not finalize the blob.",
                retryable=True,
            )
        return descriptor

    def prepare_deployment(self, manifest: EdgeDeploymentManifest) -> EdgeDeploymentState:
        response = self._request(
            "POST",
            f"/edge/v1/deployments/{manifest.deployment_id}/prepare",
            json=manifest.model_dump(mode="json"),
        )
        return EdgeDeploymentState.model_validate(response.json())

    def commit_deployment(
        self,
        deployment_id: str,
        *,
        idempotency_key: str,
    ) -> EdgeDeploymentState:
        payload = EdgeDeploymentCommandRequest(idempotency_key=idempotency_key)
        response = self._request(
            "POST",
            f"/edge/v1/deployments/{deployment_id}/commit",
            json=payload.model_dump(mode="json"),
        )
        return EdgeDeploymentState.model_validate(response.json())

    def rollback_deployment(
        self,
        deployment_id: str,
        *,
        idempotency_key: str,
    ) -> EdgeDeploymentState:
        payload = EdgeDeploymentCommandRequest(idempotency_key=idempotency_key)
        response = self._request(
            "POST",
            f"/edge/v1/deployments/{deployment_id}/rollback",
            json=payload.model_dump(mode="json"),
        )
        return EdgeDeploymentState.model_validate(response.json())

    def stop_deployment(
        self,
        deployment_id: str,
        *,
        idempotency_key: str,
    ) -> EdgeDeploymentState:
        payload = EdgeDeploymentCommandRequest(idempotency_key=idempotency_key)
        response = self._request(
            "POST",
            f"/edge/v1/deployments/{deployment_id}/stop",
            json=payload.model_dump(mode="json"),
        )
        return EdgeDeploymentState.model_validate(response.json())

    def deployment(self, deployment_id: str) -> EdgeDeploymentState:
        response = self._request("GET", f"/edge/v1/deployments/{deployment_id}")
        return EdgeDeploymentState.model_validate(response.json())

    def results(
        self,
        *,
        after_sequence: int,
        limit: int = 100,
        boot_id: str | None = None,
    ) -> EdgeResultBatch:
        params: dict[str, str | int] = {"after_sequence": after_sequence, "limit": limit}
        if boot_id is not None:
            params["boot_id"] = boot_id
        response = self._request("GET", "/edge/v1/results", params=params)
        return EdgeResultBatch.model_validate(response.json())

    def result_streams(self) -> EdgeResultStreamList:
        response = self._request("GET", "/edge/v1/results/streams")
        return EdgeResultStreamList.model_validate(response.json())

    def acknowledge_results(self, ack: EdgeResultAck) -> EdgeResultAck:
        response = self._request(
            "POST",
            "/edge/v1/results/ack",
            json=ack.model_dump(mode="json"),
        )
        return EdgeResultAck.model_validate(response.json())

    def acquire_media_lease(self, request: EdgeMediaLeaseRequest) -> EdgeMediaLeaseState:
        response = self._request(
            "POST",
            "/edge/v1/media/viewer-leases",
            json=request.model_dump(mode="json"),
        )
        return EdgeMediaLeaseState.model_validate(response.json())

    def release_media_lease(self, lease_id: str) -> None:
        self._request("DELETE", f"/edge/v1/media/viewer-leases/{lease_id}")

    def preview_frame(
        self,
        *,
        lease_id: str,
        channel_id: str,
        after_sequence: int = 0,
    ) -> EdgePreviewFrame:
        response = self._request(
            "GET",
            "/edge/v1/media/preview",
            params={
                "lease_id": lease_id,
                "channel_id": channel_id,
                "after_sequence": after_sequence,
            },
        )
        return EdgePreviewFrame.model_validate(response.json())

    def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,  # noqa: ANN401 - mirrors the typed httpx keyword surface
    ) -> httpx.Response:
        try:
            response = self._client.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.TimeoutException as exc:
            raise EdgeClientError(
                EdgeClientErrorCode.TIMEOUT,
                f"Edge request timed out: {exc}",
                retryable=True,
            ) from exc
        except httpx.NetworkError as exc:
            raise EdgeClientError(
                EdgeClientErrorCode.OFFLINE,
                f"Edge is unreachable: {exc}",
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            error = _edge_error(response)
            code = EdgeClientErrorCode.REMOTE_ERROR
            if error.code in {
                "incompatible_platform",
                "incompatible_runtime",
                "missing_node_types",
            }:
                code = EdgeClientErrorCode.INCOMPATIBLE
            elif error.code == "blob_hash_mismatch":
                code = EdgeClientErrorCode.HASH_MISMATCH
            raise EdgeClientError(
                code,
                error.message,
                retryable=error.retryable,
                status_code=response.status_code,
            )
        return response


def _integer_header(response: httpx.Response, name: str) -> int:
    try:
        return int(response.headers[name])
    except (KeyError, ValueError) as exc:
        raise EdgeClientError(
            EdgeClientErrorCode.PROTOCOL_ERROR,
            f"Edge response is missing a valid {name} header.",
            retryable=False,
        ) from exc


def _edge_error(response: httpx.Response) -> EdgeControlError:
    try:
        body = response.json()
        payload = body
        if isinstance(body, dict) and "code" not in body:
            payload = body.get("detail", body)
        return EdgeControlError.model_validate(payload)
    except (ValueError, TypeError):
        return EdgeControlError(
            code="remote_error",
            message=f"Edge request failed with HTTP {response.status_code}.",
            retryable=response.status_code >= 500,
        )
