import asyncio

from cv_core import create_service_app

from runtime_orchestrator import SERVICE_NAME
from runtime_orchestrator.api.routes import router
from runtime_orchestrator.supervisor import (
    RuntimeControlOrchestrator,
    RuntimeExecutorSupervisor,
    runtime_control_loop,
)


async def _start_runtime_control(app) -> None:
    supervisor = RuntimeExecutorSupervisor(
        settings=app.state.settings,
        orchestrator_instance_id=app.state.instance_id,
    )
    orchestrator = RuntimeControlOrchestrator(
        settings=app.state.settings,
        orchestrator_instance_id=app.state.instance_id,
        supervisor=supervisor,
    )
    stop_event = asyncio.Event()
    app.state.runtime_executor_supervisor = supervisor
    app.state.runtime_control_orchestrator = orchestrator
    app.state.runtime_control_stop_event = stop_event
    app.state.runtime_control_task = asyncio.create_task(
        runtime_control_loop(orchestrator, stop_event=stop_event)
    )


async def _stop_runtime_control(app) -> None:
    stop_event = getattr(app.state, "runtime_control_stop_event", None)
    if stop_event is not None:
        stop_event.set()
    task = getattr(app.state, "runtime_control_task", None)
    if task is not None:
        await task
    supervisor = getattr(app.state, "runtime_executor_supervisor", None)
    if supervisor is not None:
        supervisor.shutdown()

app = create_service_app(
    title="CV Platform Runtime Orchestrator",
    service_name=SERVICE_NAME,
    api_router=router,
    startup_hooks=[_start_runtime_control],
    shutdown_hooks=[_stop_runtime_control],
)
