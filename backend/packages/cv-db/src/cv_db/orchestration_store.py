from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from cv_contracts import (
    EndpointRouteExample,
    ModelArchitectureSummary,
    ModelLifecycleStage,
    ModelTemplateCreateRequest,
    ModelTemplateSummary,
    ModelTemplateUpdateRequest,
    PipelineActivityItem,
    ProjectAutomationPolicy,
    ProjectDeployRequest,
    ProjectEndpointSummary,
    ProjectModelCreateRequest,
    ProjectModelSummary,
    ProjectModelUpdateRequest,
    ProjectModelWorkspaceDetail,
    ProjectPipelineDetail,
    ProjectPipelineSummary,
    ProjectPredictionBackendSummary,
    ProjectReadinessSummary,
    ProjectTrainRequest,
    PromotionCriteriaPolicy,
    TrainingJobCreateRequest,
)
from cv_contracts.modeling import training_defaults_from_persisted
from cv_core import generate_prefixed_id
from cv_datasets import LabelStudioClient, LabelStudioError, extract_declared_classes
from cv_status import (
    DatasetStatus,
    DeploymentReleaseStatus,
    EndpointMode,
    LabelProjectStatus,
    ModelLifecycleStatus,
    SnapshotStatus,
    TargetPath,
    TaskType,
    TrainingJobStatus,
    ValidationStatus,
)
from cv_utils import utc_now, utc_now_iso
from sqlalchemy import select
from sqlalchemy.orm import Session

from cv_db.model_catalog import (
    DEFAULT_TEMPLATE_DEFINITIONS,
    architecture_summary,
    default_promotion_policy,
    default_template_summary,
    is_supported_architecture,
    is_supported_template_definition,
    recipe_for_architecture,
    supported_model_architectures,
)
from cv_db.models import (
    AuditEvent,
    Dataset,
    DatasetSnapshot,
    DeploymentRelease,
    LabelProject,
    ModelFamily,
    ModelTemplate,
    ModelVersion,
    TrainingJob,
)

TERMINAL_TRAINING_STATES = {
    TrainingJobStatus.COMPLETED.value,
    TrainingJobStatus.FAILED.value,
    TrainingJobStatus.CANCELLED.value,
    TrainingJobStatus.ROLLED_BACK_IF_NEEDED.value,
}

_TEMPLATE_SIZE_ORDER = {
    "n": 0,
    "s": 1,
    "m": 2,
    "l": 3,
    "x": 4,
    "18": 0,
    "50": 1,
    "small": 0,
    "large": 1,
    "b0": 0,
    "resnet50": 0,
}


def _version_sort_key(version: str) -> tuple[int, str]:
    match = re.search(r"\d+", version)
    return (int(match.group(0)) if match else 0, version)


def _template_sort_key(item: ModelTemplateSummary) -> tuple[object, ...]:
    architecture = item.architecture
    return (
        not item.recommended,
        item.task_type.value,
        architecture.family,
        _version_sort_key(architecture.version),
        _TEMPLATE_SIZE_ORDER.get(architecture.size, 99),
        item.label,
    )


def list_project_model_templates(*, task_type: TaskType | None = None) -> list[ModelTemplateSummary]:
    items = [
        default_template_summary(template_key)
        for template_key, definition in DEFAULT_TEMPLATE_DEFINITIONS.items()
        if is_supported_template_definition(template_key)
        if task_type is None or definition["task_type"] == task_type
    ]
    return sorted(items, key=_template_sort_key)


def ensure_model_templates_seeded(session: Session) -> None:
    existing_rows = {
        row.template_key: row for row in session.scalars(select(ModelTemplate)).all()
    }
    for template_key in DEFAULT_TEMPLATE_DEFINITIONS:
        summary = default_template_summary(template_key)
        row = existing_rows.get(template_key)
        if row is None:
            session.add(
                ModelTemplate(
                    id=generate_prefixed_id("mtp"),
                    template_key=summary.template_key,
                    label=summary.label,
                    description=summary.description,
                    task_type=summary.task_type.value,
                    architecture_family=summary.architecture.family,
                    architecture_version=summary.architecture.version,
                    architecture_size=summary.architecture.size,
                    architecture_display_name=summary.architecture.display_name,
                    display_name_suffix=summary.display_name_suffix,
                    default_training_profile=summary.default_training_profile,
                    template_version=summary.template_version,
                    training_defaults=summary.defaults.model_dump(mode="json"),
                    default_promotion_policy=summary.default_promotion_policy.model_dump(mode="json"),
                    recommended=summary.recommended,
                    active=summary.active,
                    created_by="system",
                    updated_by="system",
                )
            )
            continue
        row.recommended = summary.recommended
        row.active = summary.active
        if row.created_by == "system" and (row.updated_by in {None, "system"}):
            training_defaults = summary.defaults.model_dump(mode="json")
            if row.training_defaults != training_defaults:
                row.training_defaults = training_defaults
                row.template_version += 1
                row.updated_by = "system"
        if not row.default_promotion_policy:
            row.default_promotion_policy = summary.default_promotion_policy.model_dump(mode="json")
    session.flush()


def list_model_templates(
    session: Session,
    *,
    task_type: TaskType | None = None,
    include_inactive: bool = False,
) -> list[ModelTemplateSummary]:
    ensure_model_templates_seeded(session)
    stmt = select(ModelTemplate).order_by(ModelTemplate.recommended.desc(), ModelTemplate.label.asc())
    rows = session.scalars(stmt).all()
    items = []
    for row in rows:
        if not is_supported_architecture(
            task_type=TaskType(row.task_type),
            family=row.architecture_family,
            version=row.architecture_version,
            size=row.architecture_size,
        ):
            continue
        if task_type is not None and row.task_type != task_type.value:
            continue
        if not include_inactive and not row.active:
            continue
        items.append(_model_template_summary(row))
    return sorted(items, key=_template_sort_key)


def create_model_template(
    session: Session,
    *,
    payload: ModelTemplateCreateRequest,
    created_by: str | None,
) -> ModelTemplateSummary:
    ensure_model_templates_seeded(session)
    if session.scalar(select(ModelTemplate).where(ModelTemplate.template_key == payload.template_key)):
        raise ValueError(f"Model template {payload.template_key} already exists.")
    _validate_supported_architecture(
        task_type=payload.task_type,
        family=payload.architecture_family,
        version=payload.architecture_version,
        size=payload.architecture_size,
    )
    architecture = architecture_summary(
        task_type=payload.task_type,
        family=payload.architecture_family,
        version=payload.architecture_version,
        size=payload.architecture_size,
    )
    promotion_policy = payload.default_promotion_policy or default_promotion_policy(payload.task_type)
    row = ModelTemplate(
        id=generate_prefixed_id("mtp"),
        template_key=payload.template_key,
        label=payload.label,
        description=payload.description,
        task_type=payload.task_type.value,
        architecture_family=architecture.family,
        architecture_version=architecture.version,
        architecture_size=architecture.size,
        architecture_display_name=architecture.display_name,
        display_name_suffix=payload.display_name_suffix,
        default_training_profile=payload.default_training_profile,
        template_version=1,
        training_defaults=payload.defaults.model_dump(mode="json"),
        default_promotion_policy=promotion_policy.model_dump(mode="json"),
        recommended=payload.recommended,
        active=payload.active,
        created_by=created_by,
        updated_by=created_by,
    )
    session.add(row)
    session.flush()
    return _model_template_summary(row)


def update_model_template(
    session: Session,
    *,
    template_key: str,
    payload: ModelTemplateUpdateRequest,
    updated_by: str | None,
) -> ModelTemplateSummary:
    ensure_model_templates_seeded(session)
    row = session.scalar(select(ModelTemplate).where(ModelTemplate.template_key == template_key))
    if row is None:
        raise ValueError(f"Model template {template_key} does not exist.")
    if payload.label is not None:
        row.label = payload.label
    if payload.description is not None:
        row.description = payload.description
    if payload.display_name_suffix is not None:
        row.display_name_suffix = payload.display_name_suffix
    if payload.default_training_profile is not None:
        row.default_training_profile = payload.default_training_profile
    if payload.defaults is not None:
        row.training_defaults = payload.defaults.model_dump(mode="json")
    if payload.default_promotion_policy is not None:
        row.default_promotion_policy = payload.default_promotion_policy.model_dump(mode="json")
    if payload.recommended is not None:
        row.recommended = payload.recommended
    if payload.active is not None:
        row.active = payload.active
    row.template_version += 1
    row.updated_by = updated_by
    session.flush()
    return _model_template_summary(row)


