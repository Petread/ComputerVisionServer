from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from redis import Redis
from rq import Queue, Retry

from admin_api.application.audit import write_admin_audit_event
from admin_api.dependencies import build_meta, get_settings, require_permission
from cv_contracts import (
    AcceptedJob,
    CleanupExecutionRequest,
    CleanupExecutionResult,
    CleanupImpactPreview,
    DeploymentCommandRequest,
    DeploymentModelServingCommandRequest,
    DeploymentPackageBuildRequest,
    DeploymentPackageDetail,
    DeploymentPackageSummary,
    DeploymentReleaseDetail,
    DeploymentReleaseSummary,
    DeploymentSmokeTestRequest,
    DeploymentTargetSummary,
    ListResponse,
    RollbackCommandRequest,
    SessionUser,
)
from cv_db import (
    create_promotion_request,
    execute_deployment_release_cleanup,
    get_deployment_package_detail,
    get_deployment_release_detail,
    list_deployment_packages,
    list_deployment_releases,
    list_deployment_targets,
    preview_deployment_release_cleanup,
    queued_job_payload,
    request_rollback_for_target,
    session_scope,
    validate_deployment_model_serving_command,
)
from cv_status import QueueName
from cv_status import UserRole
from cv_utils import NotFoundError, PlatformError

router = APIRouter(prefix="/deployments", tags=["deployments"])

DeploymentReader = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "deployments.read",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
            UserRole.VIEWER,
        )
    ),
]
DeploymentWriter = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "deployments.write",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
        )
    ),
]


def _deploy_queue(settings) -> Queue:
    return Queue(QueueName.DEPLOY.value, connection=Redis.from_url(settings.redis.url()))


def _retry() -> Retry:
    return Retry(max=3, interval=[5, 30, 120])


@router.get("/packages", response_model=ListResponse[DeploymentPackageSummary])
def get_packages(
    _current_user: DeploymentReader,
    settings=Depends(get_settings),
) -> ListResponse[DeploymentPackageSummary]:
    with session_scope(settings) as session:
        items = list_deployment_packages(session)
    return ListResponse(items=items, meta=build_meta())


@router.get("/packages/{model_version_id}", response_model=DeploymentPackageDetail)
def get_package(
    model_version_id: str,
    _current_user: DeploymentReader,
    settings=Depends(get_settings),
) -> DeploymentPackageDetail:
    with session_scope(settings) as session:
        item = get_deployment_package_detail(session, model_version_id=model_version_id)
    if item is None:
        raise NotFoundError(
            message="Deployment package not found",
            detail=f"No deployment package view exists for model version {model_version_id}.",
        )
    return item


@router.post("/packages", response_model=AcceptedJob, status_code=202)
def create_package_build(
    payload: DeploymentPackageBuildRequest,
    request: Request,
    current_user: DeploymentWriter,
    settings=Depends(get_settings),
) -> AcceptedJob:
    _deploy_queue(settings).enqueue(
        "control_worker.jobs.deployment.build_deployment_package",
        model_version_id=payload.model_version_id,
        job_timeout=settings.training.job_timeout_seconds,
        retry=_retry(),
    )
    accepted = AcceptedJob(**queued_job_payload(payload.model_version_id, QueueName.DEPLOY))
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="deployment.package.build.requested.v1",
        aggregate_type="model_version",
        aggregate_id=payload.model_version_id,
        payload={"job_id": accepted.job_id},
    )
    return accepted


@router.get("/targets", response_model=ListResponse[DeploymentTargetSummary])
def get_targets(
    _current_user: DeploymentReader,
    settings=Depends(get_settings),
) -> ListResponse[DeploymentTargetSummary]:
    with session_scope(settings) as session:
        items = list_deployment_targets(session)
    return ListResponse(items=items, meta=build_meta())


@router.get("/releases", response_model=ListResponse[DeploymentReleaseSummary])
def get_releases(
    _current_user: DeploymentReader,
    settings=Depends(get_settings),
) -> ListResponse[DeploymentReleaseSummary]:
    with session_scope(settings) as session:
        items = list_deployment_releases(session)
    return ListResponse(items=items, meta=build_meta())


