from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from cv_contracts import ResponseMeta
from cv_contracts.runtime import (
    RuntimeChannelTriggerablePipelineLink,
    RuntimeResultRecordSummary,
    RuntimeTriggerEndpointCreateRequest,
    RuntimeTriggerEndpointDetail,
    RuntimeTriggerEndpointListResponse,
    RuntimeTriggerEndpointReadiness,
    RuntimeTriggerEndpointSummary,
    RuntimeTriggerEndpointUpdateRequest,
    RuntimeTriggerExecutionStatus,
    RuntimeTriggerResponsePolicy,
)
from cv_core import generate_prefixed_id
from cv_status import (
    RuntimeMode,
    RuntimeObservedState,
    RuntimePipelineStatus,
    RuntimeResourceStatus,
    RuntimeSourceType,
)
from cv_utils import utc_now, utc_now_iso
from sqlalchemy import select
from sqlalchemy.orm import Session

from cv_db.models import (
    PipelineDefinition,
    PipelineVersion,
    ResultRecord,
    RuntimeChannel,
    RuntimeExecution,
    RuntimeSource,
    TriggerEndpoint,
)
from cv_db.runtime_execution_store import get_active_runtime_execution
from cv_db.runtime_model_store import require_runtime_pipeline
from cv_utils import PlatformError
from cv_db.result_store import result_record_to_summary


def list_trigger_endpoint_details(
    session: Session,
    *,
    channel_id: str | None = None,
) -> list[RuntimeTriggerEndpointDetail]:
    stmt = select(TriggerEndpoint).order_by(TriggerEndpoint.slug.asc())
    if channel_id:
        stmt = stmt.where(TriggerEndpoint.channel_id == channel_id)
    rows = session.scalars(stmt).all()
    return [_endpoint_detail(session, row) for row in rows]


def list_trigger_endpoints_response(
    session: Session,
    *,
    meta: ResponseMeta,
    channel_id: str | None = None,
) -> RuntimeTriggerEndpointListResponse:
    return RuntimeTriggerEndpointListResponse(
        items=[
            RuntimeTriggerEndpointSummary.model_validate(
                item.model_dump(include=set(RuntimeTriggerEndpointSummary.model_fields))
            )
            for item in list_trigger_endpoint_details(session, channel_id=channel_id)
        ],
        meta=meta,
    )


def get_trigger_endpoint_detail(
    session: Session,
    endpoint_id: str,
) -> RuntimeTriggerEndpointDetail | None:
    row = session.get(TriggerEndpoint, endpoint_id)
    if row is None:
        return None
    return _endpoint_detail(session, row)


def get_trigger_endpoint_for_execution(
    session: Session,
    endpoint_key: str,
) -> TriggerEndpoint | None:
    return session.scalar(
        select(TriggerEndpoint).where(TriggerEndpoint.slug == endpoint_key)
    ) or session.get(TriggerEndpoint, endpoint_key)


def create_trigger_endpoint(
    session: Session,
    payload: RuntimeTriggerEndpointCreateRequest,
    *,
    created_by: str | None,
) -> RuntimeTriggerEndpointDetail:
    channel = _trigger_channel(session, payload.channel_id)
    pipeline_version_id = payload.pipeline_version_id
    _validate_trigger_pipeline(session, pipeline_version_id)
    slug = _unique_slug(session, payload.slug or f"{channel.slug}-{payload.name}")
    row = TriggerEndpoint(
        id=generate_prefixed_id("tep"),
        channel_id=channel.id,
        pipeline_version_id=pipeline_version_id,
        display_name=payload.name.strip(),
        description=payload.description.strip() if payload.description else None,
        slug=slug,
        trigger_type=payload.trigger_type,
        method="PUT",
        enabled=payload.enabled,
        auth_required=payload.auth_required,
        auth_scope=payload.auth_scope,
        auth_policy=_auth_policy(payload.auth_required, payload.auth_scope, payload.auth_policy),
        timeout_ms=payload.timeout_ms,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        burst_limit=payload.burst_limit,
        response_policy=payload.response_policy.model_dump(mode="json"),
        request_schema=payload.request_schema,
        response_schema=payload.response_schema,
        status=RuntimeResourceStatus.DISABLED.value
        if not payload.enabled
        else RuntimeResourceStatus.DEGRADED.value,
        created_by=created_by,
        updated_by=created_by,
    )
    session.add(row)
    session.flush()
    _refresh_endpoint_status(session, row)
    session.flush()
    return _endpoint_detail(session, row)


