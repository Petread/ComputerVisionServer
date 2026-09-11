from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections import defaultdict, deque
from collections.abc import Iterable
from datetime import UTC
from typing import Any

from cv_contracts.runtime import (
    RuntimeChannelPipelineInstanceRequest,
    RuntimeGraphEdge,
    RuntimeNodePort,
    RuntimeNodePropertyOption,
    RuntimeNodeSpec,
    RuntimeNodeTypeSummary,
    RuntimePipelineAuthoringGraph,
    RuntimePipelineDefinitionCreateRequest,
    RuntimePipelineDefinitionDetail,
    RuntimePipelineDefinitionSummary,
    RuntimePipelineGraph,
    RuntimePipelineOutputCatalog,
    RuntimePipelineOutputCatalogEntry,
    RuntimePipelineOutputCatalogField,
    RuntimePipelineTemplateInstantiateRequest,
    RuntimePipelineValidationIssue,
    RuntimePipelineValidationReport,
    RuntimePipelineValidationRequest,
    RuntimePipelineVersionCreateRequest,
    RuntimePipelineVersionDetail,
    RuntimePipelineVersionSummary,
)
from cv_core import generate_prefixed_id
from cv_runtime import (
    BUILTIN_NODE_TYPE_BY_KEY,
    ExecutablePipelineGraph,
    RuntimeGraphBuildError,
    RuntimeHandlerLoadError,
    builtin_node_types,
    can_transition_pipeline_version,
    validate_camera_intrinsic_calibration,
    validate_image_view_profile,
    validate_runtime_handler_ref,
    validate_stem_curvature_process_calibration,
    validate_stem_minimum_principle_validation_artifact,
    validate_stem_surface_phase_calibration,
)
from cv_runtime.projections import normalize_output_result_config, output_policy_from_config
from cv_runtime.siemens_sps import (
    snap7_dependency_available,
    validate_siemens_sps_write_config,
)
from cv_status import (
    RuntimeChannelDesiredState,
    RuntimeControlCommand,
    RuntimeMode,
    RuntimeNodeCategory,
    RuntimeObservedState,
    RuntimePipelineStatus,
    RuntimeResourceStatus,
    TaskType,
)
from cv_utils import utc_now, utc_now_iso
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cv_db.models import (
    PipelineDefinition,
    PipelineNode,
    PipelineVersion,
    RuntimeChannel,
    RuntimeExecution,
    RuntimeModelCapabilityCache,
    RuntimeNodeTypeRegistry,
    TriggerEndpoint,
)
from cv_db.runtime_execution_store import (
    TERMINAL_RUNTIME_EXECUTION_STATUSES,
    create_runtime_control_command,
    refresh_runtime_model_capability_cache,
    request_runtime_execution_stop,
    runtime_model_capability_is_selectable_for_policy,
)


def ensure_builtin_node_types(session: Session) -> None:
    for node_type in builtin_node_types():
        payload = _node_type_payload(node_type)
        active = _runtime_node_type_active(node_type)
        row = _find_node_type_registry_row(session, node_type)
        if row is None:
            row = RuntimeNodeTypeRegistry(
                id=generate_prefixed_id("nty"),
                node_type=node_type.node_type,
                contract_version=node_type.contract_version,
                active=active,
                **payload,
            )
            try:
                with session.begin_nested():
                    session.add(row)
                    session.flush()
            except IntegrityError:
                row = _find_node_type_registry_row(session, node_type)
                if row is None:
                    raise
        for key, value in payload.items():
            setattr(row, key, value)
        row.active = active
    session.flush()


def _runtime_node_type_active(node_type: RuntimeNodeTypeSummary) -> bool:
    if node_type.node_type == "output.siemens_sps_write":
        return snap7_dependency_available()
    return True


def _find_node_type_registry_row(
    session: Session,
    node_type: RuntimeNodeTypeSummary,
) -> RuntimeNodeTypeRegistry | None:
    with session.no_autoflush:
        return session.scalar(
            select(RuntimeNodeTypeRegistry).where(
                RuntimeNodeTypeRegistry.node_type == node_type.node_type,
                RuntimeNodeTypeRegistry.contract_version == node_type.contract_version,
            )
        )


def list_runtime_node_types(
    session: Session,
    *,
    category: RuntimeNodeCategory | None = None,
    mode: RuntimeMode | None = None,
) -> list[RuntimeNodeTypeSummary]:
    ensure_builtin_node_types(session)
    rows = session.scalars(
        select(RuntimeNodeTypeRegistry)
        .where(RuntimeNodeTypeRegistry.active.is_(True))
        .order_by(
            RuntimeNodeTypeRegistry.node_category.asc(), RuntimeNodeTypeRegistry.display_name.asc()
        )
    ).all()
    items = [_registry_row_to_contract(row) for row in rows]
    if category is not None:
        items = [item for item in items if item.node_category == category]
    if mode is not None:
        items = [item for item in items if mode in item.supported_modes]
    return items


def ensure_builtin_pipeline_templates(session: Session) -> None:
    ensure_builtin_node_types(session)
    for template in _template_definitions():
        existing = session.scalar(
            select(PipelineDefinition).where(PipelineDefinition.slug == template["slug"])
        )
        if existing is not None:
            existing.scope = "template" if existing.template else existing.scope
            latest = _latest_version(session, existing)
            desired_graph = _normalize_graph(template["graph"])
            if existing.template and (
                latest is None or latest.config_digest != _digest_graph(desired_graph)
            ):
                version = _create_pipeline_version_row(
                    session,
                    definition=existing,
                    payload=RuntimePipelineVersionCreateRequest(
                        mode=template["mode"],
                        graph=template["graph"],
                        change_note="Built-in template refresh",
                    ),
                    created_by="system",
                )
                existing.name = template["name"]
                existing.description = template["description"]
                existing.latest_version_id = version.id
                existing.status = version.status
            continue
        definition = PipelineDefinition(
            id=generate_prefixed_id("pln"),
            slug=template["slug"],
            name=template["name"],
            description=template["description"],
            status=RuntimePipelineStatus.DRAFT.value,
            template=True,
            scope="template",
            created_by="system",
        )
        session.add(definition)
        session.flush()
        version = _create_pipeline_version_row(
            session,
            definition=definition,
            payload=RuntimePipelineVersionCreateRequest(
                mode=template["mode"],
                graph=template["graph"],
                change_note="Built-in template",
            ),
            created_by="system",
        )
        definition.latest_version_id = version.id
    session.flush()


def create_pipeline_definition(
    session: Session,
    payload: RuntimePipelineDefinitionCreateRequest,
    *,
    created_by: str | None,
) -> RuntimePipelineDefinitionDetail:
    ensure_builtin_node_types(session)
    slug = _unique_slug(session, payload.slug or payload.name)
    row = PipelineDefinition(
        id=generate_prefixed_id("pln"),
        slug=slug,
        name=payload.name,
        description=payload.description,
        status=RuntimePipelineStatus.DRAFT.value,
        template=payload.template,
        scope="template",
        created_by=created_by,
    )
    session.add(row)
    session.flush()
    return get_pipeline_definition_detail(session, row.id)  # type: ignore[return-value]


def instantiate_pipeline_template(
    session: Session,
    template_id: str,
    payload: RuntimePipelineTemplateInstantiateRequest,
    *,
    created_by: str | None,
) -> RuntimePipelineDefinitionDetail:
    ensure_builtin_pipeline_templates(session)
    template = _get_definition(session, template_id)
    if not template.template:
        raise ValueError("The selected pipeline is not a template.")
    template_version = _latest_version(session, template)
    if template_version is None:
        raise ValueError("The selected template has no version to copy.")

    definition = PipelineDefinition(
        id=generate_prefixed_id("pln"),
        slug=_unique_slug(session, payload.slug or payload.name),
        name=payload.name,
        description=payload.description or template.description,
        status=RuntimePipelineStatus.DRAFT.value,
        template=False,
        scope="template",
        source_template_definition_id=template.id,
        created_by=created_by,
    )
    session.add(definition)
    session.flush()
    version = _create_pipeline_version_row(
        session,
        definition=definition,
        payload=RuntimePipelineVersionCreateRequest(
            mode=RuntimeMode(template_version.mode),
            graph=RuntimePipelineGraph.model_validate(template_version.graph),
            change_note=payload.change_note,
        ),
        created_by=created_by,
    )
    definition.latest_version_id = version.id
    definition.status = version.status
    session.flush()
    return get_pipeline_definition_detail(session, definition.id)  # type: ignore[return-value]


def ensure_channel_pipeline_instance(
    session: Session,
    *,
    channel_id: str,
    payload: RuntimeChannelPipelineInstanceRequest | None = None,
    created_by: str | None,
) -> RuntimePipelineDefinitionDetail:
    ensure_builtin_pipeline_templates(session)
    channel = session.get(RuntimeChannel, channel_id)
    if channel is None or channel.deleted_at is not None:
        raise ValueError("Channel does not exist.")
    payload = payload or RuntimeChannelPipelineInstanceRequest()
    existing = session.scalar(
        select(PipelineDefinition).where(
            PipelineDefinition.scope == "channel_instance",
            PipelineDefinition.owner_channel_id == channel.id,
            PipelineDefinition.status != RuntimePipelineStatus.ARCHIVED.value,
        )
    )
    if existing is not None and not payload.replace_existing:
        version_id = existing.published_version_id or existing.latest_version_id
        if version_id:
            channel.pipeline_version_id = version_id
            channel.assigned_pipeline = existing.name
            session.flush()
        return get_pipeline_definition_detail(session, existing.id)  # type: ignore[return-value]

    source_version = _channel_instance_source_version(session, channel=channel, payload=payload)
    source_definition = _get_definition(session, source_version.pipeline_definition_id)
    if source_definition.scope != "template":
        raise ValueError("Channel pipeline instances can only be created from pipeline templates.")
    instance_name = payload.name or f"{channel.name} Pipeline"
    definition = PipelineDefinition(
        id=generate_prefixed_id("pln"),
        slug=_unique_slug(session, f"{channel.slug}-{source_definition.slug}"),
        name=instance_name,
        description=source_definition.description,
        status=RuntimePipelineStatus.DRAFT.value,
        template=False,
        scope="channel_instance",
        owner_channel_id=channel.id,
        source_template_definition_id=source_definition.id,
        created_by=created_by,
    )
    session.add(definition)
    session.flush()
    version = _create_pipeline_version_row(
        session,
        definition=definition,
        payload=RuntimePipelineVersionCreateRequest(
            mode=RuntimeMode(source_version.mode),
            graph=RuntimePipelineGraph.model_validate(source_version.graph),
            change_note="Created channel-scoped pipeline instance",
        ),
        created_by=created_by,
    )
    definition.latest_version_id = version.id
    definition.status = version.status
    if (
        RuntimePipelineStatus(source_version.status) == RuntimePipelineStatus.PUBLISHED
        and version.status == RuntimePipelineStatus.VALIDATED.value
    ):
        version.status = RuntimePipelineStatus.PUBLISHED.value
        version.published_at = utc_now()
        version.published_by = created_by
        definition.published_version_id = version.id
        definition.status = RuntimePipelineStatus.PUBLISHED.value
    channel.pipeline_version_id = version.id
    channel.assigned_pipeline = definition.name
    channel.updated_by = created_by
    channel.last_activity_at = utc_now()
    if existing is not None and payload.replace_existing:
        existing.status = RuntimePipelineStatus.ARCHIVED.value
        existing.published_version_id = None
    if version.status == RuntimePipelineStatus.PUBLISHED.value:
        _sync_trigger_endpoint_for_published_version(session, definition=definition, version=version)
    else:
        _disable_derived_trigger_endpoints(session, definition_id=definition.id)
    session.flush()
    return get_pipeline_definition_detail(session, definition.id)  # type: ignore[return-value]


def list_pipeline_definitions(
    session: Session,
    *,
    include_templates: bool = True,
    templates_only: bool = False,
    scope: str | None = None,
    owner_channel_id: str | None = None,
) -> list[RuntimePipelineDefinitionSummary]:
    ensure_builtin_pipeline_templates(session)
    if scope is not None and scope not in {"template", "channel_instance"}:
        raise ValueError("Pipeline definition scope must be template or channel_instance.")
    rows = session.scalars(
        select(PipelineDefinition).order_by(
            PipelineDefinition.template.desc(),
            PipelineDefinition.name.asc(),
        )
    ).all()
    rows = [row for row in rows if row.status != RuntimePipelineStatus.ARCHIVED.value]
    if templates_only:
        rows = [row for row in rows if row.template]
    elif not include_templates:
        rows = [row for row in rows if not row.template]
    if scope is not None:
        rows = [row for row in rows if row.scope == scope]
    if owner_channel_id is not None:
        rows = [row for row in rows if row.owner_channel_id == owner_channel_id]
    counts = _version_counts(session, [row.id for row in rows])
    version_modes = _definition_reference_version_modes(session, rows)
    return [
        _definition_summary(
            row,
            version_count=counts.get(row.id, 0),
            version_modes=version_modes,
        )
        for row in rows
    ]


def get_pipeline_definition_detail(
    session: Session,
    pipeline_definition_id: str,
) -> RuntimePipelineDefinitionDetail | None:
    ensure_builtin_pipeline_templates(session)
    row = _find_definition(session, pipeline_definition_id)
    if row is None:
        return None
    versions = session.scalars(
        select(PipelineVersion)
        .where(PipelineVersion.pipeline_definition_id == row.id)
        .order_by(PipelineVersion.version_no.desc())
    ).all()
    summaries = [_version_summary(version) for version in versions]
    version_modes = {version.id: RuntimeMode(version.mode) for version in versions}
    latest_version = None
    if row.latest_version_id:
        latest = session.get(PipelineVersion, row.latest_version_id)
        if latest is not None:
            latest_version = _version_detail(latest)
    return RuntimePipelineDefinitionDetail(
        **_definition_summary(
            row,
            version_count=len(versions),
            version_modes=version_modes,
        ).model_dump(),
        versions=summaries,
        latest_version=latest_version,
    )


def create_pipeline_version(
    session: Session,
    pipeline_definition_id: str,
    payload: RuntimePipelineVersionCreateRequest,
    *,
    created_by: str | None,
) -> RuntimePipelineVersionDetail:
    definition = _get_definition(session, pipeline_definition_id)
    if definition.status == RuntimePipelineStatus.ARCHIVED.value:
        raise ValueError("Archived pipeline definitions cannot receive new versions.")
    version = _create_pipeline_version_row(
        session,
        definition=definition,
        payload=payload,
        created_by=created_by,
    )
    definition.latest_version_id = version.id
    if version.status == RuntimePipelineStatus.VALIDATED.value:
        definition.status = RuntimePipelineStatus.VALIDATED.value
    elif definition.published_version_id is None:
        definition.status = RuntimePipelineStatus.DRAFT.value
    session.flush()
    return _version_detail(version)


def archive_pipeline_definition(
    session: Session,
    pipeline_definition_id: str,
    *,
    archived_by: str | None,
    reason: str | None = None,
) -> RuntimePipelineDefinitionSummary:
    definition = _get_definition(session, pipeline_definition_id)
    if definition.template:
        raise ValueError("Built-in pipeline templates cannot be deleted.")
    versions = session.scalars(
        select(PipelineVersion).where(PipelineVersion.pipeline_definition_id == definition.id)
    ).all()
    version_ids = [version.id for version in versions]
    channels = (
        session.scalars(
            select(RuntimeChannel).where(
                RuntimeChannel.pipeline_version_id.in_(version_ids),
                RuntimeChannel.deleted_at.is_(None),
            )
        ).all()
        if version_ids
        else []
    )
    if channels:
        channel_names = ", ".join(sorted(channel.name for channel in channels))
        raise ValueError(
            f"Pipeline is still used by runtime channels. Unassign it first: {channel_names}."
        )

    archive_note = reason or (f"Archived by {archived_by}" if archived_by else "Archived")
    for version in versions:
        version.status = RuntimePipelineStatus.ARCHIVED.value
        version.change_note = version.change_note or archive_note
    definition.status = RuntimePipelineStatus.ARCHIVED.value
    definition.published_version_id = None
    _disable_derived_trigger_endpoints(session, definition_id=definition.id)
    session.flush()
    version_modes = _definition_reference_version_modes(session, [definition])
    return _definition_summary(
        definition,
        version_count=len(versions),
        version_modes=version_modes,
    )


def validate_pipeline_request(
    session: Session,
    payload: RuntimePipelineValidationRequest,
) -> RuntimePipelineValidationReport:
    graph = normalize_pipeline_request_graph(session, payload)
    return validate_pipeline_graph(session, mode=payload.mode, graph=graph)


def normalize_pipeline_request_graph(
    session: Session,
    payload: RuntimePipelineValidationRequest | RuntimePipelineVersionCreateRequest,
) -> RuntimePipelineGraph:
    ensure_builtin_node_types(session)
    registry = {item.node_type: item for item in list_runtime_node_types(session)}
    return _normalize_request_graph(
        graph=payload.graph,
        authoring_graph=payload.authoring_graph,
        registry=registry,
    )


def runtime_pipeline_output_catalog(
    session: Session,
    graph: RuntimePipelineGraph,
) -> RuntimePipelineOutputCatalog:
    ensure_builtin_node_types(session)
    registry = {item.node_type: item for item in list_runtime_node_types(session)}
    normalized = _normalize_graph(graph, registry=registry)
    entries: list[RuntimePipelineOutputCatalogEntry] = []
    for node in normalized.nodes:
        fields = sorted(_known_result_fields_for_node(node.node_type, node))
        field_options = _catalog_fields_for_node(node.node_type, node)
        spec = registry.get(node.node_type)
        ports = spec.output_ports if spec is not None else node.output_ports
        if ports:
            for port in ports:
                entries.append(
                    RuntimePipelineOutputCatalogEntry(
                        node_key=node.node_key,
                        node_type=node.node_type,
                        output_port=port.key,
                        payload_type=port.data_type,
                        fields=fields,
                        field_options=field_options,
                    )
                )
        else:
            entries.append(
                RuntimePipelineOutputCatalogEntry(
                    node_key=node.node_key,
                    node_type=node.node_type,
                    fields=fields,
                    field_options=field_options,
                )
            )
    return RuntimePipelineOutputCatalog(entries=entries)


def validate_pipeline_version(
    session: Session,
    pipeline_version_id: str,
) -> RuntimePipelineVersionDetail:
    version = _get_version(session, pipeline_version_id)
    graph = RuntimePipelineGraph.model_validate(version.graph)
    inferred_mode = _infer_pipeline_mode(graph, fallback=RuntimeMode(version.mode))
    report = validate_pipeline_graph(session, mode=inferred_mode, graph=graph)
    version.mode = inferred_mode.value
    version.validation_report = report.model_dump(mode="json")
    if report.error_count == 0:
        current = RuntimePipelineStatus(version.status)
        if can_transition_pipeline_version(current, RuntimePipelineStatus.VALIDATED):
            version.status = RuntimePipelineStatus.VALIDATED.value
    session.flush()
    return _version_detail(version)


def publish_pipeline_version(
    session: Session,
    pipeline_version_id: str,
    *,
    published_by: str | None,
) -> RuntimePipelineVersionDetail:
    version = _get_version(session, pipeline_version_id)
    graph = RuntimePipelineGraph.model_validate(version.graph)
    inferred_mode = _infer_pipeline_mode(graph, fallback=RuntimeMode(version.mode))
    report = validate_pipeline_graph(session, mode=inferred_mode, graph=graph)
    version.mode = inferred_mode.value
    version.validation_report = report.model_dump(mode="json")
    if report.error_count:
        raise ValueError("Pipeline version cannot be published until validation passes.")
    version.status = RuntimePipelineStatus.VALIDATED.value

    definition = _get_definition(session, version.pipeline_definition_id)
    previous_id = definition.published_version_id
    if previous_id and previous_id != version.id:
        previous = session.get(PipelineVersion, previous_id)
        if previous is not None:
            previous.status = RuntimePipelineStatus.SUPERSEDED.value

    version.status = RuntimePipelineStatus.PUBLISHED.value
    version.published_at = utc_now()
    version.published_by = published_by
    definition.published_version_id = version.id
    definition.latest_version_id = version.id
    definition.status = RuntimePipelineStatus.PUBLISHED.value
    if definition.scope == "channel_instance":
        _bind_owner_channel_to_published_version(
            session,
            version=version,
            definition=definition,
            published_by=published_by,
        )
        _sync_trigger_endpoint_for_published_version(session, definition=definition, version=version)
    else:
        _disable_derived_trigger_endpoints(session, definition_id=definition.id)
    session.flush()
    return _version_detail(version)


def _bind_owner_channel_to_published_version(
    session: Session,
    *,
    version: PipelineVersion,
    definition: PipelineDefinition,
    published_by: str | None,
) -> None:
    if not definition.owner_channel_id:
        return
    channel = session.get(RuntimeChannel, definition.owner_channel_id)
    if channel is None or channel.deleted_at is not None:
        return
    if not _channel_version_can_drive_live_runtime(channel, version):
        return
    previous_desired_state = RuntimeChannelDesiredState(channel.desired_state)
    previous_observed_state = RuntimeObservedState(channel.observed_state)
    previous_version_id = channel.pipeline_version_id
    channel.pipeline_version_id = version.id
    channel.assigned_pipeline = definition.name
    channel.updated_by = published_by
    channel.last_activity_at = utc_now()
    if previous_version_id != version.id and _channel_runtime_active(previous_desired_state, previous_observed_state):
        _request_replaced_runtime_stop(session, channel=channel, new_version_id=version.id)
        create_runtime_control_command(
            session,
            channel_id=channel.id,
            command=RuntimeControlCommand.RESTART,
            requested_by=published_by,
            reason=(
                "Published channel pipeline updated; restart required to load "
                "the new published version."
            ),
        )


def _rebind_channels_to_published_version(
    session: Session,
    *,
    previous_version_id: str | None,
    version: PipelineVersion,
    definition: PipelineDefinition,
    published_by: str | None,
) -> None:
    definition_version_ids = select(PipelineVersion.id).where(
        PipelineVersion.pipeline_definition_id == definition.id
    )
    channel_match_conditions = [
        RuntimeChannel.pipeline_version_id.in_(definition_version_ids),
        RuntimeChannel.assigned_pipeline == definition.name,
    ]
    if previous_version_id:
        channel_match_conditions.append(RuntimeChannel.pipeline_version_id == previous_version_id)
    channels = session.scalars(
        select(RuntimeChannel).where(
            RuntimeChannel.deleted_at.is_(None),
            RuntimeChannel.mode == version.mode,
            or_(*channel_match_conditions),
        )
    ).all()
    for channel in channels:
        previous_desired_state = RuntimeChannelDesiredState(channel.desired_state)
        previous_observed_state = RuntimeObservedState(channel.observed_state)
        channel.pipeline_version_id = version.id
        channel.assigned_pipeline = definition.name
        channel.updated_by = published_by
        channel.last_activity_at = utc_now()
        if _channel_runtime_active(previous_desired_state, previous_observed_state):
            _request_replaced_runtime_stop(session, channel=channel, new_version_id=version.id)
            create_runtime_control_command(
                session,
                channel_id=channel.id,
                command=RuntimeControlCommand.RESTART,
                requested_by=published_by,
                reason=(
                    "Published runtime pipeline updated; restart required to load "
                    "the new published version."
                ),
            )


def _request_replaced_runtime_stop(
    session: Session,
    *,
    channel: RuntimeChannel,
    new_version_id: str,
) -> None:
    if not channel.active_runtime_execution_id:
        return
    active = session.get(RuntimeExecution, channel.active_runtime_execution_id)
    if active is None or active.pipeline_version_id == new_version_id:
        return
    if active.status in TERMINAL_RUNTIME_EXECUTION_STATUSES:
        return
    request_runtime_execution_stop(
        session,
        runtime_execution_id=active.id,
        update_channel=False,
    )


def _channel_runtime_active(
    desired_state: RuntimeChannelDesiredState,
    observed_state: RuntimeObservedState,
) -> bool:
    return desired_state in {
        RuntimeChannelDesiredState.START_REQUESTED,
        RuntimeChannelDesiredState.RUNNING,
    } or observed_state in {
        RuntimeObservedState.STARTING,
        RuntimeObservedState.VALIDATING,
        RuntimeObservedState.LOADING_PIPELINE,
        RuntimeObservedState.RUNNING,
        RuntimeObservedState.DEGRADED,
        RuntimeObservedState.RECOVERING,
    }


