from __future__ import annotations

import pytest
from control_worker.jobs.dataset import provision_labelstudio_project
from cv_datasets import LabelStudioError
from cv_db import AuditEvent, Dataset, LabelProject
from cv_status import DatasetStatus, TaskType
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.conftest import build_test_settings, configure_sqlite_factories


class _FakeLabelStudioClient:
    def __init__(self) -> None:
        self.projects: dict[int, dict[str, object]] = {}
        self.create_calls = 0
        self.update_calls = 0
        self.deleted_project_ids: list[int] = []

    def list_projects(self) -> list[dict[str, object]]:
        return list(self.projects.values())

    def create_project(self, *, title: str, label_config: str, description: str | None = None):
        self.create_calls += 1
        project = {
            "id": 71,
            "title": title,
            "label_config": label_config,
            "description": description,
        }
        self.projects[71] = project
        return project

    def get_project(self, project_id: int):
        return self.projects[project_id]

    def update_project(
        self,
        project_id: int,
        *,
        title: str,
        label_config: str,
        description: str | None = None,
    ):
        self.update_calls += 1
        project = {
            "id": project_id,
            "title": title,
            "label_config": label_config,
            "description": description,
        }
        self.projects[project_id] = project
        return project

    def delete_project(self, project_id: int) -> None:
        self.deleted_project_ids.append(project_id)
        self.projects.pop(project_id, None)


def _request() -> dict[str, object]:
    return {
        "dataset_id": "dst_temporal",
        "title": "Stem orientation sequences",
        "annotation_schema_id": "stem_orientation_angle.v1",
        "training_pipeline_slug": "stem-orientation-baseline",
        "auto_sync_enabled": True,
        "auto_snapshot_enabled": True,
        "minimum_annotation_count": 3,
        "train_ratio": 80,
        "val_ratio": 10,
        "test_ratio": 10,
        "class_balance_strategy": "none",
    }


def _prepare(monkeypatch, sqlite_engine, tmp_path):
    settings = build_test_settings("control-worker", tmp_path)
    configure_sqlite_factories(sqlite_engine, "control-worker")
    with Session(sqlite_engine) as session:
        session.add(
            Dataset(
                id="dst_temporal",
                slug="stem-orientation-sequences",
                name="Stem orientation sequences",
                task_type=TaskType.TEMPORAL_SEQUENCE.value,
                label_schema_version="stem_orientation_angle.v1",
                status=DatasetStatus.READY.value,
            )
        )
        session.commit()
    client = _FakeLabelStudioClient()
    monkeypatch.setattr(
        "control_worker.jobs.dataset.load_platform_settings",
        lambda _service: settings,
    )
    monkeypatch.setattr(
        "control_worker.jobs.dataset._label_studio_client",
        lambda _settings: client,
    )
    return client


def test_provisioning_is_idempotent_and_persists_registry_config(
    monkeypatch,
    sqlite_engine,
    tmp_path,
) -> None:
    client = _prepare(monkeypatch, sqlite_engine, tmp_path)

    first = provision_labelstudio_project(_request(), requested_by="label-manager")
    second = provision_labelstudio_project(_request(), requested_by="label-manager")

    assert first["created"] is True
    assert second["created"] is False
    assert first["label_studio_project_id"] == second["label_studio_project_id"] == 71
    assert client.create_calls == 1
    assert client.update_calls == 1
    label_config = str(client.projects[71]["label_config"])
    assert '<Video name="video" value="$video"' in label_config
    assert '<TimelineLabels name="target_orientation" toName="video">' in label_config
    for field_name in (
        "sequence_valid",
        "bow_relevance",
        "target_visible",
        "final_orientation_quality",
        "special_case",
        "comment",
    ):
        assert f'name="{field_name}"' in label_config

    with Session(sqlite_engine) as session:
        projects = session.scalars(select(LabelProject)).all()
        assert len(projects) == 1
        project = projects[0]
        assert project.dataset_id == "dst_temporal"
        assert project.label_schema_version == "stem_orientation_angle.v1"
        assert project.auto_sync_enabled is True
        assert project.auto_snapshot_enabled is True
        assert project.label_config["config_sha256"]
        assert project.label_config["provisioning"]["marker_version"] == "v1"
        audit_count = session.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.event_type == "label_project.provisioned.v1"
            )
        )
        assert audit_count == 2


def test_provisioning_compensates_mapping_failure_and_audits_error(
    monkeypatch,
    sqlite_engine,
    tmp_path,
) -> None:
    client = _prepare(monkeypatch, sqlite_engine, tmp_path)

    def fail_mapping(*args, **kwargs):
        raise RuntimeError("database mapping failed")

    monkeypatch.setattr(
        "control_worker.jobs.dataset.upsert_provisioned_label_project",
        fail_mapping,
    )

    try:
        provision_labelstudio_project(_request(), requested_by="label-manager")
    except RuntimeError as exc:
        assert str(exc) == "database mapping failed"
    else:
        raise AssertionError("provisioning failure was not propagated")

    assert client.deleted_project_ids == [71]
    assert client.projects == {}
    with Session(sqlite_engine) as session:
        assert session.scalar(select(func.count(LabelProject.id))) == 0
        failure = session.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == "label_project.provisioning_failed.v1"
            )
        )
        assert failure is not None
        assert failure.payload["stage"] == "persist_mapping"
        assert failure.payload["compensation_status"] == "deleted_created_project"


def test_provisioning_audits_label_studio_client_failure(
    monkeypatch,
    sqlite_engine,
    tmp_path,
) -> None:
    client = _prepare(monkeypatch, sqlite_engine, tmp_path)

    def fail_create(*, title: str, label_config: str, description: str | None = None):
        raise LabelStudioError("Label Studio unavailable", status_code=503)

    monkeypatch.setattr(client, "create_project", fail_create)

    with pytest.raises(LabelStudioError, match="Label Studio unavailable"):
        provision_labelstudio_project(_request(), requested_by="label-manager")

    assert client.deleted_project_ids == []
    with Session(sqlite_engine) as session:
        failure = session.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == "label_project.provisioning_failed.v1"
            )
        )
        assert failure is not None
        assert failure.payload["stage"] == "create_project"
        assert failure.payload["compensation_status"] == "not_required"