def update_trigger_endpoint(
    session: Session,
    endpoint_id: str,
    payload: RuntimeTriggerEndpointUpdateRequest,
    *,
    updated_by: str | None,
) -> RuntimeTriggerEndpointDetail:
    row = _get_endpoint(session, endpoint_id)
    if payload.channel_id is not None:
        channel = _trigger_channel(session, payload.channel_id)
        row.channel_id = channel.id
    else:
        channel = _trigger_channel(session, row.channel_id)
    if payload.pipeline_version_id is not None:
        _validate_trigger_pipeline(session, payload.pipeline_version_id)
        row.pipeline_version_id = payload.pipeline_version_id
    if payload.name is not None:
        row.display_name = payload.name.strip()
    if payload.description is not None:
        row.description = payload.description.strip() or None
    if payload.slug is not None and payload.slug != row.slug:
        row.slug = _unique_slug(session, payload.slug)
    if payload.trigger_type is not None:
        row.trigger_type = payload.trigger_type
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.auth_required is not None:
        row.auth_required = payload.auth_required
    if payload.auth_scope is not None:
        row.auth_scope = payload.auth_scope
    if (
        payload.auth_policy is not None
        or payload.auth_required is not None
        or payload.auth_scope is not None
    ):
        row.auth_policy = _auth_policy(
            bool(row.auth_required),
            row.auth_scope,
            payload.auth_policy if payload.auth_policy is not None else row.auth_policy,
        )
    if payload.timeout_ms is not None:
        row.timeout_ms = payload.timeout_ms
    if payload.rate_limit_per_minute is not None:
        row.rate_limit_per_minute = payload.rate_limit_per_minute
    if payload.burst_limit is not None:
        row.burst_limit = payload.burst_limit
    if payload.response_policy is not None:
        row.response_policy = payload.response_policy.model_dump(mode="json")
    if payload.request_schema is not None:
        row.request_schema = payload.request_schema
    if payload.response_schema is not None:
        row.response_schema = payload.response_schema
    _validate_trigger_pipeline(session, row.pipeline_version_id)
    row.updated_by = updated_by
    row.updated_at = utc_now()
    _refresh_endpoint_status(session, row)
    session.flush()
    return _endpoint_detail(session, row)


