from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from redis import Redis
from rq import Queue, Retry

from admin_api.application.audit import write_admin_audit_event
from admin_api.dependencies import get_settings, require_permission
from cv_contracts import (
    CleanupExecutionRequest,
    CleanupExecutionResult,
    CleanupImpactPreview,
    DeploymentApprovalRecordSummary,
    ListResponse,
    ModelApprovalRequest,
    ModelFamilySummary,
    ModelLifecycleCommandRequest,
    ModelVersionComparison,
    ModelVersionDetail,
    ModelVersionSummary,
    RollbackTargetRequest,
    SessionUser,
)
from cv_db import (
    archive_model_version,
    compare_model_versions,
    execute_model_version_cleanup,
    get_model_version_detail,
    list_model_families,
    list_model_versions,
    mark_rollback_candidate,
    preview_model_version_cleanup,
    record_deployment_approval,
    reject_model_version,
    session_scope,
)
from cv_db.models import LabelProject, ModelFamily, ModelVersion
from cv_status import DeploymentApprovalScope, DeploymentApprovalStatus, QueueName
from cv_status import UserRole
from cv_utils import NotFoundError, PlatformError

from admin_api.dependencies import build_meta

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/registry", tags=["registry"])

RegistryReader = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "registry.read",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
            UserRole.VIEWER,
        )
    ),
]
RegistryWriter = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "registry.write",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
        )
    ),
]


def _deploy_queue(settings) -> Queue:
    return Queue(QueueName.DEPLOY.value, connection=Redis.from_url(settings.redis.url()))


def _retry() -> Retry:
    return Retry(max=2, interval=[10, 60])


def _load_detail_or_raise(settings, model_version_id: str) -> ModelVersionDetail:
    with session_scope(settings) as session:
        item = get_model_version_detail(session, model_version_id)
    if item is None:
        raise NotFoundError(
            message="Model version not found",
            detail=f"No registered model version exists for id {model_version_id}.",
        )
    return item


@router.get("/model-families", response_model=ListResponse[ModelFamilySummary])
def get_model_families(
    _current_user: RegistryReader,
    settings=Depends(get_settings),
) -> ListResponse[ModelFamilySummary]:
    with session_scope(settings) as session:
        items = list_model_families(session)
    return ListResponse(items=items, meta=build_meta())


@router.get("/model-versions", response_model=ListResponse[ModelVersionSummary])
def get_model_versions(
    _current_user: RegistryReader,
    family_slug: str | None = None,
    settings=Depends(get_settings),
) -> ListResponse[ModelVersionSummary]:
    with session_scope(settings) as session:
        items = list_model_versions(session, family_slug=family_slug)
    return ListResponse(items=items, meta=build_meta())


@router.get("/model-versions/compare", response_model=ModelVersionComparison)
def compare_versions(
    model_version_ids: Annotated[Sequence[str], Query()],
    _current_user: RegistryReader,
    settings=Depends(get_settings),
) -> ModelVersionComparison:
    unique_ids = list(dict.fromkeys(model_version_ids))
    if len(unique_ids) < 2:
        raise PlatformError(
            code="comparison_requires_multiple_versions",
            message="Select at least two model versions to compare.",
            status_code=422,
        )
    with session_scope(settings) as session:
        return compare_model_versions(session, model_version_ids=unique_ids)


@router.get("/model-versions/{model_version_id}", response_model=ModelVersionDetail)
def get_model_version(
    model_version_id: str,
    _current_user: RegistryReader,
    settings=Depends(get_settings),
) -> ModelVersionDetail:
    return _load_detail_or_raise(settings, model_version_id)


@router.get("/model-versions/{model_version_id}/cleanup-preview", response_model=CleanupImpactPreview)
def preview_model_version_cleanup_route(
    model_version_id: str,
    _current_user: RegistryWriter,
    settings=Depends(get_settings),
) -> CleanupImpactPreview:
    with session_scope(settings) as session:
        try:
            return preview_model_version_cleanup(
                session,
                settings=settings,
                model_version_id=model_version_id,
            )
        except ValueError as exc:
            raise NotFoundError(
                message="Model version not found",
                detail=str(exc),
            ) from exc


@router.post("/model-versions/{model_version_id}:cleanup", response_model=CleanupExecutionResult)
def cleanup_model_version_route(
    model_version_id: str,
    payload: CleanupExecutionRequest,
    request: Request,
    current_user: RegistryWriter,
    settings=Depends(get_settings),
) -> CleanupExecutionResult:
    with session_scope(settings) as session:
        try:
            result = execute_model_version_cleanup(
                session,
                settings=settings,
                model_version_id=model_version_id,
                payload=payload,
                deleted_by=current_user.username,
            )
        except ValueError as exc:
            detail = str(exc)
            if "does not exist" in detail:
                raise NotFoundError(
                    message="Model version not found",
                    detail=detail,
                ) from exc
            raise PlatformError(
                code="model_cleanup_invalid",
                message="The model cleanup request could not be completed.",
                detail=detail,
                status_code=422,
            ) from exc
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="model.cleanup.executed.v1",
        aggregate_type="model_version",
        aggregate_id=model_version_id,
        payload=result.model_dump(mode="json"),
    )
    return result


