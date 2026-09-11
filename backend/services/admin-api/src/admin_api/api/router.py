from fastapi import APIRouter

from admin_api.api.routes import (
    access,
    audit,
    auth,
    batch,
    datasets,
    deployments,
    operations,
    project_pipelines,
    registry,
    settings,
    system,
    training,
    uploads,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(access.router)
api_router.include_router(system.router)
api_router.include_router(datasets.router)
api_router.include_router(project_pipelines.router)
api_router.include_router(uploads.router)
api_router.include_router(training.router)
api_router.include_router(registry.router)
api_router.include_router(deployments.router)
api_router.include_router(batch.router)
api_router.include_router(audit.router)
api_router.include_router(settings.router)
api_router.include_router(operations.router)
