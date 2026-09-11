from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlsplit

from cv_contracts import ResponseMeta
from cv_contracts.runtime import (
    RuntimeChannelCaptureSummary,
    RuntimeChannelCreateRequest,
    RuntimeChannelDetail,
    RuntimeChannelHealthSummary,
    RuntimeChannelOutputSummary,
    RuntimeChannelPlacementSummary,
    RuntimeChannelPerformanceSummary,
    RuntimeChannelRequestEndpoint,
    RuntimeChannelRuntimeState,
    RuntimeChannelStatusCounts,
    RuntimeChannelStatusOverview,
    RuntimeChannelSummary,
    RuntimeChannelTriggerablePipelineLink,
    RuntimeChannelAuthoringContext,
    RuntimeChannelAuthoringLabelProject,
    RuntimeChannelUpdateRequest,
    RuntimeStemRotationPriorityControl,
    RuntimeSourceCredentialInput,
)
from cv_core import generate_prefixed_id
from cv_runtime import can_transition_channel_desired
from cv_status import (
    HealthState,
    RuntimeChannelDesiredState,
    RuntimeControlCommand,
    RuntimeExecutionStatus,
    RuntimeMode,
    RuntimeObservedState,
    RuntimePipelineStatus,
    RuntimeResourceStatus,
    RuntimeSourceType,
)
from cv_utils import utc_now, utc_now_iso
from sqlalchemy import func, select
from sqlalchemy.orm import Session, load_only, object_session

from cv_db.edge_fleet_store import validate_edge_channel_target
from cv_db.place_store import require_facility
from cv_db.models import (
    CaptureRule,
    Dataset,
    EdgeDeploymentAttemptRecord,
    EdgeDevice,
    LabelProject,
    OutputConnector,
    PipelineDefinition,
    PipelineVersion,
    ResultRecord,
    RuntimeChannel,
    RuntimeExecution,
    RuntimeSource,
)
from cv_db.runtime_execution_store import (
    create_runtime_control_command,
    runtime_control_command_to_api,
)

_ARAVIS_DEFAULT_RTSP_HOST = "127.0.0.1"
_ARAVIS_DEFAULT_RTSP_PORT_BASE = 18554
_ARAVIS_DEFAULT_RTSP_PORT_SPAN = 10000
_RUNTIME_HEARTBEAT_STALE_AFTER_SECONDS = 60
_ARAVIS_IDENTIFIER_KEYS = (
    "camera_name",
    "camera",
    "serial_number",
    "user_defined_name",
    "device_id",
    "id_",
    "ip_address",
)
_BASLER_IDENTIFIER_KEYS = (
    "camera_name",
    "camera",
    "serial_number",
    "user_defined_name",
    "device_id",
    "id_",
    "ip_address",
    "model",
)
_BASLER_TRANSPORT_MODES = {"auto", "pylon_direct", "aravis_rtsp_bridge"}
_USB_OPENCV_BACKENDS = {"auto", "v4l2", "dshow", "msmf"}
_TERMINAL_RUNTIME_EXECUTION_STATUSES = {
    RuntimeExecutionStatus.STOPPED.value,
    RuntimeExecutionStatus.FAILED_TERMINAL.value,
    RuntimeExecutionStatus.RECOVERED.value,
}
_CHANNEL_STATE_OWNS_RUNTIME_SUMMARY = {
    RuntimeChannelDesiredState.DRAFT,
    RuntimeChannelDesiredState.STOPPED,
    RuntimeChannelDesiredState.START_REQUESTED,
    RuntimeChannelDesiredState.STOP_REQUESTED,
    RuntimeChannelDesiredState.DISABLED,
}


def default_stem_rotation_priority_control() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        RuntimeStemRotationPriorityControl().model_dump(mode="json"),
    )


def create_runtime_channel(
    session: Session,
    payload: RuntimeChannelCreateRequest,
    *,
    created_by: str | None,
    source_secret_root: Path | None = None,
) -> RuntimeChannelDetail:
    if payload.mode == RuntimeMode.TRIGGER:
        raise ValueError(
            "Channels represent sources. Add an HTTP Trigger Input and Channel frame input node "
            "in a triggerable runtime pipeline."
        )
    slug = _unique_slug(session, RuntimeChannel, payload.slug or payload.name)
    source = _resolve_or_create_source(
        session,
        source_id=payload.source_id,
        source_type=payload.source_type,
        source_config=payload.source_config,
        source_credentials=payload.source_credentials,
        source_secret_root=source_secret_root,
        channel_slug=slug,
        channel_name=payload.name,
    )
    pipeline_version_id = _validated_channel_pipeline_version_id(
        session,
        payload.pipeline_version_id
        or _resolve_pipeline_version_id(
            session,
            payload.assigned_pipeline,
        ),
        channel_mode=payload.mode,
    )
    assigned_pipeline = payload.assigned_pipeline or _pipeline_label(
        session,
        pipeline_version_id=pipeline_version_id,
    )
    edge_device_id = None
    if payload.placement.target == "edge_device":
        assert payload.placement.edge_device_id is not None
        edge_device_id = validate_edge_channel_target(
            session,
            payload.placement.edge_device_id,
        ).id
    row = RuntimeChannel(
        id=generate_prefixed_id("chn"),
        slug=slug,
        name=payload.name,
        technical_place=(
            require_facility(session, payload.facility_id).name
            if payload.facility_id else payload.technical_place
        ),
        facility_id=payload.facility_id,
        description=payload.description,
        mode=payload.mode.value,
        assigned_pipeline=assigned_pipeline,
        source_id=source.id,
        pipeline_version_id=pipeline_version_id,
        authoring_label_project_ids=_validated_authoring_label_project_ids(
            session,
            payload.authoring_label_project_ids,
        ),
        result_history_policy=payload.result_history_policy.model_dump(mode="json"),
        stem_rotation_priority_control=RuntimeStemRotationPriorityControl(
            priority_source=payload.stem_rotation_priority_source,
            priority_mode=payload.stem_rotation_priority_mode,
            optimization_enable=payload.stem_rotation_optimization_enable,
            updated_at=utc_now_iso(),
            updated_by=created_by,
        ).model_dump(mode="json"),
        execution_target=payload.placement.target,
        edge_device_id=edge_device_id,
        output_summary=payload.output_summary.model_dump(mode="json"),
        capture_summary=payload.capture_summary.model_dump(mode="json"),
        health_summary={},
        performance_summary={},
        desired_state=RuntimeChannelDesiredState.STOPPED.value,
        observed_state=RuntimeObservedState.STOPPED.value,
        health_state=HealthState.DOWN.value,
        stream_desired_state="stopped",
        stream_actual_state="stopped",
        created_by=created_by,
        updated_by=created_by,
    )
    session.add(row)
    session.flush()
    _ensure_channel_scoped_pipeline_after_assignment(session, row, actor=created_by)
    _sync_channel_trigger_endpoints(session, row.id)
    return get_runtime_channel_detail(session, row.id)  # type: ignore[return-value]


def list_runtime_channels(
    session: Session,
    *,
    search: str | None = None,
    source_type: RuntimeSourceType | None = None,
    mode: RuntimeMode | None = None,
    status: str | None = None,
    technical_place: str | None = None,
    execution_target: str | None = None,
) -> list[RuntimeChannelSummary]:
    rows = session.scalars(
        select(RuntimeChannel)
        .where(RuntimeChannel.deleted_at.is_(None))
        .order_by(RuntimeChannel.technical_place.asc(), RuntimeChannel.name.asc())
    ).all()
    context = _load_channel_context(session, rows)
    items = [_channel_summary(row, context=context) for row in rows]
    if search:
        needle = search.casefold().strip()
        items = [
            item
            for item in items
            if needle in " ".join(
                [
                    item.name,
                    item.technical_place,
                    item.source_summary,
                    item.assigned_pipeline or "",
                ]
            ).casefold()
        ]
    if source_type:
        items = [item for item in items if item.source_type == source_type]
    if mode:
        items = [item for item in items if item.mode == mode]
    if status:
        normalized_status = status.casefold()
        items = [
            item
            for item in items
            if item.status.value == normalized_status
            or item.desired_state.value == normalized_status
            or item.health_state.value == normalized_status
        ]
    if technical_place:
        normalized_place = technical_place.casefold()
        items = [item for item in items if item.technical_place.casefold() == normalized_place]
    if execution_target:
        items = [item for item in items if item.placement.target == execution_target]
    return items


def get_runtime_channel_detail(
    session: Session,
    channel_id: str,
) -> RuntimeChannelDetail | None:
    row = session.get(RuntimeChannel, channel_id)
    if row is None or row.deleted_at is not None:
        return None
    context = _load_channel_context(session, [row])
    if _maybe_queue_stale_runtime_recovery(session, row, context=context):
        context = _load_channel_context(session, [row])
    summary = _channel_summary(row, context=context)
    source = context["sources"].get(row.source_id or "")
    from cv_db.trigger_endpoint_store import triggerable_pipeline_links_for_channel

    triggerable_pipelines = triggerable_pipeline_links_for_channel(
        session,
        channel_id=row.id,
    )
    latest_result = context["results"].get(row.id)
    return RuntimeChannelDetail(
        **summary.model_dump(),
        description=row.description,
        source_config=dict(source.config or {}) if source is not None else {},
        triggerable_pipelines=triggerable_pipelines,
        request_endpoints=_channel_request_endpoints(
            session,
            row,
            triggerable_pipelines=triggerable_pipelines,
            result=latest_result,
        ),
        created_at=_iso(row.created_at) or utc_now_iso(),
        updated_at=_iso(row.updated_at),
    )


def _maybe_queue_stale_runtime_recovery(
    session: Session,
    row: RuntimeChannel,
    *,
    context: dict[str, Any],
) -> bool:
    if RuntimeChannelDesiredState(row.desired_state) != RuntimeChannelDesiredState.RUNNING:
        return False
    execution = context["executions"].get(row.id)
    if execution is None or row.active_runtime_execution_id != execution.id:
        return False
    if RuntimeExecutionStatus(execution.status) not in {
        RuntimeExecutionStatus.RUNNING,
        RuntimeExecutionStatus.FAILED_RETRYABLE,
    }:
        return False
    runtime_state = _runtime_state_summary(row, execution=execution)
    if not _runtime_heartbeat_is_stale(runtime_state):
        return False
    try:
        create_runtime_control_command(
            session,
            channel_id=row.id,
            command=RuntimeControlCommand.RESTART,
            requested_by="runtime-admin-api",
            reason=(
                "Runtime admin status detected a stale runtime heartbeat and queued "
                "an idempotent restart."
            ),
            idempotency_key=f"runtime-stale-heartbeat-recovery:{row.id}:{execution.id}",
        )
    except ValueError:
        return False
    return True


