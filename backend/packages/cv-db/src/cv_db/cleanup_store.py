from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from cv_contracts import (
    CleanupArtifactImpact,
    CleanupEntityImpact,
    CleanupExecutionRequest,
    CleanupExecutionResult,
    CleanupImpactPreview,
)
from cv_core import PlatformSettings
from cv_datasets import LabelStudioClient, LabelStudioError
from cv_status import SnapshotStatus, TrainingJobStatus, ValidationStatus
from cv_utils import utc_now
from sqlalchemy import select
from sqlalchemy.orm import Session

from cv_db.models import (
    BatchJob,
    BatchJobChunk,
    Dataset,
    DatasetAsset,
    DatasetSnapshot,
    DeploymentApprovalRecord,
    DeploymentRelease,
    DeploymentTarget,
    LabelProject,
    LabelProjectWebhook,
    ModelFamily,
    ModelPromotion,
    ModelVersion,
    TrainingCheckpoint,
    TrainingJob,
    TrainingMetric,
    UploadSession,
)

_TERMINAL_TRAINING_STATUSES = {
    TrainingJobStatus.COMPLETED.value,
    TrainingJobStatus.FAILED.value,
    TrainingJobStatus.CANCELLED.value,
    TrainingJobStatus.ROLLED_BACK_IF_NEEDED.value,
}
_CreatedRow = DatasetSnapshot | DeploymentRelease | ModelFamily | ModelVersion | TrainingJob
_DeletableRow = (
    LabelProject | DatasetSnapshot | DeploymentRelease | ModelFamily | ModelVersion | TrainingJob
)


@dataclass
class _CleanupPlan:
    scope_type: str
    scope_id: str
    display_name: str
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    will_remove: list[CleanupEntityImpact] = field(default_factory=list)
    will_remain: list[str] = field(default_factory=list)
    active_deployment_targets: list[str] = field(default_factory=list)
    artifact_paths: list[CleanupArtifactImpact] = field(default_factory=list)
    already_deleted: bool = False
    hard_delete: bool = False
    force_delete_artifacts: bool = False
    label_studio_project_ids: list[int] = field(default_factory=list)
    datasets: list[Dataset] = field(default_factory=list)
    label_projects: list[LabelProject] = field(default_factory=list)
    snapshots: list[DatasetSnapshot] = field(default_factory=list)
    training_jobs: list[TrainingJob] = field(default_factory=list)
    model_families: list[ModelFamily] = field(default_factory=list)
    model_versions: list[ModelVersion] = field(default_factory=list)
    deployment_releases: list[DeploymentRelease] = field(default_factory=list)
    clear_previous_targets: list[DeploymentTarget] = field(default_factory=list)
    clear_release_targets: list[DeploymentTarget] = field(default_factory=list)
    hard_delete_rows: list[Any] = field(default_factory=list)


def preview_project_pipeline_cleanup(
    session: Session,
    *,
    settings: PlatformSettings,
    label_project_id: str,
    delete_entire_pipeline: bool = False,
) -> CleanupImpactPreview:
    return _preview(
        _plan_project_pipeline_cleanup(
            session,
            settings=settings,
            label_project_id=label_project_id,
            delete_entire_pipeline=delete_entire_pipeline,
        )
    )


def execute_project_pipeline_cleanup(
    session: Session,
    *,
    settings: PlatformSettings,
    label_project_id: str,
    payload: CleanupExecutionRequest,
    deleted_by: str | None,
) -> CleanupExecutionResult:
    return _execute(
        session,
        _plan_project_pipeline_cleanup(
            session,
            settings=settings,
            label_project_id=label_project_id,
            delete_entire_pipeline=payload.delete_entire_pipeline,
        ),
        settings=settings,
        payload=payload,
        deleted_by=deleted_by,
    )


def preview_dataset_snapshot_cleanup(
    session: Session,
    *,
    settings: PlatformSettings,
    snapshot_id: str,
) -> CleanupImpactPreview:
    return _preview(
        _plan_dataset_snapshot_cleanup(session, settings=settings, snapshot_id=snapshot_id)
    )


def execute_dataset_snapshot_cleanup(
    session: Session,
    *,
    settings: PlatformSettings,
    snapshot_id: str,
    payload: CleanupExecutionRequest,
    deleted_by: str | None,
) -> CleanupExecutionResult:
    return _execute(
        session,
        _plan_dataset_snapshot_cleanup(session, settings=settings, snapshot_id=snapshot_id),
        settings=settings,
        payload=payload,
        deleted_by=deleted_by,
    )


def preview_training_job_cleanup(
    session: Session,
    *,
    settings: PlatformSettings,
    training_job_id: str,
) -> CleanupImpactPreview:
    return _preview(
        _plan_training_job_cleanup(
            session,
            settings=settings,
            training_job_id=training_job_id,
        )
    )


def execute_training_job_cleanup(
    session: Session,
    *,
    settings: PlatformSettings,
    training_job_id: str,
    payload: CleanupExecutionRequest,
    deleted_by: str | None,
) -> CleanupExecutionResult:
    return _execute(
        session,
        _plan_training_job_cleanup(session, settings=settings, training_job_id=training_job_id),
        settings=settings,
        payload=payload,
        deleted_by=deleted_by,
    )


def preview_model_version_cleanup(
    session: Session,
    *,
    settings: PlatformSettings,
    model_version_id: str,
) -> CleanupImpactPreview:
    return _preview(
        _plan_model_version_cleanup(
            session,
            settings=settings,
            model_version_id=model_version_id,
        )
    )


