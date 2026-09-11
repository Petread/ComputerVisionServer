from cv_core import create_service_app

from dataset_service.api.routes import router

app = create_service_app(
    title="CV Platform Dataset Service",
    service_name="dataset-service",
    api_router=router,
)