def get_runtime_channel_authoring_context(
    session: Session,
    channel_id: str,
) -> RuntimeChannelAuthoringContext:
    detail = get_runtime_channel_detail(session, channel_id)
    if detail is None:
        raise ValueError("Channel does not exist.")
    row = _get_live_channel(session, channel_id)
    source = session.get(RuntimeSource, row.source_id) if row.source_id else None
    version = session.get(PipelineVersion, row.pipeline_version_id) if row.pipeline_version_id else None
    label_projects = _channel_authoring_label_projects(
        session,
        list(row.authoring_label_project_ids or []),
    )
    labels = sorted(
        {
            label
            for project in label_projects
            for label in project.labels
            if label.strip()
        },
        key=lambda item: item.casefold(),
    )
    return RuntimeChannelAuthoringContext(
        channel=detail,
        source_config=dict(source.config or {}) if source is not None else {},
        source_metadata={
            "source_id": source.id if source is not None else None,
            "source_type": source.source_type if source is not None else None,
            "source_summary": detail.source_summary,
            "technical_place": detail.technical_place,
        },
        label_projects=label_projects,
        labels=labels,
        pipeline_definition_id=version.pipeline_definition_id if version is not None else None,
        pipeline_version_id=version.id if version is not None else None,
    )


def update_runtime_channel(
    session: Session,
    channel_id: str,
    payload: RuntimeChannelUpdateRequest,
    *,
    updated_by: str | None,
    source_secret_root: Path | None = None,
) -> RuntimeChannelDetail:
    row = _get_live_channel(session, channel_id)
    previous_desired_state = RuntimeChannelDesiredState(row.desired_state)
    previous_observed_state = RuntimeObservedState(row.observed_state)
    previous_runtime_binding = _runtime_binding_snapshot(session, row)
    if payload.name is not None:
        row.name = payload.name
    if payload.technical_place is not None:
        if payload.technical_place != row.technical_place:
            row.facility_id = None
        row.technical_place = payload.technical_place
    if "facility_id" in payload.model_fields_set:
        row.facility_id = payload.facility_id
        if payload.facility_id:
            row.technical_place = require_facility(session, payload.facility_id).name
    if payload.description is not None:
        row.description = payload.description
    if payload.mode is not None:
        if payload.mode == RuntimeMode.TRIGGER:
            raise ValueError(
                "Channels represent sources. Configure trigger execution with HTTP Trigger Input, "
                "Channel frame input, and Response Output nodes in the pipeline designer."
            )
        row.mode = payload.mode.value
    if payload.source_id is not None:
        source = session.get(RuntimeSource, payload.source_id)
        if source is None:
            raise ValueError("Source does not exist.")
        row.source_id = source.id
    if payload.source_type is not None or payload.source_config is not None:
        source = session.get(RuntimeSource, row.source_id) if row.source_id else None
        source_type = payload.source_type
        if source_type is None and source is not None:
            source_type = RuntimeSourceType(source.source_type)
        if source_type is None:
            raise ValueError("source_type is required when creating a source for this channel.")
        source_config = payload.source_config
        if source_config is None and source is not None:
            source_config = dict(source.config or {})
        source_config = source_config or {}
        if source is None:
            source_id = generate_prefixed_id("src")
            source_config, source_secret_refs = _prepare_source_config_and_secret_refs(
                source_type,
                source_config,
                source_credentials=payload.source_credentials,
                existing_secret_refs={},
                source_id=source_id,
                source_secret_root=source_secret_root,
            )
            source = RuntimeSource(
                id=source_id,
                slug=_unique_slug(session, RuntimeSource, f"{row.slug}-source"),
                name=f"{row.name} source",
                source_type=source_type.value,
                config=source_config,
                secret_refs=source_secret_refs,
                validation_status="active",
                health_state=HealthState.DOWN.value,
            )
            session.add(source)
            session.flush()
            row.source_id = source.id
        else:
            source_config, source_secret_refs = _prepare_source_config_and_secret_refs(
                source_type,
                source_config,
                source_credentials=payload.source_credentials,
                existing_secret_refs=dict(source.secret_refs or {}),
                source_id=source.id,
                source_secret_root=source_secret_root,
            )
            source.source_type = source_type.value
            source.config = source_config
            source.secret_refs = source_secret_refs
            source.name = f"{row.name} source"
    _apply_runtime_pipeline_update(session, row, payload)
    _ensure_channel_scoped_pipeline_after_assignment(session, row, actor=updated_by)
    if payload.output_summary is not None:
        row.output_summary = payload.output_summary.model_dump(mode="json")
    if payload.capture_summary is not None:
        row.capture_summary = payload.capture_summary.model_dump(mode="json")
    if payload.authoring_label_project_ids is not None:
        row.authoring_label_project_ids = _validated_authoring_label_project_ids(
            session,
            payload.authoring_label_project_ids,
        )
    if payload.result_history_policy is not None:
        row.result_history_policy = payload.result_history_policy.model_dump(mode="json")
    if any(
        value is not None
        for value in (
            payload.stem_rotation_priority_source,
            payload.stem_rotation_priority_mode,
            payload.stem_rotation_optimization_enable,
        )
    ):
        current = RuntimeStemRotationPriorityControl.model_validate(
            row.stem_rotation_priority_control or default_stem_rotation_priority_control()
        )
        row.stem_rotation_priority_control = current.model_copy(
            update={
                "priority_source": (
                    payload.stem_rotation_priority_source
                    if payload.stem_rotation_priority_source is not None
                    else current.priority_source
                ),
                "priority_mode": (
                    payload.stem_rotation_priority_mode
                    if payload.stem_rotation_priority_mode is not None
                    else current.priority_mode
                ),
                "optimization_enable": (
                    payload.stem_rotation_optimization_enable
                    if payload.stem_rotation_optimization_enable is not None
                    else current.optimization_enable
                ),
                "revision": current.revision + 1,
                "updated_at": utc_now_iso(),
                "updated_by": updated_by,
            }
        ).model_dump(mode="json")
    if payload.placement is not None:
        if previous_desired_state in {
            RuntimeChannelDesiredState.START_REQUESTED,
            RuntimeChannelDesiredState.RUNNING,
            RuntimeChannelDesiredState.STOP_REQUESTED,
        } or previous_observed_state in {
            RuntimeObservedState.STARTING,
            RuntimeObservedState.RUNNING,
            RuntimeObservedState.STOPPING,
        }:
            raise ValueError("Stop the channel before changing its execution target.")
        row.execution_target = payload.placement.target
        if payload.placement.target == "edge_device":
            assert payload.placement.edge_device_id is not None
            row.edge_device_id = validate_edge_channel_target(
                session,
                payload.placement.edge_device_id,
            ).id
        else:
            row.edge_device_id = None
    if payload.desired_state is not None:
        current = RuntimeChannelDesiredState(row.desired_state)
        if not can_transition_channel_desired(current, payload.desired_state):
            raise ValueError(
                f"Channel desired state cannot transition from {current.value} to "
                f"{payload.desired_state.value}."
            )
        row.desired_state = payload.desired_state.value
    row.updated_by = updated_by
    session.flush()
    current_runtime_binding = _runtime_binding_snapshot(session, row)
    if _requires_runtime_restart_after_update(
        previous_runtime_binding=previous_runtime_binding,
        current_runtime_binding=current_runtime_binding,
        credentials_supplied=payload.source_credentials is not None,
        previous_desired_state=previous_desired_state,
        current_desired_state=RuntimeChannelDesiredState(row.desired_state),
        previous_observed_state=previous_observed_state,
    ):
        create_runtime_control_command(
            session,
            channel_id=channel_id,
            command=RuntimeControlCommand.RESTART,
            requested_by=updated_by,
            reason=(
                "Channel runtime binding changed; restart required to apply the "
                "selected pipeline or source configuration."
            ),
        )
    _sync_channel_trigger_endpoints(session, channel_id)
    return get_runtime_channel_detail(session, channel_id)  # type: ignore[return-value]


def delete_runtime_channel(
    session: Session,
    channel_id: str,
    *,
    deleted_by: str | None,
    reason: str | None = None,
) -> RuntimeChannelSummary:
    row = _get_live_channel(session, channel_id)
    observed = RuntimeObservedState(row.observed_state)
    desired = RuntimeChannelDesiredState(row.desired_state)
    failed_edge_without_active_deployment = False
    if (
        row.execution_target == "edge_device"
        and observed == RuntimeObservedState.FAILED
        and row.edge_deployment_id is None
    ):
        latest_edge_deployment = session.scalar(
            select(EdgeDeploymentAttemptRecord)
            .where(EdgeDeploymentAttemptRecord.channel_id == row.id)
            .order_by(
                EdgeDeploymentAttemptRecord.created_at.desc(),
                EdgeDeploymentAttemptRecord.id.desc(),
            )
        )
        failed_edge_without_active_deployment = (
            latest_edge_deployment is None
            or latest_edge_deployment.status
            in {"admission_failed", "failed", "rolled_back", "stopped"}
        )
    if (
        observed
        in {
            RuntimeObservedState.STARTING,
            RuntimeObservedState.RUNNING,
            RuntimeObservedState.STOPPING,
        }
        or desired
        in {
            RuntimeChannelDesiredState.START_REQUESTED,
            RuntimeChannelDesiredState.RUNNING,
            RuntimeChannelDesiredState.STOP_REQUESTED,
        }
    ) and not failed_edge_without_active_deployment:
        raise ValueError("Stop the channel runtime before deleting the channel.")
    context = _load_channel_context(session, [row])
    summary = _channel_summary(row, context=context)
    row.deleted_at = utc_now()
    row.deleted_by = deleted_by
    row.delete_reason = reason
    row.desired_state = RuntimeChannelDesiredState.DISABLED.value
    row.health_state = HealthState.DOWN.value
    session.flush()
    _sync_channel_trigger_endpoints(session, channel_id)
    return summary


def request_runtime_channel_start(
    session: Session,
    channel_id: str,
    *,
    requested_by: str | None = None,
    reason: str | None = None,
    idempotency_key: str | None = None,
    source_secret_root: Path | None = None,
    command_payload: dict[str, Any] | None = None,
) -> tuple[RuntimeChannelSummary, Any]:
    repair_channel_source_inline_credentials(
        session,
        channel_id=channel_id,
        source_secret_root=source_secret_root,
    )
    row = _get_live_channel(session, channel_id)
    _ensure_startable_channel_pipeline(session, row)
    command, row = create_runtime_control_command(
        session,
        channel_id=channel_id,
        command=RuntimeControlCommand.START,
        requested_by=requested_by,
        reason=reason,
        idempotency_key=idempotency_key,
        payload=command_payload,
    )
    return _channel_summary(row, context=_load_channel_context(session, [row])), runtime_control_command_to_api(command)


def request_runtime_channel_stop(
    session: Session,
    channel_id: str,
    *,
    requested_by: str | None = None,
    reason: str | None = None,
    idempotency_key: str | None = None,
    command_payload: dict[str, Any] | None = None,
) -> tuple[RuntimeChannelSummary, Any]:
    command, row = create_runtime_control_command(
        session,
        channel_id=channel_id,
        command=RuntimeControlCommand.STOP,
        requested_by=requested_by,
        reason=reason,
        idempotency_key=idempotency_key,
        payload=command_payload,
    )
    return _channel_summary(row, context=_load_channel_context(session, [row])), runtime_control_command_to_api(command)


