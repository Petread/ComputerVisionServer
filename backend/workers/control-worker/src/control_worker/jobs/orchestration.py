from __future__ import annotations

from sqlalchemy import select

from cv_contracts import ProjectDeployRequest
from cv_core import load_platform_settings
from cv_db import (
    create_promotion_request,
    record_deployment_approval,
    refresh_project_prediction_backend_binding,
    resolve_project_deployment_model_version,
    resolve_project_latest_model_version,
    session_scope,
)
from cv_db.models import (
    DatasetSnapshot,
    DeploymentApprovalRecord,
    DeploymentRelease,
    DeploymentTarget,
    LabelProject,
    ModelVersion,
    TrainingJob,
)
from cv_status import (
    DeploymentApprovalStatus,
    DeploymentApprovalScope,
    DeploymentReleaseStatus,
    EndpointMode,
    TargetPath,
)
from cv_utils import utc_now_iso

from control_worker.jobs.deployment import (
    build_deployment_package,
    promote_model_release,
    stage_model_release,
)


def run_post_training_automation(training_job_id: str) -> dict[str, object]:
    settings = load_platform_settings("control-worker")
    with session_scope(settings) as session:
        training_job = session.get(TrainingJob, training_job_id)
        if training_job is None:
            raise ValueError(f"Training job {training_job_id} does not exist.")
        if training_job.model_version_id is None:
            return {
                "training_job_id": training_job_id,
                "status": "skipped",
                "reason": "training job has no registered model version",
                "finished_at": utc_now_iso(),
            }
        model_version = session.get(ModelVersion, training_job.model_version_id)
        if model_version is None:
            raise ValueError(
                f"Training job {training_job_id} references unknown model version {training_job.model_version_id}."
            )
        snapshot = (
            session.get(DatasetSnapshot, training_job.dataset_snapshot_id)
            if training_job.dataset_snapshot_id
            else None
        )
        project = None
        if snapshot is not None and snapshot.label_project_id:
            project = session.get(LabelProject, snapshot.label_project_id)
        if project is None:
            return {
                "training_job_id": training_job_id,
                "model_version_id": model_version.id,
                "status": "skipped",
                "reason": "no owning project pipeline found",
                "finished_at": utc_now_iso(),
            }
        promotion_ready = bool(
            (training_job.evaluation_summary or {}).get("promotion_ready")
            or (model_version.evaluation_summary or {}).get("promotion_ready")
        )
        auto_deploy_enabled = bool(getattr(project, "auto_deploy_enabled", True))
        target_paths = _target_paths(project.preferred_endpoint_mode or EndpointMode.REALTIME.value)

    build_deployment_package(model_version.id)
    if not auto_deploy_enabled or not promotion_ready:
        return {
            "training_job_id": training_job_id,
            "model_version_id": model_version.id,
            "status": "packaged",
            "auto_deploy_enabled": auto_deploy_enabled,
            "promotion_ready": promotion_ready,
            "finished_at": utc_now_iso(),
        }

    deployed_targets: list[str] = []
    for target_path in target_paths:
        if _ensure_active_target_matches(
            settings=settings,
            model_version_id=model_version.id,
            target_path=target_path,
        ):
            deployed_targets.append(target_path.value)
            continue
        _deploy_model_version(
            settings=settings,
            model_version_id=model_version.id,
            target_path=target_path,
            requested_by="policy-auto-deploy",
        )
        deployed_targets.append(target_path.value)

    prediction_backend = None
    if TargetPath.REALTIME.value in deployed_targets:
        with session_scope(settings) as session:
            prediction_backend = refresh_project_prediction_backend_binding(
                session,
                settings=settings,
                label_project_id=project.id,
            )

    return {
        "training_job_id": training_job_id,
        "model_version_id": model_version.id,
        "status": "deployed",
        "targets": deployed_targets,
        "prediction_backend": (
            prediction_backend.model_dump(mode="json") if prediction_backend is not None else None
        ),
        "finished_at": utc_now_iso(),
    }