@router.post("/model-versions/{model_version_id}:approve", response_model=DeploymentApprovalRecordSummary)
def approve_model_version(
    model_version_id: str,
    payload: ModelApprovalRequest,
    request: Request,
    current_user: RegistryWriter,
    settings=Depends(get_settings),
) -> DeploymentApprovalRecordSummary:
    auto_deploy_payload: dict[str, object] | None = None
    with session_scope(settings) as session:
        try:
            approval = record_deployment_approval(
                session,
                model_version_id=model_version_id,
                target_path=payload.target_path.value,
                approval_scope=payload.approval_scope.value,
                status=DeploymentApprovalStatus.APPROVED.value,
                decided_by=current_user.username,
                notes=payload.notes,
                allow_simulation_deployment=settings.training.allow_simulation_deployment,
            )
        except ValueError as exc:
            raise PlatformError(
                code="approval_invalid",
                message="The model approval record could not be created.",
                detail=str(exc),
                status_code=422,
            ) from exc
        auto_deploy_payload = _approval_auto_deploy_payload(
            session,
            model_version_id=model_version_id,
            target_path=payload.target_path.value,
            approval_scope=payload.approval_scope.value,
            requested_by=current_user.username,
        )
    item = DeploymentApprovalRecordSummary(
        approval_record_id=approval.id,
        model_version_id=approval.model_version_id,
        target_path=approval.target_path,
        approval_scope=approval.approval_scope,
        status=approval.status,
        decided_by=approval.decided_by,
        decided_at=approval.decided_at.isoformat().replace("+00:00", "Z"),
        notes=approval.notes,
        promotion_id=approval.promotion_id,
        deployment_release_id=approval.deployment_release_id,
    )
    if auto_deploy_payload is not None:
        _enqueue_approval_auto_deploy(settings, payload=auto_deploy_payload)
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="model.approved.v1",
        aggregate_type="model_version",
        aggregate_id=model_version_id,
        payload={
            "target_path": payload.target_path.value,
            "approval_scope": payload.approval_scope.value,
            "notes": payload.notes,
        },
    )
    return item


def _approval_auto_deploy_payload(
    session,
    *,
    model_version_id: str,
    target_path: str,
    approval_scope: str,
    requested_by: str | None,
) -> dict[str, object] | None:
    if approval_scope != DeploymentApprovalScope.PRODUCTION.value:
        return None
    version = session.get(ModelVersion, model_version_id)
    if version is None or not bool((version.evaluation_summary or {}).get("promotion_ready")):
        return None
    family = session.get(ModelFamily, version.model_family_id)
    if family is None or not family.label_project_id:
        return None
    project = session.get(LabelProject, family.label_project_id)
    if project is None or project.deleted_at is not None:
        return None
    if not bool(getattr(project, "auto_deploy_enabled", True)):
        return None
    return {
        "label_project_id": project.id,
        "preferred_endpoint_mode": target_path,
        "allow_quality_gate_bypass": False,
        "requested_by": requested_by or "approval-auto-deploy",
    }


def _enqueue_approval_auto_deploy(settings, *, payload: dict[str, object]) -> None:
    try:
        _deploy_queue(settings).enqueue(
            "control_worker.jobs.orchestration.deploy_project_latest_model",
            **payload,
            job_timeout=settings.training.job_timeout_seconds,
            retry=_retry(),
        )
    except Exception:
        logger.exception(
            "Failed to queue project deployment automation after model approval: %s.",
            payload,
        )


@router.post("/model-versions/{model_version_id}:mark-rollback-target", response_model=ModelVersionDetail)
def mark_model_version_as_rollback_target(
    model_version_id: str,
    payload: RollbackTargetRequest,
    request: Request,
    current_user: RegistryWriter,
    settings=Depends(get_settings),
) -> ModelVersionDetail:
    with session_scope(settings) as session:
        try:
            mark_rollback_candidate(
                session,
                model_version_id=model_version_id,
                target_path=payload.target_path.value,
                enabled=payload.enabled,
                marked_by=current_user.username,
                notes=payload.notes,
            )
        except ValueError as exc:
            raise PlatformError(
                code="rollback_target_invalid",
                message="The rollback target marker could not be updated.",
                detail=str(exc),
                status_code=422,
            ) from exc
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="model.rollback_target.updated.v1",
        aggregate_type="model_version",
        aggregate_id=model_version_id,
        payload={"target_path": payload.target_path.value, "enabled": payload.enabled},
    )
    return _load_detail_or_raise(settings, model_version_id)


@router.post("/model-versions/{model_version_id}:archive", response_model=ModelVersionDetail)
def archive_model_version_route(
    model_version_id: str,
    payload: ModelLifecycleCommandRequest,
    request: Request,
    current_user: RegistryWriter,
    settings=Depends(get_settings),
) -> ModelVersionDetail:
    with session_scope(settings) as session:
        try:
            archive_model_version(
                session,
                model_version_id=model_version_id,
                archived_by=current_user.username,
                reason=payload.reason,
            )
        except ValueError as exc:
            raise PlatformError(
                code="archive_invalid",
                message="The model version could not be archived.",
                detail=str(exc),
                status_code=422,
            ) from exc
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="model.archived.v1",
        aggregate_type="model_version",
        aggregate_id=model_version_id,
        payload={"reason": payload.reason},
    )
    return _load_detail_or_raise(settings, model_version_id)


@router.post("/model-versions/{model_version_id}:reject", response_model=ModelVersionDetail)
def reject_model_version_route(
    model_version_id: str,
    payload: ModelLifecycleCommandRequest,
    request: Request,
    current_user: RegistryWriter,
    settings=Depends(get_settings),
) -> ModelVersionDetail:
    with session_scope(settings) as session:
        try:
            reject_model_version(
                session,
                model_version_id=model_version_id,
                rejected_by=current_user.username,
                reason=payload.reason,
            )
        except ValueError as exc:
            raise PlatformError(
                code="reject_invalid",
                message="The model version could not be rejected.",
                detail=str(exc),
                status_code=422,
            ) from exc
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="model.rejected.v1",
        aggregate_type="model_version",
        aggregate_id=model_version_id,
        payload={"reason": payload.reason},
    )
    return _load_detail_or_raise(settings, model_version_id)
