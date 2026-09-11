from pathlib import Path

import yaml


def test_edge_agent_openapi_exports_m4_result_stream_and_media_contracts() -> None:
    contract = yaml.safe_load(
        Path("contracts/openapi/edge-agent.v1.yaml").read_text(encoding="utf-8")
    )
    paths = contract["paths"]
    assert "/edge/v1/results/streams" in paths
    assert "/edge/v1/media/viewer-leases" in paths
    assert "/edge/v1/media/preview" in paths
    assert "EdgePreviewFrame" in contract["components"]["schemas"]


def test_runtime_admin_openapi_exports_m4_sync_and_viewer_leases() -> None:
    contract = yaml.safe_load(
        Path("contracts/openapi/runtime-admin-api.v1.yaml").read_text(encoding="utf-8")
    )
    paths = contract["paths"]
    assert "/api/v1/runtime/edge-result-sync" in paths
    assert "/api/v1/runtime/channels/{channel_id}/edge-viewer-leases" in paths
    assert "EdgeResultSyncStatus" in contract["components"]["schemas"]


def test_m4_follow_up_adr_and_hil_report_define_result_and_media_gate() -> None:
    plan = Path("docs/runtime/edge-orchestration-m4-follow-up-plan.md").read_text(
        encoding="utf-8"
    )
    adr = Path("docs/adr/0005-edge-result-sync-and-demand-driven-preview.md").read_text(
        encoding="utf-8"
    )
    hil = Path("docs/runtime/edge-jetson-orin-nano-m4-hil-report.md").read_text(
        encoding="utf-8"
    )
    assert "software_complete" in plan
    assert "pending_hardware" in plan
    assert "existing `result_records` domain" in adr
    assert "lease-driven" in adr
    assert "pending_hardware" in hil
