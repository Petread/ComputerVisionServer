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
    ListResponse,
    ModelTemplateCreateRequest,
    ModelTemplateSummary,
    ModelTemplateUpdateRequest,
    ProjectAutomationPolicy,
    ProjectDeployRequest,
    ProjectModelCreateRequest,
    ProjectModelSummary,
    ProjectModelUpdateRequest,
    ProjectModelWorkspaceDetail,
    ProjectPipelineDetail,
    ProjectPipelineSummary,
    ProjectPredictionBackendSummary,
    ProjectTrainRequest,
    SessionUser,
    SupportedModelArchitecture,
)
from cv_db.model_catalog import supported_model_architectures
from cv_db import (
    build_project_training_request,
    create_model_template,
    create_project_model_family,
    ensure_project_prediction_backend,
    create_training_job_request,
    execute_project_pipeline_cleanup,
    get_project_model_workspace_detail,
    get_project_pipeline_detail,
    list_model_templates,
    list_project_pipeline_summaries,
    preview_project_pipeline_cleanup,
    queued_job_payload,
    resolve_project_deployment_model_version,
    resolve_project_deployment_targets,
    resolve_project_latest_model_version,
    session_scope,
    update_model_template,
    update_project_automation_policy,
    update_project_model_family,
)
from cv_datasets import LabelStudioError
from cv_status import QueueName, TaskType, UserRole
from cv_utils import NotFoundError, PlatformError

router = APIRouter(tags=["project-pipelines"])

PipelineReader = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "datasets.read",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
            UserRole.LABEL_MANAGER,
            UserRole.VIEWER,
        )
    ),
]
PipelineWriter = Annotated[
    SessionUser,
    Depends(
        require_permission(
            "training.write",
            UserRole.PLATFORM_ADMIN,
            UserRole.ML_ENGINEER,
            UserRole.OPERATOR,
            UserRole.LABEL_MANAGER,
        )
    ),
]


def _queue(queue_name: QueueName, settings) -> Queue:
    return Queue(queue_name.value, connection=Redis.from_url(settings.redis.url()))


def _retry() -> Retry:
    return Retry(max=3, interval=[5, 30, 120])


@router.post("/project-pipelines:refresh", response_model=AcceptedJob, status_code=202)
def refresh_project_catalog(
    request: Request,
    current_user: PipelineWriter,
    settings=Depends(get_settings),
) -> AcceptedJob:
    _queue(QueueName.DATASET, settings).enqueue(
        "control_worker.jobs.dataset.sync_labelstudio_projects",
        retry=_retry(),
    )
    accepted = AcceptedJob(**queued_job_payload("label-project-sync", QueueName.DATASET))
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="project_pipeline.catalog_refresh.requested.v1",
        aggregate_type="system",
        aggregate_id="labelstudio",
        payload={"job_id": accepted.job_id},
    )
    return accepted


@router.get("/project-pipelines", response_model=ListResponse[ProjectPipelineSummary])
def list_project_pipelines(
    _current_user: PipelineReader,
    settings=Depends(get_settings),
) -> ListResponse[ProjectPipelineSummary]:
    with session_scope(settings) as session:
        items = list_project_pipeline_summaries(session)
    return ListResponse(
        items=[ProjectPipelineSummary.model_validate(item.model_dump()) for item in items],
        meta=build_meta(),
    )


@router.get("/project-pipelines/{label_project_id}", response_model=ProjectPipelineDetail)
def get_project_pipeline(
    label_project_id: str,
    _current_user: PipelineReader,
    settings=Depends(get_settings),
) -> ProjectPipelineDetail:
    with session_scope(settings) as session:
        item = get_project_pipeline_detail(session, label_project_id=label_project_id)
    if item is None:
        raise NotFoundError(
            message="Project pipeline not found",
            detail=f"No pipeline exists for label project id {label_project_id}.",
        )
    return item


