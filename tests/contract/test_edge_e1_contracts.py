from pathlib import Path

import yaml
from cv_contracts import EdgePlantTriggerRequest
from pydantic import ValidationError


def test_edge_plant_trigger_contract_is_strict_and_bounded() -> None:
    request = EdgePlantTriggerRequest(payload={"part_id": "4711"}, timeout_ms=1000)
    assert request.schema_version == "edge.plant.trigger.request.v1"

    try:
        EdgePlantTriggerRequest.model_validate(
            {"payload": {}, "timeout_ms": 1000, "unexpected": True}
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("Edge plant trigger contract accepted an unknown field.")


def test_e1_follow_up_and_plant_openapi_are_exported() -> None:
    contract = yaml.safe_load(
        Path("contracts/openapi/edge-plant.v1.yaml").read_text(encoding="utf-8")
    )
    assert "/edge-plant/v1/health" in contract["paths"]
    assert "/edge-plant/v1/channels/{channel_id}/trigger" in contract["paths"]
    assert "EdgePlantTriggerResponse" in contract["components"]["schemas"]

    plan = Path("docs/runtime/edge-orchestration-e1-follow-up-plan.md").read_text(
        encoding="utf-8"
    )
    assert "software_complete" in plan
    assert "pending_hardware" in plan
    assert "TensorRT" in plan
    assert "9444" in plan