def execute_model_version_cleanup(
    session: Session,
    *,
    settings: PlatformSettings,
    model_version_id: str,
    payload: CleanupExecutionRequest,
    deleted_by: str | None,
) -> CleanupExecutionResult:
    return _execute(
        session,
        _plan_model_version_cleanup(session, settings=settings, model_version_id=model_version_id),
        settings=settings,
        payload=payload,
        deleted_by=deleted_by,
    )


def preview_deployment_release_cleanup(
    session: Session,
    *,
    settings: PlatformSettings,
    deployment_release_id: str,
) -> CleanupImpactPreview:
    return _preview(
        _plan_deployment_release_cleanup(
            session,
            settings=settings,
            deployment_release_id=deployment_release_id,
        )
    )


def execute_deployment_release_cleanup(
    session: Session,
    *,
    settings: PlatformSettings,
    deployment_release_id: str,
    payload: CleanupExecutionRequest,
    deleted_by: str | None,
) -> CleanupExecutionResult:
    return _execute(
        session,
        _plan_deployment_release_cleanup(
            session,
            settings=settings,
            deployment_release_id=deployment_release_id,
        ),
        settings=settings,
        payload=payload,
        deleted_by=deleted_by,
    )


def _plan_project_pipeline_cleanup(
    session: Session,
    *,
    settings: PlatformSettings,
    label_project_id: str,
    delete_entire_pipeline: bool = False,
) -> _CleanupPlan:
    project = session.get(LabelProject, label_project_id)
    if project is None:
        raise ValueError(f"Label project {label_project_id} does not exist.")
    if project.deleted_at is not None:
        return _already_deleted_plan(
            scope_type="project_pipeline",
            scope_id=label_project_id,
            display_name=project.title,
            will_remain=[
                "Audit events remain available and the project pipeline is already marked deleted.",
                "Shared dataset upload assets remain under the dataset.",
            ],
        )

    if delete_entire_pipeline:
        return _plan_entire_project_pipeline_delete(
            session,
            settings=settings,
            project=project,
        )

    snapshots = session.scalars(
        select(DatasetSnapshot)
        .where(
            DatasetSnapshot.label_project_id == label_project_id,
            DatasetSnapshot.deleted_at.is_(None),
        )
        .order_by(DatasetSnapshot.snapshot_no.desc(), DatasetSnapshot.created_at.desc())
    ).all()
    families = session.scalars(
        select(ModelFamily)
        .where(
            ModelFamily.label_project_id == label_project_id,
            ModelFamily.deleted_at.is_(None),
        )
        .order_by(ModelFamily.created_at.desc())
    ).all()
    family_ids = [row.id for row in families]
    snapshot_ids = [row.id for row in snapshots]
    versions = session.scalars(
        select(ModelVersion)
        .where(
            ModelVersion.model_family_id.in_(family_ids) if family_ids else False,
            ModelVersion.deleted_at.is_(None),
        )
        .order_by(ModelVersion.created_at.desc(), ModelVersion.version_no.desc())
    ).all() if family_ids else []
    jobs: list[TrainingJob] = []
    if family_ids or snapshot_ids:
        job_stmt = (
            select(TrainingJob)
            .where(TrainingJob.deleted_at.is_(None))
            .order_by(TrainingJob.created_at.desc())
        )
        if family_ids and snapshot_ids:
            job_stmt = job_stmt.where(
                TrainingJob.model_family_id.in_(family_ids)
                | TrainingJob.dataset_snapshot_id.in_(snapshot_ids)
            )
        elif family_ids:
            job_stmt = job_stmt.where(TrainingJob.model_family_id.in_(family_ids))
        else:
            job_stmt = job_stmt.where(TrainingJob.dataset_snapshot_id.in_(snapshot_ids))
        jobs = session.scalars(job_stmt).all()
    version_ids = [row.id for row in versions]
    releases = session.scalars(
        select(DeploymentRelease)
        .where(
            DeploymentRelease.model_version_id.in_(version_ids) if version_ids else False,
            DeploymentRelease.deleted_at.is_(None),
        )
        .order_by(DeploymentRelease.created_at.desc())
    ).all() if version_ids else []

    retained_snapshot_ids, retained_job_ids, retained_version_ids, retained_release_ids = (
        _project_pipeline_retention_ids(
            session,
            snapshots=snapshots,
            families=families,
            jobs=jobs,
            versions=versions,
            releases=releases,
        )
    )
    snapshots_to_remove = [row for row in snapshots if row.id not in retained_snapshot_ids]
    jobs_to_remove = [row for row in jobs if row.id not in retained_job_ids]
    versions_to_remove = [row for row in versions if row.id not in retained_version_ids]
    releases_to_remove = [row for row in releases if row.id not in retained_release_ids]

    active_targets, previous_targets = _release_target_refs(session, releases=releases_to_remove)
    warnings = []
    if previous_targets:
        warnings.append(
            "Rollback references to deleted releases will be cleared from deployment targets."
        )
    retained_running_jobs = [
        row
        for row in jobs
        if row.id in retained_job_ids and row.status not in _TERMINAL_TRAINING_STATUSES
    ]
    if retained_running_jobs:
        warnings.append(
            "Running or queued training runs are retained and are not part of this cleanup."
        )
    return _CleanupPlan(
        scope_type="project_pipeline",
        scope_id=label_project_id,
        display_name=project.title,
        blocked_reasons=_blocked_reasons(active_targets),
        warnings=warnings + [
            (
                "The Label Studio project, automation policy, model definitions, "
                "and latest usable state remain active."
            ),
            "Generated artifacts are only removed when artifact deletion is explicitly requested.",
            (
                "Audit events remain available and cleaned records are marked deleted "
                "for traceability."
            ),
        ],
        will_remove=[
            _impact("dataset_snapshot", "Older dataset snapshots", snapshots_to_remove),
            _impact("training_job", "Older training runs", jobs_to_remove),
            _impact("model_version", "Older model versions", versions_to_remove),
            _impact("deployment_release", "Older deployment metadata", releases_to_remove),
        ],
        will_remain=[
            "Project mapping, Label Studio linkage, and automation policy remain active.",
            f"{len(families)} model family record(s) remain available for future training.",
            f"{len(retained_snapshot_ids)} latest or referenced dataset snapshot(s) remain.",
            f"{len(retained_job_ids)} latest or active training run(s) remain.",
            f"{len(retained_version_ids)} latest, approved, or deployed model version(s) remain.",
            "Shared dataset upload assets remain under the dataset.",
            "Audit events remain available and cleaned records are marked deleted.",
        ],
        active_deployment_targets=sorted(active_targets),
        artifact_paths=_dedupe_artifact_impacts(
            [
                CleanupArtifactImpact(
                    path=str(
                        settings.storage.labelstudio_project_export_root(project.id, snapshot.id)
                    ),
                    category="labelstudio-export",
                )
                for snapshot in snapshots_to_remove
            ]
            + [
                CleanupArtifactImpact(
                    path=str(settings.storage.snapshot_root(snapshot.id)),
                    category="dataset-snapshot",
                )
                for snapshot in snapshots_to_remove
            ]
            + [
                CleanupArtifactImpact(
                    path=str(Path(settings.storage.training_root) / f"trn_{job.id}"),
                    category="training-run",
                )
                for job in jobs_to_remove
            ]
            + [
                CleanupArtifactImpact(
                    path=str(settings.storage.serving_package_root(version.id)),
                    category="serving-package",
                )
                for version in versions_to_remove
            ]
            + [
                CleanupArtifactImpact(
                    path=str(Path(settings.storage.serving_root) / "history" / f"rel_{release.id}"),
                    category="deployment-history",
                )
                for release in releases_to_remove
            ]
        ),
        snapshots=snapshots_to_remove,
        training_jobs=jobs_to_remove,
        model_versions=versions_to_remove,
        deployment_releases=releases_to_remove,
        clear_previous_targets=previous_targets,
    )