def request_runtime_channel_restart(
    session: Session,
    channel_id: str,
    *,
    requested_by: str | None = None,
    reason: str | None = None,
    idempotency_key: str | None = None,
    source_secret_root: Path | None = None,
    command_payload: dict[str, Any] | None = None,
) -> tuple[RuntimeChannelSummary, Any]:
    repair_channel_source_inline_credentials(
        session,
        channel_id=channel_id,
        source_secret_root=source_secret_root,
    )
    row = _get_live_channel(session, channel_id)
    _ensure_startable_channel_pipeline(session, row)
    command, row = create_runtime_control_command(
        session,
        channel_id=channel_id,
        command=RuntimeControlCommand.RESTART,
        requested_by=requested_by,
        reason=reason,
        idempotency_key=idempotency_key,
        payload=command_payload,
    )
    return _channel_summary(row, context=_load_channel_context(session, [row])), runtime_control_command_to_api(command)


def build_runtime_channel_status_overview(
    session: Session,
    *,
    meta: ResponseMeta,
) -> RuntimeChannelStatusOverview:
    items = list_runtime_channels(session)
    status_counts = Counter(item.status.value for item in items)
    desired_counts = Counter(item.desired_state.value for item in items)
    counts = RuntimeChannelStatusCounts(
        total=len(items),
        running=status_counts[RuntimeObservedState.RUNNING.value],
        stopped=status_counts[RuntimeObservedState.STOPPED.value],
        degraded=status_counts[RuntimeObservedState.DEGRADED.value],
        failed=status_counts[RuntimeObservedState.FAILED.value],
        draft=desired_counts[RuntimeChannelDesiredState.DRAFT.value],
        start_requested=desired_counts[RuntimeChannelDesiredState.START_REQUESTED.value],
        stop_requested=desired_counts[RuntimeChannelDesiredState.STOP_REQUESTED.value],
    )
    return RuntimeChannelStatusOverview(
        counts=counts,
        health=[item.health for item in items],
        technical_places=sorted({item.technical_place for item in items}),
        source_types=sorted({item.source_type for item in items}, key=lambda item: item.value),
        modes=sorted({item.mode for item in items}, key=lambda item: item.value),
        meta=meta,
    )


def _get_live_channel(session: Session, channel_id: str) -> RuntimeChannel:
    row = session.get(RuntimeChannel, channel_id)
    if row is None or row.deleted_at is not None:
        raise ValueError("Channel does not exist.")
    return row


def _runtime_binding_snapshot(session: Session, row: RuntimeChannel) -> dict[str, Any]:
    source = session.get(RuntimeSource, row.source_id) if row.source_id else None
    version = session.get(PipelineVersion, row.pipeline_version_id) if row.pipeline_version_id else None
    return {
        "mode": row.mode,
        "pipeline_version_id": row.pipeline_version_id,
        "pipeline_version_mode": version.mode if version is not None else None,
        "source_id": row.source_id,
        "source_type": source.source_type if source is not None else None,
        "source_config": dict(source.config or {}) if source is not None else {},
        "source_secret_refs": dict(source.secret_refs or {}) if source is not None else {},
        "result_history_policy": dict(row.result_history_policy or {}),
        "execution_target": row.execution_target,
        "edge_device_id": row.edge_device_id,
    }


def _requires_runtime_restart_after_update(
    *,
    previous_runtime_binding: dict[str, Any],
    current_runtime_binding: dict[str, Any],
    credentials_supplied: bool,
    previous_desired_state: RuntimeChannelDesiredState,
    current_desired_state: RuntimeChannelDesiredState,
    previous_observed_state: RuntimeObservedState,
) -> bool:
    if current_desired_state in {
        RuntimeChannelDesiredState.DRAFT,
        RuntimeChannelDesiredState.STOPPED,
        RuntimeChannelDesiredState.STOP_REQUESTED,
        RuntimeChannelDesiredState.DISABLED,
    }:
        return False
    if previous_desired_state == RuntimeChannelDesiredState.START_REQUESTED:
        return False
    current_pipeline_mode = current_runtime_binding.get("pipeline_version_mode")
    if current_pipeline_mode and current_pipeline_mode != current_runtime_binding.get("mode"):
        return False
    was_runtime_active = previous_desired_state == RuntimeChannelDesiredState.RUNNING or previous_observed_state in {
        RuntimeObservedState.STARTING,
        RuntimeObservedState.VALIDATING,
        RuntimeObservedState.LOADING_PIPELINE,
        RuntimeObservedState.RUNNING,
        RuntimeObservedState.DEGRADED,
        RuntimeObservedState.RECOVERING,
    }
    if not was_runtime_active:
        return False
    return credentials_supplied or previous_runtime_binding != current_runtime_binding


def _load_channel_context(session: Session, rows: list[RuntimeChannel]) -> dict[str, Any]:
    source_ids = {row.source_id for row in rows if row.source_id}
    sources = {
        row.id: row
        for row in session.scalars(
            select(RuntimeSource).where(RuntimeSource.id.in_(source_ids))
        ).all()
    } if source_ids else {}

    channel_ids = [row.id for row in rows]
    executions: dict[str, RuntimeExecution] = {}
    results: dict[str, ResultRecord] = {}
    if channel_ids:
        active_execution_ids = {
            row.active_runtime_execution_id for row in rows if row.active_runtime_execution_id
        }
        active_executions = {
            execution.id: execution
            for execution in session.scalars(
                select(RuntimeExecution).where(RuntimeExecution.id.in_(active_execution_ids))
            ).all()
        } if active_execution_ids else {}
        latest_execution_ids = (
            select(
                RuntimeExecution.id.label("id"),
                func.row_number()
                .over(
                    partition_by=RuntimeExecution.channel_id,
                    order_by=(RuntimeExecution.created_at.desc(), RuntimeExecution.id.desc()),
                )
                .label("row_rank"),
            )
            .where(RuntimeExecution.channel_id.in_(channel_ids))
            .subquery()
        )
        latest_executions: dict[str, RuntimeExecution] = {}
        for execution in session.scalars(
            select(RuntimeExecution)
            .join(latest_execution_ids, RuntimeExecution.id == latest_execution_ids.c.id)
            .where(latest_execution_ids.c.row_rank == 1)
        ).all():
            latest_executions[execution.channel_id] = execution
        for row in rows:
            active_execution = active_executions.get(row.active_runtime_execution_id or "")
            if (
                active_execution is not None
                and active_execution.status not in _TERMINAL_RUNTIME_EXECUTION_STATUSES
            ):
                executions[row.id] = active_execution
                continue
            latest_execution = latest_executions.get(row.id)
            if latest_execution is not None:
                executions[row.id] = latest_execution

        results.update(_latest_result_summaries_for_channels(session, channel_ids))

    pipeline_version_ids = {row.pipeline_version_id for row in rows if row.pipeline_version_id}
    pipeline_labels = _pipeline_labels(session, pipeline_version_ids)
    capture_counts = _capture_counts(session, channel_ids)
    output_counts = _output_counts(session, channel_ids)
    edge_device_ids = {row.edge_device_id for row in rows if row.edge_device_id}
    edge_devices = (
        {
            device.id: device
            for device in session.scalars(
                select(EdgeDevice).where(EdgeDevice.id.in_(edge_device_ids))
            ).all()
        }
        if edge_device_ids
        else {}
    )
    edge_deployments: dict[str, EdgeDeploymentAttemptRecord] = {}
    if channel_ids:
        for channel_id in channel_ids:
            attempt = session.scalar(
                select(EdgeDeploymentAttemptRecord)
                .where(EdgeDeploymentAttemptRecord.channel_id == channel_id)
                .order_by(EdgeDeploymentAttemptRecord.created_at.desc())
                .limit(1)
            )
            if attempt is not None:
                edge_deployments[channel_id] = attempt
    return {
        "sources": sources,
        "executions": executions,
        "results": results,
        "pipeline_labels": pipeline_labels,
        "capture_counts": capture_counts,
        "output_counts": output_counts,
        "edge_devices": edge_devices,
        "edge_deployments": edge_deployments,
    }


def _latest_result_summaries_for_channels(
    session: Session,
    channel_ids: list[str],
) -> dict[str, ResultRecord]:
    results: dict[str, ResultRecord] = {}
    for channel_id in channel_ids:
        result = session.scalar(
            select(ResultRecord)
            .options(
                load_only(
                    ResultRecord.id,
                    ResultRecord.channel_id,
                    ResultRecord.pipeline_version_id,
                    ResultRecord.completed_at,
                    ResultRecord.status,
                    ResultRecord.mode,
                    ResultRecord.timing,
                )
            )
            .where(
                ResultRecord.channel_id == channel_id,
                ResultRecord.history_retained.is_(True),
            )
            .order_by(ResultRecord.completed_at.desc(), ResultRecord.id.desc())
            .limit(1)
        )
        if result is not None:
            results[channel_id] = result
    return results


def _channel_summary(row: RuntimeChannel, *, context: dict[str, Any]) -> RuntimeChannelSummary:
    source = context["sources"].get(row.source_id or "")
    source_type = (
        RuntimeSourceType(source.source_type)
        if source is not None
        else RuntimeSourceType.FILE_TEST
    )
    source_config = dict(source.config or {}) if source is not None else {}
    source_summary = _source_summary(source_type, source_config)
    assigned_pipeline = row.assigned_pipeline or context["pipeline_labels"].get(
        row.pipeline_version_id
    )
    capture_summary = _capture_summary(row, context=context)
    output_summary = _output_summary(row, context=context)
    execution = context["executions"].get(row.id)
    result = context["results"].get(row.id)
    runtime_state = _runtime_state_summary(row, execution=execution)
    health = _health_summary(row)
    if _runtime_heartbeat_is_stale(runtime_state):
        health = health.model_copy(
            update={
                "state": HealthState.DEGRADED,
                "status_text": "Runtime heartbeat is stale",
                "last_heartbeat_at": runtime_state.last_heartbeat_at,
                "recovery_hint": (
                    "Restart the channel or realtime executor if the heartbeat does not refresh."
                ),
            }
        )
    performance = _performance_summary(row, execution=execution, result=result)
    observed_state = RuntimeObservedState(row.observed_state)
    health_state = health.state
    edge_device = context["edge_devices"].get(row.edge_device_id or "")
    edge_deployment = context["edge_deployments"].get(row.id)
    placement = RuntimeChannelPlacementSummary(
        target=row.execution_target,
        edge_device_id=row.edge_device_id,
        edge_device_name=edge_device.name if edge_device is not None else None,
        edge_management_ip=edge_device.management_ip if edge_device is not None else None,
        edge_connectivity_state=(
            edge_device.connectivity_state if edge_device is not None else None
        ),
        workload_release_id=(
            edge_deployment.workload_release_id if edge_deployment is not None else None
        ),
        edge_deployment_id=edge_deployment.id if edge_deployment is not None else None,
        edge_deployment_status=(
            edge_deployment.status if edge_deployment is not None else None
        ),
        edge_admission_status=(
            "rejected"
            if edge_deployment is not None and edge_deployment.status == "admission_failed"
            else "admitted"
            if edge_deployment is not None
            and edge_deployment.status
            in {"transferring", "preparing", "activating", "running", "stopped"}
            else None
        ),
    )
    return RuntimeChannelSummary(
        channel_id=row.id,
        slug=row.slug,
        name=row.name,
        technical_place=row.technical_place,
        facility_id=row.facility_id,
        mode=RuntimeMode(row.mode),
        source_type=source_type,
        source_summary=source_summary,
        assigned_pipeline=assigned_pipeline,
        output_summary=output_summary,
        capture_summary=capture_summary,
        health=health,
        performance=performance,
        runtime_state=runtime_state,
        source_id=row.source_id,
        pipeline_version_id=row.pipeline_version_id,
        authoring_label_project_ids=list(row.authoring_label_project_ids or []),
        result_history_policy=dict(row.result_history_policy or {}),
        stem_rotation_priority_control=RuntimeStemRotationPriorityControl.model_validate(
            row.stem_rotation_priority_control or default_stem_rotation_priority_control()
        ),
        placement=placement,
        desired_state=RuntimeChannelDesiredState(row.desired_state),
        observed_state=observed_state,
        status=observed_state,
        health_state=health_state,
        last_activity_at=_iso(row.last_activity_at),
        last_error=row.last_error,
    )


