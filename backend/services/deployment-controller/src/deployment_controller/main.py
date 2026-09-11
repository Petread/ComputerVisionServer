from cv_core import create_service_app

from deployment_controller.api.routes import router
from deployment_controller.lifecycle import start_active_release_reloader, stop_active_release_reloader

app = create_service_app(
    title="CV Platform Deployment Controller",
    service_name="deployment-controller",
    api_router=router,
    startup_hooks=[start_active_release_reloader],
    shutdown_hooks=[stop_active_release_reloader],
)
