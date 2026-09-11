from cv_core import create_service_app

from result_service import SERVICE_NAME
from result_service.api.routes import router

app = create_service_app(
    title="CV Platform Result Service",
    service_name=SERVICE_NAME,
    api_router=router,
)