def validate_pipeline_graph(
    session: Session,
    *,
    mode: RuntimeMode,
    graph: RuntimePipelineGraph,
) -> RuntimePipelineValidationReport:
    mode = _infer_pipeline_mode(graph, fallback=mode)
    registry = {item.node_type: item for item in list_runtime_node_types(session)}
    issues: list[RuntimePipelineValidationIssue] = []
    nodes_by_key = {node.node_key: node for node in graph.nodes}
    specs_by_key = {node.node_key: registry.get(node.node_type) for node in graph.nodes}

    if not graph.nodes:
        issues.append(_issue("error", "empty_graph", "Add at least one source node."))

    source_nodes = [
        node for node in graph.nodes if node.node_category == RuntimeNodeCategory.SOURCE
    ]
    trigger_nodes = [
        node for node in graph.nodes if node.node_category == RuntimeNodeCategory.TRIGGER
    ]
    http_trigger_nodes = [node for node in trigger_nodes if node.node_type == "trigger.http_put"]
    interval_trigger_nodes = [
        node for node in trigger_nodes if node.node_type == "trigger.interval"
    ]
    if len(source_nodes) != 1:
        issues.append(
            _issue(
                "error",
                "source_count_invalid",
                "A runtime pipeline must have exactly one source node.",
            )
        )
    elif mode in {RuntimeMode.TRIGGER, RuntimeMode.INTERVAL}:
        allowed_source_types = {"source.channel_frame", "source.test_photo"}
        if mode == RuntimeMode.TRIGGER:
            allowed_source_types.update(
                {
                    "source.synchronized_camera_frames",
                    "source.single_board_camera_test",
                }
            )
        if source_nodes[0].node_type not in allowed_source_types:
            issues.append(
                _issue(
                    "warning"
                    if source_nodes[0].node_type == "source.channel_snapshot"
                    else "error",
                    "channel_frame_source_required",
                    (
                        "Triggered and interval pipelines must use a compatible channel "
                        "or synchronized-camera input."
                    ),
                    node_key=source_nodes[0].node_key,
                )
            )
    if mode == RuntimeMode.TRIGGER:
        if len(http_trigger_nodes) != 1 or interval_trigger_nodes:
            issues.append(
                _issue(
                    "error",
                    "trigger_entry_count_invalid",
                    "HTTP trigger pipelines must have exactly one HTTP Trigger node.",
                )
            )
        elif len(source_nodes) == 1 and not any(
            edge.from_node_key == http_trigger_nodes[0].node_key
            and edge.from_port == "trigger"
            and edge.to_node_key == source_nodes[0].node_key
            and edge.to_port == "trigger"
            for edge in graph.edges
        ):
            issues.append(
                _issue(
                    "error",
                    "trigger_entry_not_connected",
                    "Connect the HTTP Trigger node to the Channel frame input trigger port.",
                    node_key=http_trigger_nodes[0].node_key,
                )
            )
    elif mode == RuntimeMode.INTERVAL:
        if len(interval_trigger_nodes) != 1 or http_trigger_nodes:
            issues.append(
                _issue(
                    "error",
                    "interval_trigger_count_invalid",
                    "Interval pipelines must have exactly one Interval Trigger node.",
                )
            )
        elif len(source_nodes) == 1 and not any(
            edge.from_node_key == interval_trigger_nodes[0].node_key
            and edge.from_port == "trigger"
            and edge.to_node_key == source_nodes[0].node_key
            and edge.to_port == "trigger"
            for edge in graph.edges
        ):
            issues.append(
                _issue(
                    "error",
                    "interval_trigger_not_connected",
                    "Connect the Interval Trigger node to the Channel frame input trigger port.",
                    node_key=interval_trigger_nodes[0].node_key,
                )
            )
    elif trigger_nodes:
        issues.append(
            _issue(
                "error",
                "trigger_node_not_allowed",
                "Pipelines without a trigger execution node run as realtime channel pipelines.",
            )
        )
    if (
        mode != RuntimeMode.TRIGGER
        and len(source_nodes) == 1
        and source_nodes[0].node_type != "source.channel_frame"
    ):
        issues.append(
            _issue(
                "warning",
                "channel_source_recommended",
                "Configure camera input on the channel and use Channel frame input in new pipeline versions.",
                node_key=source_nodes[0].node_key,
                field="node_type",
            )
        )

    terminal_nodes = [
        node
        for node in graph.nodes
        if node.node_category in {RuntimeNodeCategory.OUTPUT, RuntimeNodeCategory.CAPTURE}
    ]
    response_nodes = [
        node
        for node in graph.nodes
        if node.node_type in {"output.http_trigger_response", "output.result"}
    ]
    if not terminal_nodes:
        issues.append(
            _issue(
                "error",
                "missing_output",
                "Add at least one output or capture node before publishing.",
            )
        )
    if mode == RuntimeMode.TRIGGER and not response_nodes:
        issues.append(
            _issue(
                "error",
                "trigger_response_missing",
                "Trigger pipelines must include a Result output node.",
            )
        )

    for node in graph.nodes:
        spec = registry.get(node.node_type)
        if spec is None:
            issues.append(
                _issue(
                    "error",
                    "unknown_node_type",
                    f"Unknown node type {node.node_type}.",
                    node_key=node.node_key,
                )
            )
            continue
        if node.node_category != spec.node_category:
            issues.append(
                _issue(
                    "error",
                    "node_category_mismatch",
                    f"{spec.display_name} belongs to {spec.node_category.value}.",
                    node_key=node.node_key,
                )
            )
        if mode not in spec.supported_modes:
            issues.append(
                _issue(
                    "error",
                    "node_mode_incompatible",
                    f"{spec.display_name} is not available in {mode.value} mode.",
                    node_key=node.node_key,
                )
            )
        try:
            validate_runtime_handler_ref(
                spec.runtime_handler_ref,
                expected_node_type=spec.node_type,
            )
        except RuntimeHandlerLoadError as exc:
            issues.append(
                _issue(
                    "error",
                    "handler_unavailable",
                    f"{spec.display_name} cannot run: {exc}",
                    node_key=node.node_key,
                    field="runtime_handler_ref",
                )
            )
        issues.extend(_validate_node_properties(session, node, spec))
        if node.node_category == RuntimeNodeCategory.INFERENCE:
            issues.extend(_validate_inference_node(session, node, spec))

    incoming: dict[tuple[str, str], list[RuntimeGraphEdge]] = defaultdict(list)
    outgoing: dict[str, list[RuntimeGraphEdge]] = defaultdict(list)
    for index, edge in enumerate(graph.edges):
        outgoing[edge.from_node_key].append(edge)
        incoming[(edge.to_node_key, edge.to_port)].append(edge)
        from_node = nodes_by_key.get(edge.from_node_key)
        to_node = nodes_by_key.get(edge.to_node_key)
        from_spec = specs_by_key.get(edge.from_node_key)
        to_spec = specs_by_key.get(edge.to_node_key)
        if from_node is None or to_node is None or from_spec is None or to_spec is None:
            continue
        from_port = _find_port(from_spec.output_ports, edge.from_port)
        to_port = _find_port(to_spec.input_ports, edge.to_port)
        source_trigger_edge = (
            mode in {RuntimeMode.TRIGGER, RuntimeMode.INTERVAL}
            and from_node.node_category == RuntimeNodeCategory.TRIGGER
            and from_port is not None
            and from_port.data_type == "trigger"
            and to_node.node_type
            in {
                "source.channel_snapshot",
                "source.channel_frame",
                "source.synchronized_camera_frames",
                "source.single_board_camera_test",
                "source.test_photo",
            }
            and edge.to_port == "trigger"
        )
        if to_node.node_category == RuntimeNodeCategory.SOURCE and not source_trigger_edge:
            issues.append(
                _issue(
                    "error",
                    "source_has_input",
                    "Source nodes cannot receive graph inputs.",
                    edge_index=index,
                    node_key=to_node.node_key,
                )
            )
        if from_port is None:
            issues.append(
                _issue(
                    "error",
                    "unknown_output_port",
                    f"{from_spec.display_name} has no output port named {edge.from_port}.",
                    edge_index=index,
                    node_key=from_node.node_key,
                )
            )
            continue
        if to_port is None:
            issues.append(
                _issue(
                    "error",
                    "unknown_input_port",
                    f"{to_spec.display_name} has no input port named {edge.to_port}.",
                    edge_index=index,
                    node_key=to_node.node_key,
                )
            )
            continue
        if not _ports_compatible(from_port.data_type, to_port.data_type, to_spec):
            issues.append(
                _issue(
                    "error",
                    "port_type_incompatible",
                    (
                        f"{from_spec.display_name}.{edge.from_port} outputs "
                        f"{from_port.data_type}, but {to_spec.display_name}.{edge.to_port} "
                        f"expects {to_port.data_type}."
                    ),
                    edge_index=index,
                    node_key=to_node.node_key,
                )
            )

    for (node_key, port_key), edges in incoming.items():
        if len(edges) <= 1:
            continue
        issues.append(
            _issue(
                "error",
                "input_port_multiple_sources",
                "Each input port can receive data from only one upstream output.",
                node_key=node_key,
                field=port_key,
                metadata={
                    "source_nodes": [edge.from_node_key for edge in edges],
                    "source_ports": [edge.from_port for edge in edges],
                },
            )
        )

    issues.extend(
        _validate_board_cut_model_class_maps(
            session,
            graph=graph,
            nodes_by_key=nodes_by_key,
            incoming=incoming,
        )
    )
    issues.extend(
        _validate_board_surface_class_mappings(
            session,
            graph=graph,
            nodes_by_key=nodes_by_key,
            incoming=incoming,
        )
    )
    issues.extend(_validate_model_binding_groups(graph=graph))
    issues.extend(
        _validate_board_cut_inspection_lengths(
            graph=graph,
            nodes_by_key=nodes_by_key,
            incoming=incoming,
        )
    )
    issues.extend(
        _validate_single_board_camera_test_graph(
            mode=mode,
            graph=graph,
            nodes_by_key=nodes_by_key,
            incoming=incoming,
        )
    )
    issues.extend(
        _validate_stem_process_calibration_graph(
            graph=graph,
            nodes_by_key=nodes_by_key,
            incoming=incoming,
        )
    )
    issues.extend(
        _validate_inference_image_view_graph(
            session,
            graph=graph,
            nodes_by_key=nodes_by_key,
        )
    )
    issues.extend(
        _validate_stem_surface_phase_calibration_graph(
            graph=graph,
            nodes_by_key=nodes_by_key,
            incoming=incoming,
        )
    )
    issues.extend(
        _validate_stem_minimum_principle_validation_graph(
            graph=graph,
            nodes_by_key=nodes_by_key,
        )
    )
    issues.extend(_validate_stem_shadow_mode_graph(graph=graph))

    for node in graph.nodes:
        spec = specs_by_key.get(node.node_key)
        if spec is None:
            continue
        for input_port in spec.input_ports:
            if input_port.required and not incoming.get((node.node_key, input_port.key)):
                issues.append(
                    _issue(
                        "error",
                        "missing_required_input",
                        f"{spec.display_name} requires input port {input_port.key}.",
                        node_key=node.node_key,
                        field=input_port.key,
                    )
                )
        if (
            spec.output_ports
            and node.node_category not in {RuntimeNodeCategory.OUTPUT, RuntimeNodeCategory.CAPTURE}
            and not outgoing.get(node.node_key)
            and not bool(
                spec.compatibility_constraints.get("allow_unconnected_outputs", False)
            )
        ):
            issues.append(
                _issue(
                    "warning",
                    "node_output_unused",
                    f"{spec.display_name} is not connected to a downstream node.",
                    node_key=node.node_key,
                )
            )

    if _has_cycle(graph):
        issues.append(_issue("error", "cycle_detected", "Pipeline graphs must be acyclic."))

    issues.extend(
        _validate_visual_object_semantics(
            graph=graph,
            specs_by_key=specs_by_key,
            incoming=incoming,
            outgoing=outgoing,
        )
    )
    issues.extend(
        _validate_measurement_workflow_semantics(
            graph=graph,
            specs_by_key=specs_by_key,
            incoming=incoming,
        )
    )
    issues.extend(
        _validate_computer_vision_workflow_semantics(
            graph=graph,
            specs_by_key=specs_by_key,
            incoming=incoming,
        )
    )
    for node in graph.nodes:
        if node.node_type == "output.siemens_sps_write":
            issues.extend(
                _validate_siemens_sps_write_node(
                    graph=graph,
                    node=node,
                    spec=specs_by_key.get(node.node_key),
                    mode=mode,
                )
            )

    if not any(issue.severity == "error" for issue in issues):
        try:
            ExecutablePipelineGraph.build(
                pipeline_version_id="validation",
                graph=graph,
                handler_refs={
                    item.node_type: item.runtime_handler_ref for item in registry.values()
                },
            )
        except RuntimeGraphBuildError as exc:
            issues.append(
                _issue(
                    "error",
                    "graph_not_executable",
                    str(exc),
                )
            )

    errors = sum(1 for issue in issues if issue.severity == "error")
    warnings = sum(1 for issue in issues if issue.severity == "warning")
    return RuntimePipelineValidationReport(
        status="invalid" if errors else "valid",
        checked_at=utc_now_iso(),
        error_count=errors,
        warning_count=warnings,
        issues=issues,
    )


def _create_pipeline_version_row(
    session: Session,
    *,
    definition: PipelineDefinition,
    payload: RuntimePipelineVersionCreateRequest,
    created_by: str | None,
) -> PipelineVersion:
    registry = {item.node_type: item for item in list_runtime_node_types(session)}
    graph = _normalize_request_graph(
        graph=payload.graph,
        authoring_graph=payload.authoring_graph,
        registry=registry,
    )
    inferred_mode = _infer_pipeline_mode(graph, fallback=payload.mode)
    report = validate_pipeline_graph(session, mode=inferred_mode, graph=graph)
    latest_no = (
        session.scalar(
            select(func.max(PipelineVersion.version_no)).where(
                PipelineVersion.pipeline_definition_id == definition.id
            )
        )
        or 0
    )
    version = PipelineVersion(
        id=generate_prefixed_id("plv"),
        pipeline_definition_id=definition.id,
        version_no=int(latest_no) + 1,
        status=(
            RuntimePipelineStatus.VALIDATED.value
            if report.error_count == 0
            else RuntimePipelineStatus.DRAFT.value
        ),
        mode=inferred_mode.value,
        graph=graph.model_dump(mode="json"),
        validation_report=report.model_dump(mode="json"),
        model_capability_constraints=_model_constraints(graph),
        node_contract_versions={node.node_key: node.node_contract_version for node in graph.nodes},
        config_digest=_digest_graph(graph),
        change_note=payload.change_note,
        created_by=created_by,
    )
    session.add(version)
    session.flush()
    _replace_pipeline_node_rows(session, version, graph)
    return version


def _infer_pipeline_mode(
    graph: RuntimePipelineGraph,
    *,
    fallback: RuntimeMode,
) -> RuntimeMode:
    _ = fallback
    node_types = {node.node_type for node in graph.nodes}
    if "trigger.http_put" in node_types:
        return RuntimeMode.TRIGGER
    if "trigger.interval" in node_types:
        return RuntimeMode.INTERVAL
    return RuntimeMode.REALTIME


def _sync_trigger_endpoint_for_published_version(
    session: Session,
    *,
    definition: PipelineDefinition,
    version: PipelineVersion,
) -> None:
    graph = RuntimePipelineGraph.model_validate(version.graph)
    if RuntimeMode(version.mode) != RuntimeMode.TRIGGER:
        _disable_derived_trigger_endpoints(session, definition_id=definition.id)
        return
    trigger_nodes = [node for node in graph.nodes if node.node_type == "trigger.http_put"]
    source_nodes = [
        node
        for node in graph.nodes
        if node.node_type in {
            "source.channel_frame",
            "source.channel_snapshot",
            "source.test_photo",
        }
    ]
    response_nodes = [
        node
        for node in graph.nodes
        if node.node_type in {"output.http_trigger_response", "output.result"}
    ]
    if len(trigger_nodes) != 1 or len(source_nodes) != 1 or not response_nodes:
        _disable_derived_trigger_endpoints(session, definition_id=definition.id)
        return

    trigger_node = trigger_nodes[0]
    source_node = source_nodes[0]
    response_node = response_nodes[0]

    if source_node.node_type == "source.channel_snapshot":
        channel_id = str((source_node.config or {}).get("channel_id") or "").strip()
        channel = session.get(RuntimeChannel, channel_id) if channel_id else None
        if channel is None or channel.deleted_at is not None:
            _disable_derived_trigger_endpoints(session, definition_id=definition.id)
            return
        endpoint = _sync_channel_bound_trigger_endpoint(
            session,
            definition=definition,
            version=version,
            trigger_node=trigger_node,
            response_node=response_node,
            channel=channel,
            legacy_source_node=source_node,
        )
        _disable_derived_trigger_endpoints(
            session,
            definition_id=definition.id,
            keep_endpoint_ids={endpoint.id},
        )
        return

    if definition.scope == "channel_instance" and definition.owner_channel_id:
        channel = session.get(RuntimeChannel, definition.owner_channel_id)
        channels = [channel] if channel is not None and channel.deleted_at is None else []
    else:
        channels = session.scalars(
            select(RuntimeChannel).where(
                RuntimeChannel.pipeline_version_id == version.id,
                RuntimeChannel.deleted_at.is_(None),
            )
        ).all()
    keep_endpoint_ids: set[str] = set()
    for channel in channels:
        endpoint = _sync_channel_bound_trigger_endpoint(
            session,
            definition=definition,
            version=version,
            trigger_node=trigger_node,
            response_node=response_node,
            channel=channel,
        )
        keep_endpoint_ids.add(endpoint.id)
    _disable_derived_trigger_endpoints(
        session,
        definition_id=definition.id,
        keep_endpoint_ids=keep_endpoint_ids,
    )


def sync_trigger_endpoints_for_channel(
    session: Session,
    *,
    channel_id: str,
) -> None:
    channel = session.get(RuntimeChannel, channel_id)
    if channel is None or channel.deleted_at is not None:
        _disable_channel_trigger_endpoints(session, channel_id=channel_id)
        return
    keep_endpoint_ids: set[str] = set()
    definitions = session.scalars(
        select(PipelineDefinition).where(
            PipelineDefinition.scope == "channel_instance",
            PipelineDefinition.owner_channel_id == channel.id,
            PipelineDefinition.status != RuntimePipelineStatus.ARCHIVED.value,
            PipelineDefinition.published_version_id.is_not(None),
        )
    ).all()
    for definition in definitions:
        version = (
            session.get(PipelineVersion, definition.published_version_id)
            if definition.published_version_id
            else None
        )
        endpoint = _sync_channel_frame_trigger_endpoint(
            session,
            definition=definition,
            version=version,
            channel=channel,
        )
        if endpoint is not None:
            keep_endpoint_ids.add(endpoint.id)

    _disable_channel_trigger_endpoints(
        session,
        channel_id=channel.id,
        keep_endpoint_ids=keep_endpoint_ids,
    )

def _sync_channel_frame_trigger_endpoint(
    session: Session,
    *,
    definition: PipelineDefinition,
    version: PipelineVersion | None,
    channel: RuntimeChannel,
) -> TriggerEndpoint | None:
    if not _version_is_published_trigger(version):
        return None
    assert version is not None
    graph = RuntimePipelineGraph.model_validate(version.graph)
    trigger_nodes = [node for node in graph.nodes if node.node_type == "trigger.http_put"]
    source_nodes = [
        node
        for node in graph.nodes
        if node.node_type in {"source.channel_frame", "source.test_photo"}
    ]
    response_nodes = [
        node
        for node in graph.nodes
        if node.node_type in {"output.http_trigger_response", "output.result"}
    ]
    if len(trigger_nodes) != 1 or len(source_nodes) != 1 or not response_nodes:
        return None
    return _sync_channel_bound_trigger_endpoint(
        session,
        definition=definition,
        version=version,
        trigger_node=trigger_nodes[0],
        response_node=response_nodes[0],
        channel=channel,
    )


def _sync_channel_bound_trigger_endpoint(
    session: Session,
    *,
    definition: PipelineDefinition,
    version: PipelineVersion,
    trigger_node: RuntimeNodeSpec,
    response_node: RuntimeNodeSpec,
    channel: RuntimeChannel,
    legacy_source_node: RuntimeNodeSpec | None = None,
) -> TriggerEndpoint:
    trigger_config = trigger_node.config or {}
    row = session.scalar(
        select(TriggerEndpoint).where(
            TriggerEndpoint.pipeline_definition_id == definition.id,
            TriggerEndpoint.trigger_node_key == trigger_node.node_key,
            TriggerEndpoint.channel_id == channel.id,
        )
    )
    if row is None:
        row = TriggerEndpoint(
            id=generate_prefixed_id("tep"),
            pipeline_definition_id=definition.id,
            trigger_node_key=trigger_node.node_key,
            derived_from_pipeline=True,
            slug=_trigger_endpoint_slug(
                session,
                definition=definition,
                trigger_node=trigger_node,
                channel=channel if legacy_source_node is None else None,
            ),
            display_name=f"{definition.name} HTTP trigger",
            method="PUT",
            rate_limit_per_minute=60,
            burst_limit=4,
            created_by=version.published_by,
        )
        session.add(row)

    row.channel_id = channel.id
    row.pipeline_version_id = version.id
    row.derived_from_pipeline = True
    row.display_name = f"{definition.name} HTTP trigger"
    row.description = "Derived from the published runtime pipeline HTTP Trigger Input node."
    row.slug = _trigger_endpoint_slug(
        session,
        definition=definition,
        trigger_node=trigger_node,
        channel=channel if legacy_source_node is None else None,
        ignore_endpoint_id=row.id,
    )
    row.trigger_type = "http_put"
    row.method = str(trigger_config.get("method") or "PUT").upper()
    row.enabled = bool(trigger_config.get("endpoint_enabled", True))
    row.auth_required = bool(trigger_config.get("auth_required", False))
    row.auth_scope = "runtime.trigger.invoke"
    row.auth_policy = {
        "type": "api_key",
        "required": bool(row.auth_required),
        "scope": row.auth_scope,
        "source": "pipeline_trigger_node",
    }
    row.timeout_ms = _bounded_int(trigger_config.get("timeout_ms"), 50, 30_000, 2500)
    response_policy = _response_policy_from_response_node(response_node)
    row.response_policy = response_policy
    row.request_schema = dict(trigger_config.get("request_schema") or {})
    row.response_schema = {
        "source": "pipeline_response_output_node",
        "node_key": response_node.node_key,
        "fields": response_policy.get("fields", []),
    }
    row.status = (
        RuntimeResourceStatus.DEGRADED.value
        if bool(row.enabled)
        else RuntimeResourceStatus.DISABLED.value
    )
    row.updated_by = version.published_by
    row.updated_at = utc_now()
    return row


def _disable_derived_trigger_endpoints(
    session: Session,
    *,
    definition_id: str,
    keep_endpoint_ids: set[str] | None = None,
) -> None:
    keep_endpoint_ids = keep_endpoint_ids or set()
    for row in session.scalars(
        select(TriggerEndpoint).where(
            TriggerEndpoint.pipeline_definition_id == definition_id,
            TriggerEndpoint.derived_from_pipeline.is_(True),
        )
    ).all():
        if row.id in keep_endpoint_ids:
            continue
        row.enabled = False
        row.status = RuntimeResourceStatus.DISABLED.value
        row.updated_at = utc_now()


def _disable_channel_trigger_endpoints(
    session: Session,
    *,
    channel_id: str,
    keep_endpoint_ids: set[str] | None = None,
) -> None:
    keep_endpoint_ids = keep_endpoint_ids or set()
    for row in session.scalars(
        select(TriggerEndpoint).where(
            TriggerEndpoint.channel_id == channel_id,
            TriggerEndpoint.derived_from_pipeline.is_(True),
        )
    ).all():
        if row.id in keep_endpoint_ids:
            continue
        row.enabled = False
        row.status = RuntimeResourceStatus.DISABLED.value
        row.updated_at = utc_now()


def _channel_version_can_drive_live_runtime(
    channel: RuntimeChannel,
    version: PipelineVersion | None,
) -> bool:
    if version is None:
        return False
    return RuntimeMode(channel.mode) == RuntimeMode(version.mode)


def _version_is_published_trigger(version: PipelineVersion | None) -> bool:
    if version is None:
        return False
    return (
        RuntimeMode(version.mode) == RuntimeMode.TRIGGER
        and RuntimePipelineStatus(version.status) == RuntimePipelineStatus.PUBLISHED
    )


def _trigger_endpoint_slug(
    session: Session,
    *,
    definition: PipelineDefinition,
    trigger_node: RuntimeNodeSpec,
    channel: RuntimeChannel | None = None,
    ignore_endpoint_id: str | None = None,
) -> str:
    configured = str((trigger_node.config or {}).get("endpoint_slug") or "").strip()
    if channel is not None:
        base = _slugify(f"{channel.slug}-{configured or definition.slug}-{trigger_node.node_key}")
    else:
        base = _slugify(configured or f"{definition.slug}-{trigger_node.node_key}")
    rows = session.execute(select(TriggerEndpoint.id, TriggerEndpoint.slug)).all()
    existing = {slug for endpoint_id, slug in rows if endpoint_id != ignore_endpoint_id}
    candidate = base
    suffix = 2
    while candidate in existing:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _response_policy_from_response_node(node: RuntimeNodeSpec) -> dict[str, Any]:
    config = node.config or {}
    try:
        output_policy = (
            output_policy_from_config(config, output_node_key=node.node_key)
            if node.node_type == "output.result"
            else None
        )
    except ValidationError:
        output_policy = None
    if output_policy is not None:
        projection_fields = [
            {
                "source_field": projection.key,
                "response_key": projection.key,
                "required": projection.required,
                "default_value": projection.default_value,
            }
            for projection in output_policy.projections
            if "response" in projection.destinations
        ]
        if projection_fields:
            return {
                "mode": "configured_fields",
                "include_unmapped": False,
                "fields": projection_fields,
            }
    mappings = config.get("result_fields") or config.get("response_fields")
    fields: list[dict[str, Any]] = []
    if isinstance(mappings, list):
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            source_field = str(mapping.get("source_field") or "").strip()
            response_key = str(mapping.get("response_key") or source_field).strip()
            if source_field and response_key:
                fields.append(
                    {
                        "source_field": source_field,
                        "response_key": response_key,
                        "required": bool(mapping.get("required", False)),
                    }
                )
    if not fields:
        for field in _field_list(config.get("fields")):
            fields.append({"source_field": field, "response_key": field, "required": False})
    return {
        "mode": "configured_fields" if fields else "all_final_response",
        "include_unmapped": False,
        "fields": fields,
    }