def _plan_entire_project_pipeline_delete(
    session: Session,
    *,
    settings: PlatformSettings,
    project: LabelProject,
) -> _CleanupPlan:
    snapshots = session.scalars(
        select(DatasetSnapshot)
        .where(DatasetSnapshot.label_project_id == project.id)
        .order_by(DatasetSnapshot.snapshot_no.desc(), DatasetSnapshot.created_at.desc())
    ).all()
    families = session.scalars(
        select(ModelFamily)
        .where(ModelFamily.label_project_id == project.id)
        .order_by(ModelFamily.created_at.desc())
    ).all()
    family_ids = [row.id for row in families]
    snapshot_ids = [row.id for row in snapshots]
    versions = session.scalars(
        select(ModelVersion)
        .where(ModelVersion.model_family_id.in_(family_ids) if family_ids else False)
        .order_by(ModelVersion.created_at.desc(), ModelVersion.version_no.desc())
    ).all() if family_ids else []
    jobs: list[TrainingJob] = []
    if family_ids or snapshot_ids:
        job_stmt = select(TrainingJob).order_by(TrainingJob.created_at.desc())
        if family_ids and snapshot_ids:
            job_stmt = job_stmt.where(
                TrainingJob.model_family_id.in_(family_ids)
                | TrainingJob.dataset_snapshot_id.in_(snapshot_ids)
            )
        elif family_ids:
            job_stmt = job_stmt.where(TrainingJob.model_family_id.in_(family_ids))
        else:
            job_stmt = job_stmt.where(TrainingJob.dataset_snapshot_id.in_(snapshot_ids))
        jobs = session.scalars(job_stmt).all()

    job_ids = [row.id for row in jobs]
    version_ids = [row.id for row in versions]
    releases = session.scalars(
        select(DeploymentRelease)
        .where(DeploymentRelease.model_version_id.in_(version_ids) if version_ids else False)
        .order_by(DeploymentRelease.created_at.desc())
    ).all() if version_ids else []
    release_ids = [row.id for row in releases]

    checkpoints = session.scalars(
        select(TrainingCheckpoint).where(
            TrainingCheckpoint.training_job_id.in_(job_ids) if job_ids else False
        )
    ).all() if job_ids else []
    metrics = session.scalars(
        select(TrainingMetric).where(
            TrainingMetric.training_job_id.in_(job_ids) if job_ids else False
        )
    ).all() if job_ids else []
    promotions = session.scalars(
        select(ModelPromotion).where(
            ModelPromotion.model_version_id.in_(version_ids) if version_ids else False
        )
    ).all() if version_ids else []
    approvals = session.scalars(
        select(DeploymentApprovalRecord).where(
            DeploymentApprovalRecord.model_version_id.in_(version_ids) if version_ids else False
        )
    ).all() if version_ids else []
    batch_jobs = session.scalars(
        select(BatchJob).where(
            BatchJob.deployment_release_id.in_(release_ids) if release_ids else False
        )
    ).all() if release_ids else []
    batch_job_ids = [row.id for row in batch_jobs]
    batch_chunks = session.scalars(
        select(BatchJobChunk).where(
            BatchJobChunk.batch_job_id.in_(batch_job_ids) if batch_job_ids else False
        )
    ).all() if batch_job_ids else []

    dataset = session.get(Dataset, project.dataset_id) if project.dataset_id else None
    datasets = [dataset] if dataset is not None else []
    upload_sessions = session.scalars(
        select(UploadSession).where(UploadSession.dataset_id == dataset.id)
    ).all() if dataset else []
    dataset_assets = session.scalars(
        select(DatasetAsset).where(DatasetAsset.dataset_id == dataset.id)
    ).all() if dataset else []
    shared_projects = session.scalars(
        select(LabelProject).where(
            LabelProject.dataset_id == dataset.id,
            LabelProject.id != project.id,
            LabelProject.deleted_at.is_(None),
        )
    ).all() if dataset else []
    webhooks = session.scalars(
        select(LabelProjectWebhook).where(LabelProjectWebhook.label_project_id == project.id)
    ).all()
    release_targets = _release_targets_for_releases(session, releases=releases)

    blocked_reasons = []
    if shared_projects:
        names = ", ".join(sorted(row.title for row in shared_projects))
        blocked_reasons.append(
            f"The dataset is still used by other active project pipeline(s): {names}."
        )

    artifact_paths = _dedupe_artifact_impacts(
        [
            CleanupArtifactImpact(
                path=str(settings.storage.labelstudio_exports_root() / f"lpr_{project.id}"),
                category="labelstudio-export",
            ),
        ]
        + [
            CleanupArtifactImpact(
                path=str(settings.storage.snapshot_root(snapshot.id)),
                category="dataset-snapshot",
            )
            for snapshot in snapshots
        ]
        + [
            CleanupArtifactImpact(
                path=str(Path(settings.storage.training_root) / f"trn_{job.id}"),
                category="training-run",
            )
            for job in jobs
        ]
        + [
            CleanupArtifactImpact(
                path=str(settings.storage.serving_package_root(version.id)),
                category="serving-package",
            )
            for version in versions
        ]
        + [
            CleanupArtifactImpact(
                path=str(Path(settings.storage.serving_root) / "history" / f"rel_{release.id}"),
                category="deployment-history",
            )
            for release in releases
        ]
        + [
            CleanupArtifactImpact(
                path=str(
                    settings.storage.triton_model_root(
                        release.target_path,
                        release.triton_model_name,
                    )
                ),
                category="triton-model",
            )
            for release in releases
        ]
        + (
            [
                CleanupArtifactImpact(
                    path=str(settings.storage.uploads_root() / f"dts_{dataset.id}"),
                    category="dataset-upload",
                )
            ]
            if dataset
            else []
        )
    )

    return _CleanupPlan(
        scope_type="project_pipeline_full_delete",
        scope_id=project.id,
        display_name=project.title,
        blocked_reasons=blocked_reasons,
        warnings=[
            "The Label Studio project will be deleted through the Label Studio API.",
            (
                "Runtime channels and capture rules are retained and may point to deleted "
                "project data."
            ),
            "Deployment targets that reference deleted releases will be cleared.",
            (
                "All managed artifacts in the preview are deleted even if artifact deletion "
                "is unchecked."
            ),
            "Audit events remain available for traceability.",
        ],
        will_remove=[
            CleanupEntityImpact(
                scope_type="labelstudio_project",
                scope_id=str(project.label_studio_project_id),
                display_name="Label Studio project",
                count=1,
            ),
            _impact("project_pipeline", "Project pipeline record", [project]),
            _impact("dataset", "Dataset record", datasets),
            _impact("dataset_asset", "Dataset upload assets", dataset_assets),
            _impact("upload_session", "Dataset upload sessions", upload_sessions),
            _impact("dataset_snapshot", "Dataset snapshots", snapshots),
            _impact("training_job", "Training runs", jobs),
            _impact("training_checkpoint", "Training checkpoints", checkpoints),
            _impact("training_metric", "Training metrics", metrics),
            _impact("model_family", "Model records", families),
            _impact("model_version", "Model versions", versions),
            _impact("model_promotion", "Model promotions", promotions),
            _impact("deployment_approval", "Deployment approvals", approvals),
            _impact("deployment_release", "Deployment metadata", releases),
            _impact("batch_job", "Batch jobs", batch_jobs),
            _impact("batch_job_chunk", "Batch job chunks", batch_chunks),
            _impact("label_project_webhook", "Project webhooks", webhooks),
        ],
        will_remain=[
            (
                "Runtime channels, runtime pipelines, capture rules, and historical audit "
                "events remain."
            ),
            "References from retained runtime records may be unresolved after deletion.",
        ],
        active_deployment_targets=sorted(
            target.target_path
            for target in release_targets
            if target.active_release_id in release_ids
        ),
        artifact_paths=artifact_paths,
        hard_delete=True,
        force_delete_artifacts=True,
        label_studio_project_ids=[project.label_studio_project_id],
        datasets=datasets,
        label_projects=[project],
        snapshots=snapshots,
        training_jobs=jobs,
        model_families=families,
        model_versions=versions,
        deployment_releases=releases,
        clear_release_targets=release_targets,
        hard_delete_rows=[
            *batch_chunks,
            *batch_jobs,
            *approvals,
            *promotions,
            *releases,
            *versions,
            *checkpoints,
            *metrics,
            *jobs,
            *snapshots,
            *dataset_assets,
            *upload_sessions,
            *webhooks,
            *families,
            project,
            *datasets,
        ],
    )


