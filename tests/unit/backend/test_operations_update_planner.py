from __future__ import annotations

from operations_agent.planner import classify_change


def test_realtime_executor_source_changes_restart_live_worker_owner() -> None:
    change = classify_change(
        "backend/services/realtime-executor/src/realtime_executor/live.py",
        change_type="modified",
    )

    assert change.restart_required is True
    assert "realtime-executor" in change.affected_services
    assert "runtime-orchestrator" in change.affected_services