def deploy_project_latest_model(
    label_project_id: str,
    preferred_endpoint_mode: str | None = None,
    model_family_id: str | None = None,
    model_version_id: str | None = None,
    allow_quality_gate_bypass: bool = False,
    requested_by: str | None = None,
) -> dict[str, object]:
    settings = load_platform_settings("control-worker")
    with session_scope(settings) as session:
        project = session.get(LabelProject, label_project_id)
        if project is None:
            raise ValueError(f"Label project {label_project_id} does not exist.")
        latest_version = resolve_project_deployment_model_version(
            session,
            label_project_id=label_project_id,
            payload=ProjectDeployRequest(
                model_family_id=model_family_id,
                model_version_id=model_version_id,
                allow_quality_gate_bypass=allow_quality_gate_bypass,
            ),
        )
        if latest_version is None:
            latest_trained_version = resolve_project_latest_model_version(
                session,
                label_project_id=label_project_id,
                model_family_id=model_family_id,
            )
            if latest_trained_version is None:
                raise ValueError(f"Label project {label_project_id} has no trained model version.")
            raise ValueError(
                f"Label project {label_project_id} has no approved model version to deploy yet."
            )
        target_paths = _target_paths(
            preferred_endpoint_mode
            or project.preferred_endpoint_mode
            or EndpointMode.REALTIME.value
        )
        promotion_ready = bool((latest_version.evaluation_summary or {}).get("promotion_ready"))
        if not allow_quality_gate_bypass and not promotion_ready:
            raise ValueError(
                f"Model version {latest_version.id} did not pass the configured promotion quality gates."
            )

    build_deployment_package(latest_version.id)
    deployed_targets: list[str] = []
    for target_path in target_paths:
        if _ensure_active_target_matches(
            settings=settings,
            model_version_id=latest_version.id,
            target_path=target_path,
        ):
            deployed_targets.append(target_path.value)
            continue
        _deploy_model_version(
            settings=settings,
            model_version_id=latest_version.id,
            target_path=target_path,
            requested_by=requested_by or "project-approval",
            allow_quality_gate_bypass=allow_quality_gate_bypass,
        )
        deployed_targets.append(target_path.value)

    prediction_backend = None
    if TargetPath.REALTIME.value in deployed_targets:
        with session_scope(settings) as session:
            prediction_backend = refresh_project_prediction_backend_binding(
                session,
                settings=settings,
                label_project_id=label_project_id,
            )

    return {
        "label_project_id": project.id,
        "model_version_id": latest_version.id,
        "model_family_id": model_family_id,
        "status": "deployed",
        "targets": deployed_targets,
        "quality_gate_bypassed": allow_quality_gate_bypass,
        "prediction_backend": (
            prediction_backend.model_dump(mode="json") if prediction_backend is not None else None
        ),
        "finished_at": utc_now_iso(),
    }


def _deploy_model_version(
    *,
    settings,
    model_version_id: str,
    target_path: TargetPath,
    requested_by: str,
    allow_quality_gate_bypass: bool = False,
) -> None:
    with session_scope(settings) as session:
        _ensure_approval(
            settings=settings,
            session=session,
            model_version_id=model_version_id,
            target_path=target_path,
            approval_scope=DeploymentApprovalScope.STAGING.value,
            decided_by=requested_by,
            notes="Single-click project pipeline deployment approval.",
            allow_quality_gate_bypass=allow_quality_gate_bypass,
        )
        _ensure_approval(
            settings=settings,
            session=session,
            model_version_id=model_version_id,
            target_path=target_path,
            approval_scope=DeploymentApprovalScope.PRODUCTION.value,
            decided_by=requested_by,
            notes="Single-click project pipeline deployment approval.",
            allow_quality_gate_bypass=allow_quality_gate_bypass,
        )
        stage_promotion_id = create_promotion_request(
            session,
            model_version_id=model_version_id,
            target_path=target_path.value,
            requested_by=requested_by,
            requested_state="staged",
            allow_simulation_deployment=settings.training.allow_simulation_deployment,
            allow_quality_gate_bypass=allow_quality_gate_bypass,
        )
    stage_model_release(stage_promotion_id)
    with session_scope(settings) as session:
        production_promotion_id = create_promotion_request(
            session,
            model_version_id=model_version_id,
            target_path=target_path.value,
            requested_by=requested_by,
            requested_state="production",
            allow_simulation_deployment=settings.training.allow_simulation_deployment,
            allow_quality_gate_bypass=allow_quality_gate_bypass,
        )
    promote_model_release(production_promotion_id)


def _ensure_approval(
    *,
    settings,
    session,
    model_version_id: str,
    target_path: TargetPath,
    approval_scope: str,
    decided_by: str,
    notes: str,
    allow_quality_gate_bypass: bool = False,
) -> None:
    approved = session.scalar(
        select(DeploymentApprovalRecord.id)
        .where(
            DeploymentApprovalRecord.model_version_id == model_version_id,
            DeploymentApprovalRecord.target_path == target_path.value,
            DeploymentApprovalRecord.approval_scope == approval_scope,
            DeploymentApprovalRecord.status == DeploymentApprovalStatus.APPROVED.value,
        )
        .limit(1)
    )
    if approved:
        return
    record_deployment_approval(
        session,
        model_version_id=model_version_id,
        target_path=target_path.value,
        approval_scope=approval_scope,
        status=DeploymentApprovalStatus.APPROVED.value,
        decided_by=decided_by,
        notes=notes,
        allow_simulation_deployment=settings.training.allow_simulation_deployment,
        allow_quality_gate_bypass=allow_quality_gate_bypass,
    )


def _ensure_active_target_matches(*, settings, model_version_id: str, target_path: TargetPath) -> bool:
    with session_scope(settings) as session:
        existing_release_id = session.scalar(
            select(DeploymentRelease.id)
            .where(
                DeploymentRelease.model_version_id == model_version_id,
                DeploymentRelease.target_path == target_path.value,
                DeploymentRelease.status == DeploymentReleaseStatus.ACTIVATED.value,
                DeploymentRelease.retired_at.is_(None),
                DeploymentRelease.deleted_at.is_(None),
            )
            .limit(1)
        )
        if existing_release_id:
            return True
        target = session.get(DeploymentTarget, target_path.value)
        if target is None or not target.active_release_id:
            return False
        release = session.get(DeploymentRelease, target.active_release_id)
        return bool(release and release.model_version_id == model_version_id)


def _target_paths(mode: str) -> list[TargetPath]:
    resolved = EndpointMode(mode)
    if resolved == EndpointMode.BOTH:
        return [TargetPath.REALTIME, TargetPath.BATCH]
    return [TargetPath(resolved.value)]
