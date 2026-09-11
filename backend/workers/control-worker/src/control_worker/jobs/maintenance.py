from __future__ import annotations

from cv_utils import utc_now_iso


def collect_service_health() -> dict[str, str]:
    return {"status": "collected", "finished_at": utc_now_iso()}


def reconcile_abandoned_jobs() -> dict[str, str]:
    return {"status": "reconciled", "finished_at": utc_now_iso()}

