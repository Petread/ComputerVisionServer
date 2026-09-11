from cv_core import create_service_app
from fastapi import FastAPI

from camera_media import SERVICE_NAME
from camera_media.api import router
from camera_media.worker import CameraMediaCoordinator


async def _start(app: FastAPI) -> None:
    coordinator = CameraMediaCoordinator(
        settings=app.state.settings,
        instance_id=app.state.instance_id,
    )
    coordinator.start()
    app.state.camera_media_coordinator = coordinator


async def _stop(app: FastAPI) -> None:
    coordinator = getattr(app.state, "camera_media_coordinator", None)
    if coordinator is not None:
        coordinator.stop()


app = create_service_app(
    title="CV Platform Camera Media",
    service_name=SERVICE_NAME,
    api_router=router,
    startup_hooks=[_start],
    shutdown_hooks=[_stop],
)
