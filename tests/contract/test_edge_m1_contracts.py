from __future__ import annotations

from pathlib import Path

import yaml

EXPECTED_FLEET_PATHS = {
    "/api/v1/runtime/edge-devices",
    "/api/v1/runtime/edge-devices/{edge_device_id}",
    "/api/v1/runtime/edge-devices/{edge_device_id}:reconcile",
}


def test_runtime_admin_openapi_exports_m1_fleet_and_placement_contracts() -> None:
    contract = yaml.safe_load(
        Path("contracts/openapi/runtime-admin-api.v1.yaml").read_text(encoding="utf-8")
    )

    assert EXPECTED_FLEET_PATHS <= set(contract["paths"])
    schemas = contract["components"]["schemas"]
    assert "EdgeDeviceRegistrationRequest" in schemas
    assert "EdgeDeviceDetail" in schemas
    assert "EdgeDeviceReconcileResponse" in schemas
    assert "RuntimeChannelPlacementRequest" in schemas
    assert "placement" in schemas["RuntimeChannelCreateRequest"]["properties"]
    assert "placement" in schemas["RuntimeChannelSummary"]["properties"]


def test_m1_migration_extends_current_head_with_fleet_tables() -> None:
    content = Path("backend/migrations/versions/20260721_0028_edge_fleet_foundations.py").read_text(
        encoding="utf-8"
    )

    assert 'revision = "20260721_0028"' in content
    assert 'down_revision = "20260721_0027"' in content
    for table_name in (
        "edge_devices",
        "edge_capability_snapshots",
        "edge_fleet_commands",
        "edge_result_cursors",
    ):
        assert f'"{table_name}"' in content
    assert '"execution_target"' in content
    assert 'server_default="server"' in content


def test_m1_follow_up_plan_records_scope_failure_semantics_and_gate() -> None:
    content = Path("docs/runtime/edge-orchestration-m1-follow-up-plan.md").read_text(
        encoding="utf-8"
    )
    normalized = content.casefold()

    assert "exactly one historical row exists per fixed management ipv4 address" in normalized
    assert "offline reconciliation retains last reported health" in normalized
    assert "starting an edge-placed channel is therefore rejected explicitly" in normalized
    assert "m1 is software-complete" in normalized