def _project_pipeline_retention_ids(
    session: Session,
    *,
    snapshots: list[DatasetSnapshot],
    families: list[ModelFamily],
    jobs: list[TrainingJob],
    versions: list[ModelVersion],
    releases: list[DeploymentRelease],
) -> tuple[set[str], set[str], set[str], set[str]]:
    retained_snapshot_ids: set[str] = set()
    retained_job_ids: set[str] = set()
    retained_version_ids: set[str] = set()
    retained_release_ids: set[str] = set()

    snapshot_by_id = {row.id: row for row in snapshots}
    job_by_id = {row.id: row for row in jobs}
    version_by_id = {row.id: row for row in versions}

    latest_snapshot = _latest_snapshot(snapshots)
    if latest_snapshot is not None:
        retained_snapshot_ids.add(latest_snapshot.id)
    latest_valid_snapshot = _latest_snapshot(
        [
            row
            for row in snapshots
            if row.status == SnapshotStatus.READY.value
            and row.validation_status == ValidationStatus.PASSED.value
        ]
    )
    if latest_valid_snapshot is not None:
        retained_snapshot_ids.add(latest_valid_snapshot.id)

    latest_project_job = _latest_created(jobs)
    if latest_project_job is not None:
        retained_job_ids.add(latest_project_job.id)
    for job in jobs:
        if job.status not in _TERMINAL_TRAINING_STATUSES:
            retained_job_ids.add(job.id)

    for family in families:
        family_versions = [row for row in versions if row.model_family_id == family.id]
        latest_family_version = _latest_model_version(family_versions)
        if latest_family_version is not None:
            retained_version_ids.add(latest_family_version.id)

        family_jobs = [
            row
            for row in jobs
            if row.model_family_id == family.id or row.model_family_slug == family.slug
        ]
        latest_family_job = _latest_created(family_jobs)
        if latest_family_job is not None:
            retained_job_ids.add(latest_family_job.id)

    latest_approved_version = _latest_created(
        [
            row
            for row in versions
            if bool((row.evaluation_summary or {}).get("promotion_ready"))
        ]
    )
    if latest_approved_version is not None:
        retained_version_ids.add(latest_approved_version.id)

    active_release_ids = {
        target.active_release_id
        for target in session.scalars(select(DeploymentTarget)).all()
        if target.active_release_id
    }
    for release in releases:
        if release.id in active_release_ids:
            retained_release_ids.add(release.id)
            if release.model_version_id in version_by_id:
                retained_version_ids.add(release.model_version_id)

    for release in _latest_releases_by_target(releases):
        retained_release_ids.add(release.id)
        if release.model_version_id in version_by_id:
            retained_version_ids.add(release.model_version_id)

    changed = True
    while changed:
        changed = False
        for version in versions:
            if version.id not in retained_version_ids:
                continue
            if version.training_job_id and version.training_job_id in job_by_id:
                changed |= _add_once(retained_job_ids, version.training_job_id)
            if version.dataset_snapshot_id and version.dataset_snapshot_id in snapshot_by_id:
                changed |= _add_once(retained_snapshot_ids, version.dataset_snapshot_id)

        for job in jobs:
            if job.id not in retained_job_ids:
                continue
            if job.model_version_id and job.model_version_id in version_by_id:
                changed |= _add_once(retained_version_ids, job.model_version_id)
            if job.dataset_snapshot_id in snapshot_by_id:
                changed |= _add_once(retained_snapshot_ids, job.dataset_snapshot_id)

        for release in releases:
            if release.id not in retained_release_ids:
                continue
            if release.model_version_id in version_by_id:
                changed |= _add_once(retained_version_ids, release.model_version_id)

    return retained_snapshot_ids, retained_job_ids, retained_version_ids, retained_release_ids


