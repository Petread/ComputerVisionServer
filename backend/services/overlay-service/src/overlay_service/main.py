from cv_core import create_service_app

from overlay_service import SERVICE_NAME
from overlay_service.api.routes import router

app = create_service_app(
    title="CV Platform Overlay Service",
    service_name=SERVICE_NAME,
    api_router=router,
)