@router.post("/project-pipelines/{label_project_id}:sync", response_model=AcceptedJob, status_code=202)
def sync_project_pipeline(
    label_project_id: str,
    request: Request,
    current_user: PipelineWriter,
    settings=Depends(get_settings),
) -> AcceptedJob:
    _queue(QueueName.DATASET, settings).enqueue(
        "control_worker.jobs.webhook.reconcile_project_counters",
        label_project_id=label_project_id,
        force_new_snapshot=True,
        retry=_retry(),
    )
    accepted = AcceptedJob(**queued_job_payload(label_project_id, QueueName.DATASET))
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="project_pipeline.sync.requested.v1",
        aggregate_type="label_project",
        aggregate_id=label_project_id,
        payload={"job_id": accepted.job_id},
    )
    return accepted


@router.get("/model-templates", response_model=ListResponse[ModelTemplateSummary])
def get_model_templates(
    _current_user: PipelineReader,
    settings=Depends(get_settings),
    task_type: TaskType | None = None,
) -> ListResponse[ModelTemplateSummary]:
    with session_scope(settings) as session:
        items = list_model_templates(session, task_type=task_type)
    return ListResponse(items=items, meta=build_meta())


@router.get("/model-capabilities", response_model=ListResponse[SupportedModelArchitecture])
def get_model_capabilities(
    _current_user: PipelineReader,
    task_type: TaskType | None = None,
) -> ListResponse[SupportedModelArchitecture]:
    return ListResponse(items=supported_model_architectures(task_type=task_type), meta=build_meta())


@router.post("/model-templates", response_model=ModelTemplateSummary, status_code=201)
def create_model_template_route(
    payload: ModelTemplateCreateRequest,
    request: Request,
    current_user: PipelineWriter,
    settings=Depends(get_settings),
) -> ModelTemplateSummary:
    with session_scope(settings) as session:
        try:
            item = create_model_template(
                session,
                payload=payload,
                created_by=current_user.username,
            )
        except ValueError as exc:
            raise PlatformError(
                code="model_template_invalid",
                message="The model template could not be created.",
                detail=str(exc),
                status_code=422,
            ) from exc
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="model_template.created.v1",
        aggregate_type="model_template",
        aggregate_id=item.template_key,
        payload=item.model_dump(mode="json"),
    )
    return item


@router.put("/model-templates/{template_key}", response_model=ModelTemplateSummary)
def update_model_template_route(
    template_key: str,
    payload: ModelTemplateUpdateRequest,
    request: Request,
    current_user: PipelineWriter,
    settings=Depends(get_settings),
) -> ModelTemplateSummary:
    with session_scope(settings) as session:
        try:
            item = update_model_template(
                session,
                template_key=template_key,
                payload=payload,
                updated_by=current_user.username,
            )
        except ValueError as exc:
            raise PlatformError(
                code="model_template_invalid",
                message="The model template could not be updated.",
                detail=str(exc),
                status_code=422,
            ) from exc
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="model_template.updated.v1",
        aggregate_type="model_template",
        aggregate_id=item.template_key,
        payload=item.model_dump(mode="json"),
    )
    return item


@router.put(
    "/project-pipelines/{label_project_id}/automation-policy",
    response_model=ProjectAutomationPolicy,
)
def update_pipeline_policy(
    label_project_id: str,
    payload: ProjectAutomationPolicy,
    request: Request,
    current_user: PipelineWriter,
    settings=Depends(get_settings),
) -> ProjectAutomationPolicy:
    with session_scope(settings) as session:
        try:
            item = update_project_automation_policy(
                session,
                label_project_id=label_project_id,
                payload=payload,
            )
        except ValueError as exc:
            detail = str(exc)
            if "does not exist" in detail:
                raise NotFoundError(
                    message="Project pipeline not found",
                    detail=detail,
                ) from exc
            raise PlatformError(
                code="project_automation_policy_invalid",
                message="The project automation policy is invalid.",
                detail=detail,
                status_code=422,
            ) from exc
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="project_pipeline.policy.updated.v1",
        aggregate_type="label_project",
        aggregate_id=label_project_id,
        payload=item.model_dump(mode="json"),
    )
    return item


