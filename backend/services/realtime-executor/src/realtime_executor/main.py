from cv_core import create_service_app

from realtime_executor import SERVICE_NAME
from realtime_executor.api.routes import router

app = create_service_app(
    title="CV Platform Realtime Executor",
    service_name=SERVICE_NAME,
    api_router=router,
)
