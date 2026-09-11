import asyncio
import os

from cv_core import create_service_app
from fastapi import APIRouter, FastAPI

from runtime_admin_api import SERVICE_NAME
from runtime_admin_api.api.edge_routes import router as edge_router
from runtime_admin_api.api.routes import router
from runtime_admin_api.api.place_routes import router as place_router
from runtime_admin_api.edge_deployment_controller import EdgeDeploymentController
from runtime_admin_api.edge_fleet_reconciler import EdgeFleetReconciler
from runtime_admin_api.edge_result_sync import EdgeResultSynchronizer

api_router = APIRouter()
api_router.include_router(router)
api_router.include_router(edge_router)
api_router.include_router(place_router)


async def _start_edge_fleet_reconciler(app: FastAPI) -> None:
    reconciler = EdgeFleetReconciler.from_environment(app.state.settings)
    stop_event = asyncio.Event()
    app.state.edge_fleet_reconciler = reconciler
    app.state.edge_fleet_reconciler_stop_event = stop_event
    if os.getenv("YBB_EDGE_RECONCILE_ENABLED", "true").lower() in {"1", "true", "yes"}:
        app.state.edge_fleet_reconciler_task = asyncio.create_task(reconciler.run(stop_event))


async def _stop_edge_fleet_reconciler(app: FastAPI) -> None:
    stop_event = getattr(app.state, "edge_fleet_reconciler_stop_event", None)
    if stop_event is not None:
        stop_event.set()
    task = getattr(app.state, "edge_fleet_reconciler_task", None)
    if task is not None:
        await task


async def _start_edge_deployment_controller(app: FastAPI) -> None:
    controller = EdgeDeploymentController.from_environment(app.state.settings)
    stop_event = asyncio.Event()
    app.state.edge_deployment_controller = controller
    app.state.edge_deployment_controller_stop_event = stop_event
    if os.getenv("YBB_EDGE_DEPLOYMENT_ENABLED", "true").lower() in {"1", "true", "yes"}:
        app.state.edge_deployment_controller_task = asyncio.create_task(controller.run(stop_event))


async def _stop_edge_deployment_controller(app: FastAPI) -> None:
    stop_event = getattr(app.state, "edge_deployment_controller_stop_event", None)
    if stop_event is not None:
        stop_event.set()
    task = getattr(app.state, "edge_deployment_controller_task", None)
    if task is not None:
        await task


async def _start_edge_result_synchronizer(app: FastAPI) -> None:
    synchronizer = EdgeResultSynchronizer.from_environment(app.state.settings)
    stop_event = asyncio.Event()
    app.state.edge_result_synchronizer = synchronizer
    app.state.edge_result_synchronizer_stop_event = stop_event
    if os.getenv("YBB_EDGE_RESULT_SYNC_ENABLED", "true").lower() in {"1", "true", "yes"}:
        app.state.edge_result_synchronizer_task = asyncio.create_task(
            synchronizer.run(stop_event)
        )


async def _stop_edge_result_synchronizer(app: FastAPI) -> None:
    stop_event = getattr(app.state, "edge_result_synchronizer_stop_event", None)
    if stop_event is not None:
        stop_event.set()
    task = getattr(app.state, "edge_result_synchronizer_task", None)
    if task is not None:
        await task


app = create_service_app(
    title="CV Platform Runtime Admin API",
    service_name=SERVICE_NAME,
    api_router=api_router,
    startup_hooks=[
        _start_edge_fleet_reconciler,
        _start_edge_deployment_controller,
        _start_edge_result_synchronizer,
    ],
    shutdown_hooks=[
        _stop_edge_result_synchronizer,
        _stop_edge_deployment_controller,
        _stop_edge_fleet_reconciler,
    ],
)