def create_project_model_family(
    session: Session,
    *,
    label_project_id: str,
    payload: ProjectModelCreateRequest,
) -> ProjectModelSummary:
    ensure_model_templates_seeded(session)
    project = session.get(LabelProject, label_project_id)
    if project is None or project.deleted_at is not None:
        raise ValueError(f"Label project {label_project_id} does not exist.")

    template = _model_template_record(session, payload.model_template_key)
    template_task_type = TaskType(template.task_type)
    if payload.task_type is not None and payload.task_type != template_task_type:
        raise ValueError(
            f"Template {payload.model_template_key} targets task type {template_task_type.value} "
            f"but the request declared {payload.task_type.value}."
        )
    project_task_type = TaskType(project.task_type)
    if project_task_type != template_task_type:
        _retarget_project_task_type_for_model_creation(
            session,
            project=project,
            current_task_type=project_task_type,
            target_task_type=template_task_type,
            template=template,
        )
        project_task_type = template_task_type
    task_type = template_task_type
    _validate_supported_architecture(
        task_type=task_type,
        family=payload.architecture_family,
        version=payload.architecture_version,
        size=payload.architecture_size,
    )
    class_names = _validate_project_model_classes(project, payload.class_names)
    segmentation_mode = _resolve_segmentation_mode(
        task_type=task_type,
        architecture_family=payload.architecture_family,
        requested_mode=payload.segmentation_mode,
    )
    template_promotion_policy = _template_promotion_policy(template)
    if payload.promotion_policy is not None and not template_promotion_policy.allow_model_override:
        raise ValueError("The selected template does not allow model-level promotion policy overrides.")
    promotion_policy = payload.promotion_policy or template_promotion_policy

    display_name = (
        payload.display_name.strip()
        if payload.display_name and payload.display_name.strip()
        else _default_display_name(project.title, template_key=payload.model_template_key)
    )
    slug = _unique_family_slug(session, _slugify(display_name))
    architecture = architecture_summary(
        task_type=task_type,
        family=payload.architecture_family,
        version=payload.architecture_version,
        size=payload.architecture_size,
    )
    family = ModelFamily(
        id=generate_prefixed_id("mfy"),
        slug=slug,
        label_project_id=project.id,
        display_name=display_name,
        description=payload.description,
        template_key=payload.model_template_key,
        template_version=template.template_version,
        training_profile_key=template.default_training_profile,
        training_overrides=(
            payload.training_overrides.model_dump(mode="json")
            if payload.training_overrides is not None
            else {}
        ),
        promotion_policy=promotion_policy.model_dump(mode="json"),
        class_names=class_names,
        segmentation_mode=segmentation_mode,
        task_type=task_type.value,
        architecture_family=architecture.family,
        architecture_version=architecture.version,
        architecture_size=architecture.size,
        architecture_display_name=architecture.display_name,
        contract_major=1,
        input_schema_ref=f"contracts/{task_type.value}/input.v1.json",
        output_schema_ref=f"contracts/{task_type.value}/output.v1.json",
        active=True,
    )
    session.add(family)
    project.default_model_template = project.default_model_template or payload.model_template_key
    project.default_training_profile = project.default_training_profile or template.default_training_profile
    session.flush()
    return _project_model_summary(
        family,
        versions_by_family={},
        jobs_by_family={},
    )


def update_project_model_family(
    session: Session,
    *,
    label_project_id: str,
    model_family_id: str,
    payload: ProjectModelUpdateRequest,
) -> ProjectModelSummary:
    family = session.get(ModelFamily, model_family_id)
    if (
        family is None
        or family.label_project_id != label_project_id
        or family.deleted_at is not None
    ):
        raise ValueError("The selected model does not belong to this project.")

    fields = payload.model_fields_set
    if "display_name" in fields and payload.display_name is not None:
        family.display_name = payload.display_name.strip()
    if "description" in fields:
        family.description = payload.description.strip() if payload.description else None
    if "class_names" in fields and payload.class_names is not None:
        project = session.get(LabelProject, label_project_id)
        if project is None or project.deleted_at is not None:
            raise ValueError(f"Label project {label_project_id} does not exist.")
        family.class_names = _validate_project_model_classes(project, payload.class_names)
    if "promotion_policy" in fields and payload.promotion_policy is not None:
        template = _model_template_record(
            session,
            family.template_key or _default_template_key(TaskType(family.task_type)),
        )
        if not _template_promotion_policy(template).allow_model_override:
            raise ValueError("The selected template does not allow model-level quality gate overrides.")
        family.promotion_policy = payload.promotion_policy.model_dump(mode="json")

    session.flush()
    versions = session.scalars(
        select(ModelVersion)
        .where(ModelVersion.model_family_id == family.id, ModelVersion.deleted_at.is_(None))
        .order_by(ModelVersion.created_at.desc())
    ).all()
    jobs = session.scalars(
        select(TrainingJob)
        .where(TrainingJob.model_family_id == family.id, TrainingJob.deleted_at.is_(None))
        .order_by(TrainingJob.created_at.desc())
    ).all()
    return _project_model_summary(
        family,
        versions_by_family={family.id: versions},
        jobs_by_family={family.id: jobs},
    )


def _retarget_project_task_type_for_model_creation(
    session: Session,
    *,
    project: LabelProject,
    current_task_type: TaskType,
    target_task_type: TaskType,
    template: ModelTemplate,
) -> None:
    allowed_retarget_pairs = (
        {TaskType.CLASSIFICATION, TaskType.ANOMALY},
        {TaskType.DETECTION, TaskType.ORIENTED_DETECTION},
    )
    if {current_task_type, target_task_type} not in allowed_retarget_pairs:
        raise ValueError(
            f"Template {template.template_key} targets task type {target_task_type.value} "
            f"but the project pipeline is {current_task_type.value}."
        )

    incompatible_family = session.scalar(
        select(ModelFamily).where(
            ModelFamily.label_project_id == project.id,
            ModelFamily.task_type != target_task_type.value,
            ModelFamily.deleted_at.is_(None),
        )
    )
    if incompatible_family is not None:
        raise ValueError(
            "This project already has models for another task type. Clean up the existing "
            "project models before changing between compatible task variants."
        )

    project.task_type = target_task_type.value
    project.default_model_template = template.template_key
    project.default_model_family_id = None
    project.auto_train_enabled = False
    project.default_training_profile = template.default_training_profile
    if target_task_type == TaskType.ANOMALY:
        project.class_balance_strategy = "none"

    if project.dataset_id:
        dataset = session.get(Dataset, project.dataset_id)
        if dataset is not None:
            dataset.task_type = target_task_type.value
            if dataset.status == DatasetStatus.SNAPSHOTTED.value:
                dataset.status = DatasetStatus.READY.value

    for snapshot in session.scalars(
        select(DatasetSnapshot).where(
            DatasetSnapshot.label_project_id == project.id,
            DatasetSnapshot.status == SnapshotStatus.READY.value,
            DatasetSnapshot.deleted_at.is_(None),
        )
    ).all():
        snapshot.status = SnapshotStatus.SUPERSEDED.value
        snapshot.processing_error = (
            "Superseded because the project model type changed from "
            f"{current_task_type.value} to {target_task_type.value}."
        )


def update_project_automation_policy(
    session: Session,
    *,
    label_project_id: str,
    payload: ProjectAutomationPolicy,
) -> ProjectAutomationPolicy:
    project = session.get(LabelProject, label_project_id)
    if project is None or project.deleted_at is not None:
        raise ValueError(f"Label project {label_project_id} does not exist.")

    if payload.default_model_template is not None:
        template = _model_template_record(session, payload.default_model_template)
        template_task_type = TaskType(template.task_type)
        project_task_type = TaskType(project.task_type)
        if template_task_type != project_task_type:
            raise ValueError(
                f"Default template {payload.default_model_template} targets task type {template_task_type.value} "
                f"but the project pipeline is {project_task_type.value}."
            )

    if payload.default_model_family_id is not None:
        family = session.get(ModelFamily, payload.default_model_family_id)
        if (
            family is None
            or family.label_project_id != project.id
            or family.deleted_at is not None
            or not family.active
        ):
            raise ValueError("The default model must be an active model from this project.")
        if TaskType(family.task_type) != TaskType(project.task_type):
            raise ValueError("The default model must use the same task type as the project.")
    if payload.auto_train_enabled and payload.default_model_family_id is None:
        raise ValueError("Choose a default project model before enabling automatic training.")

    project.auto_sync_enabled = payload.auto_sync_enabled
    project.auto_snapshot_enabled = payload.auto_snapshot_enabled
    project.auto_train_enabled = payload.auto_train_enabled
    project.auto_deploy_enabled = payload.auto_deploy_enabled
    project.default_model_template = payload.default_model_template
    project.default_model_family_id = payload.default_model_family_id
    project.default_training_profile = payload.default_training_profile
    project.preferred_endpoint_mode = payload.preferred_endpoint_mode.value
    session.flush()
    return _automation_policy(project)


def build_project_training_request(
    session: Session,
    *,
    label_project_id: str,
    payload: ProjectTrainRequest | None = None,
) -> TrainingJobCreateRequest:
    request_payload = payload or ProjectTrainRequest()
    ensure_model_templates_seeded(session)
    project = session.get(LabelProject, label_project_id)
    if project is None or project.deleted_at is not None:
        raise ValueError(f"Label project {label_project_id} does not exist.")

    project_task_type = TaskType(project.task_type)
    model_family = _select_project_model_family(session, project=project, payload=request_payload)
    project_task_type = _resolve_project_task_type_for_model_family(
        session,
        project=project,
        project_task_type=project_task_type,
        model_family=model_family,
    )
    snapshot = _select_training_snapshot(session, project=project, payload=request_payload)
    candidate_template_key = (
        request_payload.model_template_key
        or model_family.template_key
        or project.default_model_template
        or _default_template_key(project_task_type)
    )
    template_key = _coerce_compatible_template_key(
        session,
        project_task_type,
        candidate_template_key,
    )
    template = _model_template_record(session, template_key)
    template_defaults = (template.training_defaults or {}).get("training") or {}
    defaults = training_defaults_from_persisted(
        model_family.training_overrides or template_defaults
    )
    training_profile_key = (
        request_payload.training_profile_key
        or model_family.training_profile_key
        or project.default_training_profile
        or template.default_training_profile
    )
    architecture = _row_architecture(
        task_type=project_task_type,
        family=model_family.architecture_family,
        version=model_family.architecture_version,
        size=model_family.architecture_size,
        display_name=model_family.architecture_display_name,
    )
    recipe_name, recipe_version = recipe_for_architecture(
        task_type=project_task_type,
        family=architecture.family,
        version=architecture.version,
    )
    best_metric_name, best_metric_mode, _default_criteria = _quality_defaults_for_task(project_task_type)
    promotion_policy = _model_family_promotion_policy(model_family, task_type=project_task_type)
    optimizer_settings = {
        **dict(defaults.optimizer_settings or {}),
        "class_balance_strategy": _class_balance_strategy(project),
    }

    return TrainingJobCreateRequest(
        model_family_slug=model_family.slug,
        dataset_snapshot_id=snapshot.id,
        task_type=project_task_type,
        architecture=architecture,
        model_template_key=template_key,
        model_template_version=template.template_version,
        recipe_name=recipe_name,
        recipe_version=recipe_version,
        max_epochs=defaults.epochs,
        learning_rate=defaults.learning_rate,
        image_size=defaults.image_size,
        batch_size=defaults.batch_size,
        optimizer=defaults.optimizer,
        optimizer_settings=optimizer_settings,
        checkpoint_interval_steps=defaults.checkpoint_interval_steps,
        resolved_training_defaults=defaults,
        parameter_overrides={
            "epochs": defaults.epochs,
            "learning_rate": defaults.learning_rate,
            "image_size": defaults.image_size,
            "batch_size": defaults.batch_size,
            "optimizer": defaults.optimizer,
            "optimizer_settings": optimizer_settings,
            "class_balance_strategy": optimizer_settings["class_balance_strategy"],
            "checkpoint_interval_steps": defaults.checkpoint_interval_steps,
            "template_key": template_key,
            "template_version": template.template_version,
            "training_profile_key": training_profile_key,
            "architecture": architecture.model_dump(mode="json"),
            "promotion_policy": promotion_policy.model_dump(mode="json"),
            "class_names": list(model_family.class_names or []),
            "segmentation_mode": getattr(model_family, "segmentation_mode", None),
        },
        best_checkpoint_rule={
            "metric_name": best_metric_name,
            "mode": best_metric_mode,
            "phase": "validation",
            "minimum_epoch": 1,
        },
        promotion_criteria=promotion_policy.criteria,
        promotion_policy=promotion_policy,
        allow_duplicate_snapshot_training=request_payload.allow_duplicate_snapshot_training,
        idempotency_key=None,
    )


