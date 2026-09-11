from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from cv_contracts import CleanupExecutionRequest
from cv_core import PlatformSettings
from cv_db import (
    Dataset,
    DatasetSnapshot,
    DeploymentRelease,
    DeploymentTarget,
    LabelProject,
    execute_project_pipeline_cleanup,
    preview_project_pipeline_cleanup,
)
from cv_db.models import ModelFamily, ModelVersion, TrainingJob
from cv_status import (
    DatasetStatus,
    DeploymentReleaseStatus,
    LabelProjectStatus,
    QueueName,
    SnapshotStatus,
    TargetPath,
    TaskType,
    ValidationStatus,
)
from pytest import MonkeyPatch
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from tests.conftest import build_test_settings


class _FakeLabelStudioClient:
    deleted_project_ids: list[int] = []

    def __init__(self, **_kwargs: object) -> None:
        pass

    def delete_project(self, project_id: int) -> None:
        self.deleted_project_ids.append(project_id)


def test_project_pipeline_cleanup_prunes_old_runs_and_keeps_latest_state(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    settings = build_test_settings("admin-api", tmp_path)

    with Session(sqlite_engine, expire_on_commit=False) as session:
        _seed_cleanup_project(session)

        old_paths = _managed_artifact_paths(settings, "old")
        latest_paths = _managed_artifact_paths(settings, "latest")
        for path in [*old_paths, *latest_paths]:
            path.mkdir(parents=True, exist_ok=True)
            (path / "sentinel.txt").write_text(path.name, encoding="utf-8")

        preview = preview_project_pipeline_cleanup(
            session,
            settings=settings,
            label_project_id="lpr_cleanup",
        )
        impacts = {item.scope_type: item.count for item in preview.will_remove}

        assert preview.blocked is False
        assert impacts == {
            "dataset_snapshot": 1,
            "training_job": 1,
            "model_version": 1,
            "deployment_release": 1,
        }
        assert all("latest" not in artifact.path for artifact in preview.artifact_paths)

        result = execute_project_pipeline_cleanup(
            session,
            settings=settings,
            label_project_id="lpr_cleanup",
            payload=CleanupExecutionRequest(
                confirmation_token=preview.confirmation_token,
                reason="trim old project state",
                delete_artifacts=True,
            ),
            deleted_by="tester",
        )

        assert result.already_deleted is False
        assert {item.scope_type: item.count for item in result.deleted_entities} == impacts

        project = session.get(LabelProject, "lpr_cleanup")
        family = session.get(ModelFamily, "mfy_cleanup")
        old_snapshot = session.get(DatasetSnapshot, "dss_old")
        latest_snapshot = session.get(DatasetSnapshot, "dss_latest")
        old_job = session.get(TrainingJob, "trn_old")
        latest_job = session.get(TrainingJob, "trn_latest")
        old_version = session.get(ModelVersion, "mvr_old")
        latest_version = session.get(ModelVersion, "mvr_latest")
        old_release = session.get(DeploymentRelease, "dpr_old")
        latest_release = session.get(DeploymentRelease, "dpr_latest")
        target = session.get(DeploymentTarget, TargetPath.REALTIME.value)

        assert project is not None and project.deleted_at is None
        assert family is not None and family.deleted_at is None and family.active is True
        assert old_snapshot is not None and old_snapshot.deleted_at is not None
        assert latest_snapshot is not None and latest_snapshot.deleted_at is None
        assert old_job is not None and old_job.deleted_at is not None
        assert latest_job is not None and latest_job.deleted_at is None
        assert old_version is not None and old_version.deleted_at is not None
        assert latest_version is not None and latest_version.deleted_at is None
        assert old_release is not None and old_release.deleted_at is not None
        assert latest_release is not None and latest_release.deleted_at is None
        assert target is not None
        assert target.active_release_id == "dpr_latest"
        assert target.previous_release_id is None

        assert all(not path.exists() for path in old_paths)
        assert all(path.exists() for path in latest_paths)


def test_project_pipeline_full_delete_removes_project_dependencies_and_artifacts(
    sqlite_engine: Engine,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    settings = build_test_settings("admin-api", tmp_path)
    _FakeLabelStudioClient.deleted_project_ids = []
    monkeypatch.setattr("cv_db.cleanup_store.LabelStudioClient", _FakeLabelStudioClient)

    with Session(sqlite_engine, expire_on_commit=False) as session:
        _seed_cleanup_project(session)

        artifact_paths = [
            *_managed_artifact_paths(settings, "old"),
            *_managed_artifact_paths(settings, "latest"),
            settings.storage.triton_model_root(TargetPath.REALTIME.value, "cleanup-project"),
            settings.storage.uploads_root() / "dts_dst_cleanup",
        ]
        for path in artifact_paths:
            path.mkdir(parents=True, exist_ok=True)
            (path / "sentinel.txt").write_text(path.name, encoding="utf-8")

        preview = preview_project_pipeline_cleanup(
            session,
            settings=settings,
            label_project_id="lpr_cleanup",
            delete_entire_pipeline=True,
        )
        impacts = {item.scope_type: item.count for item in preview.will_remove}

        assert preview.blocked is False
        assert impacts["labelstudio_project"] == 1
        assert impacts["project_pipeline"] == 1
        assert impacts["dataset"] == 1
        assert impacts["dataset_snapshot"] == 2
        assert impacts["training_job"] == 2
        assert impacts["model_family"] == 1
        assert impacts["model_version"] == 2
        assert impacts["deployment_release"] == 2

        result = execute_project_pipeline_cleanup(
            session,
            settings=settings,
            label_project_id="lpr_cleanup",
            payload=CleanupExecutionRequest(
                confirmation_token=preview.confirmation_token,
                reason="delete entire pipeline",
                delete_artifacts=False,
                delete_entire_pipeline=True,
            ),
            deleted_by="tester",
        )
        session.flush()

        assert result.scope_type == "project_pipeline_full_delete"
        assert _FakeLabelStudioClient.deleted_project_ids == [42]
        assert session.get(LabelProject, "lpr_cleanup") is None
        assert session.get(Dataset, "dst_cleanup") is None
        assert session.get(DatasetSnapshot, "dss_old") is None
        assert session.get(DatasetSnapshot, "dss_latest") is None
        assert session.get(TrainingJob, "trn_old") is None
        assert session.get(TrainingJob, "trn_latest") is None
        assert session.get(ModelFamily, "mfy_cleanup") is None
        assert session.get(ModelVersion, "mvr_old") is None
        assert session.get(ModelVersion, "mvr_latest") is None
        assert session.get(DeploymentRelease, "dpr_old") is None
        assert session.get(DeploymentRelease, "dpr_latest") is None

        target = session.get(DeploymentTarget, TargetPath.REALTIME.value)
        assert target is not None
        assert target.active_release_id is None
        assert target.previous_release_id is None
        assert all(not path.exists() for path in artifact_paths)


def _seed_cleanup_project(session: Session) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    dataset = Dataset(
        id="dst_cleanup",
        slug="cleanup",
        name="Cleanup Dataset",
        task_type=TaskType.CLASSIFICATION.value,
        label_schema_version="1",
        status=DatasetStatus.SNAPSHOTTED.value,
        created_at=base,
    )
    project = LabelProject(
        id="lpr_cleanup",
        dataset_id=dataset.id,
        label_studio_project_id=42,
        title="Cleanup Project",
        task_type=TaskType.CLASSIFICATION.value,
        label_config={"labels": ["ok", "nok"]},
        label_schema_version="1",
        training_pipeline_slug="cleanup-project",
        status=LabelProjectStatus.ACTIVE.value,
        minimum_annotation_count=1,
        required_classes=["ok", "nok"],
        qualifying_actions=["ANNOTATION_UPDATED"],
        created_at=base,
    )
    old_snapshot = _snapshot("dss_old", 1, base + timedelta(minutes=1))
    latest_snapshot = _snapshot("dss_latest", 2, base + timedelta(minutes=2))
    family = ModelFamily(
        id="mfy_cleanup",
        slug="cleanup-project",
        label_project_id=project.id,
        display_name="Cleanup model",
        task_type=TaskType.CLASSIFICATION.value,
        contract_major=1,
        input_schema_ref="contracts/classification/input.v1.json",
        output_schema_ref="contracts/classification/output.v1.json",
        active=True,
        created_at=base + timedelta(minutes=3),
    )
    old_job = _training_job(
        "trn_old",
        family_id=family.id,
        snapshot_id=old_snapshot.id,
        model_version_id="mvr_old",
        created_at=base + timedelta(minutes=4),
    )
    latest_job = _training_job(
        "trn_latest",
        family_id=family.id,
        snapshot_id=latest_snapshot.id,
        model_version_id="mvr_latest",
        created_at=base + timedelta(minutes=5),
        promotion_ready=True,
    )
    old_version = _model_version(
        "mvr_old",
        version_no=1,
        family_id=family.id,
        job_id=old_job.id,
        snapshot_id=old_snapshot.id,
        created_at=base + timedelta(minutes=6),
    )
    latest_version = _model_version(
        "mvr_latest",
        version_no=2,
        family_id=family.id,
        job_id=latest_job.id,
        snapshot_id=latest_snapshot.id,
        created_at=base + timedelta(minutes=7),
        promotion_ready=True,
    )
    old_release = _release(
        "dpr_old",
        family_id=family.id,
        version_id=old_version.id,
        triton_version=1,
        created_at=base + timedelta(minutes=8),
    )
    latest_release = _release(
        "dpr_latest",
        family_id=family.id,
        version_id=latest_version.id,
        triton_version=2,
        created_at=base + timedelta(minutes=9),
    )
    target = DeploymentTarget(
        target_path=TargetPath.REALTIME.value,
        active_release_id=latest_release.id,
        previous_release_id=old_release.id,
        updated_at=base + timedelta(minutes=10),
    )
    session.add_all(
        [
            dataset,
            project,
            old_snapshot,
            latest_snapshot,
            family,
            old_job,
            latest_job,
            old_version,
            latest_version,
            old_release,
            latest_release,
            target,
        ]
    )
    session.commit()


def _snapshot(snapshot_id: str, snapshot_no: int, created_at: datetime) -> DatasetSnapshot:
    return DatasetSnapshot(
        id=snapshot_id,
        dataset_id="dst_cleanup",
        label_project_id="lpr_cleanup",
        snapshot_no=snapshot_no,
        status=SnapshotStatus.READY.value,
        item_count=10,
        annotation_count=10,
        manifest_uri="snapshot_manifest.json",
        content_sha256=f"sha-{snapshot_id}",
        dvc_rev=f"rev-{snapshot_no}",
        created_by="tester",
        export_format="JSON",
        label_schema_version="1",
        split_metadata={},
        validation_report={},
        validation_status=ValidationStatus.PASSED.value,
        statistics={},
        idempotency_key=f"key-{snapshot_id}",
        created_at=created_at,
    )


def _training_job(
    job_id: str,
    *,
    family_id: str,
    snapshot_id: str,
    model_version_id: str,
    created_at: datetime,
    promotion_ready: bool = False,
) -> TrainingJob:
    return TrainingJob(
        id=job_id,
        model_family_id=family_id,
        model_family_slug="cleanup-project",
        dataset_snapshot_id=snapshot_id,
        task_type=TaskType.CLASSIFICATION.value,
        status="completed",
        queue_name=QueueName.TRAINING.value,
        requested_by="tester",
        recipe_name="baseline_cls",
        recipe_version="1.0.0",
        best_checkpoint_rule={"metric_name": "accuracy_top1", "mode": "max"},
        promotion_criteria=[],
        config_snapshot={},
        progress={"progress_percent": 100},
        evaluation_summary={"promotion_ready": promotion_ready},
        metrics_summary={"accuracy_top1": 0.95 if promotion_ready else 0.1},
        model_version_id=model_version_id,
        created_at=created_at,
    )


def _model_version(
    version_id: str,
    *,
    version_no: int,
    family_id: str,
    job_id: str,
    snapshot_id: str,
    created_at: datetime,
    promotion_ready: bool = False,
) -> ModelVersion:
    return ModelVersion(
        id=version_id,
        model_family_id=family_id,
        version_no=version_no,
        training_job_id=job_id,
        dataset_snapshot_id=snapshot_id,
        status="validated",
        metrics_summary={"accuracy_top1": 0.95 if promotion_ready else 0.1},
        evaluation_summary={"promotion_ready": promotion_ready},
        created_at=created_at,
    )


def _release(
    release_id: str,
    *,
    family_id: str,
    version_id: str,
    triton_version: int,
    created_at: datetime,
) -> DeploymentRelease:
    return DeploymentRelease(
        id=release_id,
        target_path=TargetPath.REALTIME.value,
        model_family_id=family_id,
        model_version_id=version_id,
        triton_model_name="cleanup-project",
        triton_model_version=triton_version,
        status=DeploymentReleaseStatus.ACTIVATED.value,
        activated_at=created_at,
        created_at=created_at,
    )


def _managed_artifact_paths(settings: PlatformSettings, suffix: str) -> list[Path]:
    snapshot_id = f"dss_{suffix}"
    job_id = f"trn_{suffix}"
    version_id = f"mvr_{suffix}"
    release_id = f"dpr_{suffix}"
    return [
        settings.storage.labelstudio_project_export_root("lpr_cleanup", snapshot_id),
        settings.storage.snapshot_root(snapshot_id),
        Path(settings.storage.training_root) / f"trn_{job_id}",
        settings.storage.serving_package_root(version_id),
        Path(settings.storage.serving_root) / "history" / f"rel_{release_id}",
    ]