def _plan_dataset_snapshot_cleanup(
    session: Session,
    *,
    settings: PlatformSettings,
    snapshot_id: str,
) -> _CleanupPlan:
    snapshot = session.get(DatasetSnapshot, snapshot_id)
    if snapshot is None:
        raise ValueError(f"Dataset snapshot {snapshot_id} does not exist.")
    display_name = f"Snapshot #{snapshot.snapshot_no}"
    if snapshot.deleted_at is not None:
        return _already_deleted_plan(
            scope_type="dataset_snapshot",
            scope_id=snapshot_id,
            display_name=display_name,
            will_remain=[
                "Linked training runs and model versions remain available.",
                "Audit events remain available and the snapshot is already marked deleted.",
            ],
        )
    linked_jobs = session.scalars(
        select(TrainingJob).where(
            TrainingJob.dataset_snapshot_id == snapshot_id,
            TrainingJob.deleted_at.is_(None),
        )
    ).all()
    return _CleanupPlan(
        scope_type="dataset_snapshot",
        scope_id=snapshot_id,
        display_name=display_name,
        warnings=[
            "Generated artifacts are only removed when artifact deletion is explicitly requested.",
            (
                "Audit events remain available and cleaned records are marked deleted "
                "for traceability."
            ),
        ],
        will_remove=[
            CleanupEntityImpact(
                scope_type="dataset_snapshot",
                scope_id=snapshot_id,
                display_name=display_name,
                count=1,
            )
        ],
        will_remain=[
            f"{len(linked_jobs)} linked training run(s) remain available.",
            "Audit events remain available and the snapshot is marked deleted.",
        ],
        artifact_paths=_dedupe_artifact_impacts(
            [
                CleanupArtifactImpact(
                    path=str(settings.storage.snapshot_root(snapshot.id)),
                    category="dataset-snapshot",
                ),
                CleanupArtifactImpact(
                    path=str(
                        settings.storage.labelstudio_project_export_root(
                            snapshot.label_project_id or "unknown",
                            snapshot.id,
                        )
                    ),
                    category="labelstudio-export",
                ),
            ]
        ),
        snapshots=[snapshot],
    )


