from cv_core import create_service_app
from fastapi.openapi.utils import get_openapi

from trigger_executor import SERVICE_NAME
from trigger_executor.api.routes import router

app = create_service_app(
    title="CV Platform Trigger Executor",
    service_name=SERVICE_NAME,
    api_router=router,
)


def _custom_openapi() -> dict[str, object]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
        description=app.description,
    )
    security_schemes = schema.setdefault("components", {}).setdefault(
        "securitySchemes",
        {},
    )
    security_schemes["TriggerBearer"] = {"type": "http", "scheme": "bearer"}
    security_schemes["TriggerApiKey"] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
    }
    trigger_operation = schema.get("paths", {}).get(
        "/api/v1/runtime/triggers/{endpoint_slug}",
        {},
    ).get("put")
    if trigger_operation is not None:
        trigger_operation["security"] = [
            {},
            {"TriggerBearer": []},
            {"TriggerApiKey": []},
        ]
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = _custom_openapi