def resolve_project_latest_model_version(
    session: Session,
    *,
    label_project_id: str,
    model_family_id: str | None = None,
) -> ModelVersion | None:
    families = session.scalars(
        select(ModelFamily)
        .where(ModelFamily.label_project_id == label_project_id, ModelFamily.deleted_at.is_(None))
        .order_by(ModelFamily.created_at.desc())
    ).all()
    if not families:
        return None
    if model_family_id:
        families = [item for item in families if item.id == model_family_id]
        if not families:
            return None
    family_ids = [row.id for row in families]
    return session.scalar(
        select(ModelVersion)
        .where(ModelVersion.model_family_id.in_(family_ids), ModelVersion.deleted_at.is_(None))
        .order_by(ModelVersion.created_at.desc())
    )


def resolve_project_latest_approved_model_version(
    session: Session,
    *,
    label_project_id: str,
    model_family_id: str | None = None,
) -> ModelVersion | None:
    families = session.scalars(
        select(ModelFamily)
        .where(ModelFamily.label_project_id == label_project_id, ModelFamily.deleted_at.is_(None))
        .order_by(ModelFamily.created_at.desc())
    ).all()
    if not families:
        return None
    if model_family_id:
        families = [item for item in families if item.id == model_family_id]
        if not families:
            return None
    family_ids = [row.id for row in families]
    versions = session.scalars(
        select(ModelVersion)
        .where(ModelVersion.model_family_id.in_(family_ids), ModelVersion.deleted_at.is_(None))
        .order_by(ModelVersion.created_at.desc())
    ).all()
    return next(
        (item for item in versions if bool((item.evaluation_summary or {}).get("promotion_ready"))),
        None,
    )


def resolve_project_deployment_model_version(
    session: Session,
    *,
    label_project_id: str,
    payload: ProjectDeployRequest,
) -> ModelVersion | None:
    if payload.model_version_id:
        version = session.get(ModelVersion, payload.model_version_id)
        family = session.get(ModelFamily, version.model_family_id) if version is not None else None
        if (
            version is None
            or version.deleted_at is not None
            or family is None
            or family.deleted_at is not None
            or family.label_project_id != label_project_id
        ):
            raise ValueError("The selected model version does not belong to this project.")
        if payload.model_family_id and family.id != payload.model_family_id:
            raise ValueError("The selected model version does not belong to the selected model.")
        if payload.allow_quality_gate_bypass or bool(
            (version.evaluation_summary or {}).get("promotion_ready")
        ):
            return version
        return None

    resolver = (
        resolve_project_latest_model_version
        if payload.allow_quality_gate_bypass
        else resolve_project_latest_approved_model_version
    )
    return resolver(
        session,
        label_project_id=label_project_id,
        model_family_id=payload.model_family_id,
    )


def resolve_project_deployment_targets(
    session: Session,
    *,
    label_project_id: str,
    payload: ProjectDeployRequest | None = None,
) -> tuple[LabelProject, list[TargetPath]]:
    project = session.get(LabelProject, label_project_id)
    if project is None or project.deleted_at is not None:
        raise ValueError(f"Label project {label_project_id} does not exist.")
    preferred_mode = payload.preferred_endpoint_mode.value if payload and payload.preferred_endpoint_mode else (
        project.preferred_endpoint_mode or EndpointMode.REALTIME.value
    )
    return project, _target_paths(EndpointMode(preferred_mode))


def resolve_project_active_realtime_deployment(
    session: Session,
    *,
    label_project_id: str,
    model_family_id: str | None = None,
) -> tuple[LabelProject, ModelFamily, ModelVersion, DeploymentRelease] | None:
    project = session.get(LabelProject, label_project_id)
    if project is None or project.deleted_at is not None:
        return None
    statement = (
        select(DeploymentRelease, ModelFamily, ModelVersion)
        .join(ModelFamily, ModelFamily.id == DeploymentRelease.model_family_id)
        .join(ModelVersion, ModelVersion.id == DeploymentRelease.model_version_id)
        .where(
            ModelFamily.label_project_id == label_project_id,
            ModelFamily.active.is_(True),
            ModelFamily.deleted_at.is_(None),
            ModelVersion.deleted_at.is_(None),
            DeploymentRelease.target_path == TargetPath.REALTIME.value,
            DeploymentRelease.status == DeploymentReleaseStatus.ACTIVATED.value,
            DeploymentRelease.retired_at.is_(None),
            DeploymentRelease.deleted_at.is_(None),
        )
        .order_by(DeploymentRelease.activated_at.desc(), DeploymentRelease.created_at.desc())
    )
    if model_family_id is not None:
        statement = statement.where(ModelFamily.id == model_family_id)
    row = session.execute(statement).first()
    if row is None:
        return None
    release, family, version = row
    return project, family, version, release


def ensure_project_prediction_backend(
    session: Session,
    *,
    settings,
    label_project_id: str,
    model_family_id: str | None = None,
) -> ProjectPredictionBackendSummary:
    active = resolve_project_active_realtime_deployment(
        session,
        label_project_id=label_project_id,
        model_family_id=model_family_id,
    )
    if active is None:
        project = session.get(LabelProject, label_project_id)
        if project is None or project.deleted_at is not None:
            raise ValueError(f"Label project {label_project_id} does not exist.")
        raise ValueError(
            "Label Studio predictions require an active realtime deployment for this project."
        )
    project, family, version, _release = active
    response = _upsert_labelstudio_ml_backend(
        settings=settings,
        project=project,
        family=family,
        version=version,
    )
    _record_prediction_backend_link(
        project=project,
        backend=response,
        family=family,
        version=version,
    )
    session.flush()
    return _prediction_backend_summary(
        project,
        family_by_id={family.id: family},
        version_by_id={version.id: version},
    )


def refresh_project_prediction_backend_binding(
    session: Session,
    *,
    settings,
    label_project_id: str,
) -> ProjectPredictionBackendSummary | None:
    project = session.get(LabelProject, label_project_id)
    if project is None or project.deleted_at is not None:
        return None
    if not bool(getattr(project, "prediction_backend_enabled", False)):
        return None

    active = resolve_project_active_realtime_deployment(
        session,
        label_project_id=label_project_id,
        model_family_id=getattr(project, "prediction_model_family_id", None),
    )
    if active is None:
        project.prediction_backend_status = "no_realtime_deployment"
        project.prediction_last_error = (
            "The project is linked for Label Studio predictions, but no active realtime deployment is available."
        )
        session.flush()
        return _prediction_backend_summary(project)

    project, family, version, _release = active
    try:
        response = _upsert_labelstudio_ml_backend(
            settings=settings,
            project=project,
            family=family,
            version=version,
        )
    except LabelStudioError as exc:
        project.prediction_backend_status = "failed"
        project.prediction_last_error = str(exc)[:1000]
        session.flush()
        return _prediction_backend_summary(
            project,
            family_by_id={family.id: family},
            version_by_id={version.id: version},
        )

    _record_prediction_backend_link(
        project=project,
        backend=response,
        family=family,
        version=version,
    )
    session.flush()
    return _prediction_backend_summary(
        project,
        family_by_id={family.id: family},
        version_by_id={version.id: version},
    )


def list_project_pipeline_summaries(session: Session) -> list[ProjectPipelineSummary]:
    return _pipeline_summaries(session)


def _upsert_labelstudio_ml_backend(
    *,
    settings,
    project: LabelProject,
    family: ModelFamily,
    version: ModelVersion,
) -> dict[str, Any]:
    client = LabelStudioClient(
        base_url=settings.label_studio.base_url,
        token=settings.label_studio.token(),
        timeout_seconds=settings.label_studio.request_timeout_seconds,
    )
    backend_url = _prediction_backend_url(settings, project)
    title = _prediction_backend_title(project)
    description = (
        "CV Platform prediction adapter. This is the Label Studio prediction default; "
        "runtime channels can independently use every deployed project model."
    )
    extra_params = {
        "label_project_id": project.id,
        "model_family_slug": family.slug,
        "model_version_id": version.id,
        "model_version_no": version.version_no,
        "auto_update_from_deployment": True,
    }

    if project.prediction_backend_id:
        try:
            return client.update_ml_backend(
                int(project.prediction_backend_id),
                project=project.label_studio_project_id,
                url=backend_url,
                title=title,
                description=description,
                auth_method="NONE",
                auto_update=True,
                is_interactive=False,
                timeout=settings.label_studio.ml_backend_timeout_seconds,
                extra_params=extra_params,
            )
        except LabelStudioError:
            project.prediction_backend_id = None
            project.prediction_backend_status = "reconnecting"

    existing_backend = _find_existing_prediction_backend(
        client.list_ml_backends(project.label_studio_project_id),
        backend_url=backend_url,
        title=title,
    )
    if existing_backend is not None and existing_backend.get("id") is not None:
        backend_id = int(existing_backend["id"])
        return client.update_ml_backend(
            backend_id,
            project=project.label_studio_project_id,
            url=backend_url,
            title=title,
            description=description,
            auth_method="NONE",
            auto_update=True,
            is_interactive=False,
            timeout=settings.label_studio.ml_backend_timeout_seconds,
            extra_params=extra_params,
        )

    return client.create_ml_backend(
        project_id=project.label_studio_project_id,
        url=backend_url,
        title=title,
        description=description,
        extra_params=extra_params,
        timeout_seconds=settings.label_studio.ml_backend_timeout_seconds,
        is_interactive=False,
    )


