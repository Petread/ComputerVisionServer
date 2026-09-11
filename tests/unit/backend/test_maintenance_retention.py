from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cv_db import ResultRecord, RuntimeChannel, session_scope
from maintenance_worker import jobs
from maintenance_worker.jobs import _prune_backup_sets_by_age
from sqlalchemy import func, select
from tests.conftest import build_test_settings, configure_sqlite_factories


def test_backup_retention_removes_only_expired_managed_backup_sets(tmp_path: Path) -> None:
    backup_root = tmp_path / "backups"
    old_backup = backup_root / "metadata_20260101T000000Z"
    current_backup = backup_root / "metadata_20260720T000000Z"
    unrelated = backup_root / "manual-do-not-delete"
    for directory in (old_backup, current_backup, unrelated):
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text("{}", encoding="utf-8")

    now = datetime(2026, 7, 20, tzinfo=UTC)
    old_timestamp = (now - timedelta(days=90)).timestamp()
    os.utime(old_backup, (old_timestamp, old_timestamp))

    removed = _prune_backup_sets_by_age(
        root=backup_root,
        cutoff=now - timedelta(days=30),
    )

    assert removed == [str(old_backup)]
    assert not old_backup.exists()
    assert current_backup.exists()
    assert unrelated.exists()


def test_runtime_retention_bounds_forced_shadow_history_when_channel_history_is_disabled(
    sqlite_engine,
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = build_test_settings("maintenance-worker", tmp_path)
    configure_sqlite_factories(sqlite_engine, "maintenance-worker")
    monkeypatch.setattr(jobs, "load_platform_settings", lambda _service_name: settings)
    now = datetime.now(UTC)
    with session_scope(settings) as session:
        session.add(
            RuntimeChannel(
                id="chn_shadow_retention",
                slug="shadow-retention",
                name="Shadow retention",
                technical_place="longitudinal-conveyor-1",
                mode="realtime",
                result_history_policy={
                    "enabled": False,
                    "retention_hours": 24,
                    "sample_interval_seconds": 60,
                    "max_records": 100,
                },
            )
        )
        for index in range(150):
            session.add(
                ResultRecord(
                    id=f"rrc_shadow_retention_{index}",
                    channel_id="chn_shadow_retention",
                    pipeline_version_id="plv_shadow_retention",
                    runtime_execution_id="rex_shadow_retention",
                    correlation_id=f"corr_shadow_retention_{index}",
                    completed_at=now - timedelta(seconds=150 - index),
                    mode="realtime",
                    status="completed",
                    result_type="stem_orientation",
                    schema_version="runtime.execution.result.v1",
                    result={},
                    trigger_response={
                        "schema_version": "stem.orientation.v3",
                        "session_id": f"session-{index}",
                        "shadow_mode": True,
                    },
                    history_retained=True,
                )
            )

    outcome = jobs.runtime_result_retention()

    with session_scope(settings) as session:
        remaining = session.scalar(
            select(func.count(ResultRecord.id)).where(
                ResultRecord.channel_id == "chn_shadow_retention"
            )
        )
    assert outcome["deleted_result_count"] == 50
    assert remaining == 100
