from cv_core import create_service_app

from training_orchestrator.api.routes import router

app = create_service_app(
    title="CV Platform Training Orchestrator",
    service_name="training-orchestrator",
    api_router=router,
)

