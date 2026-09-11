from cv_core import create_service_app

from admin_api.api.router import api_router
from admin_api.websockets.job_feed import register_job_feed

app = create_service_app(
    title="CV Platform Admin API",
    service_name="admin-api",
    api_router=api_router,
    configure_app=register_job_feed,
)