def mark_trigger_endpoint_invoked(
    session: Session,
    *,
    endpoint_id: str,
    status: RuntimeTriggerExecutionStatus,
    latency_ms: float | None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> TriggerEndpoint:
    row = _get_endpoint(session, endpoint_id)
    row.last_invoked_at = utc_now()
    row.last_status = status
    row.last_latency_ms = latency_ms
    row.last_error_code = error_code
    row.last_error_message = error_message
    row.invocation_count = int(row.invocation_count or 0) + 1
    if status in {"failed", "timeout", "rejected"} or error_code:
        row.failure_count = int(row.failure_count or 0) + 1
    _refresh_endpoint_status(session, row)
    session.flush()
    return row


def record_trigger_timeout_result(
    session: Session,
    *,
    endpoint_id: str,
    correlation_id: str,
    timeout_ms: int,
    latency_ms: float,
) -> ResultRecord:
    endpoint = _get_endpoint(session, endpoint_id)
    row = ResultRecord(
        id=generate_prefixed_id("rrc"),
        channel_id=endpoint.channel_id,
        pipeline_version_id=endpoint.pipeline_version_id,
        runtime_execution_id=None,
        trigger_endpoint_id=endpoint.id,
        correlation_id=correlation_id,
        completed_at=utc_now(),
        mode=RuntimeMode.TRIGGER.value,
        status="timeout",
        result_type="timeout",
        schema_version="runtime.execution.result.v1",
        result={
            "status": "timeout",
            "error_code": "runtime_trigger_timeout",
            "timeout_ms": timeout_ms,
            "correlation_id": correlation_id,
            "trigger_endpoint_id": endpoint.id,
            "pipeline_version_id": endpoint.pipeline_version_id,
        },
        raw_inference_outputs=[],
        normalized_inference_outputs=[],
        derived_outputs=[],
        overlay_primitives=[],
        trigger_response={},
        runtime_metadata={
            "runtime_execution_id": None,
            "trigger_endpoint_id": endpoint.id,
            "endpoint_slug": endpoint.slug,
            "mode": RuntimeMode.TRIGGER.value,
            "timeout_ms": timeout_ms,
        },
        timing={"total_ms": latency_ms, "timeout_ms": timeout_ms},
        model_routes=[],
    )
    session.add(row)
    mark_trigger_endpoint_invoked(
        session,
        endpoint_id=endpoint_id,
        status="timeout",
        latency_ms=latency_ms,
        error_code="runtime_trigger_timeout",
        error_message="Trigger execution exceeded endpoint timeout.",
    )
    session.flush()
    return row


def trigger_endpoint_readiness(
    session: Session,
    row: TriggerEndpoint,
) -> RuntimeTriggerEndpointReadiness:
    channel = session.get(RuntimeChannel, row.channel_id)
    if channel is None or channel.deleted_at is not None:
        return RuntimeTriggerEndpointReadiness(
            ready=False,
            reason="The assigned channel no longer exists.",
            channel_id=row.channel_id,
            endpoint_pipeline_version_id=row.pipeline_version_id,
        )
    endpoint_pipeline_id = row.pipeline_version_id or channel.pipeline_version_id
    try:
        runtime_execution = require_runtime_pipeline(
            session, execution_id=channel.active_runtime_execution_id or "",
            pipeline_version_id=endpoint_pipeline_id or "",
        )
    except PlatformError as exc:
        return RuntimeTriggerEndpointReadiness(
            ready=False, reason=exc.message, channel_id=channel.id,
            endpoint_pipeline_version_id=endpoint_pipeline_id,
            actual_state=RuntimeObservedState(channel.observed_state),
        )
    if not row.enabled:
        return RuntimeTriggerEndpointReadiness(
            ready=False,
            reason="The endpoint is disabled.",
            channel_id=channel.id,
            endpoint_pipeline_version_id=endpoint_pipeline_id,
            actual_state=RuntimeObservedState(channel.observed_state),
        )
    try:
        pipeline_version = _validate_trigger_pipeline(session, endpoint_pipeline_id)
    except ValueError as exc:
        return RuntimeTriggerEndpointReadiness(
            ready=False,
            reason=str(exc),
            channel_id=channel.id,
            endpoint_pipeline_version_id=endpoint_pipeline_id,
            actual_state=RuntimeObservedState(channel.observed_state),
        )
    if _pipeline_uses_test_photo(pipeline_version):
        return RuntimeTriggerEndpointReadiness(
            ready=True,
            runtime_instance_id=runtime_execution.id,
            reason="The embedded test photo is ready.",
            actual_state=RuntimeObservedState(channel.observed_state),
            channel_id=channel.id,
            pipeline_version_id=endpoint_pipeline_id,
            endpoint_pipeline_version_id=endpoint_pipeline_id,
        )
    source = session.get(RuntimeSource, channel.source_id) if channel.source_id else None
    frame_ready, reason, execution = _source_frame_readiness(session, channel, source)
    execution = execution or runtime_execution
    if not frame_ready:
        return RuntimeTriggerEndpointReadiness(
            ready=False,
            reason=reason,
            runtime_instance_id=execution.id if execution is not None else None,
            actual_state=RuntimeObservedState(channel.observed_state),
            channel_id=channel.id,
            pipeline_version_id=endpoint_pipeline_id,
            endpoint_pipeline_version_id=endpoint_pipeline_id,
            last_heartbeat_at=_iso(execution.heartbeat_at) if execution is not None else None,
        )
    return RuntimeTriggerEndpointReadiness(
        ready=True,
        reason=reason,
        runtime_instance_id=execution.id if execution is not None else None,
        actual_state=RuntimeObservedState(channel.observed_state),
        channel_id=channel.id,
        pipeline_version_id=endpoint_pipeline_id,
        endpoint_pipeline_version_id=endpoint_pipeline_id,
        last_heartbeat_at=_iso(execution.heartbeat_at) if execution is not None else None,
    )


def _endpoint_detail(session: Session, row: TriggerEndpoint) -> RuntimeTriggerEndpointDetail:
    channel = session.get(RuntimeChannel, row.channel_id)
    readiness = trigger_endpoint_readiness(session, row)
    pipeline_label = _pipeline_label(session, row.pipeline_version_id)
    last_result = _last_result(session, row.id)
    status = _derived_status(row, readiness)
    return RuntimeTriggerEndpointDetail(
        trigger_endpoint_id=row.id,
        channel_id=row.channel_id,
        channel_name=channel.name if channel is not None else None,
        name=row.display_name or row.slug,
        description=row.description,
        slug=row.slug,
        trigger_type=row.trigger_type or "http_put",
        method="PUT",
        path=f"/api/v1/runtime/triggers/{row.slug}",
        enabled=bool(row.enabled),
        auth_required=bool(row.auth_required),
        auth_scope=row.auth_scope,
        auth_policy=_auth_policy(bool(row.auth_required), row.auth_scope, row.auth_policy),
        pipeline_definition_id=row.pipeline_definition_id,
        pipeline_version_id=row.pipeline_version_id,
        trigger_node_key=row.trigger_node_key,
        derived_from_pipeline=bool(row.derived_from_pipeline),
        pipeline_label=pipeline_label,
        timeout_ms=int(row.timeout_ms or 2500),
        rate_limit_per_minute=int(row.rate_limit_per_minute or 60),
        burst_limit=int(row.burst_limit or 4),
        status=status,
        readiness=readiness,
        last_invoked_at=_iso(row.last_invoked_at),
        last_status=row.last_status,  # type: ignore[arg-type]
        last_latency_ms=float(row.last_latency_ms) if row.last_latency_ms is not None else None,
        last_error_code=row.last_error_code,
        last_error_message=row.last_error_message,
        invocation_count=int(row.invocation_count or 0),
        failure_count=int(row.failure_count or 0),
        response_policy=RuntimeTriggerResponsePolicy.model_validate(row.response_policy or {}),
        request_schema=dict(row.request_schema or {}),
        response_schema=dict(row.response_schema or {}),
        last_result=_result_summary(last_result) if last_result is not None else None,
        created_at=_iso(row.created_at) or utc_now_iso(),
        updated_at=_iso(row.updated_at),
        created_by=row.created_by,
        updated_by=row.updated_by,
    )


def _refresh_endpoint_status(session: Session, row: TriggerEndpoint) -> None:
    readiness = trigger_endpoint_readiness(session, row)
    row.status = _derived_status(row, readiness).value
    row.updated_at = utc_now()


def _derived_status(
    row: TriggerEndpoint,
    readiness: RuntimeTriggerEndpointReadiness,
) -> RuntimeResourceStatus:
    if not bool(row.enabled):
        return RuntimeResourceStatus.DISABLED
    if readiness.ready:
        return RuntimeResourceStatus.ACTIVE
    if row.last_status == "failed":
        return RuntimeResourceStatus.FAILED
    return RuntimeResourceStatus.DEGRADED


def _trigger_channel(session: Session, channel_id: str) -> RuntimeChannel:
    row = session.get(RuntimeChannel, channel_id)
    if row is None or row.deleted_at is not None:
        raise ValueError("Trigger endpoint channel does not exist.")
    if row.source_id is None:
        raise ValueError("Triggerable pipelines require a channel with a configured source.")
    return row


def _validate_trigger_pipeline(
    session: Session,
    pipeline_version_id: str | None,
) -> PipelineVersion:
    if not pipeline_version_id:
        raise ValueError("Trigger endpoints require a published triggerable pipeline.")
    version = session.get(PipelineVersion, pipeline_version_id)
    if version is None:
        raise ValueError("Trigger endpoint pipeline version does not exist.")
    if RuntimeMode(version.mode) != RuntimeMode.TRIGGER:
        raise ValueError("Trigger endpoint pipeline version must be triggerable.")
    if RuntimePipelineStatus(version.status) != RuntimePipelineStatus.PUBLISHED:
        raise ValueError("Trigger endpoint pipeline version must be published.")
    return version


def _pipeline_uses_test_photo(version: PipelineVersion) -> bool:
    graph = version.graph if isinstance(version.graph, dict) else {}
    nodes = graph.get("nodes") if isinstance(graph, dict) else []
    return any(
        isinstance(node, dict) and node.get("node_type") == "source.test_photo"
        for node in (nodes if isinstance(nodes, list) else [])
    )


def _get_endpoint(session: Session, endpoint_id: str) -> TriggerEndpoint:
    row = session.get(TriggerEndpoint, endpoint_id)
    if row is None:
        raise ValueError("Trigger endpoint metadata does not exist.")
    return row


def triggerable_pipeline_links_for_channel(
    session: Session,
    *,
    channel_id: str,
    pipeline_version_id: str | None = None,
) -> list[RuntimeChannelTriggerablePipelineLink]:
    stmt = select(TriggerEndpoint).where(
        TriggerEndpoint.channel_id == channel_id,
        TriggerEndpoint.enabled.is_(True),
    )
    if pipeline_version_id:
        stmt = stmt.where(TriggerEndpoint.pipeline_version_id == pipeline_version_id)
    rows = session.scalars(
        stmt.order_by(TriggerEndpoint.display_name.asc(), TriggerEndpoint.slug.asc())
    ).all()
    return [
        RuntimeChannelTriggerablePipelineLink(
            trigger_endpoint_id=row.id,
            name=row.display_name or row.slug,
            description=row.description,
            slug=row.slug,
            trigger_type=row.trigger_type or "http_put",
            method="PUT",
            path=f"/api/v1/runtime/triggers/{row.slug}",
            enabled=bool(row.enabled),
            auth_required=bool(row.auth_required),
            auth_scope=row.auth_scope,
            status=_derived_status(row, trigger_endpoint_readiness(session, row)),
            pipeline_version_id=row.pipeline_version_id,
            pipeline_label=_pipeline_label(session, row.pipeline_version_id),
            last_invoked_at=_iso(row.last_invoked_at),
            last_status=row.last_status,
            last_latency_ms=float(row.last_latency_ms)
            if row.last_latency_ms is not None
            else None,
        )
        for row in rows
    ]


def _source_frame_readiness(
    session: Session,
    channel: RuntimeChannel,
    source: RuntimeSource | None,
) -> tuple[bool, str, RuntimeExecution | None]:
    if source is None:
        return False, "The assigned channel has no configured source.", None
    source_type = RuntimeSourceType(source.source_type)
    source_config = dict(source.config or {})
    snapshot_url = str(
        source_config.get("snapshot_url")
        or source_config.get("url")
        or source_config.get("http_url")
        or ""
    ).strip()
    if source_type == RuntimeSourceType.FILE_TEST:
        if source_config.get("path") and source_config.get("managed_test_file"):
            return True, "The triggerable pipeline can acquire a frame from the managed channel test file.", None
        return False, "Test file trigger execution requires a managed uploaded test file.", None
    if source_type == RuntimeSourceType.SNAPSHOT:
        if snapshot_url.lower().startswith(("http://", "https://")):
            return True, "The triggerable pipeline can acquire a frame from the channel snapshot URL.", None
        return False, "The channel snapshot source must expose an HTTP(S) URL.", None
    if snapshot_url.lower().startswith(("http://", "https://")):
        return True, "The triggerable pipeline can acquire a frame from the channel HTTP(S) URL.", None
    execution = get_active_runtime_execution(session, channel_id=channel.id)
    live_source_types = {
        RuntimeSourceType.RTSP,
        RuntimeSourceType.USB_OPENCV,
        RuntimeSourceType.ARAVIS_GIGE,
        RuntimeSourceType.FLIR_BLACKFLY,
        RuntimeSourceType.BASLER_PYLON,
    }
    if source_type in live_source_types:
        if _live_source_has_on_demand_capture_config(source_type, source_config):
            return (
                True,
                "The triggerable pipeline can acquire a frame from the channel camera source on demand.",
                execution,
            )
        return False, "The channel camera source is missing the configuration required for trigger capture.", execution
    if source_config.get("path"):
        return False, "The channel source uses a server-local path and cannot be triggered safely.", None
    return (
        False,
        "This channel source type does not provide a production frame acquisition path yet.",
        execution,
    )


def _live_source_has_on_demand_capture_config(
    source_type: RuntimeSourceType,
    source_config: dict[str, object],
) -> bool:
    if source_type == RuntimeSourceType.RTSP:
        raw_url = str(source_config.get("url") or "").strip().lower()
        if raw_url.startswith(("rtsp://", "rtsps://")):
            return True
        return bool(str(source_config.get("host") or "").strip())
    if source_type == RuntimeSourceType.USB_OPENCV:
        return source_config.get("device_index") is not None or bool(
            str(source_config.get("device_path") or "").strip()
        )
    if source_type == RuntimeSourceType.FLIR_BLACKFLY:
        return True
    if source_type == RuntimeSourceType.BASLER_PYLON:
        return any(
            str(source_config.get(key) or "").strip()
            for key in (
                "camera_name",
                "camera",
                "serial_number",
                "user_defined_name",
                "device_id",
                "id_",
                "ip_address",
                "model",
            )
        )
    if source_type == RuntimeSourceType.ARAVIS_GIGE:
        return bool(str(source_config.get("camera_name") or "").strip())
    return False


def _pipeline_label(session: Session, pipeline_version_id: str | None) -> str | None:
    if not pipeline_version_id:
        return None
    version = session.get(PipelineVersion, pipeline_version_id)
    if version is None:
        return None
    definition = session.get(PipelineDefinition, version.pipeline_definition_id)
    if definition is None:
        return f"Pipeline v{version.version_no}"
    return f"{definition.name} v{version.version_no}"


def _last_result(session: Session, endpoint_id: str) -> ResultRecord | None:
    return session.scalar(
        select(ResultRecord)
        .where(ResultRecord.trigger_endpoint_id == endpoint_id)
        .order_by(ResultRecord.completed_at.desc())
    )


def _result_summary(row: ResultRecord) -> RuntimeResultRecordSummary:
    return result_record_to_summary(row)


def _unique_slug(session: Session, value: str) -> str:
    base = _slugify(value)
    existing = set(session.scalars(select(TriggerEndpoint.slug)).all())
    candidate = base
    suffix = 2
    while candidate in existing:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _slugify(value: str) -> str:
    candidate = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return candidate or "trigger-endpoint"


def _auth_policy(
    auth_required: bool,
    auth_scope: str,
    stored: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        **dict(stored or {}),
        "type": "api_key",
        "required": auth_required,
        "scope": auth_scope,
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")