def _plan_training_job_cleanup(
    session: Session,
    *,
    settings: PlatformSettings,
    training_job_id: str,
) -> _CleanupPlan:
    job = session.get(TrainingJob, training_job_id)
    if job is None:
        raise ValueError(f"Training job {training_job_id} does not exist.")
    display_name = job.model_family_slug
    if job.deleted_at is not None:
        return _already_deleted_plan(
            scope_type="training_job",
            scope_id=training_job_id,
            display_name=display_name,
            will_remain=[
                "Registered model versions remain available.",
                "Audit events remain available and the training run is already marked deleted.",
            ],
        )
    linked_versions = session.scalars(
        select(ModelVersion).where(
            ModelVersion.training_job_id == training_job_id,
            ModelVersion.deleted_at.is_(None),
        )
    ).all()
    return _CleanupPlan(
        scope_type="training_job",
        scope_id=training_job_id,
        display_name=display_name,
        warnings=[
            "Generated artifacts are only removed when artifact deletion is explicitly requested.",
            (
                "Audit events remain available and cleaned records are marked deleted "
                "for traceability."
            ),
        ],
        will_remove=[
            CleanupEntityImpact(
                scope_type="training_job",
                scope_id=training_job_id,
                display_name=display_name,
                count=1,
            )
        ],
        will_remain=[
            f"{len(linked_versions)} linked model version(s) remain available.",
            "Audit events remain available and the training run is marked deleted.",
        ],
        artifact_paths=_dedupe_artifact_impacts(
            [
                CleanupArtifactImpact(
                    path=str(Path(settings.storage.training_root) / f"trn_{job.id}"),
                    category="training-run",
                )
            ]
        ),
        training_jobs=[job],
    )


def _plan_model_version_cleanup(
    session: Session,
    *,
    settings: PlatformSettings,
    model_version_id: str,
) -> _CleanupPlan:
    version = session.get(ModelVersion, model_version_id)
    if version is None:
        raise ValueError(f"Model version {model_version_id} does not exist.")
    display_name = f"Version {version.version_no}"
    if version.deleted_at is not None:
        return _already_deleted_plan(
            scope_type="model_version",
            scope_id=model_version_id,
            display_name=display_name,
            will_remain=[
                "The model family record remains available.",
                "Audit events remain available and the model version is already marked deleted.",
            ],
        )
    releases = session.scalars(
        select(DeploymentRelease).where(
            DeploymentRelease.model_version_id == model_version_id,
            DeploymentRelease.deleted_at.is_(None),
        )
    ).all()
    active_targets, previous_targets = _release_target_refs(session, releases=releases)
    return _CleanupPlan(
        scope_type="model_version",
        scope_id=model_version_id,
        display_name=display_name,
        blocked_reasons=_blocked_reasons(active_targets),
        warnings=[
            *(
                ["Rollback references to deleted releases will be cleared from deployment targets."]
                if previous_targets
                else []
            ),
            "Generated artifacts are only removed when artifact deletion is explicitly requested.",
            (
                "Audit events remain available and cleaned records are marked deleted "
                "for traceability."
            ),
        ],
        will_remove=[
            CleanupEntityImpact(
                scope_type="model_version",
                scope_id=model_version_id,
                display_name=display_name,
                count=1,
            ),
            _impact("deployment_release", "Deployment metadata", releases),
        ],
        will_remain=[
            "The model family record remains available.",
            "Audit events remain available and the model version is marked deleted.",
        ],
        active_deployment_targets=sorted(active_targets),
        artifact_paths=_dedupe_artifact_impacts(
            [
                CleanupArtifactImpact(
                    path=str(settings.storage.serving_package_root(version.id)),
                    category="serving-package",
                )
            ]
            + [
                CleanupArtifactImpact(
                    path=str(Path(settings.storage.serving_root) / "history" / f"rel_{release.id}"),
                    category="deployment-history",
                )
                for release in releases
            ]
        ),
        model_versions=[version],
        deployment_releases=releases,
        clear_previous_targets=previous_targets,
    )


