"""Invalid Edge registration must return its validation error as JSON."""

from __future__ import annotations

from cv_contracts.edge import EdgeDeviceRegistrationRequest
from cv_observability.exceptions import install_exception_handlers
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_invalid_edge_network_scope_returns_validation_response() -> None:
    app = FastAPI()
    install_exception_handlers(app)

    @app.post("/edge")
    def register(payload: EdgeDeviceRegistrationRequest) -> dict[str, str]:
        return {"name": payload.name}

    response = TestClient(app).post(
        "/edge",
        json={
            "name": "Edge Test",
            "address": {
                "management_ip": "192.168.50.21",
                "control_port": 9443,
                "plant_api_port": 9444,
                "rtsp_port": 8554,
            },
            "allowed_cidr": "192.168.60.0/24",
            "server_ip": "192.168.60.10",
        },
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "invalid_request"
    assert "management_ip must belong to allowed_cidr" in body["detail"]
    assert body["validation_errors"][0]["message"] == body["detail"]
    assert isinstance(body["metadata"]["errors"][0]["ctx"]["error"], str)