def _channel_request_endpoints(
    session: Session,
    row: RuntimeChannel,
    *,
    triggerable_pipelines: list[RuntimeChannelTriggerablePipelineLink],
    result: ResultRecord | None,
) -> list[RuntimeChannelRequestEndpoint]:
    endpoints: list[RuntimeChannelRequestEndpoint] = []
    pipeline_mode = _pipeline_version_mode(session, row.pipeline_version_id)
    is_trigger_pipeline = pipeline_mode == RuntimeMode.TRIGGER
    if not is_trigger_pipeline:
        has_result_output = _pipeline_has_result_output(session, row.pipeline_version_id)
        history_enabled = bool((row.result_history_policy or {}).get("enabled"))
        live_available = bool(row.pipeline_version_id) and has_result_output and history_enabled
        if not row.pipeline_version_id:
            unavailable_reason = (
                "Dem Kanal ist keine veroeffentlichte Live-Pipeline zugewiesen."
            )
        elif not has_result_output and not history_enabled:
            unavailable_reason = (
                "Die Live-Pipeline benoetigt einen Result Output und fuer den Kanal muss die "
                "Ergebnis-Historie aktiviert sein."
            )
        elif not has_result_output:
            unavailable_reason = (
                "Die Live-Pipeline benoetigt einen Result Output, bevor Ergebnisse abgefragt "
                "werden koennen."
            )
        elif not history_enabled:
            unavailable_reason = (
                "Die Ergebnis-Historie ist fuer diesen Kanal deaktiviert."
            )
        else:
            unavailable_reason = None
        latest_result_current = (
            result is not None and result.pipeline_version_id == row.pipeline_version_id
        )
        endpoints.append(
            RuntimeChannelRequestEndpoint(
                kind="latest_result",
                label="Aktuelles Live-Ergebnis abfragen",
                description=(
                    "Der Live-Kanal laeuft dauerhaft; dieser GET-Request liefert das zuletzt "
                    "persistierte Runtime-Pipeline-Ergebnis."
                ),
                method="GET",
                path=f"/api/v1/runtime/channels/{row.id}/results/latest",
                available=live_available,
                unavailable_reason=unavailable_reason,
                auth_required=False,
                auth_label="runtime.results.read",
                pipeline_version_id=row.pipeline_version_id,
                pipeline_label=_pipeline_label(session, pipeline_version_id=row.pipeline_version_id),
                status=RuntimeResourceStatus.ACTIVE
                if latest_result_current
                else RuntimeResourceStatus.DEGRADED,
                last_activity_at=_iso(result.completed_at) if result is not None else None,
            )
        )

    for trigger in triggerable_pipelines:
        endpoints.append(
            RuntimeChannelRequestEndpoint(
                kind="trigger_execution",
                label="HTTP Trigger ausloesen",
                description=(
                    trigger.description
                    or "Dieser PUT-Request nimmt bei Eingang ein Bild auf und fuehrt die Trigger-Pipeline synchron aus."
                ),
                method=trigger.method,
                path=trigger.path,
                available=bool(trigger.enabled) and trigger.status == RuntimeResourceStatus.ACTIVE,
                unavailable_reason=None
                if bool(trigger.enabled) and trigger.status == RuntimeResourceStatus.ACTIVE
                else "Der Trigger-Endpunkt ist noch nicht bereit oder deaktiviert.",
                auth_required=bool(trigger.auth_required),
                auth_label=trigger.auth_scope or "runtime.trigger.invoke",
                trigger_endpoint_id=trigger.trigger_endpoint_id,
                pipeline_version_id=trigger.pipeline_version_id,
                pipeline_label=trigger.pipeline_label,
                status=trigger.status,
                last_activity_at=trigger.last_invoked_at,
            )
        )
    return endpoints


def _pipeline_version_mode(session: Session, pipeline_version_id: str | None) -> RuntimeMode | None:
    if not pipeline_version_id:
        return None
    version = session.get(PipelineVersion, pipeline_version_id)
    if version is None:
        return None
    return RuntimeMode(version.mode)


def _pipeline_has_result_output(session: Session, pipeline_version_id: str | None) -> bool:
    if not pipeline_version_id:
        return False
    version = session.get(PipelineVersion, pipeline_version_id)
    if version is None or not isinstance(version.graph, dict):
        return False
    nodes = version.graph.get("nodes")
    if not isinstance(nodes, list):
        return False
    return any(
        isinstance(node, dict)
        and node.get("node_type") in {"output.result", "output.persisted_result"}
        for node in nodes
    )


def _runtime_state_summary(
    row: RuntimeChannel,
    *,
    execution: RuntimeExecution | None,
) -> RuntimeChannelRuntimeState:
    actual_state = RuntimeObservedState(row.observed_state)
    loading_state = "not_started"
    runtime_instance_id = None
    last_heartbeat_at = None
    last_start_at = None
    last_stop_at = None
    assigned_runtime_worker = None
    health_summary: dict[str, Any] = dict(row.health_summary or {})
    metrics_summary: dict[str, Any] = dict(row.performance_summary or {})
    pipeline_version_id = row.pipeline_version_id
    last_error = row.last_error
    heartbeat_age_seconds = None
    recovery_attempts = 0
    recovery_hint = _runtime_recovery_hint(row, execution=execution)
    if execution is not None:
        runtime_instance_id = execution.id
        actual_state = RuntimeObservedState(getattr(execution, "actual_state", None) or row.observed_state)
        loading_state = str(getattr(execution, "loading_state", None) or "not_started")
        last_heartbeat_at = _iso(execution.heartbeat_at)
        heartbeat_at = _aware_datetime(execution.heartbeat_at)
        if heartbeat_at is not None:
            heartbeat_age_seconds = max(int((utc_now() - heartbeat_at).total_seconds()), 0)
        last_start_at = _iso(execution.started_at)
        last_stop_at = _iso(execution.stopped_at)
        assigned_runtime_worker = execution.assigned_runtime_worker or execution.executor_instance_id
        health_summary = dict(execution.health_summary or health_summary)
        metrics_summary = dict(execution.metrics_summary or metrics_summary)
        pipeline_version_id = execution.pipeline_version_id or pipeline_version_id
        last_error = execution.last_error or last_error
        recovery_attempts = int(execution.recovery_attempts or 0)
        recovery_hint = _runtime_recovery_hint(row, execution=execution)
    channel_desired_state = RuntimeChannelDesiredState(row.desired_state)
    if channel_desired_state in _CHANNEL_STATE_OWNS_RUNTIME_SUMMARY:
        actual_state = RuntimeObservedState(row.observed_state)
        loading_state = {
            RuntimeChannelDesiredState.DRAFT: "not_started",
            RuntimeChannelDesiredState.STOPPED: "stopped",
            RuntimeChannelDesiredState.START_REQUESTED: "starting",
            RuntimeChannelDesiredState.STOP_REQUESTED: "stopping",
            RuntimeChannelDesiredState.DISABLED: "disabled",
        }[channel_desired_state]
        health_summary = dict(row.health_summary or health_summary)
        metrics_summary = dict(row.performance_summary or metrics_summary)
        last_error = row.last_error
        recovery_hint = _runtime_recovery_hint(row, execution=None)
    return RuntimeChannelRuntimeState(
        channel_id=row.id,
        runtime_instance_id=runtime_instance_id,
        execution_mode=RuntimeMode(row.mode),
        desired_state=RuntimeChannelDesiredState(row.desired_state),
        actual_state=actual_state,
        loading_state=loading_state,
        last_heartbeat_at=last_heartbeat_at,
        last_error=last_error,
        last_start_at=last_start_at,
        last_stop_at=last_stop_at,
        assigned_pipeline_version_id=pipeline_version_id,
        assigned_runtime_worker=assigned_runtime_worker,
        heartbeat_age_seconds=heartbeat_age_seconds,
        recovery_attempts=recovery_attempts,
        recovery_hint=recovery_hint,
        health_summary=health_summary,
        metrics_summary=metrics_summary,
    )


def _runtime_heartbeat_is_stale(runtime_state: RuntimeChannelRuntimeState) -> bool:
    if runtime_state.actual_state != RuntimeObservedState.RUNNING:
        return False
    heartbeat_age_seconds = runtime_state.heartbeat_age_seconds
    if heartbeat_age_seconds is None:
        return False
    return heartbeat_age_seconds > _RUNTIME_HEARTBEAT_STALE_AFTER_SECONDS


def _validated_authoring_label_project_ids(
    session: Session,
    label_project_ids: list[str] | None,
) -> list[str]:
    ids: list[str] = []
    for raw in label_project_ids or []:
        value = str(raw).strip()
        if value and value not in ids:
            ids.append(value)
    if not ids:
        return []
    rows = {
        row.id
        for row in session.scalars(
            select(LabelProject).where(
                LabelProject.id.in_(ids),
                LabelProject.deleted_at.is_(None),
            )
        ).all()
    }
    missing = [item for item in ids if item not in rows]
    if missing:
        raise ValueError(f"Authoring label project does not exist: {', '.join(missing)}.")
    return ids


def _channel_authoring_label_projects(
    session: Session,
    label_project_ids: list[str],
) -> list[RuntimeChannelAuthoringLabelProject]:
    ids = _validated_authoring_label_project_ids(session, label_project_ids)
    if not ids:
        return []
    rows = session.scalars(
        select(LabelProject).where(LabelProject.id.in_(ids), LabelProject.deleted_at.is_(None))
    ).all()
    by_id = {row.id: row for row in rows}
    return [
        _authoring_label_project(row)
        for label_project_id in ids
        if (row := by_id.get(label_project_id)) is not None
    ]


