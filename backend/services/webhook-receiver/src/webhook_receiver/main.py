from cv_core import create_service_app

from webhook_receiver.api.routes import router

app = create_service_app(
    title="CV Platform Webhook Receiver",
    service_name="webhook-receiver",
    api_router=router,
)