def _field_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def _result_key_from_label(label: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", label.strip().lower()).strip("_")
    return normalized or "unknown"


def _bounded_int(raw: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return fallback
    return min(max(parsed, minimum), maximum)


def _optional_number(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _optional_int(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _normalize_request_graph(
    *,
    graph: RuntimePipelineGraph | None,
    authoring_graph: RuntimePipelineAuthoringGraph | None,
    registry: dict[str, RuntimeNodeTypeSummary],
) -> RuntimePipelineGraph:
    if authoring_graph is not None:
        return _normalize_graph(authoring_graph, registry=registry)
    if graph is None:
        raise ValueError("graph or authoring_graph is required.")
    return _normalize_graph(graph, registry=registry)


def _normalize_graph(
    graph: RuntimePipelineGraph | RuntimePipelineAuthoringGraph,
    *,
    registry: dict[str, RuntimeNodeTypeSummary] | None = None,
) -> RuntimePipelineGraph:
    registry = registry or BUILTIN_NODE_TYPE_BY_KEY
    normalized_nodes: list[RuntimeNodeSpec] = []
    for node in graph.nodes:
        spec = registry.get(node.node_type)
        if spec is None:
            if isinstance(node, RuntimeNodeSpec):
                normalized_nodes.append(node)
                continue
            raise ValueError(f"Unknown node type {node.node_type}.")
        config = dict(spec.default_config)
        config.update(node.config or {})
        if node.node_type == "output.result":
            config = normalize_output_result_config(config, output_node_key=node.node_key)
        node_payload = node.model_dump(mode="python")
        if isinstance(node, RuntimeNodeSpec):
            node_payload = {
                **node_payload,
                "input_ports": spec.input_ports,
                "output_ports": spec.output_ports,
                "node_category": spec.node_category,
                "node_contract_version": spec.contract_version,
                "config": config,
            }
        else:
            node_payload = {
                **node_payload,
                "input_ports": spec.input_ports,
                "output_ports": spec.output_ports,
                "node_category": spec.node_category,
                "node_contract_version": spec.contract_version,
                "config": config,
            }
        normalized_nodes.append(RuntimeNodeSpec(**node_payload))
    normalized_edges = list(graph.edges)
    _add_stem_measurement_eligibility_compatibility_edge(
        nodes=normalized_nodes,
        edges=normalized_edges,
    )
    return RuntimePipelineGraph(nodes=normalized_nodes, edges=normalized_edges)


def _add_stem_measurement_eligibility_compatibility_edge(
    *,
    nodes: list[RuntimeNodeSpec],
    edges: list[RuntimeGraphEdge],
) -> None:
    """Upgrade the unambiguous early template graph without manual rewiring."""
    eligibility_nodes = [
        node for node in nodes if node.node_type == "analysis.stem_measurement_eligibility"
    ]
    curvature_nodes = [
        node for node in nodes if node.node_type == "analysis.stem_horizontal_curvature"
    ]
    if len(eligibility_nodes) != 1 or len(curvature_nodes) != 1:
        return
    curvature = curvature_nodes[0]
    if any(
        edge.to_node_key == curvature.node_key and edge.to_port == "eligibility"
        for edge in edges
    ):
        return
    eligibility = eligibility_nodes[0]
    edges.append(
        RuntimeGraphEdge(
            from_node_key=eligibility.node_key,
            from_port="result",
            to_node_key=curvature.node_key,
            to_port="eligibility",
        )
    )


def _replace_pipeline_node_rows(
    session: Session,
    version: PipelineVersion,
    graph: RuntimePipelineGraph,
) -> None:
    session.query(PipelineNode).filter(PipelineNode.pipeline_version_id == version.id).delete()
    for node in graph.nodes:
        session.add(
            PipelineNode(
                id=generate_prefixed_id("pnd"),
                pipeline_version_id=version.id,
                node_key=node.node_key,
                node_type=node.node_type,
                node_category=node.node_category.value,
                node_contract_version=node.node_contract_version,
                input_ports=[item.model_dump(mode="json") for item in node.input_ports],
                output_ports=[item.model_dump(mode="json") for item in node.output_ports],
                config=node.config,
                logical_model_ref=(
                    node.logical_model_ref.model_dump(mode="json")
                    if node.logical_model_ref
                    else None
                ),
                position=node.position,
            )
        )
    session.flush()


def _validate_node_properties(
    session: Session,
    node: RuntimeNodeSpec,
    spec: RuntimeNodeTypeSummary,
) -> list[RuntimePipelineValidationIssue]:
    issues: list[RuntimePipelineValidationIssue] = []
    config = node.config or {}
    for key in config:
        if any(secret_hint in key.lower() for secret_hint in ("password", "secret", "token")):
            issues.append(
                _issue(
                    "error",
                    "inline_secret_forbidden",
                    "Node properties must reference secrets rather than storing secret values.",
                    node_key=node.node_key,
                    field=key,
                )
            )
    for field in spec.property_schema:
        value = config.get(field.key, field.default_value)
        if field.field_type == "model_ref":
            continue
        if field.required and _empty_value(value):
            issues.append(
                _issue(
                    "error",
                    "required_property_missing",
                    f"{field.label} is required.",
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if field.field_type == "select" and not _valid_select_value(value, field.options):
            issues.append(
                _issue(
                    "error",
                    "property_option_invalid",
                    f"{field.label} must use one of the configured options.",
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if field.field_type == "label_project_list" and not _string_list(value):
            issues.append(
                _issue(
                    "error",
                    "property_list_invalid",
                    f"{field.label} must be a list of project identifiers.",
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if (
            field.field_type == "image_upload"
            and not _empty_value(value)
            and not _valid_image_upload(value)
        ):
            issues.append(
                _issue(
                    "error",
                    "image_upload_invalid",
                    f"{field.label} must be a supported image up to 5 MB.",
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if field.field_type == "image_upload_list" and not _valid_image_upload_list(value):
            issues.append(
                _issue(
                    "error",
                    "image_upload_list_invalid",
                    (
                        f"{field.label} must contain at most 100 supported images up to 5 MB each "
                        "and 100 MB total."
                    ),
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if field.field_type == "channel_ref":
            if not isinstance(value, str) or not value.strip():
                if field.required:
                    issues.append(
                        _issue(
                            "error",
                            "channel_binding_missing",
                            f"{field.label} must select a channel.",
                            node_key=node.node_key,
                            field=field.key,
                        )
                    )
            else:
                channel = session.get(RuntimeChannel, value)
                if channel is None or channel.deleted_at is not None or channel.source_id is None:
                    issues.append(
                        _issue(
                            "error",
                            "channel_binding_invalid",
                            f"{field.label} must reference an existing channel with a source.",
                            node_key=node.node_key,
                            field=field.key,
                        )
                    )
        if (
            field.field_type == "response_mapping"
            and not _empty_value(value)
            and not _valid_response_mapping(value)
        ):
            issues.append(
                _issue(
                    "error",
                    "response_mapping_invalid",
                    f"{field.label} must map source fields to response keys.",
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if field.field_type == "result_projection_table" and not _valid_result_projection_table(
            value
        ):
            issues.append(
                _issue(
                    "error",
                    "result_projection_table_invalid",
                    f"{field.label} must contain valid result projection definitions.",
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if field.field_type == "edge_role_mapping" and not _valid_edge_role_mapping(value):
            issues.append(
                _issue(
                    "error",
                    "edge_role_mapping_invalid",
                    "Edge role mappings must map detected classes to start_edge, side_edge, left_edge, right_edge, or custom.",
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if field.field_type == "measurement_rule_table" and not _valid_measurement_rule_table(
            value
        ):
            issues.append(
                _issue(
                    "error",
                    "measurement_rule_table_invalid",
                    "Measurement rules must list a feature type and one or more supported measurement bases.",
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if field.field_type == "clearance_rule_table" and not _valid_clearance_rule_table(value):
            issues.append(
                _issue(
                    "error",
                    "clearance_rule_table_invalid",
                    "Clearance rules must include feature type, measurement basis, multiplier, and before/after settings.",
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if field.field_type == "cv_line_selection_rules" and not _valid_cv_line_selection_rules(
            value
        ):
            issues.append(
                _issue(
                    "error",
                    "cv_line_selection_rules_invalid",
                    "CV line selection rules must define reference edge roles, angle tolerance, minimum length, and ranking.",
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if field.field_type == "metric_apply_to" and not _valid_metric_apply_to(value):
            issues.append(
                _issue(
                    "error",
                    "metric_apply_to_invalid",
                    "Metric conversion targets must be selected from measurements, clearance zones, free region, or selected result fields.",
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if field.field_type == "quality_rule_table" and not _valid_quality_rule_table(value):
            issues.append(
                _issue(
                    "error",
                    "quality_rule_table_invalid",
                    "Quality rules must use a supported rule type, comparison operator, and numeric threshold.",
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if field.field_type == "spatial_rule_table" and not _valid_spatial_rule_table(value):
            issues.append(
                _issue(
                    "error",
                    "spatial_rule_table_invalid",
                    "Spatial rules must use supported relations, class filters, percent thresholds, and count settings.",
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if field.field_type == "color_profile_table" and not _valid_color_profile_table(value):
            issues.append(
                _issue(
                    "error",
                    "color_profile_table_invalid",
                    "Color profiles must define enabled HSV ranges, area limits, and shape filters.",
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if field.field_type == "direction_reference" and not _valid_direction_reference(value):
            issues.append(
                _issue(
                    "error",
                    "direction_reference_invalid",
                    "Reference direction must be a non-degenerate normalized line with start and end points.",
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if field.field_type == "board_surface_calibration" and not _valid_board_surface_calibration(
            value,
            node=node,
            field_key=field.key,
        ):
            issues.append(
                _issue(
                    "error",
                    "board_surface_calibration_invalid",
                    (
                        "Select every required board surface and define four non-collinear "
                        "image points with four metric surface points."
                    ),
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if (
            field.field_type == "camera_intrinsic_calibration"
            and not _valid_camera_intrinsic_calibration(value)
        ):
            issues.append(
                _issue(
                    "error",
                    "camera_intrinsic_calibration_invalid",
                    (
                        "Calculate and apply a valid intrinsic camera calibration profile "
                        "before publishing this pipeline version."
                    ),
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if (
            field.field_type == "image_view_profile"
            and not _empty_value(value)
            and not _valid_image_view_profile(value)
        ):
            issues.append(
                _issue(
                    "error",
                    "image_view_profile_invalid",
                    "Calculate and apply a valid canonical image view profile.",
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if (
            field.field_type == "stem_curvature_process_calibration"
            and not _empty_value(value)
            and not _valid_stem_curvature_process_calibration(value)
        ):
            issues.append(
                _issue(
                    "error",
                    "stem_curvature_process_calibration_invalid",
                    (
                        "Calculate and apply a valid X/Y process calibration with "
                        "ordered measurement gates before publishing this pipeline version."
                    ),
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if (
            field.field_type == "stem_surface_phase_calibration"
            and not _empty_value(value)
            and not _valid_stem_surface_phase_calibration(value)
        ):
            issues.append(
                _issue(
                    "error",
                    "stem_surface_phase_calibration_invalid",
                    (
                        "Calculate and apply a valid raw-phase to known-angle profile "
                        "before publishing calibrated degree outputs."
                    ),
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if (
            field.field_type == "stem_minimum_principle_validation"
            and not _empty_value(value)
            and not _valid_stem_minimum_principle_validation(value)
        ):
            issues.append(
                _issue(
                    "error",
                    "stem_minimum_principle_validation_invalid",
                    (
                        "Recalculate the objective reference-body validation artifact; "
                        "its schema, status evidence or content hash is invalid."
                    ),
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if field.field_type == "board_cut_class_rules" and not _valid_board_cut_class_rules(
            value
        ):
            issues.append(
                _issue(
                    "error",
                    "board_cut_class_rules_invalid",
                    (
                        "Every deployed model class must be confirmed as cut-relevant or "
                        "irrelevant and use one of the supported metric measurements."
                    ),
                    node_key=node.node_key,
                    field=field.key,
                )
            )
        if field.field_type == "zone_list":
            zone_issues = _validate_box_list(
                value,
                field_name=field.label,
                code="zone_config_invalid",
                message_suffix="Use normalized boxes with x/y/width/height or x_min/y_min/x_max/y_max.",
                node_key=node.node_key,
                field_key=field.key,
            )
            issues.extend(zone_issues)
        if field.field_type == "roi":
            polygon_roi = (
                node.node_type == "analysis.active_stem_tracker"
                and field.key in {"transfer_rois", "longitudinal_conveyor_rois"}
            )
            roi_issues = _validate_box_list(
                value,
                field_name=field.label,
                code="roi_config_invalid",
                message_suffix=(
                    "Use normalized convex polygons with at least three ordered x/y points, "
                    "or normalized ROI boxes with x/y/width/height or "
                    "x_min/y_min/x_max/y_max."
                    if polygon_roi
                    else "Use normalized ROI boxes with x/y/width/height or "
                    "x_min/y_min/x_max/y_max."
                ),
                node_key=node.node_key,
                field_key=field.key,
                allow_polygons=polygon_roi,
            )
            issues.extend(roi_issues)
        if isinstance(value, int | float):
            if field.min_value is not None and value < field.min_value:
                issues.append(
                    _issue(
                        "error",
                        "property_below_minimum",
                        f"{field.label} must be at least {field.min_value}.",
                        node_key=node.node_key,
                        field=field.key,
                    )
                )
            if field.max_value is not None and value > field.max_value:
                issues.append(
                    _issue(
                        "error",
                        "property_above_maximum",
                        f"{field.label} must be at most {field.max_value}.",
                        node_key=node.node_key,
                        field=field.key,
                    )
                )
    if node.node_type == "source.synchronized_camera_frames":
        role_channels = [
            str(config.get(f"{role}_channel_id") or "").strip()
            for role in ("upper", "lower", "end_face")
        ]
        selected = [channel_id for channel_id in role_channels if channel_id]
        if len(selected) != len(set(selected)):
            issues.append(
                _issue(
                    "error",
                    "synchronized_camera_roles_not_unique",
                    "Upper, lower, and end-face roles must select three different cameras.",
                    node_key=node.node_key,
                )
            )
    if node.node_type == "source.test_photo":
        photo = config.get("photo")
        photos = config.get("photos")
        if not _valid_image_upload(photo) and not _valid_image_upload_list(photos, require_photo=True):
            issues.append(
                _issue(
                    "error",
                    "test_photo_missing",
                    "Upload one test photo or a photo sequence before publishing.",
                    node_key=node.node_key,
                )
            )
    return issues


def _validate_visual_object_semantics(
    *,
    graph: RuntimePipelineGraph,
    specs_by_key: dict[str, RuntimeNodeTypeSummary | None],
    incoming: dict[tuple[str, str], list[RuntimeGraphEdge]],
    outgoing: dict[str, list[RuntimeGraphEdge]],
) -> list[RuntimePipelineValidationIssue]:
    issues: list[RuntimePipelineValidationIssue] = []
    display_nodes = [node for node in graph.nodes if node.node_type == "output.display_output"]
    if len(display_nodes) > 1:
        issues.append(
            _issue(
                "error",
                "display_output_multiple",
                "A channel live view pipeline can have only one Display output node.",
                metadata={"display_nodes": [node.node_key for node in display_nodes]},
            )
        )

    terminal_visual_contexts = _terminal_visual_context_nodes(graph, specs_by_key, outgoing)
    if not display_nodes and len(terminal_visual_contexts) > 1:
        issues.append(
            _issue(
                "error",
                "display_output_ambiguous",
                (
                    "Multiple visual image contexts can reach runtime outputs. Add a Display "
                    "output node to choose the live-view image context explicitly."
                ),
                metadata={"visual_context_nodes": terminal_visual_contexts},
            )
        )

    for display_node in display_nodes:
        display_frame_edges = incoming.get((display_node.node_key, "frame"), [])
        if not display_frame_edges:
            continue
        source_edge = display_frame_edges[0]
        source_spec = specs_by_key.get(source_edge.from_node_key)
        if source_spec is None or not _has_output_type(source_spec, source_edge.from_port, "frame"):
            issues.append(
                _issue(
                    "error",
                    "display_context_invalid",
                    "Display output must receive a concrete image context from a frame output.",
                    node_key=display_node.node_key,
                    field="frame",
                )
            )

    for overlay_node in [node for node in graph.nodes if node.node_type == "output.overlay"]:
        upstream_node_keys = _upstream_node_keys(graph, overlay_node.node_key)
        if not any(
            _node_outputs_frame(specs_by_key.get(node_key)) for node_key in upstream_node_keys
        ):
            issues.append(
                _issue(
                    "error",
                    "overlay_context_missing",
                    "Overlay output must be connected to results that originate from an image context.",
                    node_key=overlay_node.node_key,
                )
            )
    return issues


def _terminal_visual_context_nodes(
    graph: RuntimePipelineGraph,
    specs_by_key: dict[str, RuntimeNodeTypeSummary | None],
    outgoing: dict[str, list[RuntimeGraphEdge]],
) -> list[str]:
    terminal: list[str] = []
    for node in graph.nodes:
        spec = specs_by_key.get(node.node_key)
        if not _node_outputs_frame(spec):
            continue
        frame_ports = {
            port.key for port in (spec.output_ports if spec else []) if port.data_type == "frame"
        }
        frame_edges = [
            edge for edge in outgoing.get(node.node_key, []) if edge.from_port in frame_ports
        ]
        if not frame_edges:
            terminal.append(node.node_key)
            continue
        reaches_non_transform_consumer = False
        for edge in frame_edges:
            target_spec = specs_by_key.get(edge.to_node_key)
            if target_spec is None:
                continue
            if not _node_outputs_frame(target_spec):
                reaches_non_transform_consumer = True
        if reaches_non_transform_consumer:
            terminal.append(node.node_key)
    return sorted(set(terminal))


def _upstream_node_keys(graph: RuntimePipelineGraph, node_key: str) -> set[str]:
    reverse_edges: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        reverse_edges[edge.to_node_key].append(edge.from_node_key)
    pending = deque(reverse_edges.get(node_key, []))
    upstream: set[str] = set()
    while pending:
        current = pending.popleft()
        if current in upstream:
            continue
        upstream.add(current)
        pending.extend(reverse_edges.get(current, []))
    return upstream


def _node_outputs_frame(spec: RuntimeNodeTypeSummary | None) -> bool:
    return spec is not None and any(port.data_type == "frame" for port in spec.output_ports)


def _has_output_type(spec: RuntimeNodeTypeSummary, port_key: str, data_type: str) -> bool:
    return any(port.key == port_key and port.data_type == data_type for port in spec.output_ports)


def _validate_measurement_workflow_semantics(
    *,
    graph: RuntimePipelineGraph,
    specs_by_key: dict[str, RuntimeNodeTypeSummary | None],
    incoming: dict[tuple[str, str], list[RuntimeGraphEdge]],
) -> list[RuntimePipelineValidationIssue]:
    issues: list[RuntimePipelineValidationIssue] = []
    nodes_by_key = {node.node_key: node for node in graph.nodes}
    for node in graph.nodes:
        if node.node_type in {
            "analysis.reference_edge_from_features",
            "analysis.feature_measurement",
        }:
            upstream = _upstream_node_keys(graph, node.node_key)
            has_feature_detection = any(
                nodes_by_key.get(upstream_key) is not None
                and nodes_by_key[upstream_key].node_type == "inference.detection"
                for upstream_key in upstream
            )
            if not incoming.get((node.node_key, "features")) or not has_feature_detection:
                issues.append(
                    _issue(
                        "error",
                        "feature_detection_input_missing",
                        "Connect the existing Feature Detection output to this node; this workflow must not create a parallel detector.",
                        node_key=node.node_key,
                        field="features",
                    )
                )
        if node.node_type == "analysis.reference_edge_from_features":
            config = _config_with_spec_defaults(node, specs_by_key.get(node.node_key))
            roles = {
                str(item.get("edge_role") or item.get("role") or "")
                for item in config.get("class_role_mappings", [])
                if isinstance(item, dict)
            }
            if not {"start_edge", "side_edge"}.issubset(roles):
                issues.append(
                    _issue(
                        "error",
                        "edge_role_mapping_missing_required_roles",
                        "Map detected feature classes to both start_edge and side_edge before publishing.",
                        node_key=node.node_key,
                        field="class_role_mappings",
                    )
                )
        if node.node_type == "analysis.clearance_zone_generator":
            measurement_edges = incoming.get((node.node_key, "measurements"), [])
            if measurement_edges:
                source = nodes_by_key.get(measurement_edges[0].from_node_key)
                if source is not None and source.node_type == "analysis.feature_measurement":
                    available_bases = _measurement_bases_from_node(
                        source,
                        specs_by_key.get(source.node_key),
                    )
                    for rule in _clearance_rules_from_node(node, specs_by_key.get(node.node_key)):
                        basis = str(rule.get("measurement_basis") or "")
                        if basis and basis not in available_bases:
                            issues.append(
                                _issue(
                                    "error",
                                    "clearance_measurement_basis_unavailable",
                                    (
                                        f"Clearance rule uses {basis}, but the upstream Feature "
                                        "Measurement node does not produce that basis."
                                    ),
                                    node_key=node.node_key,
                                    field="clearance_rules",
                                    metadata={"measurement_basis": basis},
                                )
                            )
        if node.node_type == "analysis.pixel_to_metric":
            config = _config_with_spec_defaults(node, specs_by_key.get(node.node_key))
            try:
                mm_per_pixel = float(config.get("mm_per_pixel"))
            except (TypeError, ValueError):
                mm_per_pixel = 0
            if mm_per_pixel <= 0:
                issues.append(
                    _issue(
                        "error",
                        "metric_calibration_invalid",
                        "Pixel to metric conversion requires a positive mm_per_pixel value.",
                        node_key=node.node_key,
                        field="mm_per_pixel",
                    )
                )
        if node.node_type == "analysis.quality_rule_evaluation" and not (
            incoming.get((node.node_key, "measurements"))
            or incoming.get((node.node_key, "free_region"))
        ):
            issues.append(
                _issue(
                    "error",
                    "quality_rule_inputs_missing",
                    "Quality rule evaluation requires feature measurements or a free-region input.",
                    node_key=node.node_key,
                )
            )
        if node.node_type in {"output.http_trigger_response", "output.result"}:
            issues.extend(_validate_response_mapping_sources(graph, node))
        if node.node_type == "output.result":
            issues.extend(_validate_output_projection_sources(graph, node))
    return issues


def _validate_computer_vision_workflow_semantics(
    *,
    graph: RuntimePipelineGraph,
    specs_by_key: dict[str, RuntimeNodeTypeSummary | None],
    incoming: dict[tuple[str, str], list[RuntimeGraphEdge]],
) -> list[RuntimePipelineValidationIssue]:
    issues: list[RuntimePipelineValidationIssue] = []
    for node in graph.nodes:
        if node.node_type == "computer_vision.canny_edge_detection":
            config = _config_with_spec_defaults(node, specs_by_key.get(node.node_key))
            lower = _optional_number(config.get("lower_threshold"))
            upper = _optional_number(config.get("upper_threshold"))
            if lower is None or upper is None or lower < 0 or upper < 0 or lower >= upper:
                issues.append(
                    _issue(
                        "error",
                        "canny_thresholds_invalid",
                        "Canny thresholds must be non-negative and lower_threshold must be below upper_threshold.",
                        node_key=node.node_key,
                        field="lower_threshold",
                        metadata={"lower_threshold": lower, "upper_threshold": upper},
                    )
                )
            blur_kernel = _optional_int(config.get("blur_kernel_size"))
            if blur_kernel is None or blur_kernel < 0 or (blur_kernel > 1 and blur_kernel % 2 == 0):
                issues.append(
                    _issue(
                        "error",
                        "canny_blur_kernel_invalid",
                        "Canny blur kernel must be 0, 1, or an odd positive integer.",
                        node_key=node.node_key,
                        field="blur_kernel_size",
                    )
                )
            aperture = _optional_int(config.get("aperture_size"))
            if aperture not in {3, 5, 7}:
                issues.append(
                    _issue(
                        "error",
                        "canny_aperture_invalid",
                        "Canny aperture size must be 3, 5, or 7.",
                        node_key=node.node_key,
                        field="aperture_size",
                    )
                )
        if node.node_type == "computer_vision.reference_edge_from_cv_edges":
            config = _config_with_spec_defaults(node, specs_by_key.get(node.node_key))
            rules = _cv_line_selection_rules_from_node(node, specs_by_key.get(node.node_key))
            required_roles = {
                str(rule.get("edge_role") or "")
                for rule in rules
                if bool(
                    rule.get(
                        "required", str(rule.get("edge_role") or "") in {"start_edge", "side_edge"}
                    )
                )
            }
            if not incoming.get((node.node_key, "edges")):
                issues.append(
                    _issue(
                        "error",
                        "cv_edges_input_missing",
                        "Reference Edge From CV Edges requires the Canny edge output.",
                        node_key=node.node_key,
                        field="edges",
                    )
                )
            if not {"start_edge", "side_edge"}.issubset(required_roles):
                issues.append(
                    _issue(
                        "error",
                        "cv_reference_edge_roles_missing",
                        "Configure required CV line selection rules for start_edge and side_edge.",
                        node_key=node.node_key,
                        field="line_selection_rules",
                    )
                )
            method = str(config.get("line_extraction_method") or "")
            if method != "hough_line_segments":
                issues.append(
                    _issue(
                        "error",
                        "cv_line_extraction_method_invalid",
                        "Only Hough line segments are currently publishable for CV reference edge extraction.",
                        node_key=node.node_key,
                        field="line_extraction_method",
                    )
                )
    return issues


def _config_with_spec_defaults(
    node: RuntimeNodeSpec,
    spec: RuntimeNodeTypeSummary | None,
) -> dict[str, Any]:
    if spec is None:
        return dict(node.config or {})
    return {**dict(spec.default_config or {}), **dict(node.config or {})}


def _measurement_bases_from_node(
    node: RuntimeNodeSpec,
    spec: RuntimeNodeTypeSummary | None = None,
) -> set[str]:
    bases: set[str] = set()
    config = _config_with_spec_defaults(node, spec)
    raw = config.get("measurement_rules") or []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            bases.update(_field_list(item.get("bases") or item.get("measurement_bases")))
    if not bases and str(config.get("fallback_behavior") or "use_bbox") == "use_bbox":
        bases.update({"bbox_width", "bbox_height", "approximate_diameter"})
    return bases


def _clearance_rules_from_node(
    node: RuntimeNodeSpec,
    spec: RuntimeNodeTypeSummary | None = None,
) -> list[dict[str, Any]]:
    raw = _config_with_spec_defaults(node, spec).get("clearance_rules") or []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _cv_line_selection_rules_from_node(
    node: RuntimeNodeSpec,
    spec: RuntimeNodeTypeSummary | None = None,
) -> list[dict[str, Any]]:
    raw = _config_with_spec_defaults(node, spec).get("line_selection_rules") or []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _validate_response_mapping_sources(
    graph: RuntimePipelineGraph,
    node: RuntimeNodeSpec,
) -> list[RuntimePipelineValidationIssue]:
    mappings = (node.config or {}).get("result_fields") or (node.config or {}).get("response_fields")
    if not isinstance(mappings, list) or not mappings:
        return []
    known = _known_result_fields_reaching_node(graph, node.node_key)
    if not known:
        return []
    issues: list[RuntimePipelineValidationIssue] = []
    for mapping in mappings:
        if not isinstance(mapping, dict):
            continue
        source_field = str(mapping.get("source_field") or "").strip()
        source_base = source_field.split(".", 1)[0]
        if not source_field or source_field in known or source_base in known:
            continue
        issues.append(
            _issue(
                "error",
                "response_field_not_produced",
                f"Response field {source_field} is not produced by a reachable upstream node.",
                node_key=node.node_key,
                field="response_fields",
                metadata={"source_field": source_field, "known_fields": sorted(known)},
            )
        )
    return issues


def _validate_output_projection_sources(
    graph: RuntimePipelineGraph,
    node: RuntimeNodeSpec,
) -> list[RuntimePipelineValidationIssue]:
    try:
        policy = output_policy_from_config(node.config or {}, output_node_key=node.node_key)
    except ValidationError as exc:
        return [
            _issue(
                "error",
                "result_projection_table_invalid",
                "Result projections are not valid.",
                node_key=node.node_key,
                field="projections",
                metadata={"errors": exc.errors()},
            )
        ]
    if policy is None:
        return []

    issues: list[RuntimePipelineValidationIssue] = []
    nodes_by_key = {item.node_key: item for item in graph.nodes}
    upstream_fields = _known_result_fields_reaching_node(graph, node.node_key)
    response_projection_count = 0
    for projection in policy.projections:
        if "response" in projection.destinations:
            response_projection_count += 1
        source = projection.source
        known_fields = upstream_fields
        if source.node_key:
            source_node = nodes_by_key.get(source.node_key)
            if source_node is None:
                issues.append(
                    _issue(
                        "error",
                        "projection_source_node_unknown",
                        f"Projection source node {source.node_key} does not exist.",
                        node_key=node.node_key,
                        field=f"projections.{projection.key}.source.node_key",
                    )
                )
                continue
            if source.node_key == node.node_key:
                known_fields = {
                    item.key for item in policy.projections
                } | _known_result_fields_for_node(node.node_type, node)
            else:
                known_fields = _known_result_fields_for_node(source_node.node_type, source_node)
        if source.path and not _projection_path_known(source.path, known_fields):
            issues.append(
                _issue(
                    "error",
                    "projection_source_field_not_produced",
                    f"Projection source field {source.path} is not produced by the selected nodes.",
                    node_key=node.node_key,
                    field=f"projections.{projection.key}.source.path",
                    metadata={
                        "source_field": source.path,
                        "known_fields": sorted(known_fields),
                    },
                )
            )
    if _infer_pipeline_mode(graph, fallback=RuntimeMode.REALTIME) == RuntimeMode.TRIGGER:
        if policy.projections and response_projection_count == 0:
            issues.append(
                _issue(
                    "error",
                    "trigger_response_projection_missing",
                    (
                        "Trigger pipelines must mark at least one Result projection for "
                        "the response destination."
                    ),
                    node_key=node.node_key,
                    field="projections",
                )
            )
    return issues


def _projection_path_known(path: str, known_fields: set[str]) -> bool:
    if not known_fields:
        return True
    if path in known_fields:
        return True
    base = path.split(".", 1)[0]
    if base in known_fields:
        return True
    return path.startswith("payload.")


def _validate_siemens_sps_write_node(
    *,
    graph: RuntimePipelineGraph,
    node: RuntimeNodeSpec,
    spec: RuntimeNodeTypeSummary | None,
    mode: RuntimeMode,
) -> list[RuntimePipelineValidationIssue]:
    known = _known_result_fields_reaching_node(graph, node.node_key)
    config = _config_with_spec_defaults(node, spec)
    validation_issues = validate_siemens_sps_write_config(
        config,
        known_fields=known,
        mode=mode,
        dependency_available=snap7_dependency_available(),
    )
    return [
        _issue(
            issue.severity,
            issue.code,
            issue.message,
            node_key=node.node_key,
            field=issue.field,
            metadata=issue.metadata or {},
        )
        for issue in validation_issues
    ]


def _known_result_fields_reaching_node(graph: RuntimePipelineGraph, node_key: str) -> set[str]:
    nodes_by_key = {node.node_key: node for node in graph.nodes}
    upstream = _upstream_node_keys(graph, node_key)
    fields: set[str] = set()
    for upstream_key in upstream:
        node = nodes_by_key.get(upstream_key)
        if node is None:
            continue
        fields.update(_known_result_fields_for_node(node.node_type, node))
    return fields


def _catalog_fields_for_node(
    node_type: str,
    node: RuntimeNodeSpec | None = None,
) -> list[RuntimePipelineOutputCatalogField]:
    return [
        RuntimePipelineOutputCatalogField(
            key=field,
            label=_catalog_field_label(field),
            value_type=_catalog_field_value_type(field),
            aggregate_ops=_catalog_field_aggregate_ops(field),
            filterable_fields=_catalog_filterable_fields(field),
            list_item_fields=_catalog_list_item_fields(field),
        )
        for field in sorted(_known_result_fields_for_node(node_type, node))
    ]


def _catalog_field_label(field: str) -> str:
    labels = {
        "active": "Active",
        "anomaly_score": "Anomaly score",
        "class": "Class",
        "classification": "Classification",
        "classification.top1.label": "Top class label",
        "classification.top1.score": "Top class score",
        "count": "Count",
        "count_total": "Total count",
        "decision": "Decision",
        "detections": "Detected objects",
        "distance_mm": "Distance mm",
        "distance_px": "Distance px",
        "evaluated_rules": "Evaluated rules",
        "feature_count": "Feature count",
        "feature_measurements": "Feature measurements",
        "free_region_found": "Free region found",
        "image_context_id": "Image context",
        "inside_count": "Objects inside zones",
        "is_anomalous": "Is anomalous",
        "labels": "Labels",
        "match_count": "Match count",
        "matches": "Matches",
        "objects": "Objects",
        "outside_count": "Objects outside zones",
        "outside_objects": "Outside objects",
        "predicted_label": "Predicted label",
        "quality.decision": "Quality decision",
        "quality.pass": "Quality pass",
        "schema_version": "Result contract version",
        "valid": "Measurement valid",
        "cut_position_mm": "Cut position [mm]",
        "longitudinal_shift_mm": "Longitudinal shift [mm]",
        "inspection_length_mm": "Inspection length [mm]",
        "capture_skew_ms": "Capture skew [ms]",
        "uncertainty_mm": "Uncertainty [mm]",
        "dominant_constraint": "Dominant cut constraint",
        "reason_code": "Reason code",
        "views_used": "Camera views used",
        "result": "Result",
        "result_label": "Result label",
        "rules": "Rule results",
        "score": "Score",
        "segments": "Segments",
        "signal": "Signal",
        "status": "Status",
        "threshold": "Threshold",
        "top_label": "Top label",
        "top_label.label": "Top label",
        "top_label.score": "Top score",
        "zones": "Zones",
    }
    if field in labels:
        return labels[field]
    return re.sub(r"[_\.]+", " ", field).strip().capitalize() or field


def _catalog_field_value_type(field: str) -> str:
    if field in {
        "objects",
        "detections",
        "segments",
        "matches",
        "zones",
        "rules",
        "labels",
        "features",
        "feature_measurements",
        "clearance_zones",
        "outside_objects",
        "regions",
        "rois",
        "evaluated_rules",
        "enrichments",
        "views_used",
    }:
        return "object_list"
    if field in {
        "top_label",
        "classification",
        "quality",
        "measurement",
        "frame",
        "heatmap",
        "dominant_constraint",
    } or field.endswith("_frame"):
        return "object"
    if (
        field == "count"
        or field.startswith("count_")
        or field.endswith("_count")
        or field.endswith("_total")
        or field.endswith("_px")
        or field.endswith("_mm")
        or field.endswith("_score")
        or field.endswith(".score")
        or field
        in {
            "score",
            "threshold",
            "distance_px",
            "distance_mm",
            "edge_pixel_count",
            "contour_count",
            "line_candidate_count",
            "feature_count",
            "zone_count",
            "inside_count",
            "outside_count",
            "match_count",
        }
    ):
        return "number"
    if field.startswith("is_") or field.endswith("_found") or field.endswith(".pass") or field in {
        "active",
        "result",
        "decision",
        "quality.pass",
        "valid",
    }:
        return "boolean"
    if field.endswith(".label") or field.endswith("_label") or field in {
        "class",
        "status",
        "signal",
        "predicted_label",
        "result_label",
        "image_context_id",
        "reason_code",
        "workpiece_id",
        "schema_version",
    }:
        return "text"
    return "auto"


def _catalog_field_aggregate_ops(field: str) -> list[str]:
    value_type = _catalog_field_value_type(field)
    if value_type == "number":
        return ["none", "first", "count", "sum", "avg", "min", "max"]
    if value_type == "boolean":
        return ["none", "any", "all", "count"]
    if value_type == "text":
        return ["none", "first", "count"]
    if value_type in {"object_list", "list"}:
        return ["count", "list", "first", "any", "all"]
    if value_type in {"object", "map"}:
        return ["none", "first"]
    return ["none", "count", "list", "first"]


def _catalog_filterable_fields(field: str) -> list[str]:
    value_type = _catalog_field_value_type(field)
    if value_type not in {"object_list", "list"}:
        return []
    return [
        "object_id",
        "label",
        "score",
        "zone",
        "defect",
        "defect_status",
        "skewed",
        "center.x",
        "center.y",
        "position.x",
        "position.y",
        "box.x",
        "box.y",
    ]


def _catalog_list_item_fields(field: str) -> list[str]:
    value_type = _catalog_field_value_type(field)
    if value_type not in {"object_list", "list"}:
        return []
    return [
        "object_id",
        "label",
        "score",
        "position",
        "center",
        "box",
        "zone",
        "defect_status",
    ]


def _known_result_fields_for_node(
    node_type: str,
    node: RuntimeNodeSpec | None = None,
) -> set[str]:
    common = {"result", "decision"}
    if node_type == "inference.detection":
        return common | {
            "count",
            "detection.count",
            "detections",
            "objects",
            "image_context_id",
        }
    if node_type == "inference.classification":
        return common | {
            "count",
            "labels",
            "top_label",
            "top_label.label",
            "top_label.score",
            "classification.top1.label",
            "classification.top1.score",
            "class",
            "classification",
            "score",
            "image_context_id",
            "source_object_id",
            "enrichments",
        }
    if node_type == "inference.segmentation":
        return common | {"count", "segments", "objects", "image_context_id"}
    if node_type == "inference.anomaly":
        return common | {
            "score",
            "anomaly.score",
            "anomaly.decision",
            "anomaly_score",
            "predicted_label",
            "is_anomalous",
            "threshold",
            "heatmap",
            "image_context_id",
        }
    if node_type == "computer_vision.canny_edge_detection":
        return common | {
            "cv_edges",
            "edge_pixel_count",
            "contour_count",
            "line_candidate_count",
            "image_context_id",
        }
    if node_type == "computer_vision.reference_edge_from_cv_edges":
        return common | {"reference_edges", "edge_count", "image_context_id"}
    if node_type == "computer_vision.color_object_segmentation":
        fields = common | {
            "count",
            "count_total",
            "counts_by_label",
            "objects",
            "regions",
            "image_context_id",
            "source",
            "algorithm",
        }
        if node is not None:
            config = dict(node.config or {})
            raw_profiles = config.get("profiles") or config.get("color_profiles") or []
            if isinstance(raw_profiles, list):
                for item in raw_profiles:
                    if not isinstance(item, dict) or item.get("enabled") is False:
                        continue
                    label = str(
                        item.get("label") or item.get("class_name") or ""
                    ).strip()
                    if label:
                        fields.add(f"count_{_result_key_from_label(label)}")
        return fields
    if node_type == "analysis.feature_measurement":
        return common | {"feature_measurements", "features", "feature_count"}
    if node_type in {
        "analysis.multi_view_board_cut_fusion",
        "analysis.single_view_board_cut_decision",
    }:
        return common | {
            "schema_version",
            "workpiece_id",
            "valid",
            "status",
            "cut_position_mm",
            "longitudinal_shift_mm",
            "inspection_length_mm",
            "capture_skew_ms",
            "views_used",
            "uncertainty_mm",
            "dominant_constraint",
            "reason_code",
        }
    if node_type == "analysis.clearance_zone_generator":
        return common | {"clearance_zones", "clearance_zone_count"}
    if node_type == "analysis.first_free_region_finder":
        return common | {
            "free_region",
            "free_region_found",
            "distance_px",
            "distance_mm",
            "distance_from_origin_px",
            "distance_from_origin_mm",
            "start_position_px",
            "start_position_mm",
        }
    if node_type == "analysis.pixel_to_metric":
        return common | {
            "mm_per_pixel",
            "reference_edges",
            "coordinate_frame",
            "feature_measurements",
            "features",
            "clearance_zones",
            "free_region",
            "free_region_found",
            "distance_px",
            "distance_mm",
            "distance_from_origin_px",
            "distance_from_origin_mm",
            "start_position_px",
            "start_position_mm",
            "status",
            "result_label",
            "decision",
        }
    if node_type == "analysis.quality_rule_evaluation":
        return common | {"status", "result_label", "evaluated_rules", "messages", "decision"}
    if node_type == "analysis.object_counting":
        return common | {"count"}
    if node_type == "analysis.detection_size":
        return common | {"count", "sizes", "measurements", "objects"}
    if node_type == "analysis.zone_detection":
        return common | {
            "signal",
            "active",
            "zone_match",
            "zone_count",
            "inside_count",
            "outside_count",
            "zones",
            "outside_objects",
            "rules",
            "matches",
            "match_count",
        }
    if node_type == "analysis.object_orientation_signal":
        return common | {
            "signal",
            "active",
            "signal_key",
            "match_policy",
            "match_count",
            "rules",
            "matches",
            "oriented_objects",
            "violation_count",
            "inspected_count",
            "status",
        }
    if node_type in {"analysis.close_detection", "analysis.object_spatial_signal"}:
        return common | {
            "signal",
            "active",
            "signal_key",
            "match_policy",
            "match_count",
            "rules",
            "matches",
        }
    if node_type == "analysis.movement_direction":
        return common | {
            "signal",
            "active",
            "direction",
            "direction_label",
            "matches",
            "match_count",
            "tracks",
        }
    if node_type == "analysis.rule_evaluation":
        return common | {"count", "rule_expression"}
    if node_type == "analysis.stem_curvature_phase_solver":
        # Runtime projection evaluation exposes the immutable V3 decision under this
        # namespace once RESULT_READY is reached. The value is intentionally absent
        # during earlier session states, which is why template projections are optional.
        return common | {
            "stem_curvature_phase_solution",
            "stem_orientation_result",
            "session_id",
            "track_id",
            "state",
            "observation_count",
            "observed_phase_span_deg",
            "curvature_frozen",
            "decision_gate_reached",
            "target_phase_deg",
            "maximum_phase_deg",
            "fitted_min_curvature_mm",
            "fitted_max_curvature_mm",
            "curvature_span_mm",
            "minimum_unique",
            "bow_relevant",
            "confidence",
            "reason_codes",
        }
    return common


def _validate_inference_node(
    session: Session,
    node: RuntimeNodeSpec,
    spec: RuntimeNodeTypeSummary,
) -> list[RuntimePipelineValidationIssue]:
    issues: list[RuntimePipelineValidationIssue] = []
    model_ref = node.logical_model_ref
    if model_ref is None:
        return [
            _issue(
                "error",
                "model_reference_missing",
                "Inference nodes require a logical model reference.",
                node_key=node.node_key,
                field="logical_model_ref",
            )
        ]
    required_task_type = spec.compatibility_constraints.get("required_task_type")
    if required_task_type and not _runtime_task_type_compatible(
        required_task_type,
        model_ref.required_task_type.value,
    ):
        issues.append(
            _issue(
                "error",
                "model_task_incompatible",
                (
                    f"{spec.display_name} requires a {required_task_type} model, "
                    f"but the node references {model_ref.required_task_type.value}."
                ),
                node_key=node.node_key,
                field="logical_model_ref",
            )
        )
    allowed_target_paths = spec.compatibility_constraints.get("allowed_target_paths") or []
    if allowed_target_paths and model_ref.target_path.value not in allowed_target_paths:
        issues.append(
            _issue(
                "error",
                "model_target_incompatible",
                f"{spec.display_name} must use one of: {', '.join(allowed_target_paths)}.",
                node_key=node.node_key,
                field="logical_model_ref",
            )
        )
    refresh_runtime_model_capability_cache(session)
    rows = session.scalars(
        select(RuntimeModelCapabilityCache).where(
            RuntimeModelCapabilityCache.model_family_slug == model_ref.model_family_slug,
            RuntimeModelCapabilityCache.target_path == model_ref.target_path.value,
            RuntimeModelCapabilityCache.contract_major == model_ref.contract_major,
        )
    ).all()
    now = utc_now()
    rows = [
        row
        for row in rows
        if _not_expired(row.expires_at, now)
    ]
    if model_ref.selection_policy == "explicit_release":
        rows = [row for row in rows if row.deployment_release_id == model_ref.deployment_release_id]
    selectable_rows = [
        row
        for row in rows
        if runtime_model_capability_is_selectable_for_policy(
            dict(row.capability or {}),
            model_ref.selection_policy,
        )
    ]
    if not selectable_rows:
        has_matching_ref = bool(rows)
        statuses = sorted(
            {
                str((row.capability or {}).get("deployment_status") or "unknown")
                for row in rows
            }
        )
        issues.append(
            _issue(
                "error",
                "model_selection_policy_unavailable"
                if has_matching_ref
                else "model_reference_missing",
                (
                    _model_reference_policy_message(model_ref.selection_policy, statuses)
                    if has_matching_ref
                    else (
                        "No deployed model capability matches this logical model reference. "
                        "Choose a published model before publishing the pipeline."
                    )
                ),
                node_key=node.node_key,
                field="logical_model_ref",
            )
        )
        return issues
    for row in selectable_rows:
        if not _runtime_task_type_compatible(
            model_ref.required_task_type.value,
            row.task_type,
        ):
            issues.append(
                _issue(
                    "error",
                    "model_task_incompatible",
                    (
                        f"Model capability reports {row.task_type}, "
                        f"but the node requires {model_ref.required_task_type.value}."
                    ),
                    node_key=node.node_key,
                    field="logical_model_ref",
                )
            )
    return issues


def _validate_inference_image_view_graph(
    session: Session,
    *,
    graph: RuntimePipelineGraph,
    nodes_by_key: dict[str, RuntimeNodeSpec],
) -> list[RuntimePipelineValidationIssue]:
    """Validate the selected immutable model contract against its upstream view."""
    issues: list[RuntimePipelineValidationIssue] = []
    incoming_by_node: dict[str, list[RuntimeGraphEdge]] = defaultdict(list)
    for edge in graph.edges:
        incoming_by_node[edge.to_node_key].append(edge)

    for node in graph.nodes:
        if node.node_category != RuntimeNodeCategory.INFERENCE:
            continue
        report_requested = bool(
            (node.config or {}).get("require_image_view_compatibility", False)
        )
        model_ref = node.logical_model_ref
        if model_ref is None:
            continue

        pending = deque([node.node_key])
        visited: set[str] = set()
        view_profiles = []
        while pending:
            current = pending.popleft()
            if current in visited:
                continue
            visited.add(current)
            for edge in incoming_by_node.get(current, []):
                upstream = nodes_by_key.get(edge.from_node_key)
                if upstream is None:
                    continue
                if upstream.node_type == "preprocessing.process_coordinate_rectify":
                    try:
                        view_profiles.append(
                            validate_image_view_profile(
                                (upstream.config or {}).get("view_profile")
                            )
                        )
                    except (TypeError, ValueError):
                        pass
                else:
                    pending.append(upstream.node_key)
        unique_profiles = {
            (profile.view_profile_id, profile.profile_checksum): profile
            for profile in view_profiles
        }
        if len(unique_profiles) != 1:
            continue
        view_profile = next(iter(unique_profiles.values()))

        rows = session.scalars(
            select(RuntimeModelCapabilityCache).where(
                RuntimeModelCapabilityCache.model_family_slug == model_ref.model_family_slug,
                RuntimeModelCapabilityCache.target_path == model_ref.target_path.value,
                RuntimeModelCapabilityCache.contract_major == model_ref.contract_major,
            )
        ).all()
        now = utc_now()
        rows = [row for row in rows if _not_expired(row.expires_at, now)]
        if model_ref.selection_policy == "explicit_release":
            rows = [
                row
                for row in rows
                if row.deployment_release_id == model_ref.deployment_release_id
            ]
        rows = [
            row
            for row in rows
            if runtime_model_capability_is_selectable_for_policy(
                dict(row.capability or {}), model_ref.selection_policy
            )
        ]
        if len(rows) != 1:
            # The ordinary model-reference validator reports missing/ambiguous releases.
            continue
        capability = dict(rows[0].capability or {})
        preprocessing = capability.get("preprocessing")
        requirement = (
            preprocessing.get("image_view") if isinstance(preprocessing, dict) else None
        )
        if not isinstance(requirement, dict):
            if report_requested:
                issues.append(
                    _issue(
                        "warning",
                        "model_image_view_metadata_missing",
                        (
                            "The selected model does not declare its training image view. This "
                            "is provenance information only and does not block inference."
                        ),
                        node_key=node.node_key,
                        field="logical_model_ref",
                    )
                )
            continue

        actual = {
            "view_profile_id": view_profile.view_profile_id,
            "profile_checksum": view_profile.profile_checksum,
            "input_width": view_profile.output_width,
            "input_height": view_profile.output_height,
            "color_space": "BGR",
            "undistort_preprocessing_version": "camera.undistort.v1",
            "image_view_preprocessing_version": "image.view-runtime.v1",
        }
        mismatches = {
            key: {"expected": expected, "actual": actual.get(key)}
            for key, expected in requirement.items()
            if key in actual and expected is not None and expected != actual.get(key)
        }
        if not mismatches:
            continue
        if not report_requested:
            continue
        mismatch_text = "; ".join(
            f"{key}: model={values['expected']}, view={values['actual']}"
            for key, values in mismatches.items()
        )
        issues.append(
            _issue(
                "warning",
                "model_image_view_difference",
                (
                    "The selected model was trained with different image-view metadata: "
                    f"{mismatch_text}. Inference remains enabled; verify segmentation quality "
                    "for this camera and view."
                ),
                node_key=node.node_key,
                field="logical_model_ref",
                metadata={"mismatches": mismatches},
            )
        )
    return issues


def _find_port(ports: Iterable[Any], key: str) -> Any | None:
    for candidate in ports:
        if candidate.key == key:
            return candidate
    return None


def _ports_compatible(
    output_type: str,
    input_type: str,
    input_node_type: RuntimeNodeTypeSummary,
) -> bool:
    if output_type == input_type:
        return True
    aliases = input_node_type.compatibility_constraints.get("port_aliases", {})
    accepted = aliases.get(input_type, [])
    if output_type in accepted:
        return True
    if input_type == "result" and output_type in {
        "detections",
        "classifications",
        "segments",
        "anomaly",
        "positions",
        "measurements",
        "stem_horizontal_curvature",
        "cv_edges",
        "reference_edges",
        "coordinate_frame",
        "feature_measurements",
        "clearance_zones",
        "free_region",
        "quality_decision",
        "events",
        "roi_set",
        "frame",
        "overlay",
        "object_set",
        "region_set",
    }:
        return True
    if input_type == "object_set" and output_type in {
        "detections",
        "segments",
        "region_set",
        "object_set",
    }:
        return True
    return False


def _has_cycle(graph: RuntimePipelineGraph) -> bool:
    nodes = {node.node_key for node in graph.nodes}
    indegree = {node_key: 0 for node_key in nodes}
    adjacency: dict[str, list[str]] = {node_key: [] for node_key in nodes}
    for edge in graph.edges:
        if edge.from_node_key not in nodes or edge.to_node_key not in nodes:
            continue
        adjacency[edge.from_node_key].append(edge.to_node_key)
        indegree[edge.to_node_key] += 1
    queue = deque([node_key for node_key, degree in indegree.items() if degree == 0])
    visited = 0
    while queue:
        node_key = queue.popleft()
        visited += 1
        for next_key in adjacency[node_key]:
            indegree[next_key] -= 1
            if indegree[next_key] == 0:
                queue.append(next_key)
    return visited != len(nodes)


def _version_summary(version: PipelineVersion) -> RuntimePipelineVersionSummary:
    report = _report_from_row(version)
    return RuntimePipelineVersionSummary(
        pipeline_version_id=version.id,
        pipeline_definition_id=version.pipeline_definition_id,
        version_no=version.version_no,
        status=RuntimePipelineStatus(version.status),
        mode=RuntimeMode(version.mode),
        config_digest=version.config_digest,
        validation_status=report.status,
        error_count=report.error_count,
        warning_count=report.warning_count,
        change_note=version.change_note,
        created_at=_iso(version.created_at),
        published_at=_iso(version.published_at),
    )


def _version_detail(version: PipelineVersion) -> RuntimePipelineVersionDetail:
    return RuntimePipelineVersionDetail(
        **_version_summary(version).model_dump(),
        graph=RuntimePipelineGraph.model_validate(version.graph),
        validation_report=_report_from_row(version),
        model_capability_constraints=version.model_capability_constraints or {},
        node_contract_versions=version.node_contract_versions or {},
    )


def _definition_summary(
    row: PipelineDefinition,
    *,
    version_count: int,
    version_modes: dict[str, RuntimeMode] | None = None,
) -> RuntimePipelineDefinitionSummary:
    version_modes = version_modes or {}
    return RuntimePipelineDefinitionSummary(
        pipeline_definition_id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        status=RuntimePipelineStatus(row.status),
        latest_version_id=row.latest_version_id,
        latest_version_mode=version_modes.get(row.latest_version_id or ""),
        published_version_id=row.published_version_id,
        published_version_mode=version_modes.get(row.published_version_id or ""),
        template=row.template,
        scope=row.scope,
        owner_channel_id=row.owner_channel_id,
        source_template_definition_id=row.source_template_definition_id,
        version_count=version_count,
    )


def _report_from_row(version: PipelineVersion) -> RuntimePipelineValidationReport:
    if version.validation_report:
        return RuntimePipelineValidationReport.model_validate(version.validation_report)
    return RuntimePipelineValidationReport(
        status="invalid",
        checked_at=utc_now_iso(),
        error_count=1,
        warning_count=0,
        issues=[
            RuntimePipelineValidationIssue(
                severity="error",
                code="validation_missing",
                message="This version has not been validated yet.",
            )
        ],
    )


def _registry_row_to_contract(row: RuntimeNodeTypeRegistry) -> RuntimeNodeTypeSummary:
    return RuntimeNodeTypeSummary(
        node_type=row.node_type,
        node_category=RuntimeNodeCategory(row.node_category),
        contract_version=row.contract_version,
        display_name=row.display_name,
        description=row.description or "",
        supported_modes=[RuntimeMode(item) for item in row.supported_modes],
        input_ports=[RuntimeNodePort.model_validate(item) for item in row.input_ports],
        output_ports=[RuntimeNodePort.model_validate(item) for item in row.output_ports],
        config_schema_ref=row.config_schema_ref,
        property_schema=list(row.property_schema or []),
        default_config=dict(row.default_config or {}),
        runtime_handler_ref=row.runtime_handler_ref,
        compatibility_constraints=row.compatibility_constraints or {},
    )


def _node_type_payload(node_type: RuntimeNodeTypeSummary) -> dict[str, Any]:
    return {
        "node_category": node_type.node_category.value,
        "display_name": node_type.display_name,
        "description": node_type.description,
        "supported_modes": [item.value for item in node_type.supported_modes],
        "input_ports": [item.model_dump(mode="json") for item in node_type.input_ports],
        "output_ports": [item.model_dump(mode="json") for item in node_type.output_ports],
        "config_schema_ref": node_type.config_schema_ref,
        "property_schema": [item.model_dump(mode="json") for item in node_type.property_schema],
        "default_config": node_type.default_config,
        "runtime_handler_ref": node_type.runtime_handler_ref,
        "compatibility_constraints": node_type.compatibility_constraints,
    }


def _model_constraints(graph: RuntimePipelineGraph) -> dict[str, Any]:
    refs = []
    for node in graph.nodes:
        if node.logical_model_ref:
            refs.append(
                {
                    "node_key": node.node_key,
                    **node.logical_model_ref.model_dump(mode="json"),
                }
            )
    return {"model_refs": refs}


def _digest_graph(graph: RuntimePipelineGraph) -> str:
    payload = json.dumps(graph.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _version_counts(session: Session, definition_ids: list[str]) -> dict[str, int]:
    if not definition_ids:
        return {}
    rows = session.execute(
        select(PipelineVersion.pipeline_definition_id, func.count(PipelineVersion.id))
        .where(PipelineVersion.pipeline_definition_id.in_(definition_ids))
        .group_by(PipelineVersion.pipeline_definition_id)
    ).all()
    return {str(definition_id): int(count) for definition_id, count in rows}


def _definition_reference_version_modes(
    session: Session,
    definitions: list[PipelineDefinition],
) -> dict[str, RuntimeMode]:
    version_ids = {
        version_id
        for definition in definitions
        for version_id in (definition.latest_version_id, definition.published_version_id)
        if version_id
    }
    if not version_ids:
        return {}
    rows = session.execute(
        select(PipelineVersion.id, PipelineVersion.mode).where(PipelineVersion.id.in_(version_ids))
    ).all()
    return {str(version_id): RuntimeMode(mode) for version_id, mode in rows}


def _latest_version(session: Session, definition: PipelineDefinition) -> PipelineVersion | None:
    if definition.latest_version_id:
        return session.get(PipelineVersion, definition.latest_version_id)
    return session.scalar(
        select(PipelineVersion)
        .where(PipelineVersion.pipeline_definition_id == definition.id)
        .order_by(PipelineVersion.version_no.desc())
        .limit(1)
    )


def _find_definition(session: Session, pipeline_definition_id: str) -> PipelineDefinition | None:
    return session.scalar(
        select(PipelineDefinition).where(
            (PipelineDefinition.id == pipeline_definition_id)
            | (PipelineDefinition.slug == pipeline_definition_id)
        )
    )


def _get_definition(session: Session, pipeline_definition_id: str) -> PipelineDefinition:
    row = _find_definition(session, pipeline_definition_id)
    if row is None:
        raise ValueError("Pipeline definition does not exist.")
    return row


def _channel_instance_source_version(
    session: Session,
    *,
    channel: RuntimeChannel,
    payload: RuntimeChannelPipelineInstanceRequest,
) -> PipelineVersion:
    if payload.template_version_id:
        return _get_version(session, payload.template_version_id)
    if payload.template_definition_id:
        definition = _get_definition(session, payload.template_definition_id)
        version_id = definition.published_version_id or definition.latest_version_id
        if not version_id:
            raise ValueError("The selected pipeline template has no version to copy.")
        return _get_version(session, version_id)
    if channel.pipeline_version_id:
        return _get_version(session, channel.pipeline_version_id)
    raise ValueError("Choose a pipeline template before creating a channel pipeline instance.")


def _get_version(session: Session, pipeline_version_id: str) -> PipelineVersion:
    row = session.get(PipelineVersion, pipeline_version_id)
    if row is None:
        raise ValueError("Pipeline version does not exist.")
    return row


def _unique_slug(session: Session, value: str) -> str:
    base = _slugify(value)
    existing = set(session.scalars(select(PipelineDefinition.slug)).all())
    candidate = base
    suffix = 2
    while candidate in existing:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _slugify(value: str) -> str:
    candidate = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return candidate or "pipeline"


def _issue(
    severity: str,
    code: str,
    message: str,
    *,
    node_key: str | None = None,
    edge_index: int | None = None,
    field: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> RuntimePipelineValidationIssue:
    return RuntimePipelineValidationIssue(
        severity=severity,  # type: ignore[arg-type]
        code=code,
        message=message,
        node_key=node_key,
        edge_index=edge_index,
        field=field,
        metadata=metadata or {},
    )


def _empty_value(value: Any) -> bool:
    return value is None or value == "" or value == []


def _valid_image_upload(value: Any) -> bool:
    size = _image_upload_size_bytes(value)
    return size is not None and size <= 5 * 1024 * 1024


def _image_upload_size_bytes(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    data_url = value.get("data_url")
    if not isinstance(data_url, str) or "," not in data_url:
        return None
    header, encoded = data_url.split(",", 1)
    if header not in {
        "data:image/jpeg;base64",
        "data:image/png;base64",
        "data:image/webp;base64",
        "data:image/bmp;base64",
    }:
        return None
    try:
        return len(base64.b64decode(encoded, validate=True))
    except (ValueError, binascii.Error):
        return None


def _valid_image_upload_list(value: Any, *, require_photo: bool = False) -> bool:
    if value in (None, []):
        return not require_photo
    if not isinstance(value, list) or len(value) > 100 or (require_photo and not value):
        return False
    if not all(_valid_image_upload(item) for item in value):
        return False
    return sum(_image_upload_size_bytes(item) or 0 for item in value) <= 100 * 1024 * 1024


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def _valid_response_mapping(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        source_field = item.get("source_field")
        response_key = item.get("response_key", source_field)
        if not isinstance(source_field, str) or not source_field.strip():
            return False
        if not isinstance(response_key, str) or not response_key.strip():
            return False
    return True


def _valid_result_projection_table(value: Any) -> bool:
    if value in (None, "", []):
        return True
    if not isinstance(value, list):
        return False
    try:
        output_policy_from_config({"projections": value})
    except ValidationError:
        return False
    return True


def _valid_edge_role_mapping(value: Any) -> bool:
    roles = {"start_edge", "side_edge", "left_edge", "right_edge", "custom"}
    sides = {"left", "right", "top", "bottom", "longest_side"}
    if not isinstance(value, list) or not value:
        return False
    seen_roles: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            return False
        feature_class = str(item.get("feature_class") or item.get("class_name") or "").strip()
        role = str(item.get("edge_role") or item.get("role") or "").strip()
        bbox_side = str(item.get("bbox_side") or "longest_side").strip()
        if not feature_class or role not in roles or bbox_side not in sides:
            return False
        seen_roles.add(role)
    return {"start_edge", "side_edge"}.issubset(seen_roles)


def _valid_measurement_rule_table(value: Any) -> bool:
    allowed = _measurement_bases()
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        feature_type = str(item.get("feature_type") or "").strip()
        bases = _field_list(item.get("bases") or item.get("measurement_bases"))
        if not feature_type or not bases or any(basis not in allowed for basis in bases):
            return False
    return True


def _valid_board_surface_calibration(
    value: Any,
    *,
    node: RuntimeNodeSpec,
    field_key: str,
) -> bool:
    entries = [value] if isinstance(value, dict) else value
    if not isinstance(entries, list) or not entries:
        return False
    if node.node_type == "analysis.board_view_cut_constraints":
        view_role = str((node.config or {}).get("view_role") or "upper")
        expected = {"top", "side_a"} if view_role == "upper" else {"bottom", "side_b"}
    elif node.node_type == "analysis.end_face_cut_constraints":
        expected = {"end_face"}
    else:
        expected = set()
    roles: set[str] = set()
    image_longitudinal_directions: list[tuple[float, float]] = []
    image_board_end_directions: list[tuple[float, float]] = []
    required_length = None
    if node.node_type == "analysis.board_view_cut_constraints":
        config = node.config or {}
        required_length = float(config.get("inspection_length_mm", 300.0)) + max(
            float(config.get("safe_visibility_margin_mm", 100.0)), 0.0
        )
    for item in entries:
        if not isinstance(item, dict):
            return False
        role = str(item.get("surface_role") or "")
        image_points = item.get("image_points")
        world_points = item.get("world_points_mm")
        if not role or role in roles:
            return False
        if not _valid_calibration_quad(image_points, normalized=True):
            return False
        if not _valid_calibration_quad(world_points, normalized=False):
            return False
        upper_longitudinal = (
            float(image_points[1]["x"]) - float(image_points[0]["x"]),
            float(image_points[1]["y"]) - float(image_points[0]["y"]),
        )
        lower_longitudinal = (
            float(image_points[2]["x"]) - float(image_points[3]["x"]),
            float(image_points[2]["y"]) - float(image_points[3]["y"]),
        )
        board_end_direction = (
            float(image_points[0]["x"]) - float(image_points[3]["x"]),
            float(image_points[0]["y"]) - float(image_points[3]["y"]),
        )
        if (
            upper_longitudinal[0] ** 2 + upper_longitudinal[1] ** 2 <= 1e-12
            or lower_longitudinal[0] ** 2 + lower_longitudinal[1] ** 2 <= 1e-12
            or board_end_direction[0] ** 2 + board_end_direction[1] ** 2 <= 1e-12
        ):
            return False
        if (
            upper_longitudinal[0] * lower_longitudinal[0]
            + upper_longitudinal[1] * lower_longitudinal[1]
            <= 0
        ):
            return False
        image_direction = (
            upper_longitudinal[0] + lower_longitudinal[0],
            upper_longitudinal[1] + lower_longitudinal[1],
        )
        cross_product = abs(
            image_direction[0] * board_end_direction[1]
            - image_direction[1] * board_end_direction[0]
        )
        if cross_product <= 1e-9:
            return False
        image_longitudinal_directions.append(image_direction)
        image_board_end_directions.append(board_end_direction)
        roles.add(role)
        world_x_values = [float(point["x"]) for point in world_points]
        if (
            required_length is not None
            and max(world_x_values) - min(world_x_values) < required_length
        ):
            return False
    if expected and roles != expected:
        return False
    if len(image_longitudinal_directions) > 1:
        first = image_longitudinal_directions[0]
        if any(
            first[0] * direction[0] + first[1] * direction[1] <= 0
            for direction in image_longitudinal_directions[1:]
        ):
            return False
    if len(image_board_end_directions) > 1:
        first = image_board_end_directions[0]
        if any(
            first[0] * direction[0] + first[1] * direction[1] <= 0
            for direction in image_board_end_directions[1:]
        ):
            return False
    return True


def _valid_camera_intrinsic_calibration(value: Any) -> bool:
    try:
        validate_camera_intrinsic_calibration(value, require_valid=True)
    except (TypeError, ValueError):
        return False
    return True


def _valid_stem_curvature_process_calibration(value: Any) -> bool:
    try:
        validate_stem_curvature_process_calibration(value, require_valid=True)
    except (TypeError, ValueError):
        return False
    return True


def _valid_image_view_profile(value: Any) -> bool:
    try:
        validate_image_view_profile(value)
    except (TypeError, ValueError):
        return False
    return True


def _valid_stem_surface_phase_calibration(value: Any) -> bool:
    try:
        validate_stem_surface_phase_calibration(value)
    except (TypeError, ValueError):
        return False
    return True


def _valid_stem_minimum_principle_validation(value: Any) -> bool:
    try:
        validate_stem_minimum_principle_validation_artifact(value)
    except (TypeError, ValueError):
        return False
    return True


def _validate_stem_shadow_mode_graph(
    *,
    graph: RuntimePipelineGraph,
) -> list[RuntimePipelineValidationIssue]:
    shadow_solvers = [
        node
        for node in graph.nodes
        if node.node_type == "analysis.stem_curvature_phase_solver"
        and bool((node.config or {}).get("shadow_mode_enabled", False))
    ]
    if not shadow_solvers:
        return []
    active_sps_nodes = [
        node
        for node in graph.nodes
        if node.node_type == "output.siemens_sps_write"
        and bool((node.config or {}).get("enabled", False))
        and not bool((node.config or {}).get("dry_run", True))
    ]
    plc_projection_nodes = [
        node
        for node in graph.nodes
        if any(
            isinstance(projection.get("destinations"), list)
            and "plc" in projection["destinations"]
            for projection in ((node.config or {}).get("projections") or [])
            if isinstance(projection, dict)
        )
    ]
    forbidden = active_sps_nodes or plc_projection_nodes
    if not forbidden:
        return []
    return [
        _issue(
            "error",
            "stem_shadow_mode_plc_output_forbidden",
            "Stem shadow mode cannot be published with an active PLC write path.",
            node_key=forbidden[0].node_key,
            metadata={"shadow_solver_node_keys": [node.node_key for node in shadow_solvers]},
        )
    ]


def _validate_stem_minimum_principle_validation_graph(
    *,
    graph: RuntimePipelineGraph,
    nodes_by_key: dict[str, RuntimeNodeSpec],
) -> list[RuntimePipelineValidationIssue]:
    issues: list[RuntimePipelineValidationIssue] = []
    incoming_by_node: dict[str, list[RuntimeGraphEdge]] = defaultdict(list)
    for edge in graph.edges:
        incoming_by_node[edge.to_node_key].append(edge)
    for node in graph.nodes:
        if node.node_type != "analysis.stem_curvature_phase_solver":
            continue
        raw_artifact = (node.config or {}).get("minimum_principle_validation")
        if _empty_value(raw_artifact):
            continue
        try:
            artifact = validate_stem_minimum_principle_validation_artifact(raw_artifact)
        except (TypeError, ValueError):
            continue
        pending = deque([node.node_key])
        visited: set[str] = set()
        process_profiles = []
        process_view_dimensions: dict[str, tuple[int, int]] = {}
        phase_profiles = []
        while pending:
            current = pending.popleft()
            if current in visited:
                continue
            visited.add(current)
            for edge in incoming_by_node.get(current, []):
                upstream = nodes_by_key.get(edge.from_node_key)
                if upstream is None:
                    continue
                if upstream.node_type == "preprocessing.process_coordinate_rectify":
                    try:
                        process_profile = validate_stem_curvature_process_calibration(
                            (upstream.config or {}).get("calibration"),
                            require_valid=True,
                        )
                        process_profiles.append(process_profile)
                        process_view_dimensions[process_profile.calibration_hash] = (
                            _rectified_view_dimensions(upstream, process_profile)
                        )
                    except (TypeError, ValueError):
                        pass
                elif upstream.node_type == "analysis.stem_surface_phase":
                    try:
                        phase_profiles.append(
                            validate_stem_surface_phase_calibration(
                                (upstream.config or {}).get("surface_phase_calibration")
                            )
                        )
                    except (TypeError, ValueError):
                        pass
                    pending.append(upstream.node_key)
                else:
                    pending.append(upstream.node_key)
        process_hashes = {item.calibration_hash for item in process_profiles}
        phase_hashes = {item.calibration_hash for item in phase_profiles}
        compatible = (
            process_hashes == {artifact.process_calibration_hash}
            and phase_hashes == {artifact.phase_calibration_hash}
            and bool(process_profiles)
            and bool(phase_profiles)
        )
        if compatible:
            process = process_profiles[0]
            phase = phase_profiles[0]
            expected_width, expected_height = process_view_dimensions.get(
                process.calibration_hash,
                (process.image_width, process.image_height),
            )
            compatible = (
                artifact.source_channel_id == process.source_channel_id
                and artifact.source_channel_id == phase.source_channel_id
                and artifact.process_calibration_id == process.calibration_id
                and artifact.phase_calibration_id == phase.calibration_id
                and artifact.phase_calibration_version == phase.calibration_version
                and artifact.image_width == expected_width == phase.image_width
                and artifact.image_height == expected_height == phase.image_height
            )
        if not compatible:
            issues.append(
                _issue(
                    "error",
                    "stem_minimum_principle_validation_binding_invalid",
                    (
                        "The objective minimum-principle validation must match the unique "
                        "upstream process and surface-phase calibration versions."
                    ),
                    node_key=node.node_key,
                    field="minimum_principle_validation",
                )
            )
    return issues


def _validate_stem_surface_phase_calibration_graph(
    *,
    graph: RuntimePipelineGraph,
    nodes_by_key: dict[str, RuntimeNodeSpec],
    incoming: dict[tuple[str, str], list[RuntimeGraphEdge]],
) -> list[RuntimePipelineValidationIssue]:
    issues: list[RuntimePipelineValidationIssue] = []
    incoming_by_node: dict[str, list[RuntimeGraphEdge]] = defaultdict(list)
    for edge in graph.edges:
        incoming_by_node[edge.to_node_key].append(edge)
    for node in graph.nodes:
        if node.node_type != "analysis.stem_surface_phase":
            continue
        try:
            phase_profile = validate_stem_surface_phase_calibration(
                (node.config or {}).get("surface_phase_calibration")
            )
        except (TypeError, ValueError):
            continue
        pending = deque([node.node_key])
        visited: set[str] = set()
        process_profiles = []
        process_view_dimensions: dict[str, tuple[int, int]] = {}
        while pending:
            current = pending.popleft()
            if current in visited:
                continue
            visited.add(current)
            for edge in incoming_by_node.get(current, []):
                upstream = nodes_by_key.get(edge.from_node_key)
                if upstream is None:
                    continue
                if upstream.node_type == "preprocessing.process_coordinate_rectify":
                    try:
                        process_profile = validate_stem_curvature_process_calibration(
                            (upstream.config or {}).get("calibration"),
                            require_valid=True,
                        )
                        process_profiles.append(process_profile)
                        process_view_dimensions[process_profile.calibration_hash] = (
                            _rectified_view_dimensions(upstream, process_profile)
                        )
                    except (TypeError, ValueError):
                        pass
                else:
                    pending.append(upstream.node_key)
        hashes = {item.calibration_hash for item in process_profiles}
        compatible = bool(process_profiles) and hashes == {phase_profile.process_calibration_hash}
        if not compatible:
            upstream_hashes = ", ".join(sorted(item[:12] for item in hashes)) or "none"
            issues.append(
                _issue(
                    "error",
                    "stem_surface_phase_process_calibration_mismatch",
                    (
                        "Surface phase calibration is bound to process profile "
                        f"{phase_profile.process_calibration_hash[:12]}, but the unique "
                        f"upstream process profile is {upstream_hashes}. Re-fit and apply "
                        "the degree calibration after applying the current process calibration."
                    ),
                    node_key=node.node_key,
                    field="surface_phase_calibration",
                )
            )
            continue
        process_profile = process_profiles[0]
        expected_width, expected_height = process_view_dimensions.get(
            process_profile.calibration_hash,
            (process_profile.image_width, process_profile.image_height),
        )
        if (
            phase_profile.intrinsic_calibration_hash
            != process_profile.intrinsic_calibration_hash
            or phase_profile.rotation_reference_view
            != process_profile.rotation_reference_view
            or phase_profile.positive_rotation != process_profile.positive_rotation
            or phase_profile.image_width != expected_width
            or phase_profile.image_height != expected_height
            or phase_profile.phase_estimator_version
            != str(
                (node.config or {}).get("phase_estimator_version")
                or "LK_KEYFRAME_RELOCK_V1"
            )
        ):
            issues.append(
                _issue(
                    "error",
                    "stem_surface_phase_calibration_binding_invalid",
                    (
                        "Surface phase profile binding does not match process calibration "
                        "or the selected phase estimator. Re-record and fit the calibration "
                        "after changing the estimator."
                    ),
                    node_key=node.node_key,
                    field="surface_phase_calibration",
                )
            )
    return issues


def _rectified_view_dimensions(
    node: RuntimeNodeSpec,
    process_profile: Any,
) -> tuple[int, int]:
    raw_view_profile = (node.config or {}).get("view_profile")
    if not _empty_value(raw_view_profile):
        view_profile = validate_image_view_profile(raw_view_profile)
        return view_profile.output_width, view_profile.output_height
    return process_profile.image_width, process_profile.image_height


def _validate_stem_process_calibration_graph(
    *,
    graph: RuntimePipelineGraph,
    nodes_by_key: dict[str, RuntimeNodeSpec],
    incoming: dict[tuple[str, str], list[RuntimeGraphEdge]],
) -> list[RuntimePipelineValidationIssue]:
    issues: list[RuntimePipelineValidationIssue] = []
    for node in graph.nodes:
        if node.node_type != "preprocessing.process_coordinate_rectify":
            continue
        frame_edges = incoming.get((node.node_key, "frame"), [])
        if len(frame_edges) != 1:
            continue
        upstream = nodes_by_key.get(frame_edges[0].from_node_key)
        if upstream is None or upstream.node_type != "preprocessing.camera_undistort":
            issues.append(
                _issue(
                    "error",
                    "stem_process_intrinsic_undistort_required",
                    (
                        "Stem process rectification must directly consume the frame from "
                        "Camera undistort."
                    ),
                    node_key=node.node_key,
                    field="frame",
                )
            )
            continue
        try:
            intrinsic_profile = validate_camera_intrinsic_calibration(
                (upstream.config or {}).get("calibration"),
                require_valid=True,
            )
        except (TypeError, ValueError):
            continue
        raw_view_profile = (node.config or {}).get("view_profile")
        raw_process_profile = (node.config or {}).get("calibration")
        view_profile = None
        if _empty_value(raw_view_profile) and _empty_value(raw_process_profile):
            issues.append(
                _issue(
                    "error",
                    "image_view_profile_missing",
                    (
                        "Canonical view is not configured yet. Set the four corridor corners "
                        "on the camera image in this node and select Calculate and apply view."
                    ),
                    node_key=node.node_key,
                    field="view_profile",
                )
            )
            continue
        if not _empty_value(raw_view_profile):
            try:
                view_profile = validate_image_view_profile(raw_view_profile)
            except (TypeError, ValueError):
                continue
            configured_id = str((node.config or {}).get("view_profile_id") or "").strip()
            binding_mismatches: list[str] = []
            if view_profile.intrinsic_calibration_hash != intrinsic_profile.calibration_hash:
                binding_mismatches.append("intrinsic calibration")
            if view_profile.source_channel_id != intrinsic_profile.source_channel_id:
                binding_mismatches.append("camera channel")
            if (
                view_profile.source_image_width != intrinsic_profile.image_width
                or view_profile.source_image_height != intrinsic_profile.image_height
            ):
                binding_mismatches.append(
                    "source resolution "
                    f"{view_profile.source_image_width}x{view_profile.source_image_height} "
                    f"(expected {intrinsic_profile.image_width}x{intrinsic_profile.image_height})"
                )
            if configured_id and configured_id != view_profile.view_profile_id:
                binding_mismatches.append(
                    f"configured profile ID '{configured_id}' "
                    f"(applied profile is '{view_profile.view_profile_id}')"
                )
            if binding_mismatches:
                issues.append(
                    _issue(
                        "error",
                        "image_view_intrinsic_binding_mismatch",
                        (
                            "Image view profile binding mismatch: "
                            + ", ".join(binding_mismatches)
                            + ". Calculate and apply the canonical view again."
                        ),
                        node_key=node.node_key,
                        field="view_profile",
                    )
                )
        if not _empty_value(raw_process_profile):
            try:
                process_profile = validate_stem_curvature_process_calibration(
                    raw_process_profile,
                    require_valid=True,
                )
            except (TypeError, ValueError):
                continue
            if (
                process_profile.intrinsic_calibration_hash
                != intrinsic_profile.calibration_hash
                or process_profile.source_channel_id != intrinsic_profile.source_channel_id
                or process_profile.image_width != intrinsic_profile.image_width
                or process_profile.image_height != intrinsic_profile.image_height
            ):
                issues.append(
                    _issue(
                        "error",
                        "stem_process_intrinsic_binding_mismatch",
                        (
                            "The stem process profile must match the directly connected intrinsic "
                            "profile, channel, and exact source resolution."
                        ),
                        node_key=node.node_key,
                        field="calibration",
                    )
                )
            if process_profile.measurement_geometry_mode == "CANONICAL_VIEW_CORRIDOR_V1":
                if view_profile is None:
                    issues.append(
                        _issue(
                            "error",
                            "stem_process_canonical_view_binding_missing",
                            (
                                "This process profile uses the canonical view as its measurement "
                                "corridor, but no valid canonical view profile is applied."
                            ),
                            node_key=node.node_key,
                            field="view_profile",
                        )
                    )
                elif (
                    process_profile.canonical_view_profile_id != view_profile.view_profile_id
                    or process_profile.canonical_view_profile_checksum
                    != view_profile.profile_checksum
                ):
                    issues.append(
                        _issue(
                            "error",
                            "stem_process_canonical_view_binding_mismatch",
                            (
                                "The process profile was calculated for a different canonical "
                                "view. Recalculate the process calibration after changing the "
                                "view."
                            ),
                            node_key=node.node_key,
                            field="calibration",
                        )
                    )
    return issues


def _valid_calibration_quad(value: Any, *, normalized: bool) -> bool:
    if not isinstance(value, list) or len(value) != 4:
        return False
    points: list[tuple[float, float]] = []
    for item in value:
        if not isinstance(item, dict):
            return False
        x = _optional_number(item.get("x"))
        y = _optional_number(item.get("y"))
        if x is None or y is None:
            return False
        if normalized and not (0 <= x <= 1 and 0 <= y <= 1):
            return False
        points.append((x, y))
    twice_area = abs(
        sum(
            first[0] * second[1] - second[0] * first[1]
            for first, second in zip(points, points[1:] + points[:1], strict=False)
        )
    )
    return twice_area > 1e-9


def _valid_board_cut_class_rules(value: Any) -> bool:
    bases = {
        "max_feret_diameter",
        "equivalent_diameter",
        "longitudinal_extent",
        "transverse_extent",
    }
    if not isinstance(value, list) or not value:
        return False
    seen_ids: set[int] = set()
    for item in value:
        if not isinstance(item, dict) or item.get("confirmed") is not True:
            return False
        class_id = item.get("class_id")
        if not isinstance(class_id, int) or class_id < 0 or class_id in seen_ids:
            return False
        if not str(item.get("label_snapshot") or "").strip():
            return False
        if not isinstance(item.get("relevant"), bool):
            return False
        if str(item.get("measurement_basis") or "") not in bases:
            return False
        confidence = _optional_number(item.get("minimum_confidence"))
        clearance = _optional_number(item.get("clearance_multiplier", 1.0))
        if confidence is None or not 0 <= confidence <= 1:
            return False
        if clearance is None or clearance < 0:
            return False
        for factor_name in ("depth_factor", "clearance_factor"):
            factor = _optional_number(item.get(factor_name, 1.0))
            if factor is None or factor < 0:
                return False
        seen_ids.add(class_id)
    return True


def _validate_board_cut_model_class_maps(
    session: Session,
    *,
    graph: RuntimePipelineGraph,
    nodes_by_key: dict[str, RuntimeNodeSpec],
    incoming: dict[tuple[str, str], list[RuntimeGraphEdge]],
) -> list[RuntimePipelineValidationIssue]:
    issues: list[RuntimePipelineValidationIssue] = []
    board_node_types = {
        "analysis.board_view_cut_constraints",
        "analysis.end_face_cut_constraints",
    }
    for node in graph.nodes:
        if node.node_type not in board_node_types:
            continue
        inference_node = _upstream_inference_node(
            node.node_key,
            "features",
            nodes_by_key=nodes_by_key,
            incoming=incoming,
        )
        if inference_node is None or inference_node.node_type not in {
            "inference.detection",
            "inference.segmentation",
        }:
            issues.append(
                _issue(
                    "error",
                    "board_cut_feature_model_required",
                    "Connect a deployed Detection or Segmentation model directly to the feature input.",
                    node_key=node.node_key,
                    field="features",
                )
            )
            continue
        model_ref = inference_node.logical_model_ref
        if model_ref is None:
            continue
        rows = session.scalars(
            select(RuntimeModelCapabilityCache).where(
                RuntimeModelCapabilityCache.model_family_slug == model_ref.model_family_slug,
                RuntimeModelCapabilityCache.target_path == model_ref.target_path.value,
                RuntimeModelCapabilityCache.contract_major == model_ref.contract_major,
            )
        ).all()
        if model_ref.selection_policy == "explicit_release":
            rows = [
                row
                for row in rows
                if row.deployment_release_id == model_ref.deployment_release_id
            ]
        rows = [
            row
            for row in rows
            if runtime_model_capability_is_selectable_for_policy(
                dict(row.capability or {}), model_ref.selection_policy
            )
        ]
        if not rows:
            continue
        capability = dict(rows[0].capability or {})
        raw_labels = capability.get("class_labels") or []
        labels = [
            str(item.get("label") or item.get("name") or item.get("class_name") or "")
            if isinstance(item, dict)
            else str(item)
            for item in raw_labels
        ]
        rules = (node.config or {}).get("class_rules")
        rules_by_id = {
            item.get("class_id"): item
            for item in rules or []
            if isinstance(item, dict) and isinstance(item.get("class_id"), int)
        }
        expected_ids = set(range(len(labels)))
        if not labels or set(rules_by_id) != expected_ids or any(
            str(rules_by_id[index].get("label_snapshot") or "") != label
            for index, label in enumerate(labels)
        ):
            issues.append(
                _issue(
                    "error",
                    "board_cut_class_map_confirmation_required",
                    (
                        "The deployed model class map changed or is incomplete. Confirm every "
                        "displayed class before publishing this immutable pipeline version."
                    ),
                    node_key=node.node_key,
                    field="class_rules",
                    metadata={
                        "deployment_release_id": rows[0].deployment_release_id,
                        "class_count": len(labels),
                    },
                )
            )
            continue
        class_map_version = capability.get("class_map_version")
        if class_map_version and any(
            str(rule.get("class_map_version") or "") != str(class_map_version)
            for rule in rules_by_id.values()
        ):
            issues.append(
                _issue(
                    "error",
                    "board_cut_class_map_version_changed",
                    "The model class-map version changed. Review and confirm the generated rules.",
                    node_key=node.node_key,
                    field="class_rules",
                )
            )
    return issues


def _validate_board_surface_class_mappings(
    session: Session,
    *,
    graph: RuntimePipelineGraph,
    nodes_by_key: dict[str, RuntimeNodeSpec],
    incoming: dict[tuple[str, str], list[RuntimeGraphEdge]],
) -> list[RuntimePipelineValidationIssue]:
    issues: list[RuntimePipelineValidationIssue] = []
    for node in graph.nodes:
        if node.node_type == "analysis.board_end_coordinate_frame":
            if str((node.config or {}).get("geometry_source") or "image_edges") != "segmentation":
                continue
            required_roles = {"board_main_face", "board_side_face"}
        elif node.node_type == "analysis.end_face_cut_constraints" and incoming.get(
            (node.node_key, "segments")
        ):
            required_roles = {"board_end_face"}
        else:
            continue
        inference_node = _upstream_inference_node(
            node.node_key,
            "segments",
            nodes_by_key=nodes_by_key,
            incoming=incoming,
        )
        if inference_node is None or inference_node.node_type != "inference.segmentation":
            issues.append(
                _issue(
                    "error",
                    "board_surface_segmentation_model_required",
                    "Connect one deployed Segmentation model through the polygon simplifier.",
                    node_key=node.node_key,
                    field="segments",
                )
            )
            continue
        model_ref = inference_node.logical_model_ref
        if model_ref is None:
            continue
        rows = session.scalars(
            select(RuntimeModelCapabilityCache).where(
                RuntimeModelCapabilityCache.model_family_slug == model_ref.model_family_slug,
                RuntimeModelCapabilityCache.target_path == model_ref.target_path.value,
                RuntimeModelCapabilityCache.contract_major == model_ref.contract_major,
            )
        ).all()
        if model_ref.selection_policy == "explicit_release":
            rows = [
                row for row in rows if row.deployment_release_id == model_ref.deployment_release_id
            ]
        rows = [
            row
            for row in rows
            if runtime_model_capability_is_selectable_for_policy(
                dict(row.capability or {}), model_ref.selection_policy
            )
        ]
        if not rows:
            continue
        row = rows[0]
        capability = dict(row.capability or {})
        labels = [
            str(item.get("label") or item.get("name") or item.get("class_name") or "")
            if isinstance(item, dict)
            else str(item)
            for item in capability.get("class_labels") or []
        ]
        mapping = (node.config or {}).get("board_surface_class_mapping")
        valid = isinstance(mapping, dict) and set(mapping) >= required_roles
        selected_labels: set[str] = set()
        if valid:
            for role in required_roles:
                item = mapping.get(role)
                if not isinstance(item, dict):
                    valid = False
                    break
                class_id = item.get("class_id")
                label = str(item.get("label_snapshot") or "")
                if (
                    not bool(item.get("confirmed"))
                    or not isinstance(class_id, int)
                    or class_id < 0
                    or class_id >= len(labels)
                    or labels[class_id] != label
                    or label in selected_labels
                    or str(item.get("deployment_release_id") or "")
                    != row.deployment_release_id
                ):
                    valid = False
                    break
                class_map_version = capability.get("class_map_version")
                if class_map_version and str(item.get("class_map_version") or "") != str(
                    class_map_version
                ):
                    valid = False
                    break
                selected_labels.add(label)
        if not valid:
            issues.append(
                _issue(
                    "error",
                    "board_surface_class_mapping_confirmation_required",
                    "Select and confirm every required board-surface role from the deployed model.",
                    node_key=node.node_key,
                    field="board_surface_class_mapping",
                    metadata={
                        "required_roles": sorted(required_roles),
                        "deployment_release_id": row.deployment_release_id,
                    },
                )
            )
    return issues


def _validate_model_binding_groups(
    *, graph: RuntimePipelineGraph
) -> list[RuntimePipelineValidationIssue]:
    grouped: dict[str, list[RuntimeNodeSpec]] = {}
    for node in graph.nodes:
        group = str((node.config or {}).get("model_binding_group") or "").strip()
        if group:
            grouped.setdefault(group, []).append(node)
    issues: list[RuntimePipelineValidationIssue] = []
    for group, nodes in grouped.items():
        signatures = {
            json.dumps(
                node.logical_model_ref.model_dump(mode="json")
                if node.logical_model_ref is not None
                else None,
                sort_keys=True,
            )
            for node in nodes
        }
        if len(signatures) <= 1:
            continue
        for node in nodes:
            issues.append(
                _issue(
                    "error",
                    "model_binding_group_mismatch",
                    f"All model nodes in binding group '{group}' must use the same model release.",
                    node_key=node.node_key,
                    field="logical_model_ref",
                )
            )
    return issues


def _upstream_inference_node(
    node_key: str,
    input_port: str,
    *,
    nodes_by_key: dict[str, RuntimeNodeSpec],
    incoming: dict[tuple[str, str], list[RuntimeGraphEdge]],
) -> RuntimeNodeSpec | None:
    edges = incoming.get((node_key, input_port), [])
    if len(edges) != 1:
        return None
    candidate = nodes_by_key.get(edges[0].from_node_key)
    if candidate is None:
        return None
    if candidate.node_type in {"inference.detection", "inference.segmentation"}:
        return candidate
    if candidate.node_type == "analysis.segmentation_polygon_simplification":
        return _upstream_inference_node(
            candidate.node_key,
            "segments",
            nodes_by_key=nodes_by_key,
            incoming=incoming,
        )
    return None


def _validate_board_cut_inspection_lengths(
    *,
    graph: RuntimePipelineGraph,
    nodes_by_key: dict[str, RuntimeNodeSpec],
    incoming: dict[tuple[str, str], list[RuntimeGraphEdge]],
) -> list[RuntimePipelineValidationIssue]:
    issues: list[RuntimePipelineValidationIssue] = []
    decision_ports = {
        "analysis.multi_view_board_cut_fusion": ("upper", "lower", "end_face"),
        "analysis.single_view_board_cut_decision": ("constraints",),
    }
    for decision in graph.nodes:
        ports = decision_ports.get(decision.node_type)
        if ports is None:
            continue
        decision_length = _optional_number(
            (decision.config or {}).get("inspection_length_mm", 300.0)
        )
        if decision_length is None:
            continue
        for port_key in ports:
            edges = incoming.get((decision.node_key, port_key), [])
            if len(edges) != 1:
                continue
            constraint_node = nodes_by_key.get(edges[0].from_node_key)
            if constraint_node is None or constraint_node.node_type not in {
                "analysis.board_view_cut_constraints",
                "analysis.end_face_cut_constraints",
            }:
                continue
            constraint_length = _optional_number(
                (constraint_node.config or {}).get("inspection_length_mm", 300.0)
            )
            if (
                constraint_length is None
                or abs(constraint_length - decision_length) <= 1e-6
            ):
                continue
            issues.append(
                _issue(
                    "error",
                    "board_cut_inspection_length_mismatch",
                    (
                        "Use the same inspection length in the constraint node and the "
                        "connected cut decision. Different values could otherwise produce "
                        "an incorrect zero cut or an unsafe cut position."
                    ),
                    node_key=decision.node_key,
                    field="inspection_length_mm",
                    metadata={
                        "input_port": port_key,
                        "constraint_node_key": constraint_node.node_key,
                        "constraint_inspection_length_mm": constraint_length,
                        "decision_inspection_length_mm": decision_length,
                    },
                )
            )
    return issues


def _validate_single_board_camera_test_graph(
    *,
    mode: RuntimeMode,
    graph: RuntimePipelineGraph,
    nodes_by_key: dict[str, RuntimeNodeSpec],
    incoming: dict[tuple[str, str], list[RuntimeGraphEdge]],
) -> list[RuntimePipelineValidationIssue]:
    decisions = [
        node
        for node in graph.nodes
        if node.node_type == "analysis.single_view_board_cut_decision"
    ]
    sources = [
        node for node in graph.nodes if node.node_type == "source.single_board_camera_test"
    ]
    if not decisions:
        if not sources:
            return []
        return [
            _issue(
                "error",
                "single_board_camera_decision_required",
                "A single-camera board test requires exactly one single-view cut decision.",
                node_key=sources[0].node_key,
            )
        ]
    if len(decisions) != 1:
        return [
            _issue(
                "error",
                "single_board_camera_decision_required",
                "A single-camera board test requires exactly one single-view cut decision.",
                node_key=decisions[0].node_key,
            )
        ]

    issues: list[RuntimePipelineValidationIssue] = []
    plc_nodes = [
        node for node in graph.nodes if node.node_type == "output.siemens_sps_write"
    ]
    plc_projection_nodes = [
        node
        for node in graph.nodes
        if any(
            isinstance(projection.get("destinations"), list)
            and "plc" in projection["destinations"]
            for projection in ((node.config or {}).get("projections") or [])
            if isinstance(projection, dict)
        )
    ]
    if plc_nodes or plc_projection_nodes:
        issues.append(
            _issue(
                "error",
                "single_board_camera_plc_output_forbidden",
                "Single-camera test pipelines cannot publish values to a PLC.",
                node_key=(plc_nodes or plc_projection_nodes)[0].node_key,
            )
        )

    constraint_edges = incoming.get((decisions[0].node_key, "constraints"), [])
    constraint_node = (
        nodes_by_key.get(constraint_edges[0].from_node_key)
        if len(constraint_edges) == 1
        else None
    )
    if mode == RuntimeMode.TRIGGER and not sources:
        issues.append(
            _issue(
                "error",
                "single_board_camera_trigger_source_required",
                "Triggered single-camera tests require the single board camera test source.",
                node_key=decisions[0].node_key,
            )
        )
    if mode == RuntimeMode.TRIGGER and not incoming.get(
        (decisions[0].node_key, "frame_set")
    ):
        issues.append(
            _issue(
                "error",
                "single_board_camera_frame_set_required",
                "Connect the test camera frame set to the triggered single-view decision.",
                node_key=decisions[0].node_key,
                field="frame_set",
            )
        )

    role = (
        str((sources[0].config or {}).get("camera_role") or "upper")
        if sources
        else None
    )
    compatible = False
    if role == "end_face" or (
        role is None
        and constraint_node is not None
        and constraint_node.node_type == "analysis.end_face_cut_constraints"
    ):
        compatible = constraint_node is not None and (
            constraint_node.node_type == "analysis.end_face_cut_constraints"
        )
    elif role in {"upper", "lower"} or role is None:
        compatible = (
            constraint_node is not None
            and constraint_node.node_type == "analysis.board_view_cut_constraints"
            and (
                role is None
                or str((constraint_node.config or {}).get("view_role") or "upper")
                == role
            )
        )
    if not compatible:
        issues.append(
            _issue(
                "error",
                "single_board_camera_role_mismatch",
                (
                    "The selected test camera role must match the connected longitudinal "
                    "or end-face constraint node."
                ),
                node_key=decisions[0].node_key,
                field="constraints",
                metadata={"camera_role": role or "realtime_channel"},
            )
        )
    return issues


def _valid_clearance_rule_table(value: Any) -> bool:
    allowed = _measurement_bases()
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        if item.get("enabled") is False:
            continue
        feature_type = str(item.get("feature_type") or "").strip()
        basis = str(item.get("measurement_basis") or "").strip()
        if not feature_type or basis not in allowed:
            return False
        try:
            multiplier = float(item.get("multiplier", 0))
            minimum = float(item.get("minimum_clearance_px", 0) or 0)
        except (TypeError, ValueError):
            return False
        if multiplier < 0 or minimum < 0:
            return False
    return True


def _valid_cv_line_selection_rules(value: Any) -> bool:
    roles = {"start_edge", "side_edge", "left_edge", "right_edge", "custom"}
    regions = {"any", "left", "right", "top", "bottom", "center"}
    sides = {"none", "left", "right", "top", "bottom"}
    rankings = {"strongest_longest", "longest", "nearest_to_side"}
    if not isinstance(value, list) or not value:
        return False
    seen_required: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            return False
        role = str(item.get("edge_role") or item.get("role") or "").strip()
        if role not in roles:
            return False
        target = _optional_number(item.get("target_angle_degrees"))
        tolerance = _optional_number(item.get("angle_tolerance_degrees"))
        angle_min = _optional_number(item.get("angle_min_degrees"))
        angle_max = _optional_number(item.get("angle_max_degrees"))
        minimum = _optional_number(item.get("minimum_line_length_px"))
        if minimum is None or minimum < 0:
            return False
        if target is not None and (tolerance is None or tolerance < 0 or tolerance > 90):
            return False
        if (angle_min is None) != (angle_max is None):
            return False
        if str(item.get("region_preference") or "any") not in regions:
            return False
        if str(item.get("nearest_to_image_side") or "none") not in sides:
            return False
        if str(item.get("ranking") or "strongest_longest") not in rankings:
            return False
        if bool(item.get("required", role in {"start_edge", "side_edge"})):
            seen_required.add(role)
    return {"start_edge", "side_edge"}.issubset(seen_required)


def _valid_metric_apply_to(value: Any) -> bool:
    allowed = {
        "feature_measurements",
        "clearance_zones",
        "free_region",
        "selected_result_fields",
    }
    values = _field_list(value)
    return bool(values) and all(item in allowed for item in values)


def _valid_quality_rule_table(value: Any) -> bool:
    allowed_rule_types = {
        "feature_count_by_type",
        "feature_size_by_type",
        "total_feature_length_area_by_type",
        "free_region_distance",
    }
    operators = {"<", "<=", ">", ">=", "==", "!="}
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        rule_type = str(item.get("rule_type") or "").strip()
        operator = str(item.get("operator") or "<=").strip()
        try:
            float(item.get("threshold", 0))
        except (TypeError, ValueError):
            return False
        if rule_type not in allowed_rule_types or operator not in operators:
            return False
    return True


def _valid_spatial_rule_table(value: Any) -> bool:
    relations = {"approaching", "overlap", "distance", "zone_occupancy"}
    label_pairings = {"any", "same_label", "different_label"}
    zone_modes = {"center_inside", "box_overlaps", "fully_inside"}
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        relation = str(item.get("relation") or "").strip()
        if relation not in relations:
            return False
        if str(item.get("label_pairing") or "any") not in label_pairings:
            return False
        if not _field_list(item.get("primary_labels")) and item.get("primary_labels") not in (
            None,
            "",
            [],
        ):
            return False
        if not _field_list(item.get("secondary_labels")) and item.get("secondary_labels") not in (
            None,
            "",
            [],
        ):
            return False
        if item.get("enabled") is False:
            continue
        max_distance = _optional_number(item.get("max_distance_percent"))
        min_distance = _optional_number(item.get("min_distance_percent"))
        overlap = _optional_number(item.get("min_overlap_percent"))
        closing = _optional_number(item.get("min_closing_percent"))
        if max_distance is not None and max_distance < 0:
            return False
        if min_distance is not None and min_distance < 0:
            return False
        if overlap is not None and (overlap < 0 or overlap > 100):
            return False
        if closing is not None and closing < 0:
            return False
        if relation == "distance" and max_distance is None and min_distance is None:
            return False
        if relation == "overlap" and overlap is None:
            return False
        if relation == "approaching" and closing is None:
            return False
        if relation == "zone_occupancy":
            if str(item.get("zone_match") or "center_inside") not in zone_modes:
                return False
            try:
                min_count = int(item.get("min_count") or 1)
            except (TypeError, ValueError):
                return False
            if min_count < 1:
                return False
            max_count = item.get("max_count")
            if max_count not in (None, ""):
                try:
                    if int(max_count) < min_count:
                        return False
                except (TypeError, ValueError):
                    return False
    return True


def _valid_color_profile_table(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        label = str(item.get("label") or item.get("class_name") or "").strip()
        if not label:
            return False
        if item.get("enabled") is False:
            continue
        if str(item.get("color_space") or "hsv") != "hsv":
            return False
        hue_min = _optional_number(item.get("hue_min"))
        hue_max = _optional_number(item.get("hue_max"))
        saturation_min = _optional_number(item.get("saturation_min"))
        saturation_max = _optional_number(item.get("saturation_max"))
        value_min = _optional_number(item.get("value_min"))
        value_max = _optional_number(item.get("value_max"))
        if (
            hue_min is None
            or hue_max is None
            or saturation_min is None
            or saturation_max is None
            or value_min is None
            or value_max is None
        ):
            return False
        if not (0 <= hue_min <= 179 and 0 <= hue_max <= 179):
            return False
        if not (0 <= saturation_min <= saturation_max <= 255):
            return False
        if not (0 <= value_min <= value_max <= 255):
            return False
        min_area = _optional_number(item.get("min_area_percent"))
        max_area = _optional_number(item.get("max_area_percent"))
        min_aspect = _optional_number(item.get("min_aspect_ratio"))
        if min_area is not None and min_area < 0:
            return False
        if max_area is not None and max_area < 0:
            return False
        if min_area is not None and max_area is not None and max_area < min_area:
            return False
        if min_aspect is not None and min_aspect < 1:
            return False
    return True


def _valid_direction_reference(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    start = _normalized_point(value.get("start"))
    end = _normalized_point(value.get("end"))
    if start is None or end is None:
        return False
    return abs(start[0] - end[0]) > 1e-6 or abs(start[1] - end[1]) > 1e-6


def _normalized_point(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    x = _optional_number(value.get("x"))
    y = _optional_number(value.get("y"))
    if x is None or y is None:
        return None
    if not (0 <= x <= 1 and 0 <= y <= 1):
        return None
    return x, y


def _measurement_bases() -> set[str]:
    return {
        "bbox_width",
        "bbox_height",
        "approximate_diameter",
        "polygon_length",
        "mask_area",
        "axis_projected_width",
        "axis_projected_length",
    }


def _valid_select_value(value: Any, options: list[RuntimeNodePropertyOption]) -> bool:
    valid_values = {option.value for option in options}
    if isinstance(value, list):
        return bool(value) and all(str(item) in valid_values for item in value)
    return str(value) in valid_values


def _validate_box_list(
    value: Any,
    *,
    field_name: str,
    code: str,
    message_suffix: str,
    node_key: str,
    field_key: str,
    allow_polygons: bool = False,
) -> list[RuntimePipelineValidationIssue]:
    if value in (None, []):
        return []
    if not isinstance(value, list):
        return [
            _issue(
                "error",
                code,
                (
                    f"{field_name} must be a list of structured "
                    f"{'regions' if allow_polygons else 'boxes'}. {message_suffix}"
                ),
                node_key=node_key,
                field=field_key,
            )
        ]
    issues: list[RuntimePipelineValidationIssue] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            issues.append(
                _issue(
                    "error",
                    code,
                    f"{field_name} item {index + 1} must be an object. {message_suffix}",
                    node_key=node_key,
                    field=field_key,
                )
            )
            continue
        normalized = _normalize_box(item)
        normalized_polygon = _normalize_polygon_region(item) if allow_polygons else None
        if normalized is None and normalized_polygon is None:
            issues.append(
                _issue(
                    "error",
                    code,
                    (
                        f"{field_name} item {index + 1} is not a valid normalized "
                        f"{'region' if allow_polygons else 'box'}. {message_suffix}"
                    ),
                    node_key=node_key,
                    field=field_key,
                )
            )
            continue
        if normalized is not None and not _valid_optional_box_angle(item):
            issues.append(
                _issue(
                    "error",
                    code,
                    f"{field_name} item {index + 1} has an invalid rotation angle.",
                    node_key=node_key,
                    field=field_key,
                )
            )
    return issues


def _normalize_polygon_region(
    item: dict[str, Any],
) -> tuple[tuple[float, float], ...] | None:
    raw_points = item.get("points", item.get("polygon"))
    if not isinstance(raw_points, list) or len(raw_points) < 3:
        return None
    points: list[tuple[float, float]] = []
    for raw_point in raw_points:
        point = _normalized_point(raw_point)
        if point is None:
            return None
        points.append(point)
    if abs(_signed_polygon_area(points)) <= 1e-8 or not _convex_polygon(points):
        return None
    return tuple(points)


def _signed_polygon_area(points: list[tuple[float, float]]) -> float:
    return 0.5 * sum(
        x1 * y2 - x2 * y1
        for (x1, y1), (x2, y2) in zip(
            points,
            [*points[1:], points[0]],
            strict=True,
        )
    )


def _convex_polygon(points: list[tuple[float, float]]) -> bool:
    signs: set[bool] = set()
    for index, current in enumerate(points):
        following = points[(index + 1) % len(points)]
        next_point = points[(index + 2) % len(points)]
        cross = (following[0] - current[0]) * (next_point[1] - following[1]) - (
            following[1] - current[1]
        ) * (next_point[0] - following[0])
        if abs(cross) > 1e-10:
            signs.add(cross > 0.0)
    return len(signs) == 1


def _normalize_box(item: dict[str, Any]) -> dict[str, float] | None:
    try:
        if {"x_min", "y_min", "x_max", "y_max"}.issubset(item):
            x_min = float(item["x_min"])
            y_min = float(item["y_min"])
            x_max = float(item["x_max"])
            y_max = float(item["y_max"])
        elif {"x", "y", "width", "height"}.issubset(item):
            x_min = float(item["x"])
            y_min = float(item["y"])
            x_max = x_min + float(item["width"])
            y_max = y_min + float(item["height"])
        else:
            return None
    except (TypeError, ValueError):
        return None
    if not all(0.0 <= value <= 1.0 for value in (x_min, y_min, x_max, y_max)):
        return None
    if x_max <= x_min or y_max <= y_min:
        return None
    return {
        "x_min": x_min,
        "y_min": y_min,
        "x_max": x_max,
        "y_max": y_max,
    }


def _valid_optional_box_angle(item: dict[str, Any]) -> bool:
    for key in ("angle_degrees", "rotation_degrees", "angle"):
        if key in item and not _number_like_or_empty(item.get(key)):
            return False
    nested_box = item.get("box")
    if isinstance(nested_box, dict):
        for key in ("angle_degrees", "rotation_degrees", "angle"):
            if key in nested_box and not _number_like_or_empty(nested_box.get(key)):
                return False
    return True


def _number_like_or_empty(value: Any) -> bool:
    if value is None or value == "":
        return True
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _not_expired(expires_at: Any, now: Any) -> bool:
    if expires_at is None:
        return False
    if getattr(expires_at, "tzinfo", None) is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at >= now


def _model_reference_policy_message(selection_policy: str, statuses: list[str]) -> str:
    status_text = f" Current deployment status: {', '.join(statuses)}." if statuses else ""
    if selection_policy == "active_production":
        return (
            "This model reference follows active production, but no active production "
            f"capability is available.{status_text} Promote the model to production "
            "or switch the node binding to staged test or a pinned release."
        )
    if selection_policy == "staged_test":
        return (
            "This model reference uses the staged test policy, but no staged or "
            f"production capability is available.{status_text} Stage or activate "
            "the model before publishing the pipeline."
        )
    return (
        "The pinned deployment release is not selectable for runtime execution."
        f"{status_text} Choose another deployment release before publishing."
    )


def _runtime_task_type_compatible(required_task_type: str, actual_task_type: str) -> bool:
    if required_task_type == actual_task_type:
        return True
    return {required_task_type, actual_task_type} == {
        TaskType.DETECTION.value,
        TaskType.ORIENTED_DETECTION.value,
    }


def _iso(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat()).replace("+00:00", "Z")
    return str(value)


def _template_definitions() -> list[dict[str, Any]]:
    return [
        {
            "slug": "template-realtime-detection-overlay",
            "name": "Realtime detection with overlay",
            "description": "Channel-bound realtime detection pipeline with counting, result output, and live overlay.",
            "mode": RuntimeMode.REALTIME,
            "graph": RuntimePipelineGraph(
                nodes=[
                    _template_node("channel_input", "source.channel_frame", 80, 180),
                    _template_node("frame_sampler", "preprocessing.frame_sampler", 310, 180),
                    _template_inference_node(
                        "detect_objects",
                        "inference.detection",
                        TaskType.DETECTION,
                        540,
                        180,
                    ),
                    _template_node("count_objects", "analysis.object_counting", 780, 180),
                    _template_node("result_output", "output.result", 1020, 112),
                    _template_node("overlay", "output.overlay", 1020, 248),
                    _template_node("display_live", "output.display_output", 1260, 180),
                ],
                edges=[
                    RuntimeGraphEdge(
                        from_node_key="channel_input",
                        from_port="frame",
                        to_node_key="frame_sampler",
                        to_port="frame",
                    ),
                    RuntimeGraphEdge(
                        from_node_key="frame_sampler",
                        from_port="frame",
                        to_node_key="detect_objects",
                        to_port="frame",
                    ),
                    RuntimeGraphEdge(
                        from_node_key="detect_objects",
                        from_port="detections",
                        to_node_key="count_objects",
                        to_port="objects",
                    ),
                    RuntimeGraphEdge(
                        from_node_key="count_objects",
                        from_port="counts",
                        to_node_key="result_output",
                        to_port="result",
                    ),
                    RuntimeGraphEdge(
                        from_node_key="detect_objects",
                        from_port="detections",
                        to_node_key="overlay",
                        to_port="result",
                    ),
                    RuntimeGraphEdge(
                        from_node_key="frame_sampler",
                        from_port="frame",
                        to_node_key="display_live",
                        to_port="frame",
                    ),
                    RuntimeGraphEdge(
                        from_node_key="detect_objects",
                        from_port="detections",
                        to_node_key="display_live",
                        to_port="objects",
                    ),
                ],
            ),
        },
        _stem_curvature_minimum_template_definition(),
        _stem_rotation_optimization_template_definition(),
        {
            "slug": "template-trigger-defect-response",
            "name": "Trigger defect response",
            "description": "Channel-bound trigger pipeline with detection, rule evaluation, HTTP response, and ROI capture.",
            "mode": RuntimeMode.TRIGGER,
            "graph": RuntimePipelineGraph(
                nodes=[
                    _template_node("http_trigger", "trigger.http_put", 80, 180),
                    _template_node("channel_input", "source.channel_frame", 310, 180),
                    _template_node("resize", "preprocessing.resize", 540, 180),
                    _template_inference_node(
                        "detect_defects",
                        "inference.detection",
                        TaskType.DETECTION,
                        770,
                        180,
                    ),
                    _template_node("extract_rois", "preprocessing.roi_extract", 1010, 90),
                    _template_node("evaluate_result", "analysis.rule_evaluation", 1010, 270),
                    _template_node("roi_capture", "capture.roi_labelstudio", 1250, 90),
                    _template_node("result_output", "output.result", 1250, 270),
                ],
                edges=[
                    RuntimeGraphEdge(
                        from_node_key="http_trigger",
                        from_port="trigger",
                        to_node_key="channel_input",
                        to_port="trigger",
                    ),
                    RuntimeGraphEdge(
                        from_node_key="channel_input",
                        from_port="frame",
                        to_node_key="resize",
                        to_port="frame",
                    ),
                    RuntimeGraphEdge(
                        from_node_key="resize",
                        from_port="frame",
                        to_node_key="detect_defects",
                        to_port="frame",
                    ),
                    RuntimeGraphEdge(
                        from_node_key="detect_defects",
                        from_port="detections",
                        to_node_key="extract_rois",
                        to_port="regions",
                    ),
                    RuntimeGraphEdge(
                        from_node_key="resize",
                        from_port="frame",
                        to_node_key="extract_rois",
                        to_port="frame",
                    ),
                    RuntimeGraphEdge(
                        from_node_key="extract_rois",
                        from_port="rois",
                        to_node_key="roi_capture",
                        to_port="rois",
                    ),
                    RuntimeGraphEdge(
                        from_node_key="detect_defects",
                        from_port="detections",
                        to_node_key="evaluate_result",
                        to_port="result",
                    ),
                    RuntimeGraphEdge(
                        from_node_key="evaluate_result",
                        from_port="result",
                        to_node_key="result_output",
                        to_port="result",
                    ),
                ],
            ),
        },
        {
            "slug": "template-trigger-multiview-board-cut",
            "name": "Three-camera board cut (no-code)",
            "description": (
                "Synchronized upper, lower, and end-face inspection with metric "
                "feature clearance, valid zero-cut output, and fail-closed fusion."
            ),
            "mode": RuntimeMode.TRIGGER,
            "graph": RuntimePipelineGraph(
                nodes=[
                    _template_node("http_trigger", "trigger.http_put", 40, 320),
                    _template_node("board_cameras", "source.synchronized_camera_frames", 250, 320),
                    _template_inference_node(
                        "upper_features", "inference.detection", TaskType.DETECTION, 500, 60
                    ),
                    _template_node(
                        "upper_coordinates", "analysis.board_end_coordinate_frame", 850, 190
                    ),
                    _template_node_with_config(
                        "upper_constraints",
                        "analysis.board_view_cut_constraints",
                        1160,
                        110,
                        {"view_role": "upper"},
                    ),
                    _template_inference_node(
                        "lower_features", "inference.detection", TaskType.DETECTION, 500, 350
                    ),
                    _template_node(
                        "lower_coordinates", "analysis.board_end_coordinate_frame", 850, 480
                    ),
                    _template_node_with_config(
                        "lower_constraints",
                        "analysis.board_view_cut_constraints",
                        1160,
                        400,
                        {"view_role": "lower"},
                    ),
                    _template_inference_node(
                        "end_face_features", "inference.detection", TaskType.DETECTION, 720, 680
                    ),
                    _template_node(
                        "end_face_constraints", "analysis.end_face_cut_constraints", 1160, 680
                    ),
                    _template_node(
                        "fuse_cut", "analysis.multi_view_board_cut_fusion", 1400, 340
                    ),
                    _template_node("cut_overlay", "output.overlay", 1640, 180),
                    _board_cut_result_output_node("result_output", 1640, 340),
                ],
                edges=[
                    RuntimeGraphEdge(from_node_key="http_trigger", from_port="trigger", to_node_key="board_cameras", to_port="trigger"),
                    RuntimeGraphEdge(from_node_key="board_cameras", from_port="upper_frame", to_node_key="upper_features", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="board_cameras", from_port="upper_frame", to_node_key="upper_coordinates", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="upper_features", from_port="detections", to_node_key="upper_constraints", to_port="features"),
                    RuntimeGraphEdge(from_node_key="upper_coordinates", from_port="coordinate_frame", to_node_key="upper_constraints", to_port="coordinate_frame"),
                    RuntimeGraphEdge(from_node_key="board_cameras", from_port="upper_frame", to_node_key="upper_constraints", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="board_cameras", from_port="lower_frame", to_node_key="lower_features", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="board_cameras", from_port="lower_frame", to_node_key="lower_coordinates", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="lower_features", from_port="detections", to_node_key="lower_constraints", to_port="features"),
                    RuntimeGraphEdge(from_node_key="lower_coordinates", from_port="coordinate_frame", to_node_key="lower_constraints", to_port="coordinate_frame"),
                    RuntimeGraphEdge(from_node_key="board_cameras", from_port="lower_frame", to_node_key="lower_constraints", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="board_cameras", from_port="end_face_frame", to_node_key="end_face_features", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="end_face_features", from_port="detections", to_node_key="end_face_constraints", to_port="features"),
                    RuntimeGraphEdge(from_node_key="board_cameras", from_port="end_face_frame", to_node_key="end_face_constraints", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="upper_constraints", from_port="constraints", to_node_key="fuse_cut", to_port="upper"),
                    RuntimeGraphEdge(from_node_key="lower_constraints", from_port="constraints", to_node_key="fuse_cut", to_port="lower"),
                    RuntimeGraphEdge(from_node_key="end_face_constraints", from_port="constraints", to_node_key="fuse_cut", to_port="end_face"),
                    RuntimeGraphEdge(from_node_key="board_cameras", from_port="frame_set", to_node_key="fuse_cut", to_port="frame_set"),
                    RuntimeGraphEdge(from_node_key="fuse_cut", from_port="decision", to_node_key="cut_overlay", to_port="result"),
                    RuntimeGraphEdge(from_node_key="fuse_cut", from_port="result", to_node_key="result_output", to_port="result"),
                ],
            ),
        },
        {
            "slug": "template-trigger-multiview-segmented-board-cut",
            "name": "Three-camera segmented board cut",
            "description": (
                "Synchronized upper, lower, and end-face inspection with a shared deployed "
                "board-surface segmentation model, simplified polygons, and shadow-only output."
            ),
            "mode": RuntimeMode.TRIGGER,
            "graph": RuntimePipelineGraph(
                nodes=[
                    _template_node("http_trigger", "trigger.http_put", 20, 360),
                    _template_node("board_cameras", "source.synchronized_camera_frames", 210, 360),
                    _template_node("upper_undistort", "preprocessing.camera_undistort", 430, 80),
                    _template_inference_node(
                        "upper_features", "inference.detection", TaskType.DETECTION, 650, 20
                    ),
                    _template_inference_node(
                        "upper_board_surfaces",
                        "inference.segmentation",
                        TaskType.SEGMENTATION,
                        650,
                        150,
                        {"model_binding_group": "board_surface"},
                        unbound=True,
                    ),
                    _template_node(
                        "upper_simplified", "analysis.segmentation_polygon_simplification", 870, 150
                    ),
                    _template_node_with_config(
                        "upper_coordinates",
                        "analysis.board_end_coordinate_frame",
                        1080,
                        150,
                        {"geometry_source": "segmentation", "view_role": "upper"},
                    ),
                    _template_node_with_config(
                        "upper_constraints",
                        "analysis.board_view_cut_constraints",
                        1320,
                        80,
                        {"view_role": "upper"},
                    ),
                    _template_node("lower_undistort", "preprocessing.camera_undistort", 430, 390),
                    _template_inference_node(
                        "lower_features", "inference.detection", TaskType.DETECTION, 650, 330
                    ),
                    _template_inference_node(
                        "lower_board_surfaces",
                        "inference.segmentation",
                        TaskType.SEGMENTATION,
                        650,
                        460,
                        {"model_binding_group": "board_surface"},
                        unbound=True,
                    ),
                    _template_node(
                        "lower_simplified", "analysis.segmentation_polygon_simplification", 870, 460
                    ),
                    _template_node_with_config(
                        "lower_coordinates",
                        "analysis.board_end_coordinate_frame",
                        1080,
                        460,
                        {"geometry_source": "segmentation", "view_role": "lower"},
                    ),
                    _template_node_with_config(
                        "lower_constraints",
                        "analysis.board_view_cut_constraints",
                        1320,
                        390,
                        {"view_role": "lower"},
                    ),
                    _template_node("end_face_undistort", "preprocessing.camera_undistort", 430, 700),
                    _template_inference_node(
                        "end_face_features", "inference.detection", TaskType.DETECTION, 650, 640
                    ),
                    _template_inference_node(
                        "end_face_board_surface",
                        "inference.segmentation",
                        TaskType.SEGMENTATION,
                        650,
                        770,
                        {"model_binding_group": "board_surface"},
                        unbound=True,
                    ),
                    _template_node(
                        "end_face_simplified",
                        "analysis.segmentation_polygon_simplification",
                        870,
                        770,
                    ),
                    _template_node(
                        "end_face_constraints", "analysis.end_face_cut_constraints", 1180, 700
                    ),
                    _template_node(
                        "fuse_cut", "analysis.multi_view_board_cut_fusion", 1570, 360
                    ),
                    _template_node("cut_overlay", "output.overlay", 1810, 220),
                    _board_cut_result_output_node("result_output", 1810, 420, allow_plc=False),
                ],
                edges=[
                    RuntimeGraphEdge(from_node_key="http_trigger", from_port="trigger", to_node_key="board_cameras", to_port="trigger"),
                    RuntimeGraphEdge(from_node_key="board_cameras", from_port="upper_frame", to_node_key="upper_undistort", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="upper_undistort", from_port="frame", to_node_key="upper_features", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="upper_undistort", from_port="frame", to_node_key="upper_board_surfaces", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="upper_undistort", from_port="frame", to_node_key="upper_coordinates", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="upper_board_surfaces", from_port="segments", to_node_key="upper_simplified", to_port="segments"),
                    RuntimeGraphEdge(from_node_key="upper_simplified", from_port="segments", to_node_key="upper_coordinates", to_port="segments"),
                    RuntimeGraphEdge(from_node_key="upper_features", from_port="detections", to_node_key="upper_constraints", to_port="features"),
                    RuntimeGraphEdge(from_node_key="upper_coordinates", from_port="coordinate_frame", to_node_key="upper_constraints", to_port="coordinate_frame"),
                    RuntimeGraphEdge(from_node_key="upper_undistort", from_port="frame", to_node_key="upper_constraints", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="board_cameras", from_port="lower_frame", to_node_key="lower_undistort", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="lower_undistort", from_port="frame", to_node_key="lower_features", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="lower_undistort", from_port="frame", to_node_key="lower_board_surfaces", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="lower_undistort", from_port="frame", to_node_key="lower_coordinates", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="lower_board_surfaces", from_port="segments", to_node_key="lower_simplified", to_port="segments"),
                    RuntimeGraphEdge(from_node_key="lower_simplified", from_port="segments", to_node_key="lower_coordinates", to_port="segments"),
                    RuntimeGraphEdge(from_node_key="lower_features", from_port="detections", to_node_key="lower_constraints", to_port="features"),
                    RuntimeGraphEdge(from_node_key="lower_coordinates", from_port="coordinate_frame", to_node_key="lower_constraints", to_port="coordinate_frame"),
                    RuntimeGraphEdge(from_node_key="lower_undistort", from_port="frame", to_node_key="lower_constraints", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="board_cameras", from_port="end_face_frame", to_node_key="end_face_undistort", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="end_face_undistort", from_port="frame", to_node_key="end_face_features", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="end_face_undistort", from_port="frame", to_node_key="end_face_board_surface", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="end_face_board_surface", from_port="segments", to_node_key="end_face_simplified", to_port="segments"),
                    RuntimeGraphEdge(from_node_key="end_face_simplified", from_port="segments", to_node_key="end_face_constraints", to_port="segments"),
                    RuntimeGraphEdge(from_node_key="end_face_features", from_port="detections", to_node_key="end_face_constraints", to_port="features"),
                    RuntimeGraphEdge(from_node_key="end_face_undistort", from_port="frame", to_node_key="end_face_constraints", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="upper_constraints", from_port="constraints", to_node_key="fuse_cut", to_port="upper"),
                    RuntimeGraphEdge(from_node_key="lower_constraints", from_port="constraints", to_node_key="fuse_cut", to_port="lower"),
                    RuntimeGraphEdge(from_node_key="end_face_constraints", from_port="constraints", to_node_key="fuse_cut", to_port="end_face"),
                    RuntimeGraphEdge(from_node_key="board_cameras", from_port="frame_set", to_node_key="fuse_cut", to_port="frame_set"),
                    RuntimeGraphEdge(from_node_key="fuse_cut", from_port="decision", to_node_key="cut_overlay", to_port="result"),
                    RuntimeGraphEdge(from_node_key="fuse_cut", from_port="result", to_node_key="result_output", to_port="result"),
                ],
            ),
        },
        {
            "slug": "template-realtime-single-longitudinal-segmented-board-cut-test",
            "name": "Realtime segmented longitudinal board camera test",
            "description": (
                "Continuously tests one calibrated longitudinal camera using a previously deployed "
                "segmentation model. PLC output remains disabled."
            ),
            "mode": RuntimeMode.REALTIME,
            "graph": RuntimePipelineGraph(
                nodes=[
                    _template_node("channel_input", "source.channel_frame", 40, 300),
                    _template_node("undistort", "preprocessing.camera_undistort", 260, 300),
                    _template_inference_node(
                        "features", "inference.detection", TaskType.DETECTION, 500, 100
                    ),
                    _template_inference_node(
                        "board_surfaces",
                        "inference.segmentation",
                        TaskType.SEGMENTATION,
                        500,
                        300,
                        {"model_binding_group": "board_surface"},
                        unbound=True,
                    ),
                    _template_node(
                        "simplified", "analysis.segmentation_polygon_simplification", 730, 300
                    ),
                    _template_node_with_config(
                        "coordinates",
                        "analysis.board_end_coordinate_frame",
                        950,
                        300,
                        {"geometry_source": "segmentation", "view_role": "upper"},
                    ),
                    _template_node_with_config(
                        "constraints",
                        "analysis.board_view_cut_constraints",
                        1190,
                        190,
                        {"view_role": "upper"},
                    ),
                    _template_node(
                        "fuse_cut", "analysis.single_view_board_cut_decision", 1450, 190
                    ),
                    _template_node("cut_overlay", "output.overlay", 1680, 80),
                    _board_cut_result_output_node("result_output", 1680, 300, allow_plc=False),
                ],
                edges=[
                    RuntimeGraphEdge(from_node_key="channel_input", from_port="frame", to_node_key="undistort", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="undistort", from_port="frame", to_node_key="features", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="undistort", from_port="frame", to_node_key="board_surfaces", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="board_surfaces", from_port="segments", to_node_key="simplified", to_port="segments"),
                    RuntimeGraphEdge(from_node_key="simplified", from_port="segments", to_node_key="coordinates", to_port="segments"),
                    RuntimeGraphEdge(from_node_key="undistort", from_port="frame", to_node_key="coordinates", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="features", from_port="detections", to_node_key="constraints", to_port="features"),
                    RuntimeGraphEdge(from_node_key="coordinates", from_port="coordinate_frame", to_node_key="constraints", to_port="coordinate_frame"),
                    RuntimeGraphEdge(from_node_key="undistort", from_port="frame", to_node_key="constraints", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="constraints", from_port="constraints", to_node_key="fuse_cut", to_port="constraints"),
                    RuntimeGraphEdge(from_node_key="fuse_cut", from_port="decision", to_node_key="cut_overlay", to_port="result"),
                    RuntimeGraphEdge(from_node_key="fuse_cut", from_port="result", to_node_key="result_output", to_port="result"),
                ],
            ),
        },
        {
            "slug": "template-trigger-single-longitudinal-board-cut-test",
            "name": "Single longitudinal board camera test",
            "description": (
                "Commission one upper or lower board camera with the same metric cut "
                "rules and board.cut-decision.v1 result used by production."
            ),
            "mode": RuntimeMode.TRIGGER,
            "graph": RuntimePipelineGraph(
                nodes=[
                    _template_node("http_trigger", "trigger.http_put", 40, 260),
                    _template_node_with_config(
                        "test_camera",
                        "source.single_board_camera_test",
                        250,
                        260,
                        {"camera_role": "upper"},
                    ),
                    _template_inference_node(
                        "features", "inference.detection", TaskType.DETECTION, 500, 100
                    ),
                    _template_node(
                        "coordinates", "analysis.board_end_coordinate_frame", 500, 400
                    ),
                    _template_node_with_config(
                        "constraints",
                        "analysis.board_view_cut_constraints",
                        850,
                        240,
                        {"view_role": "upper"},
                    ),
                    _template_node(
                        "fuse_cut", "analysis.single_view_board_cut_decision", 1160, 240
                    ),
                    _template_node("cut_overlay", "output.overlay", 1400, 100),
                    _board_cut_result_output_node(
                        "result_output", 1400, 300, allow_plc=False
                    ),
                ],
                edges=[
                    RuntimeGraphEdge(from_node_key="http_trigger", from_port="trigger", to_node_key="test_camera", to_port="trigger"),
                    RuntimeGraphEdge(from_node_key="test_camera", from_port="frame", to_node_key="features", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="test_camera", from_port="frame", to_node_key="coordinates", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="features", from_port="detections", to_node_key="constraints", to_port="features"),
                    RuntimeGraphEdge(from_node_key="coordinates", from_port="coordinate_frame", to_node_key="constraints", to_port="coordinate_frame"),
                    RuntimeGraphEdge(from_node_key="test_camera", from_port="frame", to_node_key="constraints", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="constraints", from_port="constraints", to_node_key="fuse_cut", to_port="constraints"),
                    RuntimeGraphEdge(from_node_key="test_camera", from_port="frame_set", to_node_key="fuse_cut", to_port="frame_set"),
                    RuntimeGraphEdge(from_node_key="fuse_cut", from_port="decision", to_node_key="cut_overlay", to_port="result"),
                    RuntimeGraphEdge(from_node_key="fuse_cut", from_port="result", to_node_key="result_output", to_port="result"),
                ],
            ),
        },
        {
            "slug": "template-trigger-single-end-face-board-cut-test",
            "name": "Single end-face board camera test",
            "description": (
                "Commission the end-face camera independently with metric feature "
                "measurement and the conservative two-diameter cut rule."
            ),
            "mode": RuntimeMode.TRIGGER,
            "graph": RuntimePipelineGraph(
                nodes=[
                    _template_node("http_trigger", "trigger.http_put", 40, 240),
                    _template_node_with_config(
                        "test_camera",
                        "source.single_board_camera_test",
                        250,
                        240,
                        {"camera_role": "end_face"},
                    ),
                    _template_inference_node(
                        "features", "inference.detection", TaskType.DETECTION, 500, 140
                    ),
                    _template_node(
                        "constraints", "analysis.end_face_cut_constraints", 820, 220
                    ),
                    _template_node(
                        "fuse_cut", "analysis.single_view_board_cut_decision", 1120, 220
                    ),
                    _template_node("cut_overlay", "output.overlay", 1360, 80),
                    _board_cut_result_output_node(
                        "result_output", 1360, 280, allow_plc=False
                    ),
                ],
                edges=[
                    RuntimeGraphEdge(from_node_key="http_trigger", from_port="trigger", to_node_key="test_camera", to_port="trigger"),
                    RuntimeGraphEdge(from_node_key="test_camera", from_port="frame", to_node_key="features", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="features", from_port="detections", to_node_key="constraints", to_port="features"),
                    RuntimeGraphEdge(from_node_key="test_camera", from_port="frame", to_node_key="constraints", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="constraints", from_port="constraints", to_node_key="fuse_cut", to_port="constraints"),
                    RuntimeGraphEdge(from_node_key="test_camera", from_port="frame_set", to_node_key="fuse_cut", to_port="frame_set"),
                    RuntimeGraphEdge(from_node_key="fuse_cut", from_port="decision", to_node_key="cut_overlay", to_port="result"),
                    RuntimeGraphEdge(from_node_key="fuse_cut", from_port="result", to_node_key="result_output", to_port="result"),
                ],
            ),
        },
        {
            "slug": "template-realtime-single-longitudinal-board-cut-test",
            "name": "Realtime longitudinal board camera test",
            "description": (
                "Continuously evaluate one upper or lower camera channel with metric "
                "board-cut rules. Intended for commissioning and dry-run observation."
            ),
            "mode": RuntimeMode.REALTIME,
            "graph": RuntimePipelineGraph(
                nodes=[
                    _template_node("channel_input", "source.channel_frame", 60, 260),
                    _template_inference_node(
                        "features", "inference.detection", TaskType.DETECTION, 340, 100
                    ),
                    _template_node(
                        "coordinates", "analysis.board_end_coordinate_frame", 340, 400
                    ),
                    _template_node_with_config(
                        "constraints",
                        "analysis.board_view_cut_constraints",
                        700,
                        240,
                        {"view_role": "upper"},
                    ),
                    _template_node(
                        "fuse_cut", "analysis.single_view_board_cut_decision", 1030, 240
                    ),
                    _template_node("cut_overlay", "output.overlay", 1270, 100),
                    _board_cut_result_output_node(
                        "result_output", 1270, 300, allow_plc=False
                    ),
                ],
                edges=[
                    RuntimeGraphEdge(from_node_key="channel_input", from_port="frame", to_node_key="features", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="channel_input", from_port="frame", to_node_key="coordinates", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="features", from_port="detections", to_node_key="constraints", to_port="features"),
                    RuntimeGraphEdge(from_node_key="coordinates", from_port="coordinate_frame", to_node_key="constraints", to_port="coordinate_frame"),
                    RuntimeGraphEdge(from_node_key="channel_input", from_port="frame", to_node_key="constraints", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="constraints", from_port="constraints", to_node_key="fuse_cut", to_port="constraints"),
                    RuntimeGraphEdge(from_node_key="fuse_cut", from_port="decision", to_node_key="cut_overlay", to_port="result"),
                    RuntimeGraphEdge(from_node_key="fuse_cut", from_port="result", to_node_key="result_output", to_port="result"),
                ],
            ),
        },
        {
            "slug": "template-realtime-single-end-face-board-cut-test",
            "name": "Realtime end-face board camera test",
            "description": (
                "Continuously evaluate an end-face camera channel with metric feature "
                "measurement and the conservative two-diameter cut rule."
            ),
            "mode": RuntimeMode.REALTIME,
            "graph": RuntimePipelineGraph(
                nodes=[
                    _template_node("channel_input", "source.channel_frame", 60, 220),
                    _template_inference_node(
                        "features", "inference.detection", TaskType.DETECTION, 340, 120
                    ),
                    _template_node(
                        "constraints", "analysis.end_face_cut_constraints", 680, 220
                    ),
                    _template_node(
                        "fuse_cut", "analysis.single_view_board_cut_decision", 1000, 220
                    ),
                    _template_node("cut_overlay", "output.overlay", 1240, 80),
                    _board_cut_result_output_node(
                        "result_output", 1240, 280, allow_plc=False
                    ),
                ],
                edges=[
                    RuntimeGraphEdge(from_node_key="channel_input", from_port="frame", to_node_key="features", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="features", from_port="detections", to_node_key="constraints", to_port="features"),
                    RuntimeGraphEdge(from_node_key="channel_input", from_port="frame", to_node_key="constraints", to_port="frame"),
                    RuntimeGraphEdge(from_node_key="constraints", from_port="constraints", to_node_key="fuse_cut", to_port="constraints"),
                    RuntimeGraphEdge(from_node_key="fuse_cut", from_port="decision", to_node_key="cut_overlay", to_port="result"),
                    RuntimeGraphEdge(from_node_key="fuse_cut", from_port="result", to_node_key="result_output", to_port="result"),
                ],
            ),
        },
    ]


def _template_node(node_key: str, node_type: str, x: float, y: float) -> RuntimeNodeSpec:
    spec = BUILTIN_NODE_TYPE_BY_KEY[node_type]
    return RuntimeNodeSpec(
        node_key=node_key,
        node_type=node_type,
        node_category=spec.node_category,
        input_ports=spec.input_ports,
        output_ports=spec.output_ports,
        config=spec.default_config,
        position={"x": x, "y": y},
    )


def _template_node_with_config(
    node_key: str,
    node_type: str,
    x: float,
    y: float,
    config: dict[str, Any],
) -> RuntimeNodeSpec:
    node = _template_node(node_key, node_type, x, y)
    return node.model_copy(update={"config": {**node.config, **config}})


def _board_cut_result_output_node(
    node_key: str,
    x: float,
    y: float,
    *,
    allow_plc: bool = True,
) -> RuntimeNodeSpec:
    node = _template_node(node_key, "output.result", x, y)
    fields = [
        ("schema_version", "Result contract version", "text"),
        ("workpiece_id", "Workpiece", "text"),
        ("valid", "Measurement valid", "boolean"),
        ("status", "Cut status", "text"),
        ("cut_position_mm", "Cut position [mm]", "number"),
        ("longitudinal_shift_mm", "Longitudinal shift [mm]", "number"),
        ("inspection_length_mm", "Inspection length [mm]", "number"),
        ("capture_skew_ms", "Capture skew [ms]", "number"),
        ("views_used", "Camera views", "list"),
        ("uncertainty_mm", "Uncertainty [mm]", "number"),
        ("dominant_constraint", "Dominant constraint", "object"),
        ("reason_code", "Reason code", "text"),
    ]
    projections = [
        {
            "key": key,
            "label": label,
            "source": {
                "node_key": "fuse_cut",
                "output_port": "result",
                "path": key,
                "value_type": value_type,
            },
            "destinations": ["response", "history"]
            + (
                ["plc"]
                if allow_plc and key in {"valid", "longitudinal_shift_mm"}
                else []
            ),
            "required": key in {"workpiece_id", "valid", "status"},
        }
        for key, label, value_type in fields
    ]
    return node.model_copy(
        update={
            "config": {
                **node.config,
                "projections": projections,
                "include_decision": False,
                "include_runtime_metadata": False,
            }
        }
    )


def _template_inference_node(
    node_key: str,
    node_type: str,
    task_type: TaskType,
    x: float,
    y: float,
    config: dict[str, Any] | None = None,
    *,
    unbound: bool = False,
) -> RuntimeNodeSpec:
    spec = BUILTIN_NODE_TYPE_BY_KEY[node_type]
    node_config = {**spec.default_config, **(config or {})}
    if unbound:
        node_config["template_model_unbound"] = True
    return RuntimeNodeSpec(
        node_key=node_key,
        node_type=node_type,
        node_category=spec.node_category,
        input_ports=spec.input_ports,
        output_ports=spec.output_ports,
        config=node_config,
        logical_model_ref=(
            None
            if unbound
            else {
                "model_family_slug": f"required-{task_type.value}-model",
                "target_path": "realtime",
                "contract_major": 1,
                "selection_policy": "active_production",
                "required_task_type": task_type.value,
            }
        ),
        position={"x": x, "y": y},
    )


def _stem_curvature_minimum_result_output_node(
    node_key: str,
    x: float,
    y: float,
) -> RuntimeNodeSpec:
    node = _template_node(node_key, "output.result", x, y)
    fields = [
        ("schema_version", "Result contract", "text"),
        ("contract", "Result contract alias", "text"),
        ("algorithm", "Orientation algorithm", "text"),
        ("algorithm_version", "Algorithm version", "text"),
        ("shadow_mode", "Shadow mode", "boolean"),
        ("plc_writes_suppressed", "PLC writes suppressed", "boolean"),
        ("session_id", "Session", "text"),
        ("track_id", "Active stem track", "text"),
        ("session_state", "Session state", "text"),
        ("decision", "Decision", "text"),
        ("signed_correction_deg", "Signed correction [deg]", "number"),
        (
            "recommended_rotation_deg_signed",
            "Recommended signed rotation [deg]",
            "number",
        ),
        (
            "recommended_rotation_deg_abs",
            "Recommended absolute rotation [deg]",
            "number",
        ),
        ("direction", "Rotation direction", "text"),
        ("curvature_min_mm", "Minimum curvature [mm]", "number"),
        ("curvature_max_mm", "Maximum curvature [mm]", "number"),
        ("curvature_span_mm", "Curvature span [mm]", "number"),
        ("target_phase_deg", "Target phase [deg]", "number"),
        ("maximum_phase_deg", "Maximum phase [deg]", "number"),
        ("current_phase_deg", "Current phase [deg]", "number"),
        ("observed_rotation_span_deg", "Observed rotation span [deg]", "number"),
        ("target_observed", "Target phase observed", "boolean"),
        (
            "phase_relocked_after_impact",
            "Phase re-locked after impact",
            "boolean",
        ),
        ("relock_status", "Re-lock status", "text"),
        ("active_stem_confidence", "Active stem confidence", "number"),
        ("geometry_confidence", "Geometry confidence", "number"),
        ("phase_confidence", "Phase confidence", "number"),
        ("fit_confidence", "Fit confidence", "number"),
        ("overall_confidence", "Overall confidence", "number"),
        ("calibration_id", "Process calibration", "text"),
        ("process_calibration_hash", "Process calibration hash", "text"),
        ("rotation_calibration_id", "Surface phase calibration", "text"),
        ("rotation_calibration_hash", "Surface phase calibration hash", "text"),
        (
            "minimum_principle_validation_status",
            "Reference validation status",
            "text",
        ),
        ("minimum_principle_validation_id", "Reference validation", "text"),
        ("decision_timestamp", "Decision timestamp", "text"),
        (
            "remaining_distance_to_first_roller_mm",
            "Remaining distance to first roller [mm]",
            "number",
        ),
        (
            "remaining_time_to_first_roller_ms",
            "Remaining time to first roller [ms]",
            "number",
        ),
        ("result_expiry", "Result expiry", "text"),
        ("deadline_met", "Decision deadline met", "boolean"),
        ("reason_codes", "Reason codes", "list"),
        ("diagnostic_graph", "Curvature phase diagnostic graph", "object"),
        ("asset_references", "Session artifact references", "object"),
    ]
    projections = [
        {
            "key": key,
            "label": label,
            "source": {
                "node_key": "solve_orientation",
                "output_port": "solution",
                "path": f"stem_orientation_result.{key}",
                "value_type": value_type,
            },
            "destinations": ["response", "history", "live_view"],
            # Before RESULT_READY the immutable V3 result deliberately does not
            # exist. Required projections would turn that normal session phase
            # into a runtime error instead of a fail-closed diagnostic state.
            "required": False,
        }
        for key, label, value_type in fields
    ]
    return node.model_copy(
        update={
            "config": {
                **node.config,
                "projections": projections,
                "include_decision": False,
                "include_runtime_metadata": True,
            }
        }
    )


def _stem_curvature_minimum_template_definition() -> dict[str, Any]:
    canonical_view_id = "stem-measurement-view-v1"
    full_view_roi = [
        {
            "label": "Canonical measurement corridor",
            "x": 0.0,
            "y": 0.0,
            "width": 1.0,
            "height": 1.0,
        }
    ]
    segmentation = _template_inference_node(
        "segment_stem",
        "inference.segmentation",
        TaskType.SEGMENTATION,
        760,
        340,
    )
    segmentation = segmentation.model_copy(
        update={
            "config": {
                **segmentation.config,
                "require_image_view_compatibility": True,
                "max_segments": 8,
            }
        }
    )
    return {
        "slug": "template-realtime-stem-curvature-minimum",
        "name": "Stammausrichtung – Krümmungsminimum",
        "description": (
            "Fail-closed Realtime-/Shadow-Vorlage mit intrinsischer Entzerrung, "
            "kanonischem Messkorridor, kompatibilitätsgebundener Segmentierung, "
            "Active-Stem-Tracking, Eligibility, horizontaler Krümmung, Mantelphase, "
            "V3-Sessionentscheidung, Diagnoseoverlay und Result-History-Projektionen. "
            "Vor dem Publizieren müssen nur Kamera-, View-/Prozess-, Modell- und "
            "Phasenkalibrierung an den Standort gebunden werden. Kein SPS-Node ist enthalten."
        ),
        "mode": RuntimeMode.REALTIME,
        "graph": RuntimePipelineGraph(
            nodes=[
                _template_node("channel_input", "source.channel_frame", 40, 340),
                _template_node("undistort", "preprocessing.camera_undistort", 280, 340),
                _template_node_with_config(
                    "canonical_view",
                    "preprocessing.process_coordinate_rectify",
                    520,
                    340,
                    {"view_profile_id": canonical_view_id},
                ),
                segmentation,
                _template_node_with_config(
                    "active_stem",
                    "analysis.active_stem_tracker",
                    1000,
                    340,
                    {
                        "transfer_rois": full_view_roi,
                        "longitudinal_conveyor_rois": full_view_roi,
                        "expected_motion_direction": "any",
                        "candidate_selection_policy": "largest_per_process_region",
                        "minimum_region_area_dominance_ratio": 1.10,
                        "prefer_longitudinal_on_acquisition": True,
                        "complete_at_decision_gate": True,
                        "minimum_visible_length_fraction": 0.55,
                        "minimum_acquisition_score": 0.55,
                        "minimum_continuity_score": 0.55,
                        "max_lost_frames": 12,
                        "reacquisition_minimum_score": 0.65,
                        "reacquisition_ambiguity_margin": 0.12,
                        "preserve_upstream_identity_during_occlusion": True,
                    },
                ),
                _template_node_with_config(
                    "measurement_eligibility",
                    "analysis.stem_measurement_eligibility",
                    1240,
                    80,
                    {
                        "view_profile_id": canonical_view_id,
                        "measurement_corridor": full_view_roi,
                        "min_visible_axis_ratio": 0.55,
                        "min_stable_frames": 3,
                        "reject_if_touching_crowd_boundary": True,
                        "reject_if_multiple_stems_in_view": False,
                        "allow_negative_frames": True,
                    },
                ),
                _template_node_with_config(
                    "stem_geometry",
                    "analysis.stem_mask_geometry",
                    1240,
                    340,
                    {
                        "analysis_mode": "phase_tracking_fast",
                        "debug_overlay": False,
                    },
                ),
                _template_node_with_config(
                    "measure_curvature",
                    "analysis.stem_horizontal_curvature",
                    1490,
                    225,
                    {"minimum_observation_interval_ms": 100},
                ),
                _template_node_with_config(
                    "track_surface_phase",
                    "analysis.stem_surface_phase",
                    1490,
                    455,
                    {
                        "phase_estimator_version": "CYLINDRICAL_SURFACE_V2",
                        "execution_mode": "HIGH_RATE_SEEDED",
                        "target_phase_fps": 25.0,
                        "tracking_max_width_px": 960,
                        "phase_frame_queue_size": 3,
                        "phase_replay_window_ms": 500,
                        "phase_replay_max_frames": 16,
                        "maximum_seed_age_ms": 300,
                        "minimum_blur_score": 18.0,
                        "maximum_forward_backward_error_px": 1.5,
                        "minimum_feature_span_ratio": 0.35,
                    },
                ),
                _template_node_with_config(
                    "solve_orientation",
                    "analysis.stem_curvature_phase_solver",
                    1750,
                    340,
                    {
                        "shadow_mode_enabled": True,
                        "shadow_evaluation_approved": False,
                        "record_session_artifacts": False,
                    },
                ),
                _stem_curvature_minimum_result_output_node(
                    "result_output",
                    2020,
                    220,
                ),
                _template_node_with_config(
                    "diagnostic_overlay",
                    "output.overlay",
                    2020,
                    390,
                    {
                        "layers": [
                            "labels",
                            "zones",
                            "lines",
                            "polygons",
                            "measurements",
                            "masks",
                        ]
                    },
                ),
                _template_node_with_config(
                    "display_model_view",
                    "output.display_output",
                    2260,
                    340,
                    {
                        "display_name": "Stammausrichtung – kanonische Modellansicht",
                        "overlay_layers": (
                            "segmentation,labels,values,zones,rois,lines,polygons,measurements"
                        ),
                        "object_layers": "segments,enriched_labels",
                    },
                ),
            ],
            edges=[
                RuntimeGraphEdge(
                    from_node_key="channel_input",
                    from_port="frame",
                    to_node_key="undistort",
                    to_port="frame",
                ),
                RuntimeGraphEdge(
                    from_node_key="undistort",
                    from_port="frame",
                    to_node_key="canonical_view",
                    to_port="frame",
                ),
                RuntimeGraphEdge(
                    from_node_key="canonical_view",
                    from_port="frame",
                    to_node_key="segment_stem",
                    to_port="frame",
                ),
                RuntimeGraphEdge(
                    from_node_key="segment_stem",
                    from_port="segments",
                    to_node_key="active_stem",
                    to_port="segments",
                ),
                RuntimeGraphEdge(
                    from_node_key="canonical_view",
                    from_port="frame",
                    to_node_key="active_stem",
                    to_port="frame",
                ),
                RuntimeGraphEdge(
                    from_node_key="canonical_view",
                    from_port="frame",
                    to_node_key="measurement_eligibility",
                    to_port="frame",
                ),
                RuntimeGraphEdge(
                    from_node_key="segment_stem",
                    from_port="segments",
                    to_node_key="measurement_eligibility",
                    to_port="segments",
                ),
                RuntimeGraphEdge(
                    from_node_key="active_stem",
                    from_port="segments",
                    to_node_key="measurement_eligibility",
                    to_port="active_stem",
                ),
                RuntimeGraphEdge(
                    from_node_key="measurement_eligibility",
                    from_port="result",
                    to_node_key="measure_curvature",
                    to_port="eligibility",
                ),
                RuntimeGraphEdge(
                    from_node_key="active_stem",
                    from_port="segments",
                    to_node_key="stem_geometry",
                    to_port="segments",
                ),
                RuntimeGraphEdge(
                    from_node_key="canonical_view",
                    from_port="frame",
                    to_node_key="stem_geometry",
                    to_port="frame",
                ),
                RuntimeGraphEdge(
                    from_node_key="active_stem",
                    from_port="segments",
                    to_node_key="measure_curvature",
                    to_port="segments",
                ),
                RuntimeGraphEdge(
                    from_node_key="canonical_view",
                    from_port="frame",
                    to_node_key="measure_curvature",
                    to_port="frame",
                ),
                RuntimeGraphEdge(
                    from_node_key="canonical_view",
                    from_port="frame",
                    to_node_key="track_surface_phase",
                    to_port="frame",
                ),
                RuntimeGraphEdge(
                    from_node_key="active_stem",
                    from_port="segments",
                    to_node_key="track_surface_phase",
                    to_port="segments",
                ),
                RuntimeGraphEdge(
                    from_node_key="stem_geometry",
                    from_port="stem_geometry",
                    to_node_key="track_surface_phase",
                    to_port="stem_geometry",
                ),
                RuntimeGraphEdge(
                    from_node_key="measure_curvature",
                    from_port="curvature",
                    to_node_key="solve_orientation",
                    to_port="curvature",
                ),
                RuntimeGraphEdge(
                    from_node_key="track_surface_phase",
                    from_port="surface_phase",
                    to_node_key="solve_orientation",
                    to_port="surface_phase",
                ),
                RuntimeGraphEdge(
                    from_node_key="active_stem",
                    from_port="segments",
                    to_node_key="solve_orientation",
                    to_port="active_stem",
                ),
                RuntimeGraphEdge(
                    from_node_key="solve_orientation",
                    from_port="solution",
                    to_node_key="result_output",
                    to_port="result",
                ),
                RuntimeGraphEdge(
                    from_node_key="solve_orientation",
                    from_port="overlay",
                    to_node_key="diagnostic_overlay",
                    to_port="result",
                ),
                RuntimeGraphEdge(
                    from_node_key="canonical_view",
                    from_port="frame",
                    to_node_key="display_model_view",
                    to_port="frame",
                ),
                RuntimeGraphEdge(
                    from_node_key="active_stem",
                    from_port="segments",
                    to_node_key="display_model_view",
                    to_port="objects",
                ),
                RuntimeGraphEdge(
                    from_node_key="solve_orientation",
                    from_port="overlay",
                    to_node_key="display_model_view",
                    to_port="result",
                ),
            ],
        ),
    }


def _stem_rotation_optimization_template_definition() -> dict[str, Any]:
    """Reuse the calibrated stem measurement path and replace only its decision tail."""
    base = _stem_curvature_minimum_template_definition()
    base_graph = base["graph"]
    assert isinstance(base_graph, RuntimePipelineGraph)
    removed_nodes = {"solve_orientation", "result_output", "diagnostic_overlay"}
    nodes = [
        (
            node.model_copy(
                update={
                    "config": {
                        **node.config,
                        "minimum_observation_interval_ms": 50,
                    }
                }
            )
            if node.node_key == "measure_curvature"
            else node
        )
        for node in base_graph.nodes
        if node.node_key not in removed_nodes
    ]
    nodes.extend(
        [
            _template_node_with_config(
                "build_slice_model",
                "analysis.stem_slice_model",
                1750,
                520,
                {
                    "slice_spacing_mm": 50.0,
                    "maximum_interpolation_gap_mm": 100.0,
                    "smoothing_window_slices": 3,
                    "maximum_material_x_step_mm": 75.0,
                    "maximum_material_x_excursion_mm": 150.0,
                },
            ),
            _template_node_with_config(
                "rotation_priority",
                "analysis.stem_rotation_priority_adapter",
                1750,
                720,
                {"priority_source": "FRONTEND"},
            ),
            _template_node_with_config(
                "optimize_rotation",
                "analysis.stem_rotation_optimizer",
                2020,
                590,
                {
                    "coarse_angle_step_deg": 1.0,
                    "fine_angle_step_deg": 0.1,
                    "fine_search_half_width_deg": 2.0,
                    "score_tolerance_mm": 1.0,
                    "min_gain_mm": 3.0,
                    "min_rotation_deg": 1.0,
                    "min_valid_slice_ratio": 0.6,
                    "min_model_quality": 0.25,
                    "rotation_negative_limit_deg": -90.0,
                    "rotation_positive_limit_deg": 90.0,
                },
            ),
            _template_node_with_config(
                "rotation_result_output",
                "output.result",
                2260,
                590,
                {
                    "projections": [],
                    "result_fields": [],
                    "include_decision": False,
                    "include_runtime_metadata": False,
                },
            ),
        ]
    )
    edges = [
        edge
        for edge in base_graph.edges
        if edge.from_node_key not in removed_nodes and edge.to_node_key not in removed_nodes
    ]
    edges.extend(
        [
            RuntimeGraphEdge(
                from_node_key="measure_curvature",
                from_port="curvature",
                to_node_key="build_slice_model",
                to_port="curvature",
            ),
            RuntimeGraphEdge(
                from_node_key="track_surface_phase",
                from_port="surface_phase",
                to_node_key="build_slice_model",
                to_port="surface_phase",
            ),
            RuntimeGraphEdge(
                from_node_key="active_stem",
                from_port="segments",
                to_node_key="build_slice_model",
                to_port="active_stem",
            ),
            RuntimeGraphEdge(
                from_node_key="canonical_view",
                from_port="frame",
                to_node_key="rotation_priority",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="build_slice_model",
                from_port="slice_model",
                to_node_key="optimize_rotation",
                to_port="slice_model",
            ),
            RuntimeGraphEdge(
                from_node_key="rotation_priority",
                from_port="priority",
                to_node_key="optimize_rotation",
                to_port="priority",
            ),
            RuntimeGraphEdge(
                from_node_key="optimize_rotation",
                from_port="result",
                to_node_key="rotation_result_output",
                to_port="result",
            ),
        ]
    )
    return {
        "slug": "template-realtime-stem-rotation-optimization",
        "name": "Stammausrichtung – geometrische Eindrehoptimierung",
        "description": (
            "Fail-closed Realtime-/Shadow-Vorlage auf dem vorhandenen kalibrierten "
            "Messpfad. Sie erzeugt ein elliptisches Scheibenmodell, übernimmt die "
            "Frontend-Priorität revisionssicher und publiziert Winkel und Geometriegewinn. "
            "Es ist kein SPS-Schreib- oder Lese-Node enthalten."
        ),
        "mode": RuntimeMode.REALTIME,
        "graph": RuntimePipelineGraph(nodes=nodes, edges=edges),
    }