def _authoring_label_project(row: LabelProject) -> RuntimeChannelAuthoringLabelProject:
    labels = set(_declared_labels_from_config(row.label_config or {}))
    labels.update(str(item).strip() for item in (row.required_classes or []) if str(item).strip())
    labels.update(str(item).strip() for item in (row.class_distribution or {}).keys() if str(item).strip())
    dataset_name = None
    session = object_session(row)
    if session is not None and row.dataset_id:
        dataset = session.get(Dataset, row.dataset_id)
        dataset_name = dataset.name if dataset is not None else None
    return RuntimeChannelAuthoringLabelProject(
        label_project_id=row.id,
        label_studio_project_id=row.label_studio_project_id,
        title=row.title,
        task_type=row.task_type,
        dataset_name=dataset_name,
        labels=sorted(labels, key=lambda item: item.casefold()),
    )


def _declared_labels_from_config(label_config: Any) -> list[str]:
    labels: set[str] = set()

    def visit(value: Any, key: str | None = None) -> None:
        if isinstance(value, str):
            if key == "raw":
                for match in re.finditer(
                    r"<(?:Choice|Label)\b[^>]*\bvalue=(['\"])(.*?)\1",
                    value,
                    flags=re.IGNORECASE,
                ):
                    if match.group(2):
                        labels.add(match.group(2))
            return
        if isinstance(value, list):
            for item in value:
                visit(item, key)
            return
        if not isinstance(value, dict):
            return
        raw_value = value.get("value")
        if isinstance(raw_value, str) and key in {"choices", "labels", None}:
            labels.add(raw_value)
        for child_key, child_value in value.items():
            if child_key in {"choices", "labels"} and isinstance(child_value, str):
                labels.add(child_value)
                continue
            if child_key in {"choices_attrs", "labels_attrs"} and isinstance(child_value, dict):
                labels.update(str(label).strip() for label in child_value if str(label).strip())
            visit(child_value, child_key)

    visit(label_config)
    return sorted(labels, key=lambda item: item.casefold())


def _resolve_or_create_source(
    session: Session,
    *,
    source_id: str | None,
    source_type: RuntimeSourceType,
    source_config: dict[str, Any],
    source_credentials: RuntimeSourceCredentialInput | None,
    source_secret_root: Path | None,
    channel_slug: str,
    channel_name: str,
) -> RuntimeSource:
    if source_id:
        source = session.get(RuntimeSource, source_id)
        if source is None:
            raise ValueError("Source does not exist.")
        return source
    generated_source_id = generate_prefixed_id("src")
    source_config, source_secret_refs = _prepare_source_config_and_secret_refs(
        source_type,
        source_config,
        source_credentials=source_credentials,
        existing_secret_refs={},
        source_id=generated_source_id,
        source_secret_root=source_secret_root,
    )
    source = RuntimeSource(
        id=generated_source_id,
        slug=_unique_slug(session, RuntimeSource, f"{channel_slug}-source"),
        name=f"{channel_name} source",
        source_type=source_type.value,
        config=source_config,
        secret_refs=source_secret_refs,
        validation_status="active",
        health_state=HealthState.DOWN.value,
    )
    session.add(source)
    session.flush()
    return source


def repair_channel_source_inline_credentials(
    session: Session,
    *,
    channel_id: str,
    source_secret_root: Path | None,
) -> RuntimeSource | None:
    row = session.get(RuntimeChannel, channel_id)
    if row is None or row.source_id is None:
        return None
    source = session.get(RuntimeSource, row.source_id)
    if source is None or RuntimeSourceType(source.source_type) != RuntimeSourceType.RTSP:
        return source
    config, secret_refs = _prepare_source_config_and_secret_refs(
        RuntimeSourceType.RTSP,
        dict(source.config or {}),
        source_credentials=None,
        existing_secret_refs=dict(source.secret_refs or {}),
        source_id=source.id,
        source_secret_root=source_secret_root,
    )
    source.config = config
    source.secret_refs = secret_refs
    session.flush()
    return source


