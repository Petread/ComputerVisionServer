from cv_core import create_service_app

from channel_service import SERVICE_NAME
from channel_service.api.routes import router

app = create_service_app(
    title="CV Platform Channel Service",
    service_name=SERVICE_NAME,
    api_router=router,
)