@router.get("/releases/{deployment_release_id}", response_model=DeploymentReleaseDetail)
def get_release(
    deployment_release_id: str,
    _current_user: DeploymentReader,
    settings=Depends(get_settings),
) -> DeploymentReleaseDetail:
    with session_scope(settings) as session:
        item = get_deployment_release_detail(session, deployment_release_id=deployment_release_id)
    if item is None:
        raise NotFoundError(
            message="Deployment release not found",
            detail=f"No deployment release exists for id {deployment_release_id}.",
    )
    return item


@router.get("/releases/{deployment_release_id}/cleanup-preview", response_model=CleanupImpactPreview)
def preview_release_cleanup(
    deployment_release_id: str,
    _current_user: DeploymentWriter,
    settings=Depends(get_settings),
) -> CleanupImpactPreview:
    with session_scope(settings) as session:
        try:
            return preview_deployment_release_cleanup(
                session,
                settings=settings,
                deployment_release_id=deployment_release_id,
            )
        except ValueError as exc:
            raise NotFoundError(
                message="Deployment release not found",
                detail=str(exc),
            ) from exc


@router.post("/releases/{deployment_release_id}:cleanup", response_model=CleanupExecutionResult)
def cleanup_release(
    deployment_release_id: str,
    payload: CleanupExecutionRequest,
    request: Request,
    current_user: DeploymentWriter,
    settings=Depends(get_settings),
) -> CleanupExecutionResult:
    with session_scope(settings) as session:
        try:
            result = execute_deployment_release_cleanup(
                session,
                settings=settings,
                deployment_release_id=deployment_release_id,
                payload=payload,
                deleted_by=current_user.username,
            )
        except ValueError as exc:
            detail = str(exc)
            if "does not exist" in detail:
                raise NotFoundError(
                    message="Deployment release not found",
                    detail=detail,
                ) from exc
            raise PlatformError(
                code="deployment_cleanup_invalid",
                message="The deployment cleanup request could not be completed.",
                detail=detail,
                status_code=422,
            ) from exc
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="deployment.release.cleanup.executed.v1",
        aggregate_type="deployment_release",
        aggregate_id=deployment_release_id,
        payload=result.model_dump(mode="json"),
    )
    return result


@router.post("/promotions", response_model=AcceptedJob, status_code=202)
def create_promotion(
    payload: DeploymentCommandRequest,
    request: Request,
    current_user: DeploymentWriter,
    settings=Depends(get_settings),
) -> AcceptedJob:
    with session_scope(settings) as session:
        try:
            promotion_id = create_promotion_request(
                session,
                model_version_id=payload.model_version_id,
                target_path=payload.target_path.value,
                requested_by=current_user.username,
                requested_state=payload.requested_state,
                allow_simulation_deployment=settings.training.allow_simulation_deployment,
            )
        except ValueError as exc:
            raise PlatformError(
                code="promotion_invalid",
                message="The deployment promotion request could not be created.",
                detail=str(exc),
                status_code=422,
            ) from exc
    _deploy_queue(settings).enqueue(
        (
            "control_worker.jobs.deployment.stage_model_release"
            if payload.requested_state == "staged"
            else "control_worker.jobs.deployment.promote_model_release"
        ),
        promotion_id=promotion_id,
        job_timeout=settings.training.job_timeout_seconds,
        retry=_retry(),
    )
    accepted = AcceptedJob(**queued_job_payload(promotion_id, QueueName.DEPLOY))
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="deployment.promotion.requested.v1",
        aggregate_type="model_promotion",
        aggregate_id=promotion_id,
        payload={
            "model_version_id": payload.model_version_id,
            "target_path": payload.target_path.value,
            "requested_state": payload.requested_state,
        },
    )
    return accepted


@router.post("/releases/{deployment_release_id}:smoke-test", response_model=AcceptedJob, status_code=202)
def trigger_smoke_test(
    deployment_release_id: str,
    _payload: DeploymentSmokeTestRequest,
    request: Request,
    current_user: DeploymentWriter,
    settings=Depends(get_settings),
) -> AcceptedJob:
    _deploy_queue(settings).enqueue(
        "control_worker.jobs.deployment.run_release_smoke_test",
        deployment_release_id=deployment_release_id,
        job_timeout=settings.training.job_timeout_seconds,
        retry=_retry(),
    )
    accepted = AcceptedJob(**queued_job_payload(deployment_release_id, QueueName.DEPLOY))
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="deployment.smoke_test.requested.v1",
        aggregate_type="deployment_release",
        aggregate_id=deployment_release_id,
        payload={"job_id": accepted.job_id},
    )
    return accepted