def _find_existing_prediction_backend(
    items: list[dict[str, Any]],
    *,
    backend_url: str,
    title: str,
) -> dict[str, Any] | None:
    for item in items:
        if item.get("url") == backend_url or item.get("title") == title:
            return item
    return None


def _record_prediction_backend_link(
    *,
    project: LabelProject,
    backend: dict[str, Any],
    family: ModelFamily,
    version: ModelVersion,
) -> None:
    backend_id = backend.get("id")
    project.prediction_backend_enabled = True
    project.prediction_backend_id = int(backend_id) if backend_id is not None else None
    project.prediction_backend_url = str(
        backend.get("url") or _prediction_backend_url_from_record(project)
    )
    project.prediction_backend_status = "linked"
    project.prediction_model_family_id = family.id
    project.prediction_model_version_id = version.id
    project.prediction_linked_at = utc_now()
    project.prediction_last_error = None


def _prediction_backend_url(settings, project: LabelProject) -> str:
    return (
        f"{settings.label_studio.ml_backend_adapter_url.rstrip('/')}"
        f"/api/v1/projects/{project.id}"
    )


def _prediction_backend_title(project: LabelProject) -> str:
    return f"CV Platform Model - {project.title}"[:200]


def _prediction_backend_url_from_record(project: LabelProject) -> str:
    return project.prediction_backend_url or f"label-project:{project.id}"


def get_project_pipeline_detail(
    session: Session,
    *,
    label_project_id: str,
) -> ProjectPipelineDetail | None:
    summaries = _pipeline_summaries(session, label_project_id=label_project_id)
    if not summaries:
        return None
    return summaries[0]


def get_project_model_workspace_detail(
    session: Session,
    *,
    label_project_id: str,
    model_family_id: str,
) -> ProjectModelWorkspaceDetail | None:
    project = session.get(LabelProject, label_project_id)
    family = session.get(ModelFamily, model_family_id)
    if (
        project is None
        or project.deleted_at is not None
        or family is None
        or family.deleted_at is not None
        or family.label_project_id != project.id
    ):
        return None

    versions = session.scalars(
        select(ModelVersion)
        .where(ModelVersion.model_family_id == family.id, ModelVersion.deleted_at.is_(None))
        .order_by(ModelVersion.created_at.desc())
    ).all()
    jobs = session.scalars(
        select(TrainingJob)
        .where(TrainingJob.model_family_id == family.id, TrainingJob.deleted_at.is_(None))
        .order_by(TrainingJob.created_at.desc())
    ).all()
    releases = session.scalars(
        select(DeploymentRelease)
        .where(
            DeploymentRelease.model_family_id == family.id,
            DeploymentRelease.deleted_at.is_(None),
        )
        .order_by(DeploymentRelease.created_at.desc())
    ).all()
    snapshot_ids = {item.dataset_snapshot_id for item in jobs if item.dataset_snapshot_id}
    aggregate_ids = {
        project.id,
        family.id,
        *snapshot_ids,
        *(item.id for item in jobs),
        *(item.id for item in versions),
        *(item.id for item in releases),
    }
    events = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.aggregate_id.in_(aggregate_ids))
        .order_by(AuditEvent.occurred_at.desc())
        .limit(100)
    ).all()
    activity = [
        PipelineActivityItem(
            occurred_at=_iso(item.occurred_at) or utc_now_iso(),
            title=_activity_title(item.event_type),
            detail=_activity_detail(item.event_type, item.payload or {}),
            aggregate_type=item.aggregate_type,
            aggregate_id=item.aggregate_id,
            event_type=item.event_type,
        )
        for item in events
    ]

    pipeline = get_project_pipeline_detail(session, label_project_id=project.id)
    if pipeline is None:
        return None
    from cv_db.registry_store import list_model_versions

    version_summaries = list_model_versions(session, family_slug=family.slug)
    model_summary = _project_model_summary(
        family,
        versions_by_family={family.id: versions},
        jobs_by_family={family.id: jobs},
    )
    endpoints = [item for item in pipeline.current_endpoints if item.model_family_id == family.id]
    return ProjectModelWorkspaceDetail(
        label_project_id=project.id,
        model=model_summary,
        versions=version_summaries,
        training_history=[item for item in (_training_summary(job) for job in jobs) if item],
        endpoints=endpoints,
        lifecycle=_model_lifecycle_stages(
            project=project,
            family=family,
            jobs=jobs,
            versions=versions,
            releases=releases,
            activity=activity,
            latest_valid_snapshot=pipeline.latest_valid_snapshot,
        ),
        activity_timeline=activity,
    )


def _model_lifecycle_stages(
    *,
    project: LabelProject,
    family: ModelFamily,
    jobs: list[TrainingJob],
    versions: list[ModelVersion],
    releases: list[DeploymentRelease],
    activity: list[PipelineActivityItem],
    latest_valid_snapshot,
) -> list[ModelLifecycleStage]:
    latest_job = jobs[0] if jobs else None
    latest_job_summary = _training_summary(latest_job)
    latest_version = versions[0] if versions else None
    version_releases = (
        [item for item in releases if item.model_version_id == latest_version.id]
        if latest_version is not None
        else []
    )

    def activities_for(*aggregate_ids: str | None) -> list[PipelineActivityItem]:
        ids = {item for item in aggregate_ids if item}
        return [item for item in activity if item.aggregate_id in ids][:12]

    dataset_snapshot_id = (
        latest_job.dataset_snapshot_id
        if latest_job is not None
        else getattr(latest_valid_snapshot, "snapshot_id", None)
    )
    stages = [
        ModelLifecycleStage(
            key="dataset",
            label="Datensatz",
            state="success" if dataset_snapshot_id else "pending",
            finished_at=getattr(latest_valid_snapshot, "created_at", None),
            detail=(
                f"Snapshot {dataset_snapshot_id} ist dem Modelllauf fest zugeordnet."
                if dataset_snapshot_id
                else "Ein gültiger Snapshot wird für das erste Training benötigt."
            ),
            activities=activities_for(dataset_snapshot_id),
        )
    ]

    if latest_job is None:
        training_state = "pending"
    elif latest_job.status in TERMINAL_TRAINING_STATES:
        training_state = "success" if latest_job.status == TrainingJobStatus.COMPLETED.value else "failed"
    else:
        training_state = "running"
    stages.append(
        ModelLifecycleStage(
            key="training",
            label="Training",
            state=training_state,
            progress_percent=(latest_job_summary.progress_percent if latest_job_summary else None),
            started_at=_iso(latest_job.started_at) if latest_job else None,
            finished_at=_iso(latest_job.finished_at) if latest_job else None,
            detail=(
                f"Epoche {latest_job_summary.current_epoch}/{latest_job_summary.total_epochs} · {latest_job.status}"
                if latest_job and latest_job_summary
                else "Noch kein Trainingslauf vorhanden."
            ),
            error=latest_job.last_error if latest_job else None,
            activities=activities_for(latest_job.id if latest_job else None),
        )
    )

    promotion_ready = bool(
        latest_job and (latest_job.evaluation_summary or {}).get("promotion_ready")
    )
    if latest_job is None or training_state == "pending":
        evaluation_state = "pending"
    elif training_state == "failed":
        evaluation_state = "blocked"
    elif latest_job.status in {TrainingJobStatus.EVALUATING.value, TrainingJobStatus.EXPORTING.value}:
        evaluation_state = "running"
    elif latest_job.status == TrainingJobStatus.COMPLETED.value:
        evaluation_state = "success" if promotion_ready else "blocked"
    else:
        evaluation_state = "pending"
    stages.append(
        ModelLifecycleStage(
            key="evaluation",
            label="Evaluation & Quality Gates",
            state=evaluation_state,
            detail=(
                "Alle Quality Gates wurden bestanden."
                if promotion_ready
                else "Die Quality Gates sind noch offen oder blockieren das Deployment."
            ),
            activities=activities_for(latest_job.id if latest_job else None),
        )
    )

    registering_states = {
        TrainingJobStatus.EXPORTING.value,
        TrainingJobStatus.REGISTERING.value,
    }
    registration_state = (
        "success"
        if latest_version
        else "running"
        if latest_job and latest_job.status in registering_states
        else "pending"
    )
    stages.append(
        ModelLifecycleStage(
            key="registration",
            label="Export & Registrierung",
            state=registration_state,
            finished_at=_iso(latest_version.created_at) if latest_version else None,
            detail=(
                f"Modellversion v{latest_version.version_no} wurde registriert."
                if latest_version
                else "Nach erfolgreicher Evaluation werden Artefakte exportiert und registriert."
            ),
            activities=activities_for(
                latest_job.id if latest_job else None,
                latest_version.id if latest_version else None,
            ),
        )
    )

    package_status = latest_version.serving_package_status if latest_version else "missing"
    package_state = {
        "building": "running",
        "ready": "success",
        "failed": "failed",
    }.get(package_status, "pending")
    artifact_format = (
        (latest_version.serving_manifest or {}).get("serving_artifact_format")
        if latest_version
        else None
    )
    stages.append(
        ModelLifecycleStage(
            key="package",
            label="TensorRT & Bereitstellungspaket",
            state=package_state,
            finished_at=_iso(latest_version.serving_package_built_at) if latest_version else None,
            detail=(
                "Paket ist bereit "
                f"({artifact_format or _serving_artifact_label(latest_version)})."
                if package_state == "success"
                else "TensorRT-Engine und Bereitstellungspaket werden nach der Registrierung gebaut."
            ),
            error=latest_version.serving_package_error if latest_version else None,
            activities=activities_for(latest_version.id if latest_version else None),
        )
    )

    active_releases = [
        item for item in version_releases if item.status == DeploymentReleaseStatus.ACTIVATED.value
    ]
    failed_releases = [
        item for item in version_releases if item.status == DeploymentReleaseStatus.ACTIVATION_FAILED.value
    ]
    running_release_states = {
        DeploymentReleaseStatus.REQUESTED.value,
        DeploymentReleaseStatus.PACKAGING.value,
        DeploymentReleaseStatus.REPOSITORY_SYNC.value,
        DeploymentReleaseStatus.LOADING.value,
        DeploymentReleaseStatus.SMOKE_TESTING.value,
    }
    running_releases = [item for item in version_releases if item.status in running_release_states]
    deployment_state = (
        "success"
        if active_releases
        else "running"
        if running_releases
        else "failed"
        if failed_releases
        else "pending"
    )
    stages.append(
        ModelLifecycleStage(
            key="deployment",
            label="Staging & Smoke-Test",
            state=deployment_state,
            detail=(
                ", ".join(f"{item.target_path}: {item.status}" for item in version_releases)
                if version_releases
                else "Noch kein Deployment für diese Modellversion gestartet."
            ),
            error=("Mindestens ein Deployment ist fehlgeschlagen." if failed_releases else None),
            activities=activities_for(*(item.id for item in version_releases)),
        )
    )
    stages.append(
        ModelLifecycleStage(
            key="activation",
            label="Aktivierung",
            state="success" if active_releases else "failed" if failed_releases else "pending",
            finished_at=max((_iso(item.activated_at) or "" for item in active_releases), default=None),
            detail=(
                "Live auf " + ", ".join(sorted({item.target_path for item in active_releases}))
                if active_releases
                else "Das bisherige gesunde Deployment bleibt aktiv, bis die neue Version bereit ist."
            ),
            activities=activities_for(*(item.id for item in active_releases)),
        )
    )

    realtime_live = any(item.target_path == TargetPath.REALTIME.value for item in active_releases)
    prediction_linked = bool(
        project.prediction_backend_enabled
        and project.prediction_model_family_id == family.id
        and project.prediction_model_version_id == (latest_version.id if latest_version else None)
    )
    preferred_mode = project.preferred_endpoint_mode or EndpointMode.REALTIME.value
    labelstudio_state = (
        "not_applicable"
        if preferred_mode == EndpointMode.BATCH.value
        else "success"
        if prediction_linked
        else "warning"
        if realtime_live
        else "pending"
    )
    stages.append(
        ModelLifecycleStage(
            key="labelstudio",
            label="Label-Studio-Verbindung",
            state=labelstudio_state,
            finished_at=_iso(project.prediction_linked_at) if prediction_linked else None,
            detail=(
                "Label Studio verwendet diese aktive Modellversion für Vorhersagen."
                if prediction_linked
                else "Ein aktives Realtime-Deployment kann mit Label Studio verbunden werden."
            ),
            error=project.prediction_last_error,
            activities=activities_for(project.id, family.id),
        )
    )
    return stages


