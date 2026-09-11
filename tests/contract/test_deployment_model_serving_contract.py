from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize(
    "contract_name",
    ["admin-api.v1.yaml", "deployment-controller.v1.yaml"],
)
def test_deployment_contract_exposes_reversible_model_serving_commands(
    contract_name: str,
) -> None:
    contract = yaml.safe_load(
        (Path("contracts/openapi") / contract_name).read_text(encoding="utf-8")
    )

    assert (
        "/api/v1/deployments/releases/{deployment_release_id}:deactivate"
        in contract["paths"]
    )
    assert (
        "/api/v1/deployments/releases/{deployment_release_id}:activate"
        in contract["paths"]
    )
    schemas = contract["components"]["schemas"]
    assert "deactivated" in schemas["DeploymentReleaseStatus"]["enum"]
    assert "serving_enabled" in schemas["DeploymentReleaseSummary"]["properties"]
    assert "serving_release_id" in schemas["DeploymentReleaseDetail"]["properties"]
    assert "DeploymentModelServingCommandRequest" in schemas


@pytest.mark.parametrize(
    "contract_name",
    ["inference-batch.v1.yaml", "inference-realtime.v1.yaml"],
)
def test_inference_contract_accepts_deactivated_release_status(contract_name: str) -> None:
    contract = yaml.safe_load(
        (Path("contracts/openapi") / contract_name).read_text(encoding="utf-8")
    )

    statuses = contract["components"]["schemas"]["DeploymentReleaseStatus"]["enum"]
    assert "deactivated" in statuses
