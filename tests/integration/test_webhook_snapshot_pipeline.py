from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from admin_api.main import app as admin_app
from control_worker.jobs.dataset import (
    _hydrate_label_studio_assets,
    create_labelstudio_snapshot_export,
    dvc_track_dataset_snapshot,
    finalize_upload_session,
    materialize_dataset_snapshot,
    poll_labelstudio_snapshot_export,
    sync_uploaded_assets_to_labelstudio,
    validate_dataset_snapshot,
)
from control_worker.jobs.webhook import process_labelstudio_event
from cv_datasets import LabelStudioError
from cv_db import Dataset, DatasetSnapshot, LabelProject, WebhookEvent
from cv_db import list_dataset_assets_for_dataset
from cv_status import DatasetStatus, LabelProjectStatus, SnapshotStatus, TaskType
from cv_utils import utc_now
from tests.conftest import build_test_settings, configure_sqlite_factories
from tests.support.scenario_helpers import FakeLabelStudioClient, login_admin
from webhook_receiver.main import app as webhook_app


class _QueuedJobs:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, object]]] = []

    def enqueue(self, job_name: str, **kwargs) -> None:
        self.items.append((job_name, kwargs))

    def enqueue_in(self, *_args, **kwargs) -> None:
        self.items.append((str(kwargs.get("job_name") or kwargs.get("func") or ""), kwargs))


class _WebhookQueue:
    items: list[tuple[str, dict[str, object]]] = []

    def __init__(self, *_args, **_kwargs) -> None:
        return None

    def enqueue(self, job_name: str, **kwargs) -> None:
        self.items.append((job_name, kwargs))


