from cv_core import create_service_app

from pipeline_definition_service import SERVICE_NAME
from pipeline_definition_service.api.routes import router

app = create_service_app(
    title="CV Platform Pipeline Definition Service",
    service_name=SERVICE_NAME,
    api_router=router,
)
