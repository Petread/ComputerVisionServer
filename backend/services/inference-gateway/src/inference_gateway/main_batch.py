from fastapi import APIRouter, FastAPI

from cv_core import create_service_app

from inference_gateway.api import batch, public_batch
from inference_gateway.runtime.lifecycle import start_local_model_reconciler, stop_local_model_reconciler


def _close_gateway(app: FastAPI) -> None:
    service = getattr(app.state, "batch_gateway_service", None)
    close = getattr(service, "close", None)
    if callable(close):
        close()
    app.state.batch_gateway_service = None

router = APIRouter()
router.include_router(public_batch.router)
router.include_router(batch.router)

app = create_service_app(
    title="CV Platform Inference Gateway Batch",
    service_name="inference-gateway-batch",
    api_router=router,
    startup_hooks=[start_local_model_reconciler],
    shutdown_hooks=[stop_local_model_reconciler, _close_gateway],
)
