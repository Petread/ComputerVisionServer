from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from cv_contracts import (
    EdgeBlobUploadStatus,
    EdgeCapabilitySnapshot,
    EdgeControlError,
    EdgeDeploymentCommandRequest,
    EdgeDeploymentManifest,
    EdgeDeploymentState,
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
from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError

from edge_agent.software import queue_software_command, software_status
from edge_agent.state import EdgeAgentConfig, EdgeAgentState, EdgeStateError

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ERROR_RESPONSES = {
    status_code: {"model": EdgeControlError, "description": "Edge control error"}
    for status_code in (404, 409, 413, 422, 503, 507)
}


def create_app(config: EdgeAgentConfig | None = None) -> FastAPI:
    edge_state = EdgeAgentState(config or EdgeAgentConfig.from_environment())

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if getattr(_application.state, "close_edge_state_on_shutdown", True):
                edge_state.close()

    application = FastAPI(
        title="YBB Edge Agent",
        version="0.8.0",
        responses=_ERROR_RESPONSES,
        lifespan=lifespan,
    )
    application.state.edge_agent = edge_state
    application.state.close_edge_state_on_shutdown = True

    @application.exception_handler(EdgeStateError)
    async def handle_edge_state_error(_request: Request, exc: EdgeStateError) -> Response:
        error = EdgeControlError(
            code=exc.code,
            message=str(exc),
            retryable=exc.retryable,
            detail=exc.detail,
        )
        return Response(
            content=error.model_dump_json(),
            status_code=exc.status_code,
            media_type="application/json",
        )

    @application.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        _request: Request,
        exc: RequestValidationError,
    ) -> Response:
        errors = [
            {
                "location": ".".join(str(part) for part in error["loc"]),
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        response = EdgeControlError(
            code="request_validation_error",
            message="The edge control request is invalid.",
            detail={"errors": errors},
        )
        return Response(
            content=response.model_dump_json(),
            status_code=422,
            media_type="application/json",
        )

    @application.get("/edge/v1/identity", response_model=EdgeDeviceIdentity)
    def identity() -> EdgeDeviceIdentity:
        return edge_state.identity()

    @application.get("/edge/v1/health", response_model=EdgeDeviceHealth)
    def health() -> EdgeDeviceHealth:
        return edge_state.health()

    @application.get("/edge/v1/capabilities", response_model=EdgeCapabilitySnapshot)
    def capabilities() -> EdgeCapabilitySnapshot:
        return edge_state.capabilities()

    @application.get("/edge/v1/runtime", response_model=EdgeRuntimeStatus)
    def runtime_status() -> EdgeRuntimeStatus:
        return edge_state.runtime_status()

    @application.get("/edge/v1/software", response_model=EdgeSoftwareStatus)
    def installed_software() -> EdgeSoftwareStatus:
        return software_status(edge_state.config.data_root)

    @application.post(
        "/edge/v1/software/commands", response_model=EdgeSoftwareStatus, status_code=202
    )
    def software_command(command: EdgeSoftwareCommand) -> EdgeSoftwareStatus:
        try:
            return queue_software_command(edge_state.config.data_root, command)
        except ValueError as exc:
            raise EdgeStateError("software_command_rejected", str(exc), status_code=409) from exc

    @application.head("/edge/v1/blobs/{sha256}")
    def blob_head(sha256: str) -> Response:
        _validate_sha256(sha256)
        status = edge_state.blob_status(sha256)
        return Response(
            status_code=200,
            headers={
                "X-Blob-Complete": str(status.complete).lower(),
                "X-Blob-Offset": str(status.received_bytes),
                "X-Blob-Size": str(status.size_bytes),
            },
        )

    @application.put("/edge/v1/blobs/{sha256}", response_model=EdgeBlobUploadStatus)
    async def blob_put(
        sha256: str,
        request: Request,
        x_upload_offset: int = Header(ge=0),
        x_total_size: int = Header(ge=0),
        x_idempotency_key: str = Header(min_length=3, max_length=160),
    ) -> EdgeBlobUploadStatus:
        _validate_sha256(sha256)
        chunk = await request.body()
        return edge_state.append_blob(
            sha256,
            chunk,
            offset=x_upload_offset,
            total_size=x_total_size,
            idempotency_key=x_idempotency_key,
        )

    @application.post(
        "/edge/v1/deployments/{deployment_id}/prepare",
        response_model=EdgeDeploymentState,
    )
    def prepare_deployment(
        deployment_id: str,
        manifest: EdgeDeploymentManifest,
    ) -> EdgeDeploymentState:
        if deployment_id != manifest.deployment_id:
            raise EdgeStateError(
                "deployment_id_mismatch",
                "Path and manifest deployment IDs must match.",
                status_code=422,
            )
        return edge_state.prepare(manifest)

    @application.post(
        "/edge/v1/deployments/{deployment_id}/commit",
        response_model=EdgeDeploymentState,
    )
    def commit_deployment(
        deployment_id: str,
        command: EdgeDeploymentCommandRequest,
    ) -> EdgeDeploymentState:
        return edge_state.commit(deployment_id, command.idempotency_key)

    @application.post(
        "/edge/v1/deployments/{deployment_id}/rollback",
        response_model=EdgeDeploymentState,
    )
    def rollback_deployment(
        deployment_id: str,
        command: EdgeDeploymentCommandRequest,
    ) -> EdgeDeploymentState:
        return edge_state.rollback(deployment_id, command.idempotency_key)

    @application.post(
        "/edge/v1/deployments/{deployment_id}/stop",
        response_model=EdgeDeploymentState,
    )
    def stop_deployment(
        deployment_id: str,
        command: EdgeDeploymentCommandRequest,
    ) -> EdgeDeploymentState:
        return edge_state.stop(deployment_id, command.idempotency_key)

    @application.get(
        "/edge/v1/deployments/{deployment_id}",
        response_model=EdgeDeploymentState,
    )
    def get_deployment(deployment_id: str) -> EdgeDeploymentState:
        return edge_state.deployment(deployment_id)

    @application.get("/edge/v1/results", response_model=EdgeResultBatch)
    def get_results(
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
        boot_id: str | None = Query(default=None, min_length=1, max_length=128),
    ) -> EdgeResultBatch:
        return edge_state.results(
            boot_id=boot_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    @application.get("/edge/v1/results/streams", response_model=EdgeResultStreamList)
    def get_result_streams() -> EdgeResultStreamList:
        return edge_state.result_streams()

    @application.post("/edge/v1/results/ack", response_model=EdgeResultAck)
    def acknowledge_results(ack: EdgeResultAck) -> EdgeResultAck:
        return edge_state.acknowledge(ack)

    @application.post("/edge/v1/media/viewer-leases", response_model=EdgeMediaLeaseState)
    def acquire_media_lease(request: EdgeMediaLeaseRequest) -> EdgeMediaLeaseState:
        return edge_state.acquire_media_lease(request)

    @application.delete("/edge/v1/media/viewer-leases/{lease_id}", status_code=204)
    def release_media_lease(lease_id: str) -> Response:
        edge_state.release_media_lease(lease_id)
        return Response(status_code=204)

    @application.get("/edge/v1/media/preview", response_model=EdgePreviewFrame)
    def get_preview_frame(
        lease_id: str = Query(min_length=3, max_length=160),
        channel_id: str = Query(min_length=3, max_length=128),
        after_sequence: int = Query(default=0, ge=0),
    ) -> EdgePreviewFrame:
        return edge_state.preview_frame(
            lease_id=lease_id,
            channel_id=channel_id,
            after_sequence=after_sequence,
        )

    return application


def _validate_sha256(value: str) -> None:
    if not _SHA256_PATTERN.fullmatch(value):
        raise EdgeStateError(
            "request_validation_error",
            "sha256 must be 64 lowercase hexadecimal chars",
            status_code=422,
        )


app = create_app()
