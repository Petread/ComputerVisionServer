import asyncio

from cv_core import create_service_app
from fastapi import FastAPI

from connector_service import SERVICE_NAME
from connector_service.api.routes import router
from connector_service.executor import connector_service_loop


async def _start_connector_service(app: FastAPI) -> None:
    stop_event = asyncio.Event()
    app.state.connector_service_stop_event = stop_event
    app.state.connector_service_task = asyncio.create_task(
        connector_service_loop(settings=app.state.settings, stop_event=stop_event)
    )


async def _stop_connector_service(app: FastAPI) -> None:
    stop_event = getattr(app.state, "connector_service_stop_event", None)
    if stop_event is not None:
        stop_event.set()
    task = getattr(app.state, "connector_service_task", None)
    if task is not None:
        await task

app = create_service_app(
    title="CV Platform Connector Service",
    service_name=SERVICE_NAME,
    api_router=router,
    startup_hooks=[_start_connector_service],
    shutdown_hooks=[_stop_connector_service],
)