def _serving_artifact_label(version: ModelVersion | None) -> str:
    return "TensorRT" if version is not None and version.tensorrt_uri else "Runtime-Artefakt"


def _pipeline_summaries(
    session: Session,
    *,
    label_project_id: str | None = None,
) -> list[ProjectPipelineDetail]:
    ensure_model_templates_seeded(session)
    project_stmt = (
        select(LabelProject)
        .where(LabelProject.deleted_at.is_(None))
        .order_by(LabelProject.created_at.desc())
    )
    if label_project_id:
        project_stmt = project_stmt.where(LabelProject.id == label_project_id)
    projects = session.scalars(project_stmt).all()
    if not projects:
        return []

    dataset_ids = {item.dataset_id for item in projects if item.dataset_id}
    project_ids = {item.id for item in projects}
    datasets = {
        item.id: item
        for item in session.scalars(select(Dataset).where(Dataset.id.in_(dataset_ids))).all()
    }
    snapshots = session.scalars(
        select(DatasetSnapshot)
        .where(
            DatasetSnapshot.label_project_id.in_(project_ids),
            DatasetSnapshot.deleted_at.is_(None),
        )
        .order_by(DatasetSnapshot.created_at.desc())
    ).all()
    snapshots_by_project: dict[str, list[DatasetSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        if snapshot.label_project_id:
            snapshots_by_project[snapshot.label_project_id].append(snapshot)

    families = session.scalars(
        select(ModelFamily)
        .where(ModelFamily.label_project_id.in_(project_ids), ModelFamily.deleted_at.is_(None))
        .order_by(ModelFamily.created_at.desc())
    ).all()
    family_ids = [item.id for item in families]
    family_by_id = {item.id: item for item in families}
    families_by_project: dict[str, list[ModelFamily]] = defaultdict(list)
    for family in families:
        if family.label_project_id:
            families_by_project[family.label_project_id].append(family)

    versions = session.scalars(
        select(ModelVersion)
        .where(ModelVersion.model_family_id.in_(family_ids), ModelVersion.deleted_at.is_(None))
        .order_by(ModelVersion.created_at.desc())
    ).all() if family_ids else []
    versions_by_family: dict[str, list[ModelVersion]] = defaultdict(list)
    for version in versions:
        versions_by_family[version.model_family_id].append(version)
    version_by_id = {item.id: item for item in versions}

    snapshot_ids = [item.id for item in snapshots]
    training_jobs = session.scalars(
        select(TrainingJob)
        .where(
            TrainingJob.dataset_snapshot_id.in_(snapshot_ids) if snapshot_ids else False,
            TrainingJob.deleted_at.is_(None),
        )
        .order_by(TrainingJob.created_at.desc())
    ).all() if snapshot_ids else []
    jobs_by_snapshot: dict[str, list[TrainingJob]] = defaultdict(list)
    jobs_by_family: dict[str, list[TrainingJob]] = defaultdict(list)
    for job in training_jobs:
        jobs_by_snapshot[job.dataset_snapshot_id].append(job)
        if job.model_family_id:
            jobs_by_family[job.model_family_id].append(job)

    releases = session.scalars(
        select(DeploymentRelease)
        .where(DeploymentRelease.deleted_at.is_(None))
        .order_by(DeploymentRelease.created_at.desc())
    ).all()
    release_by_id = {item.id: item for item in releases}
    audit_ids = {
        *project_ids,
        *(item.id for item in snapshots),
        *(item.id for item in training_jobs),
        *(item.id for item in versions),
    }
    events = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.aggregate_id.in_(audit_ids) if audit_ids else False)
        .order_by(AuditEvent.occurred_at.desc())
    ).all() if audit_ids else []
    events_by_project = _events_by_project(
        projects=projects,
        project_ids=project_ids,
        snapshots=snapshots,
        jobs=training_jobs,
        versions=versions,
        events=events,
    )

    items: list[ProjectPipelineDetail] = []
    for project in projects:
        project_snapshots = snapshots_by_project.get(project.id, [])
        latest_snapshot = project_snapshots[0] if project_snapshots else None
        latest_valid_snapshot = next(
            (
                item
                for item in project_snapshots
                if item.status == SnapshotStatus.READY.value
                and item.validation_status == ValidationStatus.PASSED.value
            ),
            None,
        )
        project_families = families_by_project.get(project.id, [])
        project_models = [
            _project_model_summary(
                family,
                versions_by_family=versions_by_family,
                jobs_by_family=jobs_by_family,
            )
            for family in project_families
        ]
        latest_approved_version = _latest_project_approved_version(
            families=project_families,
            versions_by_family=versions_by_family,
        )
        history = _project_training_history(
            project=project,
            snapshots=project_snapshots,
            jobs_by_snapshot=jobs_by_snapshot,
            jobs_by_family=jobs_by_family,
            families=project_families,
        )
        last_training = history[0] if history else None
        endpoints = [
            _project_endpoint_summary(
                project=project,
                family=family,
                versions_by_family=versions_by_family,
                version_by_id=version_by_id,
                release_by_id=release_by_id,
            )
            for family in project_families
        ]
        readiness = _readiness_summary(
            project=project,
            latest_snapshot=latest_snapshot,
            latest_valid_snapshot=latest_valid_snapshot,
        )
        next_action, next_detail = _next_action(
            project=project,
            latest_valid_snapshot=latest_valid_snapshot,
            models=project_models,
            last_training=last_training,
            endpoints=endpoints,
            latest_approved_version=latest_approved_version,
        )

        items.append(
            ProjectPipelineDetail(
                label_project_id=project.id,
                label_studio_project_id=project.label_studio_project_id,
                title=project.title,
                task_type=TaskType(project.task_type),
                status=LabelProjectStatus(project.status),
                dataset_id=project.dataset_id,
                dataset_name=datasets[project.dataset_id].name if project.dataset_id and project.dataset_id in datasets else None,
                training_pipeline_slug=project.training_pipeline_slug,
                automation_policy=_automation_policy(project),
                readiness=readiness,
                available_classes=_project_available_classes(project),
                latest_snapshot=_snapshot_summary(latest_snapshot),
                latest_valid_snapshot=_snapshot_summary(latest_valid_snapshot),
                models=project_models,
                last_training=_training_summary(last_training),
                current_endpoints=endpoints,
                prediction_backend=_prediction_backend_summary(
                    project,
                    family_by_id=family_by_id,
                    version_by_id=version_by_id,
                ),
                next_recommended_action=next_action,
                next_action_detail=next_detail,
                model_templates=list_model_templates(session, task_type=TaskType(project.task_type)),
                supported_model_architectures=supported_model_architectures(
                    task_type=TaskType(project.task_type)
                ),
                training_history=[item for item in (_training_summary(job) for job in history) if item],
                activity_timeline=events_by_project.get(project.id, [])[:14],
            )
        )
    return items


