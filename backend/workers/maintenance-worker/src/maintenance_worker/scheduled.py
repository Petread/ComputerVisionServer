from __future__ import annotations

import json
import logging
from collections.abc import Callable

from cv_utils import utc_now_iso

from maintenance_worker.jobs import (
    artifact_retention,
    checkpoint_pruning,
    cleanup_stale_locks,
    reconcile_training_jobs,
    runtime_result_retention,
    training_workspace_retention,
)

LOGGER = logging.getLogger("maintenance-worker.scheduled")


def run_scheduled_maintenance() -> dict[str, object]:
    tasks: tuple[tuple[str, Callable[[], dict[str, object]]], ...] = (
        ("reconcile_training_jobs", reconcile_training_jobs),
        ("cleanup_stale_locks", cleanup_stale_locks),
        ("runtime_result_retention", runtime_result_retention),
        ("checkpoint_pruning", checkpoint_pruning),
        ("training_workspace_retention", training_workspace_retention),
        ("artifact_retention", artifact_retention),
    )
    results: dict[str, object] = {}
    failures: dict[str, str] = {}
    for name, task in tasks:
        try:
            results[name] = task()
        except Exception as exc:  # pragma: no cover - system integration guard
            LOGGER.exception("Scheduled maintenance task %s failed.", name)
            failures[name] = str(exc)
    return {
        "status": "completed" if not failures else "completed_with_errors",
        "results": results,
        "failures": failures,
        "finished_at": utc_now_iso(),
    }


def main() -> None:
    result = run_scheduled_maintenance()
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
