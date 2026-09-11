from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from redis import Redis
from rq import Queue, Retry

from cv_contracts import (
    DeploymentApprovalRecordSummary,
    ListResponse,
    ModelApprovalRequest,
    ModelFamilySummary,
    ModelLifecycleCommandRequest,
    ModelVersionComparison,
    ModelVersionDetail,
    ModelVersionSummary,
    ResponseMeta,
    RollbackTargetRequest,
)
from cv_db import (
    archive_model_version,
    compare_model_versions,
    get_model_version_detail,
    list_model_families,
    list_model_versions,
    mark_rollback_candidate,
    record_deployment_approval,
    reject_model_version,
    session_scope,
)
from cv_db.models import LabelProject, ModelFamily, ModelVersion
from cv_status import DeploymentApprovalScope, DeploymentApprovalStatus, QueueName
from cv_utils import NotFoundError, PlatformError, get_correlation_id, get_request_id, utc_now_iso

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/registry", tags=["registry"])


def get_settings(request: Request):
    return request.app.state.settings


def build_meta() -> ResponseMeta:
    return ResponseMeta(
        request_id=get_request_id(),
        correlation_id=get_correlation_id(),
        generated_at=utc_now_iso(),
    )


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
def get_model_families(settings=Depends(get_settings)) -> ListResponse[ModelFamilySummary]:
    with session_scope(settings) as session:
        items = list_model_families(session)
    return ListResponse(items=items, meta=build_meta())


@router.get("/model-versions", response_model=ListResponse[ModelVersionSummary])
def get_model_versions(
    family_slug: str | None = None,
    settings=Depends(get_settings),
) -> ListResponse[ModelVersionSummary]:
    with session_scope(settings) as session:
        items = list_model_versions(session, family_slug=family_slug)
    return ListResponse(items=items, meta=build_meta())


@router.get("/model-versions/compare", response_model=ModelVersionComparison)
def compare_versions(
    model_version_ids: Annotated[Sequence[str], Query()],
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
def get_model_version(model_version_id: str, settings=Depends(get_settings)) -> ModelVersionDetail:
    return _load_detail_or_raise(settings, model_version_id)


@router.post(
    "/model-versions/{model_version_id}:approve",
    response_model=DeploymentApprovalRecordSummary,
)
def approve_model_version(
    model_version_id: str,
    payload: ModelApprovalRequest,
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
                decided_by="bootstrap-admin",
                notes=payload.notes,
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
            requested_by="bootstrap-admin",
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
    settings=Depends(get_settings),
) -> ModelVersionDetail:
    with session_scope(settings) as session:
        try:
            mark_rollback_candidate(
                session,
                model_version_id=model_version_id,
                target_path=payload.target_path.value,
                enabled=payload.enabled,
                marked_by="bootstrap-admin",
                notes=payload.notes,
            )
        except ValueError as exc:
            raise PlatformError(
                code="rollback_target_invalid",
                message="The rollback target marker could not be updated.",
                detail=str(exc),
                status_code=422,
            ) from exc
    return _load_detail_or_raise(settings, model_version_id)


@router.post("/model-versions/{model_version_id}:archive", response_model=ModelVersionDetail)
def archive_model_version_route(
    model_version_id: str,
    payload: ModelLifecycleCommandRequest,
    settings=Depends(get_settings),
) -> ModelVersionDetail:
    with session_scope(settings) as session:
        try:
            archive_model_version(
                session,
                model_version_id=model_version_id,
                archived_by="bootstrap-admin",
                reason=payload.reason,
            )
        except ValueError as exc:
            raise PlatformError(
                code="archive_invalid",
                message="The model version could not be archived.",
                detail=str(exc),
                status_code=422,
            ) from exc
    return _load_detail_or_raise(settings, model_version_id)


@router.post("/model-versions/{model_version_id}:reject", response_model=ModelVersionDetail)
def reject_model_version_route(
    model_version_id: str,
    payload: ModelLifecycleCommandRequest,
    settings=Depends(get_settings),
) -> ModelVersionDetail:
    with session_scope(settings) as session:
        try:
            reject_model_version(
                session,
                model_version_id=model_version_id,
                rejected_by="bootstrap-admin",
                reason=payload.reason,
            )
        except ValueError as exc:
            raise PlatformError(
                code="reject_invalid",
                message="The model version could not be rejected.",
                detail=str(exc),
                status_code=422,
            ) from exc
    return _load_detail_or_raise(settings, model_version_id)