def _project_training_history(
    *,
    project: LabelProject,
    snapshots: list[DatasetSnapshot],
    jobs_by_snapshot: dict[str, list[TrainingJob]],
    jobs_by_family: dict[str, list[TrainingJob]],
    families: list[ModelFamily],
) -> list[TrainingJob]:
    items: dict[str, TrainingJob] = {}
    for snapshot in snapshots:
        for job in jobs_by_snapshot.get(snapshot.id, []):
            items[job.id] = job
    for family in families:
        for job in jobs_by_family.get(family.id, []):
            items[job.id] = job
    return sorted(items.values(), key=lambda item: item.created_at or "", reverse=True)


def _project_model_summary(
    family: ModelFamily,
    *,
    versions_by_family: dict[str, list[ModelVersion]],
    jobs_by_family: dict[str, list[TrainingJob]],
) -> ProjectModelSummary:
    latest_version = versions_by_family.get(family.id, [None])[0]
    latest_job = jobs_by_family.get(family.id, [None])[0]
    architecture = _row_architecture(
        task_type=TaskType(family.task_type),
        family=family.architecture_family,
        version=family.architecture_version,
        size=family.architecture_size,
        display_name=family.architecture_display_name,
    )
    return ProjectModelSummary(
        model_family_id=family.id,
        label_project_id=family.label_project_id,
        display_name=family.display_name or _display_name_from_slug(family.slug),
        slug=family.slug,
        endpoint_name=family.slug,
        description=family.description,
        task_type=TaskType(family.task_type),
        architecture=architecture,
        class_names=list(getattr(family, "class_names", None) or []),
        uses_all_classes=not bool(getattr(family, "class_names", None)),
        segmentation_mode=getattr(family, "segmentation_mode", None),
        template_key=family.template_key,
        template_version=family.template_version,
        training_profile_key=family.training_profile_key,
        training_overrides=(
            training_defaults_from_persisted(family.training_overrides)
            if family.training_overrides
            else None
        ),
        promotion_policy=_model_family_promotion_policy(family),
        latest_model_version_id=latest_version.id if latest_version else None,
        latest_model_version_no=latest_version.version_no if latest_version else None,
        latest_model_status=(
            ModelLifecycleStatus(latest_version.status) if latest_version and latest_version.status else None
        ),
        latest_training_job_id=latest_job.id if latest_job else None,
        created_at=_iso(family.created_at) or utc_now_iso(),
    )


def _project_endpoint_summary(
    *,
    project: LabelProject,
    family: ModelFamily,
    versions_by_family: dict[str, list[ModelVersion]],
    version_by_id: dict[str, ModelVersion],
    release_by_id: dict[str, DeploymentRelease],
) -> ProjectEndpointSummary:
    active_rt = _latest_activated_family_release(
        releases=release_by_id.values(),
        family_id=family.id,
        target_path=TargetPath.REALTIME,
    )
    active_batch = _latest_activated_family_release(
        releases=release_by_id.values(),
        family_id=family.id,
        target_path=TargetPath.BATCH,
    )
    latest_version = versions_by_family.get(family.id, [None])[0]
    active_version = None
    if active_rt is not None:
        active_version = version_by_id.get(active_rt.model_version_id)
    elif active_batch is not None:
        active_version = version_by_id.get(active_batch.model_version_id)
    last_deployed_at = max(
        (item.activated_at for item in (active_rt, active_batch) if item and item.activated_at),
        default=None,
    )
    preferred_mode = EndpointMode(project.preferred_endpoint_mode or EndpointMode.REALTIME.value)
    architecture = _row_architecture(
        task_type=TaskType(family.task_type),
        family=family.architecture_family,
        version=family.architecture_version,
        size=family.architecture_size,
        display_name=family.architecture_display_name,
    )
    return ProjectEndpointSummary(
        endpoint_name=family.slug,
        label_project_id=project.id,
        model_family_id=family.id,
        model_family_slug=family.slug,
        display_name=family.display_name or _display_name_from_slug(family.slug),
        task_type=TaskType(family.task_type),
        architecture=architecture,
        class_names=list(getattr(family, "class_names", None) or []),
        uses_all_classes=not bool(getattr(family, "class_names", None)),
        segmentation_mode=getattr(family, "segmentation_mode", None),
        preferred_mode=preferred_mode,
        realtime_available=active_rt is not None,
        batch_available=active_batch is not None,
        realtime_release_id=active_rt.id if active_rt else None,
        batch_release_id=active_batch.id if active_batch else None,
        current_model_version_id=(
            active_rt.model_version_id
            if active_rt
            else active_batch.model_version_id if active_batch else latest_version.id if latest_version else None
        ),
        current_model_version_no=(
            active_version.version_no
            if active_version is not None
            else latest_version.version_no
            if latest_version is not None
            else None
        ),
        last_deployed_at=_iso(last_deployed_at),
        api_examples=_endpoint_examples(task_type=TaskType(family.task_type), endpoint_name=family.slug),
    )


def _automation_policy(project: LabelProject) -> ProjectAutomationPolicy:
    return ProjectAutomationPolicy(
        auto_sync_enabled=bool(getattr(project, "auto_sync_enabled", True)),
        auto_snapshot_enabled=bool(project.auto_snapshot_enabled),
        auto_train_enabled=bool(getattr(project, "auto_train_enabled", False)),
        auto_deploy_enabled=bool(getattr(project, "auto_deploy_enabled", True)),
        default_model_template=getattr(project, "default_model_template", None),
        default_model_family_id=getattr(project, "default_model_family_id", None),
        default_training_profile=getattr(project, "default_training_profile", None),
        preferred_endpoint_mode=EndpointMode(
            getattr(project, "preferred_endpoint_mode", EndpointMode.REALTIME.value)
        ),
    )


def _prediction_backend_summary(
    project: LabelProject,
    *,
    family_by_id: dict[str, ModelFamily] | None = None,
    version_by_id: dict[str, ModelVersion] | None = None,
) -> ProjectPredictionBackendSummary:
    family = (family_by_id or {}).get(project.prediction_model_family_id or "")
    version = (version_by_id or {}).get(project.prediction_model_version_id or "")
    return ProjectPredictionBackendSummary(
        enabled=bool(getattr(project, "prediction_backend_enabled", False)),
        backend_id=getattr(project, "prediction_backend_id", None),
        backend_url=getattr(project, "prediction_backend_url", None),
        status=getattr(project, "prediction_backend_status", None) or "not_configured",
        model_family_id=getattr(project, "prediction_model_family_id", None),
        model_family_slug=family.slug if family is not None else None,
        model_version_id=getattr(project, "prediction_model_version_id", None),
        model_version_no=version.version_no if version is not None else None,
        last_linked_at=_iso(getattr(project, "prediction_linked_at", None)),
        last_error=getattr(project, "prediction_last_error", None),
        auto_update=True,
    )


def _class_balance_strategy(project: LabelProject) -> str:
    value = str(getattr(project, "class_balance_strategy", None) or "none").lower()
    return value if value in {"none", "weighted_loss", "balanced_sampler"} else "none"


def _readiness_summary(
    *,
    project: LabelProject,
    latest_snapshot: DatasetSnapshot | None,
    latest_valid_snapshot: DatasetSnapshot | None,
) -> ProjectReadinessSummary:
    annotation_threshold = max(project.minimum_annotation_count, 1)
    eligible_count = int(getattr(project, "eligible_task_count", 0) or 0)
    annotation_progress = min((eligible_count / annotation_threshold) * 100, 100)
    task_progress = (
        min((eligible_count / project.task_count) * 100, 100)
        if project.task_count
        else 0
    )
    readiness_percent = round(max(annotation_progress, task_progress), 1)
    annotations_ready = eligible_count >= annotation_threshold
    if latest_valid_snapshot is not None:
        readiness_reason = "Latest valid dataset is ready for model training."
    elif not annotations_ready:
        readiness_reason = "Keep finalizing submitted data until the eligible dataset threshold is satisfied."
    elif latest_snapshot is not None and latest_snapshot.status == SnapshotStatus.SUPERSEDED.value:
        readiness_reason = "Create a fresh dataset snapshot for the current project model type."
    elif latest_snapshot is not None and latest_snapshot.status != SnapshotStatus.READY.value:
        readiness_reason = "Automation is materializing or validating the latest dataset snapshot."
    else:
        readiness_reason = "Waiting for the next qualifying annotation update to create a snapshot."
    return ProjectReadinessSummary(
        task_count=project.task_count,
        annotated_task_count=project.annotated_task_count,
        annotation_count=project.annotation_count,
        draft_task_count=int(getattr(project, "draft_task_count", 0) or 0),
        eligible_task_count=eligible_count,
        submitted_labeled_task_count=int(
            getattr(project, "submitted_labeled_task_count", 0) or 0
        ),
        submitted_background_task_count=int(
            getattr(project, "submitted_background_task_count", 0) or 0
        ),
        invalid_task_count=int(getattr(project, "invalid_task_count", 0) or 0),
        minimum_annotation_count=project.minimum_annotation_count,
        required_classes=list(getattr(project, "required_classes", None) or []),
        class_distribution=_normalized_class_distribution(
            getattr(project, "class_distribution", None)
        ),
        train_ratio=int(getattr(project, "train_ratio", 80) or 0),
        val_ratio=int(getattr(project, "val_ratio", 10) or 0),
        test_ratio=int(getattr(project, "test_ratio", 10) or 0),
        readiness_percent=readiness_percent,
        annotations_ready=annotations_ready,
        latest_snapshot_ready=latest_snapshot is not None and latest_snapshot.status == SnapshotStatus.READY.value,
        latest_valid_snapshot_ready=latest_valid_snapshot is not None,
        readiness_reason=readiness_reason,
    )