@router.post(
    "/releases/{deployment_release_id}:deactivate",
    response_model=AcceptedJob,
    status_code=202,
)
def deactivate_model_serving(
    deployment_release_id: str,
    payload: DeploymentModelServingCommandRequest,
    request: Request,
    current_user: DeploymentWriter,
    settings=Depends(get_settings),
) -> AcceptedJob:
    with session_scope(settings) as session:
        try:
            release = validate_deployment_model_serving_command(
                session,
                deployment_release_id=deployment_release_id,
                enable=False,
            )
        except ValueError as exc:
            raise PlatformError(
                code="deployment_model_deactivation_invalid",
                message="The deployed model cannot be deactivated.",
                detail=str(exc),
                status_code=422,
            ) from exc
    _deploy_queue(settings).enqueue(
        "control_worker.jobs.deployment.deactivate_deployment_model",
        deployment_release_id=deployment_release_id,
        job_timeout=settings.training.job_timeout_seconds,
        retry=_retry(),
    )
    accepted = AcceptedJob(**queued_job_payload(deployment_release_id, QueueName.DEPLOY))
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="deployment.model.deactivation.requested.v1",
        aggregate_type="triton_model",
        aggregate_id=release.triton_model_name,
        payload={
            "job_id": accepted.job_id,
            "deployment_release_id": deployment_release_id,
            "target_path": release.target_path,
            "reason": payload.reason,
        },
    )
    return accepted


@router.post(
    "/releases/{deployment_release_id}:activate",
    response_model=AcceptedJob,
    status_code=202,
)
def activate_model_serving(
    deployment_release_id: str,
    payload: DeploymentModelServingCommandRequest,
    request: Request,
    current_user: DeploymentWriter,
    settings=Depends(get_settings),
) -> AcceptedJob:
    with session_scope(settings) as session:
        try:
            release = validate_deployment_model_serving_command(
                session,
                deployment_release_id=deployment_release_id,
                enable=True,
            )
        except ValueError as exc:
            raise PlatformError(
                code="deployment_model_activation_invalid",
                message="The deployed model cannot be activated.",
                detail=str(exc),
                status_code=422,
            ) from exc
    _deploy_queue(settings).enqueue(
        "control_worker.jobs.deployment.activate_deployment_model",
        deployment_release_id=deployment_release_id,
        job_timeout=settings.training.job_timeout_seconds,
        retry=_retry(),
    )
    accepted = AcceptedJob(**queued_job_payload(deployment_release_id, QueueName.DEPLOY))
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="deployment.model.activation.requested.v1",
        aggregate_type="triton_model",
        aggregate_id=release.triton_model_name,
        payload={
            "job_id": accepted.job_id,
            "deployment_release_id": deployment_release_id,
            "target_path": release.target_path,
            "reason": payload.reason,
        },
    )
    return accepted


@router.post("/{target_path}:rollback", response_model=AcceptedJob, status_code=202)
def rollback_target(
    target_path: str,
    _payload: RollbackCommandRequest,
    request: Request,
    current_user: DeploymentWriter,
    settings=Depends(get_settings),
) -> AcceptedJob:
    with session_scope(settings) as session:
        try:
            request_rollback_for_target(session, target_path=target_path)
        except ValueError as exc:
            raise PlatformError(
                code="rollback_invalid",
                message="The rollback request could not be created.",
                detail=str(exc),
                status_code=422,
            ) from exc
    _deploy_queue(settings).enqueue(
        "control_worker.jobs.deployment.rollback_model_release",
        target_path=target_path,
        job_timeout=settings.training.job_timeout_seconds,
        retry=_retry(),
    )
    accepted = AcceptedJob(**queued_job_payload(target_path, QueueName.DEPLOY))
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="deployment.rollback.requested.v1",
        aggregate_type="deployment_target",
        aggregate_id=target_path,
        payload={"job_id": accepted.job_id},
    )
    return accepted