@router.post(
    "/project-pipelines/{label_project_id}/models",
    response_model=ProjectModelSummary,
    status_code=201,
)
def create_pipeline_model(
    label_project_id: str,
    payload: ProjectModelCreateRequest,
    request: Request,
    current_user: PipelineWriter,
    settings=Depends(get_settings),
) -> ProjectModelSummary:
    with session_scope(settings) as session:
        try:
            item = create_project_model_family(
                session,
                label_project_id=label_project_id,
                payload=payload,
            )
        except ValueError as exc:
            raise PlatformError(
                code="project_model_invalid",
                message="The project model could not be created.",
                detail=str(exc),
                status_code=422,
            ) from exc
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="project_model.created.v1",
        aggregate_type="label_project",
        aggregate_id=label_project_id,
        payload=item.model_dump(mode="json"),
    )
    return item


@router.get(
    "/project-pipelines/{label_project_id}/models/{model_family_id}",
    response_model=ProjectModelWorkspaceDetail,
)
def get_pipeline_model_workspace(
    label_project_id: str,
    model_family_id: str,
    _current_user: PipelineReader,
    settings=Depends(get_settings),
) -> ProjectModelWorkspaceDetail:
    with session_scope(settings) as session:
        item = get_project_model_workspace_detail(
            session,
            label_project_id=label_project_id,
            model_family_id=model_family_id,
        )
    if item is None:
        raise NotFoundError(
            message="Project model not found",
            detail=f"Model {model_family_id} does not belong to project {label_project_id}.",
        )
    return item


@router.patch(
    "/project-pipelines/{label_project_id}/models/{model_family_id}",
    response_model=ProjectModelSummary,
)
def update_pipeline_model(
    label_project_id: str,
    model_family_id: str,
    payload: ProjectModelUpdateRequest,
    request: Request,
    current_user: PipelineWriter,
    settings=Depends(get_settings),
) -> ProjectModelSummary:
    with session_scope(settings) as session:
        try:
            item = update_project_model_family(
                session,
                label_project_id=label_project_id,
                model_family_id=model_family_id,
                payload=payload,
            )
        except ValueError as exc:
            raise PlatformError(
                code="project_model_invalid",
                message="The project model could not be updated.",
                detail=str(exc),
                status_code=422,
            ) from exc
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="project_model.updated.v1",
        aggregate_type="model_family",
        aggregate_id=model_family_id,
        payload=item.model_dump(mode="json"),
    )
    return item


@router.get(
    "/project-pipelines/{label_project_id}/cleanup-preview",
    response_model=CleanupImpactPreview,
)
def preview_pipeline_cleanup(
    label_project_id: str,
    _current_user: PipelineWriter,
    settings=Depends(get_settings),
    delete_entire_pipeline: bool = False,
) -> CleanupImpactPreview:
    with session_scope(settings) as session:
        try:
            return preview_project_pipeline_cleanup(
                session,
                settings=settings,
                label_project_id=label_project_id,
                delete_entire_pipeline=delete_entire_pipeline,
            )
        except ValueError as exc:
            raise NotFoundError(
                message="Project pipeline not found",
                detail=str(exc),
            ) from exc


@router.post(
    "/project-pipelines/{label_project_id}:cleanup",
    response_model=CleanupExecutionResult,
)
def cleanup_pipeline(
    label_project_id: str,
    payload: CleanupExecutionRequest,
    request: Request,
    current_user: PipelineWriter,
    settings=Depends(get_settings),
) -> CleanupExecutionResult:
    with session_scope(settings) as session:
        try:
            result = execute_project_pipeline_cleanup(
                session,
                settings=settings,
                label_project_id=label_project_id,
                payload=payload,
                deleted_by=current_user.username,
            )
        except ValueError as exc:
            detail = str(exc)
            if "does not exist" in detail:
                raise NotFoundError(
                    message="Project pipeline not found",
                    detail=detail,
                ) from exc
            raise PlatformError(
                code="project_cleanup_invalid",
                message="The project cleanup request could not be completed.",
                detail=detail,
                status_code=422,
            ) from exc
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="project_pipeline.cleanup.executed.v1",
        aggregate_type="label_project",
        aggregate_id=label_project_id,
        payload=result.model_dump(mode="json"),
    )
    return result