def test_uploaded_assets_flow_to_labelstudio_and_ready_snapshot(
    sqlite_engine,
    tmp_path: Path,
    monkeypatch,
    sample_png_bytes: bytes,
) -> None:
    configure_sqlite_factories(
        sqlite_engine,
        "admin-api",
        "control-worker",
        "webhook-receiver",
    )
    admin_settings = build_test_settings("admin-api", tmp_path)
    worker_settings = build_test_settings("control-worker", tmp_path)
    webhook_settings = build_test_settings("webhook-receiver", tmp_path)
    webhook_settings.label_studio.webhook_auth.enabled = True

    admin_app.state.settings = admin_settings
    webhook_app.state.settings = webhook_settings

    fake_label_studio = FakeLabelStudioClient(
        projects=[
            {
                "id": 501,
                "title": "Widget Labels",
                "task_number": 0,
                "finished_task_number": 0,
                "num_tasks_with_annotations": 0,
                "useful_annotation_number": 0,
                "skipped_annotations_number": 0,
            }
        ]
    )
    queued_jobs = _QueuedJobs()
    _WebhookQueue.items.clear()

    monkeypatch.setattr("control_worker.jobs.dataset.load_platform_settings", lambda _service: worker_settings)
    monkeypatch.setattr("control_worker.jobs.webhook.load_platform_settings", lambda _service: worker_settings)
    monkeypatch.setattr("control_worker.jobs.dataset._label_studio_client", lambda _settings: fake_label_studio)
    monkeypatch.setattr("control_worker.jobs.dataset._dataset_queue", lambda _settings: queued_jobs)
    monkeypatch.setattr("control_worker.jobs.webhook._dataset_queue", lambda _settings: queued_jobs)
    monkeypatch.setattr("admin_api.api.routes.uploads._queue", lambda _settings: queued_jobs)
    monkeypatch.setattr("webhook_receiver.api.routes.Redis.from_url", lambda _url: object())
    monkeypatch.setattr("webhook_receiver.api.routes.Queue", _WebhookQueue)

    with TestClient(admin_app) as client:
        csrf_token = login_admin(client)
        dataset_response = client.post(
            "/api/v1/datasets",
            json={
                "name": "Integration Dataset",
                "slug": "integration-dataset",
                "task_type": "classification",
                "label_schema_version": "1.0.0",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert dataset_response.status_code == 200
        dataset_id = str(dataset_response.json()["dataset_id"])

        label_project_response = client.post(
            "/api/v1/label-projects",
            json={
                "dataset_id": dataset_id,
                "label_studio_project_id": 501,
                "title": "Widget Labels",
                "task_type": "classification",
                "training_pipeline_slug": "widget-inspection-cls",
                "auto_snapshot_enabled": True,
                "minimum_annotation_count": 1,
                "required_classes": ["accepted"],
                "qualifying_actions": ["ANNOTATION_CREATED"],
                "label_config": {"choices": [{"value": "accepted"}]},
                "train_ratio": 80,
                "val_ratio": 10,
                "test_ratio": 10,
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert label_project_response.status_code == 200
        label_project_id = str(label_project_response.json()["label_project_id"])

        upload_session_response = client.post(
            f"/api/v1/datasets/{dataset_id}/upload-sessions",
            json={
                "expected_file_count": 1,
                "target_label_project_id": label_project_id,
                "classification_label": "accepted",
                "source_folder_name": "accepted",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert upload_session_response.status_code == 200
        upload_session_id = str(upload_session_response.json()["upload_session_id"])

        upload_response = client.put(
            f"/api/v1/upload-sessions/{upload_session_id}/parts/1",
            content=sample_png_bytes,
            headers={
                "X-CSRF-Token": csrf_token,
                "X-Upload-Filename": "widget-001.png",
                "Content-Type": "image/png",
            },
        )
        assert upload_response.status_code == 200

        complete_response = client.post(
            f"/api/v1/upload-sessions/{upload_session_id}:complete",
            json={},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert complete_response.status_code == 202

    finalize_upload_session(upload_session_id)
    sync_uploaded_assets_to_labelstudio(upload_session_id)

    assert len(fake_label_studio.imported_tasks) == 1
    project_id, imported_tasks = fake_label_studio.imported_tasks[0]
    assert project_id == 501
    assert len(imported_tasks) == 1
    assert imported_tasks[0]["annotations"] == [
        {
            "ground_truth": True,
            "was_cancelled": False,
            "result": [
                {
                    "from_name": "choice",
                    "to_name": "image",
                    "type": "choices",
                    "value": {"choices": ["accepted"]},
                }
            ],
        }
    ]

    with TestClient(admin_app) as client:
        csrf_token = login_admin(client)
        duplicate_session_response = client.post(
            f"/api/v1/datasets/{dataset_id}/upload-sessions",
            json={
                "expected_file_count": 1,
                "target_label_project_id": label_project_id,
                "classification_label": "accepted",
                "source_folder_name": "accepted",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert duplicate_session_response.status_code == 200
        duplicate_session_id = str(duplicate_session_response.json()["upload_session_id"])

        duplicate_upload_response = client.put(
            f"/api/v1/upload-sessions/{duplicate_session_id}/parts/1",
            content=sample_png_bytes,
            headers={
                "X-CSRF-Token": csrf_token,
                "X-Upload-Filename": "widget-001-duplicate.png",
                "Content-Type": "image/png",
            },
        )
        assert duplicate_upload_response.status_code == 200

        duplicate_complete_response = client.post(
            f"/api/v1/upload-sessions/{duplicate_session_id}:complete",
            json={},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert duplicate_complete_response.status_code == 202

    finalize_upload_session(duplicate_session_id)
    sync_uploaded_assets_to_labelstudio(duplicate_session_id)
    assert len(fake_label_studio.imported_tasks) == 2
    duplicate_project_id, duplicate_imported_tasks = fake_label_studio.imported_tasks[1]
    assert duplicate_project_id == 501
    assert len(duplicate_imported_tasks) == 1
    assert duplicate_imported_tasks[0]["meta"]["asset_id"] == imported_tasks[0]["meta"]["asset_id"]
    assert duplicate_imported_tasks[0]["annotations"] == imported_tasks[0]["annotations"]

    fake_label_studio.export_payload = [
        {
            "id": 1,
            "data": dict(imported_tasks[0]["data"]),
            "annotations": list(imported_tasks[0]["annotations"]),
        }
    ]
    fake_label_studio.export_snapshot = {"id": 7, "status": "completed"}

    with TestClient(webhook_app) as client:
        webhook_response = client.post(
            "/api/v1/webhooks/label-studio",
            json={
                "action": "ANNOTATION_CREATED",
                "project_id": 501,
                "annotation": {"id": 11, "result": [{"value": {"choices": ["accepted"]}}]},
            },
            headers={"X-Label-Studio-Signature": "labelstudio-webhook-secret"},
        )

    assert webhook_response.status_code == 200
    event_id = str(webhook_response.json()["event_id"])
    process_labelstudio_event(event_id)

    with Session(sqlite_engine, expire_on_commit=False) as session:
        snapshot = session.scalar(select(DatasetSnapshot))
        assert snapshot is not None
        snapshot_id = snapshot.id

    create_labelstudio_snapshot_export(snapshot_id)
    poll_labelstudio_snapshot_export(snapshot_id)
    materialize_dataset_snapshot(snapshot_id)
    validate_dataset_snapshot(snapshot_id)
    dvc_track_dataset_snapshot(snapshot_id)

    with Session(sqlite_engine, expire_on_commit=False) as session:
        snapshot = session.get(DatasetSnapshot, snapshot_id)
        dataset = session.get(Dataset, dataset_id)
        event = session.get(WebhookEvent, event_id)

    assert snapshot is not None
    assert snapshot.status == SnapshotStatus.READY.value
    assert snapshot.item_count == 1
    assert snapshot.annotation_count == 1
    assert snapshot.manifest_uri
    assert Path(snapshot.manifest_uri).exists()
    assert (worker_settings.storage.snapshot_root(snapshot_id) / "label_map.json").exists()
    assert (Path(worker_settings.dvc.repo_root) / "manifests" / f"{snapshot_id}.json").exists()
    assert dataset is not None
    assert dataset.status == DatasetStatus.SNAPSHOTTED.value
    assert event is not None
    assert event.processing_status == "processed"


def test_duplicate_webhook_does_not_duplicate_snapshot_side_effects(
    sqlite_engine,
    tmp_path: Path,
    monkeypatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "control-worker", "webhook-receiver")
    worker_settings = build_test_settings("control-worker", tmp_path)
    webhook_settings = build_test_settings("webhook-receiver", tmp_path)
    webhook_settings.label_studio.webhook_auth.enabled = True
    webhook_app.state.settings = webhook_settings

    queued_jobs = _QueuedJobs()
    _WebhookQueue.items.clear()
    monkeypatch.setattr("control_worker.jobs.webhook.load_platform_settings", lambda _service: worker_settings)
    monkeypatch.setattr("control_worker.jobs.webhook._dataset_queue", lambda _settings: queued_jobs)
    monkeypatch.setattr("webhook_receiver.api.routes.Redis.from_url", lambda _url: object())
    monkeypatch.setattr("webhook_receiver.api.routes.Queue", _WebhookQueue)

    with Session(sqlite_engine, expire_on_commit=False) as session:
        dataset = Dataset(
            id="dst_dupe",
            slug="duplicate-webhook",
            name="Duplicate Webhook",
            task_type=TaskType.CLASSIFICATION.value,
            label_schema_version="1",
            status=DatasetStatus.READY.value,
            created_by="tester",
        )
        label_project = LabelProject(
            id="lpr_dupe",
            dataset_id=dataset.id,
            label_studio_project_id=900,
            title="Duplicate Project",
            task_type=TaskType.CLASSIFICATION.value,
            label_config={"choices": [{"value": "accepted"}]},
            label_schema_version="1",
            training_pipeline_slug="widget-inspection-cls",
            status=LabelProjectStatus.ACTIVE.value,
            auto_snapshot_enabled=True,
            minimum_annotation_count=1,
            required_classes=["accepted"],
            train_ratio=80,
            val_ratio=10,
            test_ratio=10,
            qualifying_actions=["ANNOTATION_CREATED"],
            task_count=1,
            annotated_task_count=1,
            annotation_count=1,
            class_distribution={"accepted": 1},
            last_activity_at=utc_now(),
        )
        session.add_all([dataset, label_project])
        session.commit()

    payload = {
        "action": "ANNOTATION_CREATED",
        "project_id": 900,
        "annotation": {"id": 77, "result": [{"value": {"choices": ["accepted"]}}]},
    }

    with TestClient(webhook_app) as client:
        first = client.post(
            "/api/v1/webhooks/label-studio",
            json=payload,
            headers={"X-Label-Studio-Signature": "labelstudio-webhook-secret"},
        )
        second = client.post(
            "/api/v1/webhooks/label-studio",
            json=payload,
            headers={"X-Label-Studio-Signature": "labelstudio-webhook-secret"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True

    process_labelstudio_event(str(first.json()["event_id"]))

    with Session(sqlite_engine, expire_on_commit=False) as session:
        snapshot_count = session.scalar(select(func.count(DatasetSnapshot.id)))

    assert snapshot_count == 1


def test_materialize_dataset_snapshot_marks_snapshot_failed_on_asset_download_error(
    sqlite_engine,
    tmp_path: Path,
    monkeypatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "control-worker")
    worker_settings = build_test_settings("control-worker", tmp_path)
    monkeypatch.setattr(
        "control_worker.jobs.dataset.load_platform_settings", lambda _service: worker_settings
    )

    with Session(sqlite_engine, expire_on_commit=False) as session:
        dataset = Dataset(
            id="dst_materialize_fail",
            slug="materialize-fail",
            name="Materialize Fail",
            task_type=TaskType.CLASSIFICATION.value,
            label_schema_version="1",
            status=DatasetStatus.READY.value,
            created_by="tester",
        )
        label_project = LabelProject(
            id="lpr_materialize_fail",
            dataset_id=dataset.id,
            label_studio_project_id=902,
            title="Materialize Fail",
            task_type=TaskType.CLASSIFICATION.value,
            label_config={"choices": [{"value": "accepted"}]},
            label_schema_version="1",
            training_pipeline_slug="materialize-fail",
            status=LabelProjectStatus.ACTIVE.value,
            auto_snapshot_enabled=True,
            minimum_annotation_count=1,
            required_classes=["accepted"],
            train_ratio=80,
            val_ratio=10,
            test_ratio=10,
            qualifying_actions=["ANNOTATION_UPDATED"],
            task_count=1,
            annotated_task_count=1,
            annotation_count=1,
            class_distribution={"accepted": 1},
            last_activity_at=utc_now(),
        )
        snapshot = DatasetSnapshot(
            id="dss_materialize_fail",
            dataset_id=dataset.id,
            label_project_id=label_project.id,
            snapshot_no=1,
            status=SnapshotStatus.EXPORTING.value,
            created_by="tester",
            export_format="JSON",
            label_schema_version="1",
            validation_status="pending",
            idempotency_key="materialize-fail-key",
            export_storage_uri=str(
                worker_settings.storage.labelstudio_project_export_root(
                    label_project.id, "dss_materialize_fail"
                )
            ),
        )
        session.add_all([dataset, label_project, snapshot])
        session.commit()

    export_root = worker_settings.storage.labelstudio_project_export_root(
        "lpr_materialize_fail", "dss_materialize_fail"
    )
    export_root.mkdir(parents=True, exist_ok=True)
    (export_root / "labelstudio-export.json").write_text(
        json.dumps(
            [
                {
                    "id": 1,
                    "data": {"image": "/label-studio/data/upload/2/missing.jpg"},
                    "annotations": [],
                }
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "control_worker.jobs.dataset._hydrate_label_studio_assets",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(LabelStudioError("asset download failed")),
    )

    with pytest.raises(LabelStudioError, match="asset download failed"):
        materialize_dataset_snapshot("dss_materialize_fail")

    with Session(sqlite_engine, expire_on_commit=False) as session:
        snapshot = session.get(DatasetSnapshot, "dss_materialize_fail")

    assert snapshot is not None
    assert snapshot.status == SnapshotStatus.FAILED.value
    assert snapshot.processing_error == "asset download failed"


def test_hydrate_label_studio_assets_reads_local_files_directly(
    sqlite_engine,
    tmp_path: Path,
    monkeypatch,
    sample_png_bytes: bytes,
) -> None:
    configure_sqlite_factories(sqlite_engine, "control-worker")
    worker_settings = build_test_settings("control-worker", tmp_path)
    dataset_id = "dst_local_file_hydrate"
    relative_path = (
        "runtime/captures/chn_test/lpr_test/manual/20260528/"
        "20260528T09292792433_bc1aa08ef8522015.jpg"
    )
    source_path = Path(worker_settings.label_studio.local_files_root) / relative_path
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(sample_png_bytes)

    with Session(sqlite_engine, expire_on_commit=False) as session:
        session.add(
            Dataset(
                id=dataset_id,
                slug="local-file-hydrate",
                name="Local File Hydrate",
                task_type=TaskType.CLASSIFICATION.value,
                label_schema_version="1",
                status=DatasetStatus.READY.value,
                created_by="tester",
            )
        )
        session.commit()

    def fail_if_label_studio_is_called(_settings):
        raise AssertionError("local-file assets should be read from shared storage")

    monkeypatch.setattr(
        "control_worker.jobs.dataset._label_studio_client",
        fail_if_label_studio_is_called,
    )

    _hydrate_label_studio_assets(
        worker_settings,
        dataset_id=dataset_id,
        export_payload=[
            {
                "id": 15652,
                "data": {
                    "image": f"/label-studio/data/local-files/?d={relative_path}",
                },
                "annotations": [],
            }
        ],
    )

    with Session(sqlite_engine, expire_on_commit=False) as session:
        assets = list_dataset_assets_for_dataset(session, dataset_id)

    assert len(assets) == 1
    assert assets[0].relative_path == relative_path
    cached_path = worker_settings.storage.dataset_asset_root(dataset_id) / relative_path
    assert cached_path.read_bytes() == sample_png_bytes