def _normalized_class_distribution(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, int] = {}
    for label, count in raw.items():
        try:
            numeric_count = int(count)
        except (TypeError, ValueError):
            continue
        if numeric_count > 0:
            result[str(label)] = numeric_count
    return result


def _project_available_classes(project: LabelProject) -> list[str]:
    declared = extract_declared_classes(getattr(project, "label_config", None))
    observed = list(_normalized_class_distribution(getattr(project, "class_distribution", {})))
    required = [
        str(item).strip()
        for item in (getattr(project, "required_classes", None) or [])
        if str(item).strip()
    ]
    return sorted(
        {item for item in [*declared, *observed, *required] if item},
        key=str.casefold,
    )


def _validate_project_model_classes(
    project: LabelProject,
    requested_classes: Iterable[str],
) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_name in requested_classes:
        name = str(raw_name).strip()
        if not name or name in seen:
            continue
        normalized.append(name)
        seen.add(name)

    available = _project_available_classes(project)
    if normalized and available:
        unknown = [name for name in normalized if name not in set(available)]
        if unknown:
            raise ValueError(
                "The selected model classes are not part of the Label Studio project: "
                + ", ".join(unknown)
                + "."
            )
    return normalized


def _resolve_segmentation_mode(
    *,
    task_type: TaskType,
    architecture_family: str,
    requested_mode: str | None,
) -> str | None:
    if task_type != TaskType.SEGMENTATION:
        if requested_mode is not None:
            raise ValueError("Segmentation mode is only valid for segmentation models.")
        return None

    supported_mode = "instance" if architecture_family.lower() == "yolo" else "semantic"
    if requested_mode is not None and requested_mode != supported_mode:
        architecture_label = (
            "YOLO segmentation architectures"
            if architecture_family.lower() == "yolo"
            else f"{architecture_family} segmentation architectures"
        )
        raise ValueError(
            f"{architecture_label} support {supported_mode} segmentation, "
            f"not {requested_mode} segmentation."
        )
    return supported_mode


def _next_action(
    *,
    project: LabelProject,
    latest_valid_snapshot: DatasetSnapshot | None,
    models: list[ProjectModelSummary],
    last_training,
    endpoints: list[ProjectEndpointSummary],
    latest_approved_version: ModelVersion | None,
) -> tuple[str, str]:
    if latest_valid_snapshot is None:
        eligible_count = int(getattr(project, "eligible_task_count", 0) or 0)
        if eligible_count >= max(int(getattr(project, "minimum_annotation_count", 1) or 1), 1):
            return (
                "Create dataset snapshot",
                "A fresh immutable snapshot is needed before training can start.",
            )
        return (
            "Continue annotation",
            "A valid immutable dataset snapshot has not been produced yet.",
        )
    if not models:
        return (
            "Create model",
            "Choose a preset once, then the project can train from the latest valid dataset without manual IDs.",
        )
    if last_training and last_training.status not in TERMINAL_TRAINING_STATES:
        return (
            "Training in progress",
            "The active run is still executing in the background and can be monitored from this pipeline page.",
        )
    if last_training is None:
        return (
            "Train new model",
            "A valid dataset and a model preset are ready. Start training directly from this pipeline page.",
        )
    if any(item.realtime_available or item.batch_available for item in endpoints):
        return (
            "Test inference",
            "A deployed endpoint is already live for this project and ready for downstream callers.",
        )
    if latest_approved_version is not None:
        if (
            getattr(project, "auto_deploy_enabled", True)
            and last_training is not None
            and getattr(last_training, "status", None) == TrainingJobStatus.COMPLETED.value
            and getattr(last_training, "model_version_id", None) == latest_approved_version.id
            and bool((last_training.evaluation_summary or {}).get("promotion_ready"))
        ):
            return (
                "Wait for deployment automation",
                "The latest approved model version will package and deploy automatically after policy checks pass.",
            )
        return (
            "Deploy latest approved",
            "A promotable model version is available. Packaging is automatic; one approval action will publish it.",
        )
    if bool(getattr(last_training, "evaluation_summary", {}) or {}) and not bool(
        (last_training.evaluation_summary or {}).get("promotion_ready")
    ):
        return (
            "Review quality gates",
            "The latest completed training run did not pass deployment thresholds, so the standard deploy action is blocked.",
        )
    if last_training and getattr(last_training, "status", None) == TrainingJobStatus.COMPLETED.value and not getattr(
        last_training, "promotion_ready", False
    ):
        return (
            "Review quality gates",
            "The latest completed training run did not pass deployment thresholds, so the standard deploy action is blocked.",
        )
    if getattr(project, "auto_deploy_enabled", True):
        return (
            "Wait for deployment automation",
            "The latest successful training run will package and deploy automatically after policy checks pass.",
        )
    return (
        "Deploy latest approved",
        "Packaging is automatic; use a single approval action when you are ready to publish the latest trained model.",
    )


def _latest_project_approved_version(
    *,
    families: list[ModelFamily],
    versions_by_family: dict[str, list[ModelVersion]],
) -> ModelVersion | None:
    versions: list[ModelVersion] = []
    for family in families:
        versions.extend(versions_by_family.get(family.id, []))
    versions.sort(key=lambda item: item.created_at or "", reverse=True)
    return next(
        (item for item in versions if bool((item.evaluation_summary or {}).get("promotion_ready"))),
        None,
    )


def _latest_activated_family_release(
    *,
    releases: Iterable[DeploymentRelease],
    family_id: str,
    target_path: TargetPath,
) -> DeploymentRelease | None:
    matches = [
        release
        for release in releases
        if release.model_family_id == family_id
        and release.target_path == target_path.value
        and release.status == DeploymentReleaseStatus.ACTIVATED.value
        and release.retired_at is None
        and release.deleted_at is None
    ]
    return max(matches, key=_release_recency_key, default=None)


def _release_recency_key(release: DeploymentRelease) -> tuple[str, str, str]:
    return (
        _iso(release.activated_at) or "",
        _iso(release.created_at) or "",
        release.id,
    )


def _events_by_project(
    *,
    projects: list[LabelProject],
    project_ids: set[str],
    snapshots: list[DatasetSnapshot],
    jobs: list[TrainingJob],
    versions: list[ModelVersion],
    events: list[AuditEvent],
) -> dict[str, list[PipelineActivityItem]]:
    snapshot_to_project = {item.id: item.label_project_id for item in snapshots if item.label_project_id}
    job_to_project: dict[str, str] = {}
    snapshot_lookup = {item.id: item for item in snapshots}
    for job in jobs:
        snapshot = snapshot_lookup.get(job.dataset_snapshot_id)
        if snapshot and snapshot.label_project_id:
            job_to_project[job.id] = snapshot.label_project_id
    version_to_project: dict[str, str] = {}
    job_lookup = {item.id: item for item in jobs}
    for version in versions:
        job = job_lookup.get(version.training_job_id or "")
        if job and job.id in job_to_project:
            version_to_project[version.id] = job_to_project[job.id]

    grouped: dict[str, list[PipelineActivityItem]] = defaultdict(list)
    for event in events:
        project_id = None
        if event.aggregate_type == "label_project" and event.aggregate_id in project_ids:
            project_id = event.aggregate_id
        elif event.aggregate_type == "dataset_snapshot":
            project_id = snapshot_to_project.get(event.aggregate_id)
        elif event.aggregate_type == "training_job":
            project_id = job_to_project.get(event.aggregate_id)
        elif event.aggregate_type == "model_version":
            project_id = version_to_project.get(event.aggregate_id)
        if project_id is None:
            continue
        grouped[project_id].append(
            PipelineActivityItem(
                occurred_at=_iso(event.occurred_at) or utc_now_iso(),
                title=_activity_title(event.event_type),
                detail=_activity_detail(event.event_type, event.payload or {}),
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                event_type=event.event_type,
            )
        )
    return grouped


def _activity_title(event_type: str) -> str:
    mapping = {
        "label_project.annotation_state_changed.v1": "Annotation activity synced",
        "labelstudio.projects.synced.v1": "Project catalog synchronized",
        "dataset.snapshot.requested.v1": "Dataset snapshot requested",
        "dataset.snapshot.created.v1": "Dataset snapshot ready",
        "dataset.snapshot.validation_failed.v1": "Dataset snapshot failed validation",
        "training.requested.v1": "Training requested",
        "training.job.state_changed.v1": "Training state changed",
        "model.approved.v1": "Deployment approval recorded",
        "deployment.promotion.requested.v1": "Deployment queued",
    }
    return mapping.get(event_type, event_type.replace(".", " ").replace("_", " "))


def _activity_detail(event_type: str, payload: dict[str, Any]) -> str:
    if event_type == "dataset.snapshot.created.v1":
        return f"Immutable dataset snapshot is ready with checksum {payload.get('content_checksum') or 'n/a'}."
    if event_type == "training.job.state_changed.v1":
        return (
            f"{payload.get('previous_state') or 'created'} -> {payload.get('current_state') or 'unknown'}"
        )
    if event_type == "deployment.promotion.requested.v1":
        return (
            f"{payload.get('requested_state') or 'unknown'} deployment requested for {payload.get('target_path') or 'unknown'}."
        )
    if event_type == "label_project.annotation_state_changed.v1":
        return (
            f"{payload.get('annotation_count') or 0} annotations across {payload.get('annotated_task_count') or 0} labeled tasks."
        )
    reason = payload.get("reason")
    if isinstance(reason, str) and reason:
        return reason
    return "Project pipeline activity recorded."


def _select_project_model_family(
    session: Session,
    *,
    project: LabelProject,
    payload: ProjectTrainRequest,
) -> ModelFamily:
    if payload.model_family_id:
        family = session.get(ModelFamily, payload.model_family_id)
        if (
            family is None
            or family.label_project_id != project.id
            or family.deleted_at is not None
        ):
            raise ValueError("The selected model does not belong to this project pipeline.")
        return family

    family = session.scalar(
        select(ModelFamily)
        .where(ModelFamily.label_project_id == project.id, ModelFamily.deleted_at.is_(None))
        .order_by(ModelFamily.created_at.desc())
    )
    if family is None:
        raise ValueError("Create a model first before starting training from this project pipeline.")
    return family