@router.post("/project-pipelines/{label_project_id}:train", response_model=AcceptedJob, status_code=202)
def train_from_project_pipeline(
    label_project_id: str,
    request: Request,
    current_user: PipelineWriter,
    settings=Depends(get_settings),
    payload: ProjectTrainRequest | None = None,
) -> AcceptedJob:
    return _queue_project_training(
        label_project_id=label_project_id,
        payload=payload,
        request=request,
        current_user=current_user,
        settings=settings,
        event_type="project_pipeline.training.requested.v1",
    )


@router.post(
    "/project-pipelines/{label_project_id}:retrain-latest",
    response_model=AcceptedJob,
    status_code=202,
)
def retrain_latest_from_project_pipeline(
    label_project_id: str,
    request: Request,
    current_user: PipelineWriter,
    settings=Depends(get_settings),
) -> AcceptedJob:
    return _queue_project_training(
        label_project_id=label_project_id,
        payload=ProjectTrainRequest(),
        request=request,
        current_user=current_user,
        settings=settings,
        event_type="project_pipeline.retrain_latest.requested.v1",
    )


@router.post(
    "/project-pipelines/{label_project_id}:deploy-latest",
    response_model=AcceptedJob,
    status_code=202,
)
def deploy_latest_from_project_pipeline(
    label_project_id: str,
    request: Request,
    current_user: PipelineWriter,
    settings=Depends(get_settings),
    payload: ProjectDeployRequest | None = None,
) -> AcceptedJob:
    return _queue_project_deployment(
        label_project_id=label_project_id,
        payload=payload or ProjectDeployRequest(),
        request=request,
        current_user=current_user,
        settings=settings,
        event_type="project_pipeline.deploy_latest.requested.v1",
    )


@router.post(
    "/project-pipelines/{label_project_id}/models/{model_family_id}:deploy",
    response_model=AcceptedJob,
    status_code=202,
)
def deploy_pipeline_model(
    label_project_id: str,
    model_family_id: str,
    request: Request,
    current_user: PipelineWriter,
    settings=Depends(get_settings),
    payload: ProjectDeployRequest | None = None,
) -> AcceptedJob:
    deploy_payload = (payload or ProjectDeployRequest()).model_copy(
        update={"model_family_id": model_family_id}
    )
    return _queue_project_deployment(
        label_project_id=label_project_id,
        payload=deploy_payload,
        request=request,
        current_user=current_user,
        settings=settings,
        event_type="project_model.deploy.requested.v1",
    )


def _queue_project_deployment(
    *,
    label_project_id: str,
    payload: ProjectDeployRequest,
    request: Request,
    current_user: SessionUser,
    settings,
    event_type: str,
) -> AcceptedJob:
    deploy_payload = payload
    with session_scope(settings) as session:
        try:
            latest_version = resolve_project_deployment_model_version(
                session,
                label_project_id=label_project_id,
                payload=deploy_payload,
            )
        except ValueError as exc:
            raise PlatformError(
                code="project_deploy_invalid",
                message="The selected model version cannot be deployed.",
                detail=str(exc),
                status_code=422,
            ) from exc
        if latest_version is None:
            latest_trained_version = resolve_project_latest_model_version(
                session,
                label_project_id=label_project_id,
                model_family_id=deploy_payload.model_family_id,
            )
            if latest_trained_version is not None:
                raise PlatformError(
                    code="project_model_not_approved",
                    message="The latest trained model has not passed the deployment quality gates yet.",
                    detail=(
                        "Standard deployment only publishes approved model versions. "
                        "Inspect the training results below, or use the advanced testing override if you intentionally "
                        "want to deploy a non-promotable model in a dev/admin workflow."
                    ),
                    status_code=422,
                )
            raise PlatformError(
                code="project_model_missing",
                message="This project has no trained model version to deploy yet.",
                detail="Create a model and complete at least one successful training run first.",
                status_code=422,
            )
        try:
            _project, target_paths = resolve_project_deployment_targets(
                session,
                label_project_id=label_project_id,
                payload=deploy_payload,
            )
        except ValueError as exc:
            raise PlatformError(
                code="project_deploy_invalid",
                message="The project deployment request could not be created.",
                detail=str(exc),
                status_code=422,
            ) from exc

    _queue(QueueName.DEPLOY, settings).enqueue(
        "control_worker.jobs.orchestration.deploy_project_latest_model",
        label_project_id=label_project_id,
        preferred_endpoint_mode=(
            deploy_payload.preferred_endpoint_mode.value
            if deploy_payload.preferred_endpoint_mode
            else None
        ),
        model_family_id=deploy_payload.model_family_id,
        model_version_id=latest_version.id,
        allow_quality_gate_bypass=deploy_payload.allow_quality_gate_bypass,
        requested_by=current_user.username,
        job_timeout=settings.training.job_timeout_seconds,
        retry=_retry(),
    )
    accepted = AcceptedJob(**queued_job_payload(latest_version.id, QueueName.DEPLOY))
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type=event_type,
        aggregate_type="label_project",
        aggregate_id=label_project_id,
        payload={
            "job_id": accepted.job_id,
            "model_version_id": latest_version.id,
            "model_family_id": deploy_payload.model_family_id,
            "target_paths": [item.value for item in target_paths],
            "allow_quality_gate_bypass": deploy_payload.allow_quality_gate_bypass,
        },
    )
    return accepted


