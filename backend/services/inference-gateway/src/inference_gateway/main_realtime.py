from fastapi import APIRouter, FastAPI

from cv_core import create_service_app

from inference_gateway.api import public_realtime, realtime
from inference_gateway.runtime.lifecycle import start_local_model_reconciler, stop_local_model_reconciler


def _close_gateway(app: FastAPI) -> None:
    service = getattr(app.state, "realtime_gateway_service", None)
    close = getattr(service, "close", None)
    if callable(close):
        close()
    app.state.realtime_gateway_service = None

router = APIRouter()
router.include_router(public_realtime.router)
router.include_router(realtime.router)

app = create_service_app(
    title="CV Platform Inference Gateway Realtime",
    service_name="inference-gateway-rt",
    api_router=router,
    startup_hooks=[start_local_model_reconciler],
    shutdown_hooks=[stop_local_model_reconciler, _close_gateway],
)