def _plan_deployment_release_cleanup(
    session: Session,
    *,
    settings: PlatformSettings,
    deployment_release_id: str,
) -> _CleanupPlan:
    release = session.get(DeploymentRelease, deployment_release_id)
    if release is None:
        raise ValueError(f"Deployment release {deployment_release_id} does not exist.")
    display_name = (
        f"{release.target_path}:{release.triton_model_name}:{release.triton_model_version}"
    )
    if release.deleted_at is not None:
        return _already_deleted_plan(
            scope_type="deployment_release",
            scope_id=deployment_release_id,
            display_name=display_name,
            will_remain=[
                "The trained model version remains available.",
                (
                    "Audit events remain available and the deployment release is already "
                    "marked deleted."
                ),
            ],
        )
    active_targets, previous_targets = _release_target_refs(session, releases=[release])
    return _CleanupPlan(
        scope_type="deployment_release",
        scope_id=deployment_release_id,
        display_name=display_name,
        blocked_reasons=_blocked_reasons(active_targets),
        warnings=[
            *(
                ["Rollback references to deleted releases will be cleared from deployment targets."]
                if previous_targets
                else []
            ),
            (
                "Release cleanup removes deployment metadata only. The serving package "
                "remains available for future deploys."
            ),
            (
                "Audit events remain available and cleaned records are marked deleted "
                "for traceability."
            ),
        ],
        will_remove=[
            CleanupEntityImpact(
                scope_type="deployment_release",
                scope_id=deployment_release_id,
                display_name=display_name,
                count=1,
            )
        ],
        will_remain=[
            "The trained model version and serving package remain available.",
            "Audit events remain available and the deployment release is marked deleted.",
        ],
        active_deployment_targets=sorted(active_targets),
        artifact_paths=_dedupe_artifact_impacts(
            [
                CleanupArtifactImpact(
                    path=str(Path(settings.storage.serving_root) / "history" / f"rel_{release.id}"),
                    category="deployment-history",
                )
            ]
        ),
        deployment_releases=[release],
        clear_previous_targets=previous_targets,
    )


def _latest_snapshot(rows: list[DatasetSnapshot]) -> DatasetSnapshot | None:
    return max(
        rows,
        key=lambda row: (
            row.snapshot_no or 0,
            str(row.created_at or ""),
            row.id,
        ),
        default=None,
    )


def _latest_model_version(rows: list[ModelVersion]) -> ModelVersion | None:
    return max(
        rows,
        key=lambda row: (
            row.version_no or 0,
            str(row.created_at or ""),
            row.id,
        ),
        default=None,
    )


def _latest_created(rows: list[_CreatedRow]) -> _CreatedRow | None:
    return max(
        rows,
        key=lambda row: (str(getattr(row, "created_at", "") or ""), row.id),
        default=None,
    )


def _latest_releases_by_target(rows: list[DeploymentRelease]) -> list[DeploymentRelease]:
    latest_by_target: dict[str, DeploymentRelease] = {}
    for row in rows:
        current = latest_by_target.get(row.target_path)
        if current is None or _release_sort_key(row) > _release_sort_key(current):
            latest_by_target[row.target_path] = row
    return list(latest_by_target.values())


def _release_sort_key(row: DeploymentRelease) -> tuple[str, str, str]:
    return (
        str(row.activated_at or ""),
        str(row.created_at or ""),
        row.id,
    )


def _add_once(target: set[str], value: str) -> bool:
    if value in target:
        return False
    target.add(value)
    return True


def _preview(plan: _CleanupPlan) -> CleanupImpactPreview:
    return CleanupImpactPreview(
        scope_type=plan.scope_type,
        scope_id=plan.scope_id,
        display_name=plan.display_name,
        confirmation_token=_confirmation_token(plan),
        blocked=bool(plan.blocked_reasons),
        blocked_reasons=plan.blocked_reasons,
        warnings=plan.warnings,
        will_remove=[item for item in plan.will_remove if item.count > 0],
        will_remain=plan.will_remain,
        active_deployment_targets=plan.active_deployment_targets,
        artifact_paths=plan.artifact_paths,
    )


def _execute(
    session: Session,
    plan: _CleanupPlan,
    *,
    settings: PlatformSettings,
    payload: CleanupExecutionRequest,
    deleted_by: str | None,
) -> CleanupExecutionResult:
    expected_token = _confirmation_token(plan)
    if payload.confirmation_token != expected_token:
        raise ValueError(
            "Cleanup confirmation token is invalid or expired. Refresh the impact preview."
        )
    if plan.blocked_reasons:
        raise ValueError("Cleanup is blocked until active deployment references are removed.")
    if plan.already_deleted:
        return CleanupExecutionResult(
            scope_type=plan.scope_type,
            scope_id=plan.scope_id,
            already_deleted=True,
            retained_entities=plan.will_remain,
            warnings=plan.warnings,
        )

    now = utc_now()
    if plan.label_studio_project_ids:
        _delete_label_studio_projects(settings, project_ids=plan.label_studio_project_ids)

    for target in plan.clear_previous_targets:
        if target.previous_release_id:
            target.previous_release_id = None
            target.updated_at = now
    for target in plan.clear_release_targets:
        changed = False
        if target.active_release_id:
            target.active_release_id = None
            changed = True
        if target.previous_release_id:
            target.previous_release_id = None
            changed = True
        if changed:
            target.updated_at = now

    if plan.hard_delete:
        for row in plan.hard_delete_rows:
            session.delete(row)
    else:
        for row in (
            plan.label_projects
            + plan.snapshots
            + plan.training_jobs
            + plan.model_families
            + plan.model_versions
            + plan.deployment_releases
        ):
            _mark_deleted(row=row, deleted_at=now, deleted_by=deleted_by, reason=payload.reason)

    deleted_artifacts: list[str] = []
    artifact_errors: list[str] = []
    if payload.delete_artifacts or plan.force_delete_artifacts:
        deleted_artifacts, artifact_errors = _delete_artifacts(
            settings,
            impacts=plan.artifact_paths,
        )

    return CleanupExecutionResult(
        scope_type=plan.scope_type,
        scope_id=plan.scope_id,
        deleted_entities=[item for item in plan.will_remove if item.count > 0],
        retained_entities=plan.will_remain,
        deleted_artifacts=deleted_artifacts,
        artifact_errors=artifact_errors,
        warnings=plan.warnings,
    )


