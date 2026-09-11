from pathlib import Path

import yaml


def test_edge_agent_openapi_exports_m3_admission_and_stop() -> None:
    contract = yaml.safe_load(
        Path("contracts/openapi/edge-agent.v1.yaml").read_text(encoding="utf-8")
    )
    assert "/edge/v1/deployments/{deployment_id}/stop" in contract["paths"]
    state = contract["components"]["schemas"]["EdgeDeploymentState"]["properties"]
    assert "admission_report" in state
    assert "engine_builds" in state


def test_runtime_admin_openapi_exports_m3_release_and_deployment_control() -> None:
    contract = yaml.safe_load(
        Path("contracts/openapi/runtime-admin-api.v1.yaml").read_text(encoding="utf-8")
    )
    paths = contract["paths"]
    assert "/api/v1/runtime/edge-deployment-controller" in paths
    assert "/api/v1/runtime/edge-workload-releases" in paths
    assert "/api/v1/runtime/edge-deployments" in paths
    schemas = contract["components"]["schemas"]
    assert "EdgeAdmissionReport" in schemas
    assert "EdgeWorkloadReleaseDetail" in schemas
    assert "EdgeDeploymentAttemptSummary" in schemas


def test_m3_follow_up_and_hil_report_define_the_two_stage_gate() -> None:
    plan = Path("docs/runtime/edge-orchestration-m3-follow-up-plan.md").read_text(encoding="utf-8")
    adr = Path("docs/adr/0004-edge-immutable-workload-releases-and-local-admission.md").read_text(
        encoding="utf-8"
    )
    hil = Path("docs/runtime/edge-jetson-orin-nano-m3-hil-report.md").read_text(encoding="utf-8")
    assert "software_complete" in plan
    assert "pending_hardware" in plan
    assert "Central admission" in adr
    assert "Local admission" in adr
    assert "pending_hardware" in hil