@router.post(
    "/project-pipelines/{label_project_id}:link-labelstudio-predictions",
    response_model=ProjectPredictionBackendSummary,
)
def link_labelstudio_predictions(
    label_project_id: str,
    request: Request,
    current_user: PipelineWriter,
    model_family_id: str | None = None,
    settings=Depends(get_settings),
) -> ProjectPredictionBackendSummary:
    with session_scope(settings) as session:
        try:
            item = ensure_project_prediction_backend(
                session,
                settings=settings,
                label_project_id=label_project_id,
                model_family_id=model_family_id,
            )
        except LabelStudioError as exc:
            raise PlatformError(
                code="labelstudio_prediction_backend_failed",
                message="Label Studio could not connect the prediction backend.",
                detail=str(exc),
                status_code=502,
                retryable=True,
            ) from exc
        except ValueError as exc:
            raise PlatformError(
                code="labelstudio_prediction_backend_invalid",
                message="The Label Studio prediction backend cannot be linked yet.",
                detail=str(exc),
                status_code=422,
            ) from exc
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type="project_pipeline.labelstudio_predictions.linked.v1",
        aggregate_type="label_project",
        aggregate_id=label_project_id,
        payload=item.model_dump(mode="json"),
    )
    return item


def _queue_project_training(
    *,
    label_project_id: str,
    payload: ProjectTrainRequest | None,
    request: Request,
    current_user: SessionUser,
    settings,
    event_type: str,
) -> AcceptedJob:
    with session_scope(settings) as session:
        try:
            training_request = build_project_training_request(
                session,
                label_project_id=label_project_id,
                payload=payload,
            )
            job = create_training_job_request(
                session,
                training_request,
                requested_by=current_user.username,
                source_service="admin-api",
            )
        except ValueError as exc:
            raise PlatformError(
                code="project_training_invalid",
                message="The project training request could not be created.",
                detail=str(exc),
                status_code=422,
            ) from exc

    _queue(QueueName.TRAINING, settings).enqueue(
        "training_worker.jobs.run_training_job",
        training_job_id=job.id,
        job_timeout=settings.training.job_timeout_seconds,
        retry=Retry(max=settings.training.safe_retry_limit, interval=[5, 30, 120]),
    )
    accepted = AcceptedJob(**queued_job_payload(job.id, QueueName.TRAINING))
    write_admin_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type=event_type,
        aggregate_type="label_project",
        aggregate_id=label_project_id,
        payload={
            "job_id": accepted.job_id,
            "dataset_snapshot_id": training_request.dataset_snapshot_id,
            "model_family_slug": training_request.model_family_slug,
            "task_type": training_request.task_type.value,
        },
    )
    return accepted