def _select_training_snapshot(
    session: Session,
    *,
    project: LabelProject,
    payload: ProjectTrainRequest,
) -> DatasetSnapshot:
    if payload.dataset_snapshot_id:
        snapshot = session.get(DatasetSnapshot, payload.dataset_snapshot_id)
        if (
            snapshot is None
            or snapshot.label_project_id != project.id
            or snapshot.deleted_at is not None
        ):
            raise ValueError("The selected dataset snapshot does not belong to this project pipeline.")
        if snapshot.status != SnapshotStatus.READY.value or snapshot.validation_status != ValidationStatus.PASSED.value:
            raise ValueError("Only the latest valid ready snapshot can be used for the happy-path training flow.")
        return snapshot

    snapshot = session.scalar(
        select(DatasetSnapshot)
        .where(
            DatasetSnapshot.label_project_id == project.id,
            DatasetSnapshot.status == SnapshotStatus.READY.value,
            DatasetSnapshot.validation_status == ValidationStatus.PASSED.value,
            DatasetSnapshot.deleted_at.is_(None),
        )
        .order_by(DatasetSnapshot.created_at.desc())
    )
    if snapshot is None:
        raise ValueError("This project does not have a valid immutable dataset snapshot yet.")
    return snapshot


def _snapshot_summary(snapshot: DatasetSnapshot | None):
    if snapshot is None:
        return None
    from cv_db.repositories import _snapshot_summary as base_snapshot_summary

    return base_snapshot_summary(snapshot)


def _training_summary(job: TrainingJob | None):
    if job is None:
        return None
    from cv_db.training_store import _job_summary as job_summary

    return job_summary(job)


def _endpoint_examples(*, task_type: TaskType, endpoint_name: str) -> list[EndpointRouteExample]:
    if task_type == TaskType.CLASSIFICATION:
        realtime_path = "/infer/classify"
    elif task_type in {TaskType.DETECTION, TaskType.ORIENTED_DETECTION}:
        realtime_path = "/infer/detect"
    elif task_type == TaskType.ANOMALY:
        realtime_path = "/infer/anomaly"
    else:
        realtime_path = "/infer/segment"
    return [
        EndpointRouteExample(
            method="GET",
            path=f"/models/{endpoint_name}",
            description="Discover the public contract and current availability for this logical endpoint.",
        ),
        EndpointRouteExample(
            method="POST",
            path=realtime_path,
            description=f"Realtime inference using multipart form-data with model_id={endpoint_name}.",
        ),
        EndpointRouteExample(
            method="POST",
            path="/batch/jobs",
            description=f"Batch inference submission using JSON with model_id={endpoint_name}.",
        ),
    ]


def _model_template_record(session: Session, template_key: str) -> ModelTemplate:
    ensure_model_templates_seeded(session)
    row = session.scalar(select(ModelTemplate).where(ModelTemplate.template_key == template_key))
    if row is None:
        raise ValueError(f"Unknown model template {template_key}.")
    if not is_supported_architecture(
        task_type=TaskType(row.task_type),
        family=row.architecture_family,
        version=row.architecture_version,
        size=row.architecture_size,
    ):
        raise ValueError(
            f"Model template {template_key} is not supported by the current real training backend."
        )
    return row


def _coerce_compatible_template_key(
    session: Session,
    task_type: TaskType,
    template_key: str,
) -> str:
    try:
        template = _model_template_record(session, template_key)
    except ValueError:
        return _default_template_key(task_type)
    if TaskType(template.task_type) != task_type:
        return _default_template_key(task_type)
    return template_key


def _resolve_project_task_type_for_model_family(
    session: Session,
    *,
    project: LabelProject,
    project_task_type: TaskType,
    model_family: ModelFamily,
) -> TaskType:
    family_task_type = TaskType(model_family.task_type)
    if family_task_type == project_task_type:
        return project_task_type

    template_key = _coerce_compatible_template_key(
        session,
        family_task_type,
        model_family.template_key or _default_template_key(family_task_type),
    )
    template = _model_template_record(session, template_key)
    _retarget_project_task_type_for_model_creation(
        session,
        project=project,
        current_task_type=project_task_type,
        target_task_type=family_task_type,
        template=template,
    )
    return family_task_type


def _default_template_key(task_type: TaskType) -> str:
    if task_type == TaskType.CLASSIFICATION:
        return "classification-resnet-18"
    if task_type == TaskType.DETECTION:
        return "detection-yolov8-n"
    if task_type == TaskType.ORIENTED_DETECTION:
        return "oriented_detection-yolov26-n"
    if task_type == TaskType.SEGMENTATION:
        return "segmentation-deeplab-v3-resnet50"
    if task_type == TaskType.ANOMALY:
        return "anomaly-patchcore-wide-resnet50-2"
    raise ValueError(f"No production training template is available for {task_type.value}.")


def _default_display_name(title: str, *, template_key: str) -> str:
    template = default_template_summary(template_key)
    return f"{title.strip()} {template.display_name_suffix}".strip()


def _row_architecture(
    *,
    task_type: TaskType,
    family: str,
    version: str,
    size: str,
    display_name: str,
) -> ModelArchitectureSummary:
    return ModelArchitectureSummary(
        task_type=task_type,
        family=family,
        version=version,
        size=size,
        display_name=display_name,
        internal_key=f"{task_type.value}:{family}:{version}:{size}",
    )


def _model_template_summary(row: ModelTemplate) -> ModelTemplateSummary:
    promotion_policy = _template_promotion_policy(row)
    stored_defaults = dict(row.training_defaults or {})
    stored_defaults["training"] = training_defaults_from_persisted(
        stored_defaults.get("training")
        if isinstance(stored_defaults.get("training"), dict)
        else {}
    ).model_dump(mode="json")
    return ModelTemplateSummary(
        template_id=row.id,
        template_key=row.template_key,
        label=row.label,
        description=row.description,
        task_type=TaskType(row.task_type),
        architecture=_row_architecture(
            task_type=TaskType(row.task_type),
            family=row.architecture_family,
            version=row.architecture_version,
            size=row.architecture_size,
            display_name=row.architecture_display_name,
        ),
        display_name_suffix=row.display_name_suffix,
        default_training_profile=row.default_training_profile,
        defaults={
            **stored_defaults,
            "promotion": promotion_policy.model_dump(mode="json"),
        },
        default_promotion_policy=promotion_policy,
        template_version=row.template_version,
        recommended=row.recommended,
        active=row.active,
    )


def _template_promotion_policy(row: ModelTemplate) -> PromotionCriteriaPolicy:
    if row.default_promotion_policy:
        return PromotionCriteriaPolicy.model_validate(row.default_promotion_policy)
    return default_promotion_policy(TaskType(row.task_type))


def _model_family_promotion_policy(
    row: ModelFamily,
    *,
    task_type: TaskType | None = None,
) -> PromotionCriteriaPolicy:
    if row.promotion_policy:
        return PromotionCriteriaPolicy.model_validate(row.promotion_policy)
    return default_promotion_policy(task_type or TaskType(row.task_type))


def _validate_supported_architecture(
    *,
    task_type: TaskType,
    family: str,
    version: str,
    size: str,
) -> None:
    if not is_supported_architecture(
        task_type=task_type,
        family=family,
        version=version,
        size=size,
    ):
        raise ValueError(
            f"The current stack does not support {family} {version} {size} for {task_type.value}."
        )


def _quality_defaults_for_task(task_type: TaskType) -> tuple[str, str, list[dict[str, Any]]]:
    if task_type == TaskType.ANOMALY:
        return (
            "image_auroc",
            "max",
            [
                {
                    "metric_name": "image_auroc",
                    "comparison": "gte",
                    "threshold": 0.9,
                    "phase": "evaluation",
                },
                {
                    "metric_name": "image_f1",
                    "comparison": "gte",
                    "threshold": 0.8,
                    "phase": "evaluation",
                },
            ],
        )
    if task_type in {TaskType.DETECTION, TaskType.ORIENTED_DETECTION}:
        return (
            "map_50_95",
            "max",
            [
                {
                    "metric_name": "map_50_95",
                    "comparison": "gte",
                    "threshold": 0.55,
                    "phase": "evaluation",
                },
                {
                    "metric_name": "map_50",
                    "comparison": "gte",
                    "threshold": 0.7,
                    "phase": "evaluation",
                },
            ],
        )
    if task_type == TaskType.SEGMENTATION:
        return (
            "mask_map_50_95",
            "max",
            [
                {
                    "metric_name": "mask_map_50_95",
                    "comparison": "gte",
                    "threshold": 0.45,
                    "phase": "evaluation",
                },
                {
                    "metric_name": "mask_map_50",
                    "comparison": "gte",
                    "threshold": 0.6,
                    "phase": "evaluation",
                },
            ],
        )
    return (
        "f1_macro",
        "max",
        [
            {
                "metric_name": "accuracy_top1",
                "comparison": "gte",
                "threshold": 0.9,
                "phase": "evaluation",
            },
            {
                "metric_name": "f1_macro",
                "comparison": "gte",
                "threshold": 0.85,
                "phase": "evaluation",
            },
        ],
    )


def _unique_family_slug(session: Session, base_slug: str) -> str:
    existing = set(session.scalars(select(ModelFamily.slug)).all())
    candidate = base_slug
    suffix = 2
    while candidate in existing:
        candidate = f"{base_slug}-{suffix}"
        suffix += 1
    return candidate


def _slugify(value: str) -> str:
    candidate = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return candidate or "model"


def _display_name_from_slug(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-"))


def _target_paths(mode: EndpointMode) -> list[TargetPath]:
    if mode == EndpointMode.BOTH:
        return [TargetPath.REALTIME, TargetPath.BATCH]
    return [TargetPath(mode.value)]


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat().replace("+00:00", "Z")
    return str(value)
