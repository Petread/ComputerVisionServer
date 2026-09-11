from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from redis import Redis
from rq import Queue, Retry

from cv_contracts import (
    AcceptedJob,
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
    ResponseMeta,
    RollbackCommandRequest,
)
from cv_db import (
    get_deployment_package_detail,
    get_deployment_release_detail,
    list_deployment_packages,
    list_deployment_releases,
    list_deployment_targets,
    queued_job_payload,
    request_rollback_for_target,
    session_scope,
    validate_deployment_model_serving_command,
)
from cv_status import QueueName
from cv_utils import NotFoundError, PlatformError, get_correlation_id, get_request_id, utc_now_iso
from deployment_controller.lifecycle import reload_active_releases_until_ready

router = APIRouter(prefix="/deployments", tags=["deployments"])


def get_settings(request: Request):
    return request.app.state.settings


def build_meta() -> ResponseMeta:
    return ResponseMeta(
        request_id=get_request_id(),
        correlation_id=get_correlation_id(),
        generated_at=utc_now_iso(),
    )


def deploy_queue(settings) -> Queue:
    return Queue(QueueName.DEPLOY.value, connection=Redis.from_url(settings.redis.url()))


def _retry() -> Retry:
    return Retry(max=3, interval=[5, 30, 120])


@router.get("/packages", response_model=ListResponse[DeploymentPackageSummary])
def get_packages(settings=Depends(get_settings)) -> ListResponse[DeploymentPackageSummary]:
    with session_scope(settings) as session:
        items = list_deployment_packages(session)
    return ListResponse(items=items, meta=build_meta())


@router.get("/packages/{model_version_id}", response_model=DeploymentPackageDetail)
def get_package(model_version_id: str, settings=Depends(get_settings)) -> DeploymentPackageDetail:
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
    settings=Depends(get_settings),
) -> AcceptedJob:
    queue = deploy_queue(settings)
    queue.enqueue(
        "control_worker.jobs.deployment.build_deployment_package",
        model_version_id=payload.model_version_id,
        job_timeout=settings.training.job_timeout_seconds,
        retry=_retry(),
    )
    return AcceptedJob(**queued_job_payload(payload.model_version_id, QueueName.DEPLOY))


@router.get("/targets", response_model=ListResponse[DeploymentTargetSummary])
def get_targets(settings=Depends(get_settings)) -> ListResponse[DeploymentTargetSummary]:
    with session_scope(settings) as session:
        items = list_deployment_targets(session)
    return ListResponse(items=items, meta=build_meta())


@router.get("/releases", response_model=ListResponse[DeploymentReleaseSummary])
def get_releases(settings=Depends(get_settings)) -> ListResponse[DeploymentReleaseSummary]:
    with session_scope(settings) as session:
        items = list_deployment_releases(session)
    return ListResponse(items=items, meta=build_meta())


@router.post("/active-releases:reload")
async def reload_active_model_releases(request: Request) -> dict[str, object]:
    missing_after_reload = await reload_active_releases_until_ready(
        request.app,
        attempts=3,
        retry_delay_seconds=2.0,
    )
    return {
        "schema_version": "deployment.active-release-reload.v1",
        "missing_after_reload": missing_after_reload,
        "meta": build_meta().model_dump(mode="json"),
    }


@router.get("/releases/{deployment_release_id}", response_model=DeploymentReleaseDetail)
def get_release(
    deployment_release_id: str,
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


@router.post("/promotions", response_model=AcceptedJob, status_code=202)
def create_promotion(
    payload: DeploymentCommandRequest,
    settings=Depends(get_settings),
) -> AcceptedJob:
    from cv_db import create_promotion_request

    with session_scope(settings) as session:
        try:
            promotion_id = create_promotion_request(
                session,
                model_version_id=payload.model_version_id,
                target_path=payload.target_path.value,
                requested_by="bootstrap-admin",
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
    queue = deploy_queue(settings)
    queue.enqueue(
        (
            "control_worker.jobs.deployment.stage_model_release"
            if payload.requested_state == "staged"
            else "control_worker.jobs.deployment.promote_model_release"
        ),
        promotion_id=promotion_id,
        job_timeout=settings.training.job_timeout_seconds,
        retry=_retry(),
    )
    return AcceptedJob(**queued_job_payload(promotion_id, QueueName.DEPLOY))


@router.post("/releases/{deployment_release_id}:smoke-test", response_model=AcceptedJob, status_code=202)
def trigger_smoke_test(
    deployment_release_id: str,
    _payload: DeploymentSmokeTestRequest,
    settings=Depends(get_settings),
) -> AcceptedJob:
    queue = deploy_queue(settings)
    queue.enqueue(
        "control_worker.jobs.deployment.run_release_smoke_test",
        deployment_release_id=deployment_release_id,
        job_timeout=settings.training.job_timeout_seconds,
        retry=_retry(),
    )
    return AcceptedJob(**queued_job_payload(deployment_release_id, QueueName.DEPLOY))


@router.post(
    "/releases/{deployment_release_id}:deactivate",
    response_model=AcceptedJob,
    status_code=202,
)
def deactivate_model_serving(
    deployment_release_id: str,
    _payload: DeploymentModelServingCommandRequest,
    settings=Depends(get_settings),
) -> AcceptedJob:
    with session_scope(settings) as session:
        try:
            validate_deployment_model_serving_command(
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
    queue = deploy_queue(settings)
    queue.enqueue(
        "control_worker.jobs.deployment.deactivate_deployment_model",
        deployment_release_id=deployment_release_id,
        job_timeout=settings.training.job_timeout_seconds,
        retry=_retry(),
    )
    return AcceptedJob(**queued_job_payload(deployment_release_id, QueueName.DEPLOY))


@router.post(
    "/releases/{deployment_release_id}:activate",
    response_model=AcceptedJob,
    status_code=202,
)
def activate_model_serving(
    deployment_release_id: str,
    _payload: DeploymentModelServingCommandRequest,
    settings=Depends(get_settings),
) -> AcceptedJob:
    with session_scope(settings) as session:
        try:
            validate_deployment_model_serving_command(
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
    queue = deploy_queue(settings)
    queue.enqueue(
        "control_worker.jobs.deployment.activate_deployment_model",
        deployment_release_id=deployment_release_id,
        job_timeout=settings.training.job_timeout_seconds,
        retry=_retry(),
    )
    return AcceptedJob(**queued_job_payload(deployment_release_id, QueueName.DEPLOY))


@router.post("/{target_path}:rollback", response_model=AcceptedJob, status_code=202)
def rollback_target(
    target_path: str,
    _payload: RollbackCommandRequest,
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
    queue = deploy_queue(settings)
    queue.enqueue(
        "control_worker.jobs.deployment.rollback_model_release",
        target_path=target_path,
        job_timeout=settings.training.job_timeout_seconds,
        retry=_retry(),
    )
    return AcceptedJob(**queued_job_payload(target_path, QueueName.DEPLOY))
