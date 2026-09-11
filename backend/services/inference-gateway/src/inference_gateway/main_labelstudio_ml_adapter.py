from fastapi import APIRouter, FastAPI

from cv_core import create_service_app

from inference_gateway.api import label_studio_ml_adapter


def _close_gateway(app: FastAPI) -> None:
    service = getattr(app.state, "label_studio_ml_adapter_service", None)
    close = getattr(service, "close", None)
    if callable(close):
        close()
    app.state.label_studio_ml_adapter_service = None

router = APIRouter()
router.include_router(label_studio_ml_adapter.router)

app = create_service_app(
    title="CV Platform Label Studio ML Adapter",
    service_name="label-studio-ml-adapter",
    api_router=router,
    shutdown_hooks=[_close_gateway],
)
