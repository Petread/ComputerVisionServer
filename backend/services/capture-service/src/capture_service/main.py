import asyncio

from cv_core import create_service_app
from fastapi import FastAPI

from capture_service import SERVICE_NAME
from capture_service.api.routes import router
from capture_service.executor import capture_service_loop


async def _start_capture_service(app: FastAPI) -> None:
    stop_event = asyncio.Event()
    app.state.capture_service_stop_event = stop_event
    app.state.capture_service_task = asyncio.create_task(
        capture_service_loop(settings=app.state.settings, stop_event=stop_event)
    )


async def _stop_capture_service(app: FastAPI) -> None:
    stop_event = getattr(app.state, "capture_service_stop_event", None)
    if stop_event is not None:
        stop_event.set()
    task = getattr(app.state, "capture_service_task", None)
    if task is not None:
        await task

app = create_service_app(
    title="CV Platform Capture Service",
    service_name=SERVICE_NAME,
    api_router=router,
    startup_hooks=[_start_capture_service],
    shutdown_hooks=[_stop_capture_service],
)
