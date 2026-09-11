from pathlib import Path

import yaml


def test_edge_agent_openapi_exports_m2_runtime_status() -> None:
    contract = yaml.safe_load(
        Path("contracts/openapi/edge-agent.v1.yaml").read_text(encoding="utf-8")
    )
    assert "/edge/v1/runtime" in contract["paths"]
    assert "EdgeRuntimeStatus" in contract["components"]["schemas"]
    assert "EdgeStorageQuotaStatus" in contract["components"]["schemas"]


def test_runtime_admin_openapi_exports_continuous_reconciler_status() -> None:
    contract = yaml.safe_load(
        Path("contracts/openapi/runtime-admin-api.v1.yaml").read_text(encoding="utf-8")
    )
    assert "/api/v1/runtime/edge-reconciler" in contract["paths"]
    assert "EdgeFleetReconcilerStatus" in contract["components"]["schemas"]


def test_m2_follow_up_and_offline_installers_capture_operational_gate() -> None:
    plan = Path("docs/runtime/edge-orchestration-m2-follow-up-plan.md").read_text(
        encoding="utf-8"
    )
    assert "journal_mode=WAL" in plan
    assert "pending_hardware" in plan
    assert "Übergang zu M3" in plan

    jetson = Path("deployment/edge/install-jetson-offline.sh").read_text(encoding="utf-8")
    windows = Path("deployment/edge/Install-WindowsOffline.ps1").read_text(encoding="utf-8")
    assert "--no-index" in jetson
    assert "ybb-edge-watchdog.timer" in jetson
    assert "--no-index" in windows
    assert "New-NetFirewallRule" in windows
