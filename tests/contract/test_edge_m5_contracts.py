from pathlib import Path

import pytest
import yaml
from cv_contracts import EdgeDeploymentPreflightRequest, EdgeDiagnosticReport
from pydantic import ValidationError


def test_runtime_admin_openapi_exports_m5_operator_workflows() -> None:
    contract = yaml.safe_load(
        Path("contracts/openapi/runtime-admin-api.v1.yaml").read_text(encoding="utf-8")
    )
    paths = contract["paths"]
    assert "/api/v1/runtime/edge-operator/deployments:preflight" in paths
    assert "/api/v1/runtime/edge-operator/deployments" in paths
    assert "/api/v1/runtime/edge-deployments/{edge_deployment_id}:rollback" in paths
    assert "/api/v1/runtime/edge-devices/{edge_device_id}/diagnostics" in paths
    assert "/api/v1/runtime/edge-devices/{edge_device_id}/diagnostics/latest" in paths
    assert "/api/v1/runtime/edge-fleet/health" in paths
    schemas = contract["components"]["schemas"]
    assert "EdgeDeploymentPreflightReport" in schemas
    assert "EdgeDiagnosticReport" in schemas
    assert "EdgeFleetHealthOverview" in schemas
    assert "EdgeRollbackResponse" in schemas


def test_m5_contracts_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        EdgeDeploymentPreflightRequest.model_validate(
            {"channel_id": "chn_test", "precision": "fp16", "url": "http://elsewhere"}
        )
    with pytest.raises(ValidationError):
        EdgeDiagnosticReport.model_validate(
            {
                "diagnostic_report_id": "edgr_test",
                "edge_device_id": "edg_test",
                "edge_device_name": "Orin",
                "status": "healthy",
                "generated_at": "2026-07-22T00:00:00Z",
                "checks": [],
                "channels": [],
                "unexpected": True,
            }
        )


def test_m5_follow_up_adr_and_hil_report_define_operator_gate() -> None:
    plan = Path("docs/runtime/edge-orchestration-m5-follow-up-plan.md").read_text(
        encoding="utf-8"
    )
    adr = Path(
        "docs/adr/0006-edge-operator-preflight-diagnostics-and-rollback.md"
    ).read_text(encoding="utf-8")
    hil = Path("docs/runtime/edge-jetson-orin-nano-m5-hil-report.md").read_text(
        encoding="utf-8"
    )
    assert "software_complete" in plan
    assert "pending_hardware" in plan
    assert "immutable, capability-bound workload" in adr
    assert "Idempotent retries" in adr
    assert "pending_hardware" in hil