def _prepare_source_config_and_secret_refs(
    source_type: RuntimeSourceType,
    source_config: dict[str, Any],
    *,
    source_credentials: RuntimeSourceCredentialInput | None,
    existing_secret_refs: dict[str, str],
    source_id: str,
    source_secret_root: Path | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    prepared_config = _normalize_source_tuning(dict(source_config or {}))
    secret_refs = dict(existing_secret_refs or {})
    inline_credentials: dict[str, str] = {}
    if source_type == RuntimeSourceType.RTSP:
        prepared_config, inline_credentials = _normalize_rtsp_config(prepared_config)
    if source_type == RuntimeSourceType.USB_OPENCV:
        prepared_config = _normalize_usb_opencv_config(prepared_config)
    if source_type == RuntimeSourceType.ARAVIS_GIGE:
        prepared_config = _normalize_aravis_gige_config(prepared_config, source_id=source_id)
    if source_type == RuntimeSourceType.FLIR_BLACKFLY:
        prepared_config = _normalize_flir_blackfly_config(prepared_config)
    if source_type == RuntimeSourceType.BASLER_PYLON:
        prepared_config = _normalize_basler_pylon_config(
            prepared_config,
            source_id=source_id,
        )
    explicit_credentials = _credential_payload(source_credentials)
    username = explicit_credentials.get("username") or inline_credentials.get("username")
    password = explicit_credentials.get("password") or inline_credentials.get("password")
    if username or password:
        if not username or not password:
            raise ValueError("RTSP credentials require both username and password.")
        if source_secret_root is None:
            raise ValueError("RTSP credentials require managed source secret storage.")
        secret_refs.update(
            _write_rtsp_secret_refs(
                source_secret_root=source_secret_root,
                source_id=source_id,
                username=username,
                password=password,
            )
        )
    _validate_source_config(source_type, prepared_config)
    return prepared_config, secret_refs


def _normalize_source_tuning(config: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(config)
    if "max_fps" in normalized:
        value = normalized.get("max_fps")
        if value is None or value == "":
            normalized.pop("max_fps", None)
        else:
            try:
                max_fps = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("Source max_fps must be a number between 0.1 and 120.") from exc
            if max_fps < 0.1 or max_fps > 120:
                raise ValueError("Source max_fps must be between 0.1 and 120.")
            normalized["max_fps"] = max_fps
    if "sample_every_n" in normalized:
        value = normalized.get("sample_every_n")
        if value is None or value == "":
            normalized.pop("sample_every_n", None)
        else:
            try:
                sample_every = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("Source sample_every_n must be an integer between 1 and 1000.") from exc
            if isinstance(value, float) and not value.is_integer():
                raise ValueError("Source sample_every_n must be an integer between 1 and 1000.")
            if sample_every < 1 or sample_every > 1000:
                raise ValueError("Source sample_every_n must be between 1 and 1000.")
            normalized["sample_every_n"] = sample_every
    if "pipeline_max_inflight" in normalized:
        value = normalized.get("pipeline_max_inflight")
        if value is None or value == "":
            normalized.pop("pipeline_max_inflight", None)
        else:
            try:
                max_inflight = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Source pipeline_max_inflight must be an integer between 1 and 16."
                ) from exc
            if isinstance(value, float) and not value.is_integer():
                raise ValueError(
                    "Source pipeline_max_inflight must be an integer between 1 and 16."
                )
            if max_inflight < 1 or max_inflight > 16:
                raise ValueError(
                    "Source pipeline_max_inflight must be between 1 and 16."
                )
            normalized["pipeline_max_inflight"] = max_inflight
    return normalized


def _credential_payload(
    source_credentials: RuntimeSourceCredentialInput | None,
) -> dict[str, str]:
    if source_credentials is None:
        return {}
    payload = source_credentials.model_dump(exclude_none=True)
    return {key: str(value).strip() for key, value in payload.items() if str(value).strip()}


def _normalize_rtsp_config(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    raw_url = str(config.get("url") or "").strip()
    if not raw_url:
        return config, {}
    parsed = urlsplit(raw_url)
    if parsed.scheme.lower() not in {"rtsp", "rtsps"}:
        return config, {}
    if not parsed.username and not parsed.password and "@" not in parsed.netloc:
        return config, {}
    if not parsed.hostname:
        raise ValueError("RTSP URL must include a camera host.")
    credentials: dict[str, str] = {}
    if parsed.username:
        credentials["username"] = unquote(parsed.username)
    if parsed.password:
        credentials["password"] = unquote(parsed.password)
    path = parsed.path.lstrip("/")
    if parsed.query:
        path = f"{path}?{parsed.query}" if path else f"?{parsed.query}"
    normalized: dict[str, Any] = {
        key: value
        for key, value in config.items()
        if key not in {"url", "username", "password"}
    }
    normalized.update(
        {
            "scheme": parsed.scheme.lower(),
            "host": parsed.hostname,
            "port": parsed.port or (322 if parsed.scheme.lower() == "rtsps" else 554),
            "path": path,
        }
    )
    return normalized, credentials


def _normalize_usb_opencv_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(config)
    raw_index = normalized.get("device_index")
    raw_path = normalized.get("device_path")
    legacy_device = normalized.pop("device", None)
    legacy_uri = normalized.pop("uri", None)
    if raw_index in (None, "") and raw_path in (None, ""):
        selector = legacy_device if legacy_device not in (None, "") else legacy_uri
        if isinstance(selector, str) and not selector.strip().isdigit():
            raw_path = selector
        elif selector not in (None, ""):
            raw_index = selector
    if raw_index not in (None, "") and raw_path not in (None, ""):
        raise ValueError(
            "USB/OpenCV channels require either device_index or device_path, not both."
        )
    if raw_index not in (None, ""):
        if isinstance(raw_index, bool):
            raise ValueError(
                "USB/OpenCV device_index must be an integer between 0 and 255."
            )
        try:
            device_index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "USB/OpenCV device_index must be an integer between 0 and 255."
            ) from exc
        if isinstance(raw_index, float) and not raw_index.is_integer():
            raise ValueError(
                "USB/OpenCV device_index must be an integer between 0 and 255."
            )
        if device_index < 0 or device_index > 255:
            raise ValueError("USB/OpenCV device_index must be between 0 and 255.")
        normalized["device_index"] = device_index
        normalized.pop("device_path", None)
    elif raw_path not in (None, ""):
        device_path = str(raw_path).strip()
        if not re.fullmatch(
            r"/dev/(?:video\d+|v4l/(?:by-id|by-path)/[^/]+)", device_path
        ):
            raise ValueError(
                "USB/OpenCV device_path must identify /dev/videoN or a stable "
                "/dev/v4l/by-id|by-path device."
            )
        normalized["device_path"] = device_path
        normalized.pop("device_index", None)

    backend = str(normalized.get("backend") or "auto").strip().lower()
    if backend not in _USB_OPENCV_BACKENDS:
        raise ValueError("USB/OpenCV backend must be auto, v4l2, dshow, or msmf.")
    normalized["backend"] = backend

    fourcc = str(normalized.get("fourcc") or "").strip().upper()
    if fourcc:
        if not re.fullmatch(r"[A-Z0-9]{4}", fourcc):
            raise ValueError("USB/OpenCV fourcc must contain exactly four letters or digits.")
        normalized["fourcc"] = fourcc
    else:
        normalized.pop("fourcc", None)

    for key in ("width", "height", "buffer_size"):
        if key not in normalized or normalized.get(key) in (None, ""):
            normalized.pop(key, None)
            continue
        if isinstance(normalized[key], bool):
            raise ValueError(f"USB/OpenCV {key} must be an integer.")
        try:
            numeric_value = int(normalized[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"USB/OpenCV {key} must be an integer.") from exc
        if isinstance(normalized[key], float) and not normalized[key].is_integer():
            raise ValueError(f"USB/OpenCV {key} must be an integer.")
        minimum, maximum = (16, 16384) if key != "buffer_size" else (1, 32)
        if numeric_value < minimum or numeric_value > maximum:
            raise ValueError(
                f"USB/OpenCV {key} must be between {minimum} and {maximum}."
            )
        normalized[key] = numeric_value

    for key in ("fps", "read_timeout_seconds", "reconnect_seconds"):
        if key not in normalized or normalized.get(key) in (None, ""):
            normalized.pop(key, None)
            continue
        if isinstance(normalized[key], bool):
            raise ValueError(f"USB/OpenCV {key} must be a positive number.")
        try:
            numeric_value = float(normalized[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"USB/OpenCV {key} must be a positive number.") from exc
        maximum = 240.0 if key == "fps" else 300.0
        if not math.isfinite(numeric_value) or numeric_value <= 0 or numeric_value > maximum:
            raise ValueError(
                f"USB/OpenCV {key} must be greater than 0 and at most {maximum:g}."
            )
        normalized[key] = numeric_value
    return normalized


def _normalize_flir_blackfly_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(config)
    for key in (
        "serial_number",
        "user_defined_name",
        "device_id",
        "id_",
        "ip_address",
        "cti_path",
        "pixel_format",
        "acquisition_mode",
    ):
        if key not in normalized:
            continue
        value = str(normalized.get(key) or "").strip()
        if value:
            normalized[key] = value
        else:
            normalized.pop(key, None)
    if normalized.get("device_id") and not normalized.get("id_"):
        normalized["id_"] = normalized["device_id"]
    if "cti_paths" in normalized:
        raw_paths = normalized.get("cti_paths") or []
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        if not isinstance(raw_paths, list):
            raise ValueError("FLIR Blackfly cti_paths must be a list of CTI paths.")
        paths = [str(path).strip() for path in raw_paths if str(path).strip()]
        if paths:
            normalized["cti_paths"] = paths
        else:
            normalized.pop("cti_paths", None)
    for key in ("fetch_timeout_seconds", "reconnect_seconds", "exposure_time_us", "gain_db"):
        if key not in normalized:
            continue
        value = normalized.get(key)
        if value is None or value == "":
            normalized.pop(key, None)
            continue
        try:
            numeric_value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"FLIR Blackfly {key} must be a positive number.") from exc
        if numeric_value <= 0:
            raise ValueError(f"FLIR Blackfly {key} must be a positive number.")
        normalized[key] = numeric_value
    features = normalized.get("features")
    if features is not None and not isinstance(features, dict):
        raise ValueError("FLIR Blackfly features must be an object of GenICam node values.")
    return normalized


def _normalize_basler_pylon_config(
    config: dict[str, Any],
    *,
    source_id: str,
) -> dict[str, Any]:
    normalized = dict(config)
    for key in (
        *_BASLER_IDENTIFIER_KEYS,
        "pixel_format",
        "acquisition_mode",
        "grab_strategy",
        "trigger_mode",
        "trigger_source",
        "trigger_activation",
        "exposure_auto",
        "gain_auto",
        "transport_mode",
        "conversion_mode",
        "conversion_pipeline",
        "source_caps",
        "src_properties",
        "bridge_name",
        "rtsp_mount",
    ):
        if key not in normalized:
            continue
        value = str(normalized.get(key) or "").strip()
        if value:
            normalized[key] = value
        else:
            normalized.pop(key, None)
    if normalized.get("device_id") and not normalized.get("id_"):
        normalized["id_"] = normalized["device_id"]
    if normalized.get("camera") and not normalized.get("camera_name"):
        normalized["camera_name"] = normalized["camera"]
    normalized["acquisition_mode"] = str(normalized.get("acquisition_mode") or "Continuous")
    normalized["grab_strategy"] = str(normalized.get("grab_strategy") or "LatestImageOnly")
    normalized["pixel_format"] = str(normalized.get("pixel_format") or "BayerRG8")
    normalized["trigger_mode"] = str(normalized.get("trigger_mode") or "Off")
    transport_mode = (
        str(normalized.get("transport_mode") or "auto")
        .strip()
        .lower()
        .replace("-", "_")
    )
    if transport_mode in {"direct", "pylon"}:
        transport_mode = "pylon_direct"
    if transport_mode in {"bridge", "aravis"}:
        transport_mode = "aravis_rtsp_bridge"
    if transport_mode not in _BASLER_TRANSPORT_MODES:
        raise ValueError(
            "Basler Pylon transport_mode must be auto, pylon_direct, or aravis_rtsp_bridge."
        )
    normalized["transport_mode"] = transport_mode
    normalized["conversion_mode"] = str(
        normalized.get("conversion_mode")
        or _default_aravis_conversion_mode(normalized["pixel_format"])
    )
    if "frame_rate" in normalized and "fps" not in normalized:
        normalized["fps"] = normalized.pop("frame_rate")
    if normalized.get("inter_packet_delay") is not None and normalized.get("packet_delay") is None:
        normalized["packet_delay"] = normalized["inter_packet_delay"]

    if transport_mode in {"auto", "aravis_rtsp_bridge"}:
        normalized["vendor"] = str(normalized.get("vendor") or "basler_pylon")
        normalized["bridge_name"] = _aravis_bridge_name(
            str(normalized.get("bridge_name") or f"basler-{source_id.removeprefix('src_')}")
        )
        normalized["rtsp_host"] = _ARAVIS_DEFAULT_RTSP_HOST
        normalized["rtsp_address"] = _ARAVIS_DEFAULT_RTSP_HOST
        normalized["rtsp_port"] = _normalize_aravis_rtsp_port(
            normalized.get("rtsp_port"),
            source_id=source_id,
        )
        normalized["rtsp_mount"] = _normalize_aravis_rtsp_mount(
            str(normalized.get("rtsp_mount") or normalized["bridge_name"])
        )
        normalized["url"] = (
            f"rtsp://{normalized['rtsp_host']}:{normalized['rtsp_port']}"
            f"{normalized['rtsp_mount']}"
        )
        normalized["rtsp_url"] = normalized["url"]

    for key in (
        "fetch_timeout_seconds",
        "reconnect_seconds",
        "exposure_time_us",
        "fps",
        "link_speed_mbps",
    ):
        if key not in normalized:
            continue
        value = normalized.get(key)
        if value is None or value == "":
            normalized.pop(key, None)
            continue
        try:
            numeric_value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Basler Pylon {key} must be a positive number.") from exc
        if numeric_value <= 0:
            raise ValueError(f"Basler Pylon {key} must be a positive number.")
        normalized[key] = numeric_value

    if "gain_db" in normalized:
        value = normalized.get("gain_db")
        if value is None or value == "":
            normalized.pop("gain_db", None)
        else:
            try:
                gain_db = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("Basler Pylon gain_db must be a non-negative number.") from exc
            if gain_db < 0:
                raise ValueError("Basler Pylon gain_db must be a non-negative number.")
            normalized["gain_db"] = gain_db

    for key in (
        "width",
        "height",
        "packet_size",
        "buffer_count",
        "output_queue_size",
        "frame_retention_ms",
        "grab_failure_reconnect_threshold",
        "h264_bitrate_kbit",
        "h264_key_int_max",
        "queue_size",
    ):
        if key not in normalized:
            continue
        value = normalized.get(key)
        if value is None or value == "":
            normalized.pop(key, None)
            continue
        try:
            integer_value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Basler Pylon {key} must be a positive integer.") from exc
        if integer_value <= 0:
            raise ValueError(f"Basler Pylon {key} must be a positive integer.")
        normalized[key] = integer_value

    for key in ("offset_x", "offset_y", "inter_packet_delay", "packet_delay"):
        if key not in normalized:
            continue
        value = normalized.get(key)
        if value is None or value == "":
            normalized.pop(key, None)
            continue
        try:
            integer_value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Basler Pylon {key} must be a non-negative integer.") from exc
        if integer_value < 0:
            raise ValueError(f"Basler Pylon {key} must be a non-negative integer.")
        normalized[key] = integer_value

    if "acquisition_frame_rate_enable" in normalized:
        value = normalized.get("acquisition_frame_rate_enable")
        if isinstance(value, bool):
            normalized["acquisition_frame_rate_enable"] = value
        elif isinstance(value, str):
            normalized["acquisition_frame_rate_enable"] = value.strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        else:
            normalized["acquisition_frame_rate_enable"] = bool(value)
    elif normalized.get("fps") is not None:
        normalized["acquisition_frame_rate_enable"] = True

    features = normalized.get("features")
    if features is not None and not isinstance(features, dict):
        raise ValueError("Basler Pylon features must be an object of GenICam node values.")
    return normalized


def _default_aravis_conversion_mode(pixel_format: object) -> str:
    value = str(pixel_format or "").strip().lower()
    if value.startswith("mono"):
        return "mono"
    if "rgb" in value or "bgr" in value or "yuv" in value:
        return "rgb"
    return "bayer"


def _normalize_aravis_gige_config(config: dict[str, Any], *, source_id: str) -> dict[str, Any]:
    normalized = dict(config)
    string_keys = {
        *_ARAVIS_IDENTIFIER_KEYS,
        "vendor",
        "pixel_format",
        "conversion_mode",
        "conversion_pipeline",
        "source_caps",
        "src_properties",
        "bridge_name",
        "rtsp_mount",
    }
    for key in string_keys:
        if key not in normalized:
            continue
        value = str(normalized.get(key) or "").strip()
        if value:
            normalized[key] = value
        else:
            normalized.pop(key, None)

    camera_name = next(
        (
            str(normalized.get(key) or "").strip()
            for key in _ARAVIS_IDENTIFIER_KEYS
            if str(normalized.get(key) or "").strip()
        ),
        "",
    )
    if camera_name:
        normalized["camera_name"] = camera_name
    normalized["vendor"] = str(normalized.get("vendor") or "flir_blackfly")
    normalized["pixel_format"] = str(normalized.get("pixel_format") or "BayerRG8")
    normalized["conversion_mode"] = str(normalized.get("conversion_mode") or "bayer")
    normalized["bridge_name"] = _aravis_bridge_name(
        str(normalized.get("bridge_name") or f"src-{source_id.removeprefix('src_')}")
    )
    normalized["rtsp_host"] = _ARAVIS_DEFAULT_RTSP_HOST
    normalized["rtsp_address"] = _ARAVIS_DEFAULT_RTSP_HOST
    normalized["rtsp_port"] = _normalize_aravis_rtsp_port(
        normalized.get("rtsp_port"),
        source_id=source_id,
    )
    normalized["rtsp_mount"] = _normalize_aravis_rtsp_mount(
        str(normalized.get("rtsp_mount") or normalized["bridge_name"])
    )
    normalized["url"] = (
        f"rtsp://{normalized['rtsp_host']}:{normalized['rtsp_port']}"
        f"{normalized['rtsp_mount']}"
    )
    normalized["rtsp_url"] = normalized["url"]

    for key in (
        "packet_size",
        "packet_delay",
        "h264_bitrate_kbit",
        "h264_key_int_max",
        "queue_size",
    ):
        if key not in normalized:
            continue
        value = normalized.get(key)
        if value is None or value == "":
            normalized.pop(key, None)
            continue
        try:
            integer_value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Aravis GigE {key} must be a positive integer.") from exc
        if integer_value <= 0:
            raise ValueError(f"Aravis GigE {key} must be a positive integer.")
        normalized[key] = integer_value

    normalized.setdefault("packet_size", 1500)
    normalized.setdefault("packet_delay", 1000)
    normalized.setdefault("h264_bitrate_kbit", 6000)
    normalized.setdefault("h264_key_int_max", 30)
    normalized.setdefault("queue_size", 2)
    features = normalized.get("features")
    if features is not None and not isinstance(features, (dict, str)):
        raise ValueError("Aravis GigE features must be an object or feature string.")
    return normalized


def _normalize_aravis_rtsp_port(value: object, *, source_id: str) -> int:
    if value is None or value == "":
        digest = hashlib.sha1(source_id.encode("utf-8")).hexdigest()
        offset = int(digest[:8], 16) % _ARAVIS_DEFAULT_RTSP_PORT_SPAN
        return _ARAVIS_DEFAULT_RTSP_PORT_BASE + offset
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Aravis GigE rtsp_port must be an integer.") from exc
    if port < 1024 or port > 65535:
        raise ValueError("Aravis GigE rtsp_port must be between 1024 and 65535.")
    return port


def _normalize_aravis_rtsp_mount(value: str) -> str:
    mount = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip().lstrip("/")).strip(".-")
    return f"/{mount or 'aravis-gige'}"


def _aravis_bridge_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip(".-")
    return name or "aravis-gige"


def _write_rtsp_secret_refs(
    *,
    source_secret_root: Path,
    source_id: str,
    username: str,
    password: str,
) -> dict[str, str]:
    source_root = source_secret_root / source_id
    source_root.mkdir(parents=True, exist_ok=True)
    username_path = source_root / "username"
    password_path = source_root / "password"
    username_path.write_text(username, encoding="utf-8")
    password_path.write_text(password, encoding="utf-8")
    for path in (username_path, password_path):
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return {
        "username_file": str(username_path),
        "password_file": str(password_path),
    }


def _validate_source_config(
    source_type: RuntimeSourceType,
    source_config: dict[str, Any],
) -> None:
    forbidden_keys = {
        key
        for key in source_config
        if any(secret_hint in key.lower() for secret_hint in ("password", "secret", "token"))
    }
    if forbidden_keys:
        raise ValueError("Source config must not contain inline secrets.")
    if source_type == RuntimeSourceType.RTSP:
        raw_url = str(source_config.get("url") or "")
        if "://" in raw_url and "@" in raw_url.split("://", 1)[1]:
            raise ValueError("RTSP URLs must use secret_refs instead of inline credentials.")
        if not (
            source_config.get("url")
            or source_config.get("host")
            or source_config.get("secret_ref")
        ):
            raise ValueError("RTSP channels require a URL, host, or secret reference.")
    if source_type == RuntimeSourceType.USB_OPENCV:
        if source_config.get("device_index") is None and not source_config.get("device_path"):
            raise ValueError("USB/OpenCV channels require a device_index or device_path.")
    if source_type == RuntimeSourceType.FLIR_BLACKFLY:
        identifiers = (
            source_config.get("serial_number"),
            source_config.get("user_defined_name"),
            source_config.get("device_id"),
            source_config.get("id_"),
            source_config.get("ip_address"),
        )
        if not any(identifiers):
            raise ValueError(
                "FLIR Blackfly channels require a serial_number, user_defined_name, "
                "device_id, id_, or ip_address."
            )
    if source_type == RuntimeSourceType.BASLER_PYLON:
        identifiers = tuple(source_config.get(key) for key in _BASLER_IDENTIFIER_KEYS)
        if not any(identifiers):
            raise ValueError(
                "Basler Pylon channels require a camera_name, serial_number, "
                "user_defined_name, device_id, id_, ip_address, or model."
            )
        trigger_mode = str(source_config.get("trigger_mode") or "Off").strip().lower()
        if trigger_mode not in {"off", "false", "0"} and not source_config.get("trigger_source"):
            raise ValueError("Basler Pylon triggered acquisition requires a trigger_source.")
        _validate_basler_link_budget(source_config)
    if source_type == RuntimeSourceType.ARAVIS_GIGE:
        if not source_config.get("camera_name"):
            raise ValueError(
                "Aravis GigE channels require a camera_name, serial_number, "
                "user_defined_name, device_id, id_, or ip_address."
            )
        if not source_config.get("bridge_name"):
            raise ValueError("Aravis GigE channels require an internal bridge_name.")
        if not source_config.get("rtsp_port") or not source_config.get("rtsp_mount"):
            raise ValueError("Aravis GigE channels require an internal RTSP bridge endpoint.")
    if source_type == RuntimeSourceType.SNAPSHOT:
        raw_url = str(source_config.get("url") or source_config.get("snapshot_url") or "")
        if source_config.get("path"):
            raise ValueError("Snapshot channels must use HTTP(S) snapshot URLs, not server-local paths.")
        if raw_url and not raw_url.lower().startswith(("http://", "https://")):
            raise ValueError("Snapshot channels require an http:// or https:// URL.")
        if "://" in raw_url and "@" in raw_url.split("://", 1)[1]:
            raise ValueError("Snapshot URLs must not contain inline credentials.")
        if not raw_url:
            raise ValueError("Snapshot channels require an HTTP(S) snapshot URL.")
    if source_type == RuntimeSourceType.FILE_TEST and not source_config.get("path"):
        raise ValueError("File/test channels require a path.")


def _validate_basler_link_budget(source_config: dict[str, Any]) -> None:
    link_speed_mbps = source_config.get("link_speed_mbps")
    if link_speed_mbps in (None, ""):
        return
    try:
        width = int(source_config.get("width") or 0)
        height = int(source_config.get("height") or 0)
        fps = float(source_config.get("fps") or 0)
        link_speed = float(link_speed_mbps)
    except (TypeError, ValueError):
        return
    bits_per_pixel = {
        "mono8": 8,
        "bayerrg8": 8,
        "bayerbg8": 8,
        "bayergb8": 8,
        "bayergr8": 8,
        "yuv422packed": 16,
        "rgb8": 24,
        "bgr8": 24,
    }.get(str(source_config.get("pixel_format") or "").strip().casefold())
    if width <= 0 or height <= 0 or fps <= 0 or link_speed <= 0 or bits_per_pixel is None:
        return
    payload_mbps = width * height * fps * bits_per_pixel / 1_000_000
    if payload_mbps > link_speed * 0.8:
        raise ValueError(
            "Basler Pylon image payload exceeds 80% of the configured link_speed_mbps "
            f"({payload_mbps:.0f} Mbit/s payload on {link_speed:.0f} Mbit/s). "
            "Use Bayer/Mono8, a smaller ROI, or a lower camera FPS."
        )


def _source_summary(source_type: RuntimeSourceType, config: dict[str, Any]) -> str:
    if source_type == RuntimeSourceType.RTSP:
        raw = str(config.get("url") or _rtsp_summary_from_parts(config) or "RTSP source")
        return _redact_inline_credentials(raw)
    if source_type == RuntimeSourceType.USB_OPENCV:
        selector = config.get("device_path")
        if selector is None:
            selector = config.get("device_index")
        return f"USB camera {selector}"
    if source_type == RuntimeSourceType.FLIR_BLACKFLY:
        label = (
            config.get("user_defined_name")
            or config.get("serial_number")
            or config.get("ip_address")
            or config.get("device_id")
            or config.get("id_")
        )
        return f"FLIR Blackfly{f' {label}' if label else ''}"
    if source_type == RuntimeSourceType.BASLER_PYLON:
        label = (
            config.get("user_defined_name")
            or config.get("camera_name")
            or config.get("serial_number")
            or config.get("ip_address")
            or config.get("device_id")
            or config.get("id_")
            or config.get("model")
        )
        return f"Basler Pylon{f' {label}' if label else ''}"
    if source_type == RuntimeSourceType.ARAVIS_GIGE:
        label = (
            config.get("camera_name")
            or config.get("serial_number")
            or config.get("ip_address")
            or config.get("device_id")
            or config.get("id_")
        )
        vendor = str(config.get("vendor") or "GigE").replace("_", " ")
        return f"Aravis GigE {vendor}{f' {label}' if label else ''}"
    if source_type == RuntimeSourceType.SNAPSHOT:
        return str(config.get("url") or config.get("snapshot_url") or "Snapshot source")
    if source_type == RuntimeSourceType.TRIGGER_CAPTURE:
        camera = config.get("camera") or config.get("device") or config.get("label")
        return f"Trigger capture{f' from {camera}' if camera else ''}"
    if source_type == RuntimeSourceType.FILE_TEST:
        return str(
            config.get("file_name")
            or config.get("original_filename")
            or config.get("path")
            or "Test file source"
        )
    return "Configured source"


def _rtsp_summary_from_parts(config: dict[str, Any]) -> str | None:
    host = str(config.get("host") or "").strip()
    if not host:
        return None
    scheme = str(config.get("scheme") or "rtsp").lower()
    port = config.get("port")
    path = str(config.get("path") or config.get("stream_path") or "").lstrip("/")
    authority = f"{host}:{port}" if port else host
    return f"{scheme}://{authority}/{path}".rstrip("/")


def _capture_summary(
    row: RuntimeChannel,
    *,
    context: dict[str, Any],
) -> RuntimeChannelCaptureSummary:
    stored = row.capture_summary or {}
    counts = context["capture_counts"].get(row.id)
    if counts is not None and not stored:
        return RuntimeChannelCaptureSummary(
            summary=(
                f"{counts['rule_count']} capture rule"
                f"{'s' if counts['rule_count'] != 1 else ''}"
            ),
            rule_count=counts["rule_count"],
            target_project_count=counts["target_project_count"],
            modes=counts["modes"],
        )
    return RuntimeChannelCaptureSummary.model_validate(stored or {})


def _output_summary(row: RuntimeChannel, *, context: dict[str, Any]) -> RuntimeChannelOutputSummary:
    stored = row.output_summary or {}
    counts = context["output_counts"].get(row.id)
    if counts is not None:
        summary = stored.get("summary") if isinstance(stored, dict) else None
        return RuntimeChannelOutputSummary(
            summary=(
                summary
                or f"{counts['connector_count']} output"
                f"{'s' if counts['connector_count'] != 1 else ''}"
            ),
            connector_count=counts["connector_count"],
            primary_outputs=counts["primary_outputs"],
        )
    return RuntimeChannelOutputSummary.model_validate(stored or {})


def _health_summary(row: RuntimeChannel) -> RuntimeChannelHealthSummary:
    stored = dict(row.health_summary or {})
    if stored:
        return RuntimeChannelHealthSummary(
            state=HealthState(stored.get("state") or row.health_state),
            status_text=str(stored.get("status_text") or "Unknown"),
            last_error=_string_or_none(stored.get("last_error") or row.last_error),
            last_heartbeat_at=_string_or_none(stored.get("last_heartbeat_at")),
            recovery_hint=_string_or_none(stored.get("recovery_hint")),
        )
    status_text = {
        RuntimeObservedState.RUNNING.value: "Running",
        RuntimeObservedState.STARTING.value: "Starting",
        RuntimeObservedState.VALIDATING.value: "Validating",
        RuntimeObservedState.LOADING_PIPELINE.value: "Loading pipeline",
        RuntimeObservedState.STOPPING.value: "Stopping",
        RuntimeObservedState.STOPPED.value: "Stopped",
        RuntimeObservedState.UNASSIGNED.value: "Needs setup",
        RuntimeObservedState.DEGRADED.value: "Needs attention",
        RuntimeObservedState.FAILED.value: "Failed",
        RuntimeObservedState.RECOVERING.value: "Recovering",
    }.get(row.observed_state, "Unknown")
    return RuntimeChannelHealthSummary(
        state=HealthState(row.health_state),
        status_text=status_text,
        last_error=row.last_error,
        recovery_hint=_runtime_recovery_hint(row, execution=None),
    )


def _performance_summary(
    row: RuntimeChannel,
    *,
    execution: RuntimeExecution | None,
    result: ResultRecord | None,
) -> RuntimeChannelPerformanceSummary:
    stored = dict(row.performance_summary or {})
    if stored:
        return RuntimeChannelPerformanceSummary.model_validate(stored)
    frame_count = int(getattr(execution, "frame_count", 0) or 0)
    drop_count = int(getattr(execution, "drop_count", 0) or 0)
    trigger_count = int(getattr(execution, "trigger_count", 0) or 0)
    drop_rate = None
    if frame_count + drop_count:
        drop_rate = round((drop_count / (frame_count + drop_count)) * 100, 2)
    latency = None
    if result is not None:
        timing = result.timing or {}
        raw_latency = timing.get("total_ms") or timing.get("latency_ms")
        latency = float(raw_latency) if raw_latency is not None else None
    return RuntimeChannelPerformanceSummary(
        fps=None,
        latency_ms=latency,
        drop_rate_percent=drop_rate,
        frame_count=frame_count,
        drop_count=drop_count,
        trigger_count=trigger_count,
        last_result_status=result.status if result is not None else None,
    )


def _pipeline_labels(session: Session, pipeline_version_ids: set[str | None]) -> dict[str, str]:
    ids = {item for item in pipeline_version_ids if item}
    if not ids:
        return {}
    versions = {
        row.id: row
        for row in session.scalars(select(PipelineVersion).where(PipelineVersion.id.in_(ids))).all()
    }
    definition_ids = {row.pipeline_definition_id for row in versions.values()}
    definitions = {
        row.id: row
        for row in session.scalars(
            select(PipelineDefinition).where(PipelineDefinition.id.in_(definition_ids))
        ).all()
    }
    return {
        version_id: (
            f"{definitions[version.pipeline_definition_id].name} v{version.version_no}"
            if version.pipeline_definition_id in definitions
            else f"Pipeline v{version.version_no}"
        )
        for version_id, version in versions.items()
    }


def _pipeline_label(session: Session, *, pipeline_version_id: str | None) -> str | None:
    if not pipeline_version_id:
        return None
    return _pipeline_labels(session, {pipeline_version_id}).get(pipeline_version_id)


def _resolve_pipeline_version_id(session: Session, assigned_pipeline: str | None) -> str | None:
    if not assigned_pipeline:
        return None
    normalized = _slugify(assigned_pipeline)
    definition = session.scalar(
        select(PipelineDefinition).where(PipelineDefinition.slug == normalized)
    )
    if definition is None:
        return None
    return definition.published_version_id or definition.latest_version_id


def _apply_runtime_pipeline_update(
    session: Session,
    row: RuntimeChannel,
    payload: RuntimeChannelUpdateRequest,
) -> None:
    fields_set = getattr(payload, "model_fields_set", set())
    assigned_pipeline_touched = "assigned_pipeline" in fields_set
    pipeline_version_touched = "pipeline_version_id" in fields_set
    if not assigned_pipeline_touched and not pipeline_version_touched and "mode" not in fields_set:
        return
    if not assigned_pipeline_touched and not pipeline_version_touched:
        _validated_channel_pipeline_version_id(
            session,
            row.pipeline_version_id,
            channel_mode=RuntimeMode(row.mode),
        )
        return

    if assigned_pipeline_touched and payload.assigned_pipeline is None:
        row.assigned_pipeline = None
        if not pipeline_version_touched or payload.pipeline_version_id is None:
            row.pipeline_version_id = None
            return

    if pipeline_version_touched:
        pipeline_version_id = payload.pipeline_version_id
        if pipeline_version_id is None and assigned_pipeline_touched:
            pipeline_version_id = _resolve_pipeline_version_id(
                session,
                payload.assigned_pipeline,
            )
        row.pipeline_version_id = _validated_channel_pipeline_version_id(
            session,
            pipeline_version_id,
            channel_mode=RuntimeMode(row.mode),
        )
        row.assigned_pipeline = _pipeline_label(
            session,
            pipeline_version_id=row.pipeline_version_id,
        ) or (payload.assigned_pipeline if assigned_pipeline_touched else None)
        return

    row.assigned_pipeline = payload.assigned_pipeline
    row.pipeline_version_id = _validated_channel_pipeline_version_id(
        session,
        _resolve_pipeline_version_id(session, payload.assigned_pipeline),
        channel_mode=RuntimeMode(row.mode),
    )


def _ensure_channel_scoped_pipeline_after_assignment(
    session: Session,
    row: RuntimeChannel,
    *,
    actor: str | None,
) -> None:
    if not row.pipeline_version_id:
        return
    version = session.get(PipelineVersion, row.pipeline_version_id)
    if version is None:
        return
    definition = session.get(PipelineDefinition, version.pipeline_definition_id)
    if (
        definition is not None
        and definition.scope == "channel_instance"
        and definition.owner_channel_id == row.id
    ):
        return
    from cv_contracts.runtime import RuntimeChannelPipelineInstanceRequest
    from cv_db.pipeline_store import ensure_channel_pipeline_instance

    ensure_channel_pipeline_instance(
        session,
        channel_id=row.id,
        payload=RuntimeChannelPipelineInstanceRequest(
            template_version_id=version.id,
            replace_existing=True,
        ),
        created_by=actor,
    )


def _ensure_startable_channel_pipeline(session: Session, row: RuntimeChannel) -> None:
    if not row.pipeline_version_id:
        raise ValueError("Channel has no published channel pipeline instance assigned.")
    version = session.get(PipelineVersion, row.pipeline_version_id)
    if version is None:
        raise ValueError("Channel pipeline version does not exist.")
    definition = session.get(PipelineDefinition, version.pipeline_definition_id)
    if (
        definition is None
        or definition.scope != "channel_instance"
        or definition.owner_channel_id != row.id
    ):
        raise ValueError("Channel start requires a channel-scoped runtime pipeline instance.")
    if RuntimePipelineStatus(version.status) != RuntimePipelineStatus.PUBLISHED:
        raise ValueError("Channel pipeline instance must be published before start.")


def _validated_channel_pipeline_version_id(
    session: Session,
    pipeline_version_id: str | None,
    *,
    channel_mode: RuntimeMode,
) -> str | None:
    if not pipeline_version_id:
        return None
    version = session.get(PipelineVersion, pipeline_version_id)
    if version is None:
        raise ValueError("Pipeline version does not exist.")
    pipeline_mode = RuntimeMode(version.mode)
    if pipeline_mode not in {channel_mode, RuntimeMode.TRIGGER}:
        raise ValueError(
            "Channel pipeline assignments must be realtime or triggerable "
            f"({pipeline_mode.value} != {channel_mode.value}). "
            "Batch or incompatible pipeline versions cannot be assigned to a managed channel."
        )
    return version.id


def _capture_counts(session: Session, channel_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not channel_ids:
        return {}
    counts: dict[str, dict[str, Any]] = {}
    for rule in session.scalars(
        select(CaptureRule).where(CaptureRule.channel_id.in_(channel_ids))
    ).all():
        entry = counts.setdefault(
            rule.channel_id,
            {"rule_count": 0, "target_project_count": 0, "modes": set()},
        )
        entry["rule_count"] += 1
        entry["target_project_count"] += len(rule.target_label_project_ids or [])
        entry["modes"].add(rule.capture_mode)
    return {
        channel_id: {
            **entry,
            "modes": sorted(entry["modes"]),
        }
        for channel_id, entry in counts.items()
    }


def _output_counts(session: Session, channel_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not channel_ids:
        return {}
    counts: dict[str, dict[str, Any]] = {}
    for connector in session.scalars(
        select(OutputConnector).where(OutputConnector.channel_id.in_(channel_ids))
    ).all():
        entry = counts.setdefault(
            connector.channel_id,
            {"connector_count": 0, "primary_outputs": set()},
        )
        entry["connector_count"] += 1
        entry["primary_outputs"].add(connector.connector_type)
    return {
        channel_id: {
            **entry,
            "primary_outputs": sorted(entry["primary_outputs"]),
        }
        for channel_id, entry in counts.items()
    }


def _unique_slug(session: Session, model: type[Any], value: str) -> str:
    base = _slugify(value)
    existing = set(session.scalars(select(model.slug)).all())
    candidate = base
    suffix = 2
    while candidate in existing:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _slugify(value: str) -> str:
    candidate = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return candidate or "channel"


def _redact_inline_credentials(value: str) -> str:
    return re.sub(r"://([^/@:]+):([^/@]+)@", "://***@", value)


def _runtime_recovery_hint(
    row: RuntimeChannel,
    *,
    execution: RuntimeExecution | None,
) -> str | None:
    stored = dict(
        (execution.health_summary if execution is not None else row.health_summary) or {}
    )
    raw_hint = stored.get("recovery_hint")
    if isinstance(raw_hint, str) and raw_hint:
        return raw_hint
    observed_state = (
        RuntimeObservedState(execution.actual_state)
        if execution is not None
        else RuntimeObservedState(row.observed_state)
    )
    if observed_state == RuntimeObservedState.FAILED:
        return "Inspect the last error, verify source/model availability, then restart the channel."
    if observed_state == RuntimeObservedState.DEGRADED:
        return "Check source health, drop rate, latency, and the latest runtime result errors."
    if observed_state == RuntimeObservedState.RECOVERING:
        return "Wait for reconciliation to finish, then restart if the heartbeat does not refresh."
    if observed_state == RuntimeObservedState.STOPPED:
        return "Start the channel when runtime execution is required."
    return None


def _aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _sync_channel_trigger_endpoints(session: Session, channel_id: str) -> None:
    from cv_db.pipeline_store import sync_trigger_endpoints_for_channel

    sync_trigger_endpoints_for_channel(session, channel_id=channel_id)


def _iso(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat()).replace("+00:00", "Z")
    return str(value)