def _mark_deleted(
    *,
    row: _DeletableRow,
    deleted_at: datetime,
    deleted_by: str | None,
    reason: str | None,
) -> None:
    if getattr(row, "deleted_at", None) is not None:
        return
    row.deleted_at = deleted_at
    row.deleted_by = deleted_by
    row.delete_reason = reason
    if hasattr(row, "active"):
        row.active = False
    if isinstance(row, DeploymentRelease) and row.retired_at is None:
        row.retired_at = deleted_at


def _release_target_refs(
    session: Session,
    *,
    releases: list[DeploymentRelease],
) -> tuple[set[str], list[DeploymentTarget]]:
    release_ids = {row.id for row in releases}
    active_targets: set[str] = set()
    clear_previous_targets: list[DeploymentTarget] = []
    if not release_ids:
        return active_targets, clear_previous_targets
    targets = session.scalars(select(DeploymentTarget)).all()
    for target in targets:
        if target.active_release_id in release_ids:
            active_targets.add(target.target_path)
        if (
            target.previous_release_id in release_ids
            and target.active_release_id not in release_ids
        ):
            clear_previous_targets.append(target)
    return active_targets, clear_previous_targets


def _release_targets_for_releases(
    session: Session,
    *,
    releases: list[DeploymentRelease],
) -> list[DeploymentTarget]:
    release_ids = {row.id for row in releases}
    if not release_ids:
        return []
    return [
        target
        for target in session.scalars(select(DeploymentTarget)).all()
        if target.active_release_id in release_ids or target.previous_release_id in release_ids
    ]


def _blocked_reasons(active_targets: set[str]) -> list[str]:
    if not active_targets:
        return []
    ordered = ", ".join(sorted(active_targets))
    return [
        (
            "One or more active deployment targets still reference this scope: "
            f"{ordered}. Undeploy or rollback first."
        )
    ]


def _dedupe_artifact_impacts(items: list[CleanupArtifactImpact]) -> list[CleanupArtifactImpact]:
    unique: dict[str, CleanupArtifactImpact] = {}
    for item in items:
        unique[item.path] = item
    return sorted(unique.values(), key=lambda item: item.path)


def _impact(scope_type: str, display_name: str, rows: list[Any]) -> CleanupEntityImpact:
    return CleanupEntityImpact(
        scope_type=scope_type,
        scope_id=scope_type,
        display_name=display_name,
        count=len(rows),
    )


def _already_deleted_plan(
    *,
    scope_type: str,
    scope_id: str,
    display_name: str,
    will_remain: list[str],
) -> _CleanupPlan:
    return _CleanupPlan(
        scope_type=scope_type,
        scope_id=scope_id,
        display_name=display_name,
        already_deleted=True,
        warnings=["This cleanup scope was already deleted. Execution is idempotent."],
        will_remain=will_remain,
    )


def _confirmation_token(plan: _CleanupPlan) -> str:
    payload = {
        "scope_type": plan.scope_type,
        "scope_id": plan.scope_id,
        "already_deleted": plan.already_deleted,
        "hard_delete": plan.hard_delete,
        "force_delete_artifacts": plan.force_delete_artifacts,
        "blocked_reasons": sorted(plan.blocked_reasons),
        "active_targets": sorted(plan.active_deployment_targets),
        "label_studio_project_ids": sorted(plan.label_studio_project_ids),
        "counts": {
            "datasets": len(plan.datasets),
            "label_projects": len(plan.label_projects),
            "snapshots": len(plan.snapshots),
            "training_jobs": len(plan.training_jobs),
            "model_families": len(plan.model_families),
            "model_versions": len(plan.model_versions),
            "deployment_releases": len(plan.deployment_releases),
        },
        "artifact_paths": [item.path for item in plan.artifact_paths],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:24]


def _delete_artifacts(
    settings: PlatformSettings,
    *,
    impacts: list[CleanupArtifactImpact],
) -> tuple[list[str], list[str]]:
    deleted: list[str] = []
    errors: list[str] = []
    for impact in sorted(impacts, key=lambda item: len(item.path), reverse=True):
        path = _resolve_artifact_path(impact.path)
        if not _path_within_cleanup_roots(settings, path):
            errors.append(f"Skipped artifact outside managed storage roots: {path}")
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=False)
                deleted.append(str(path))
            elif path.exists():
                path.unlink()
                deleted.append(str(path))
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{path}: {exc}")
    return deleted, errors


def _delete_label_studio_projects(settings: PlatformSettings, *, project_ids: list[int]) -> None:
    if not project_ids:
        return
    client = LabelStudioClient(
        base_url=settings.label_studio.base_url,
        token=settings.label_studio.token(),
        timeout_seconds=settings.label_studio.request_timeout_seconds,
    )
    for project_id in sorted(set(project_ids)):
        try:
            client.delete_project(project_id)
        except LabelStudioError as exc:
            raise ValueError(
                f"Label Studio project {project_id} could not be deleted: {exc}"
            ) from exc


def _resolve_artifact_path(value: str) -> Path:
    return Path(value)


def _path_within_cleanup_roots(settings: PlatformSettings, path: Path) -> bool:
    managed_roots = [
        settings.storage.labelstudio_exports_root(),
        settings.storage.uploads_root(),
        settings.storage.snapshots_root(),
        Path(settings.storage.training_root),
        settings.storage.serving_packages_root(),
        settings.storage.triton_root(),
        Path(settings.storage.serving_root) / "history",
    ]
    resolved = path.resolve(strict=False)
    for root in managed_roots:
        if resolved.is_relative_to(root.resolve(strict=False)):
            return True
    return False
