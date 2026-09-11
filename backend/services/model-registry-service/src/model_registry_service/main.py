from cv_core import create_service_app

from model_registry_service.api.routes import router

app = create_service_app(
    title="CV Platform Model Registry Service",
    service_name="model-registry-service",
    api_router=router,
)

