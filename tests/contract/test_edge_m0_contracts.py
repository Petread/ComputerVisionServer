from __future__ import annotations

from pathlib import Path

import yaml

EXPECTED_EDGE_PATHS = {
    "/edge/v1/identity",
    "/edge/v1/health",
    "/edge/v1/capabilities",
    "/edge/v1/blobs/{sha256}",
    "/edge/v1/deployments/{deployment_id}/prepare",
    "/edge/v1/deployments/{deployment_id}/commit",
    "/edge/v1/deployments/{deployment_id}/rollback",
    "/edge/v1/deployments/{deployment_id}",
    "/edge/v1/results",
    "/edge/v1/results/ack",
}


def test_edge_agent_openapi_exports_m0_protocol() -> None:
    contract = yaml.safe_load(
        Path("contracts/openapi/edge-agent.v1.yaml").read_text(encoding="utf-8")
    )

    assert contract["info"]["title"] == "YBB Edge Agent"
    assert EXPECTED_EDGE_PATHS <= set(contract["paths"])
    assert "EdgeDeviceIdentity" in contract["components"]["schemas"]
    assert "EdgeControlError" in contract["components"]["schemas"]
    assert "EdgeDeploymentManifest" in contract["components"]["schemas"]
    assert "EdgeResultBatch" in contract["components"]["schemas"]


def test_edge_events_are_declared_in_platform_asyncapi() -> None:
    contract = yaml.safe_load(
        Path("contracts/asyncapi/platform-events.v1.yaml").read_text(encoding="utf-8")
    )

    assert "edge.device.state-changed" in contract["channels"]
    assert "edge.deployment.state-changed" in contract["channels"]
    assert "edge.result.received" in contract["channels"]


def test_edge_follow_up_document_records_fixed_ip_trust_and_hil_gate() -> None:
    content = Path("docs/runtime/edge-orchestration-follow-up-plan.md").read_text(encoding="utf-8")

    assert "isolated_vlan_fixed_ip" in content
    assert "TCP `9443`" in content
    assert "server initiates all management calls" in content
    assert "pending_hardware" in content
    assert "ADR 0001" in content
    assert "ADR 0002" in content
