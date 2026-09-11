from __future__ import annotations

from datetime import timedelta
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from cv_core.settings import PlatformSettings, ServiceRuntimeConfig, StorageRoots, TrainingRuntimeConfig
from cv_db import (
    Base,
    Dataset,
    DatasetSnapshot,
    TrainingJob,
    claim_training_job,
    create_training_job_request,
    get_training_job_detail,
    prune_training_checkpoints,
    record_training_checkpoint,
    update_training_job_state,
)
from cv_status import DatasetStatus, QueueName, SnapshotStatus, TaskType, TrainingJobStatus
from cv_training.paths import build_training_job_paths, read_json, write_json
from cv_training.trainer import (
    TrainerArtifactValidation,
    TrainerCheckpointArtifact,
    TrainerExportArtifact,
    TrainerProgress,
    TrainerResult,
)
from cv_db.model_catalog import architecture_summary
from training_orchestrator.main import app
from training_worker.runtime import (
    RetryableTrainingError,
    TerminalTrainingError,
    _delete_pruned_checkpoint_files,
    _training_runtime_setting,
    run_training_workflow,
)
from cv_utils import utc_now


class FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict[str, object]]] = []

    def enqueue(self, func: str, **kwargs) -> None:
        self.enqueued.append((func, kwargs))


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def get(self, key: str) -> bytes | None:
        value = self.store.get(key)
        return value.encode("utf-8") if value is not None else None

    def delete(self, key: str) -> None:
        self.store.pop(key, None)


class FlakyTrainer:
    def __init__(self) -> None:
        self.calls = 0
        self.trainer_backend = "unit-test-backend"

    def run(self, *, config, control, resume_state=None) -> TrainerResult:  # noqa: ANN001
        self.calls += 1
        control.on_runtime_metadata(
            {
                "trainer_mode": "real",
                "trainer_backend": self.trainer_backend,
                "simulation_only": False,
                "real_training": True,
            }
        )
        control.on_progress(
            TrainerProgress(
                epoch_no=1,
                step_no=5,
                total_epochs=config.max_epochs,
                total_steps=config.max_epochs * config.steps_per_epoch,
                training_metrics={"loss": 0.5},
                validation_metrics={"map_50_95": 0.7, "map_50": 0.82, "recall_at_100": 0.78},
            )
        )
        checkpoint_path = config.checkpoint_dir / "iter_5.pth"
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path.write_bytes(b"real-checkpoint-binary")
        control.on_checkpoint(
            TrainerCheckpointArtifact(
                path=str(checkpoint_path),
                epoch_no=1,
                step_no=5,
                metrics={"map_50_95": 0.7, "map_50": 0.82, "recall_at_100": 0.78},
                checkpoint_kind="interval",
                is_best=True,
            )
        )
        if self.calls == 1:
            raise RetryableTrainingError("Simulated worker restart after checkpoint.")
        assert resume_state is not None
        control.on_progress(
            TrainerProgress(
                epoch_no=config.max_epochs,
                step_no=config.max_epochs * config.steps_per_epoch,
                total_epochs=config.max_epochs,
                total_steps=config.max_epochs * config.steps_per_epoch,
                training_metrics={"loss": 0.08},
                validation_metrics={"map_50_95": 0.94, "map_50": 0.98, "recall_at_100": 0.93},
            )
        )
        onnx_path = config.onnx_dir / "model.onnx"
        onnx_path.parent.mkdir(parents=True, exist_ok=True)
        onnx_path.write_bytes(b"real-onnx-binary")
        return TrainerResult(
            final_epoch=config.max_epochs,
            final_step=config.max_epochs * config.steps_per_epoch,
            best_metric_name="map_50_95",
            best_metric_value=0.94,
            best_metrics={
                "map_50_95": 0.94,
                "map_50": 0.98,
                "recall_at_100": 0.93,
            },
            final_validation_metrics={
                "map_50_95": 0.94,
                "map_50": 0.98,
                "recall_at_100": 0.93,
            },
            export_artifacts=(
                TrainerExportArtifact(
                    kind="onnx",
                    path=str(onnx_path),
                    generated=True,
                    status="ready",
                ),
            ),
            artifact_validation=TrainerArtifactValidation(
                status="passed",
                checks={"checkpoint_valid": True, "onnx_valid": True},
                details={"checkpoint": {"size_bytes": checkpoint_path.stat().st_size}, "onnx": {"size_bytes": onnx_path.stat().st_size}},
            ),
            runtime_metadata={"trainer_mode": "real", "trainer_backend": self.trainer_backend},
            best_checkpoint_path=str(checkpoint_path),
            latest_checkpoint_path=str(checkpoint_path),
        )


class FakeMlflowAdapter:
    def __init__(self) -> None:
        self.logged_artifacts: list[dict[str, object]] = []
        self.logged_metrics: list[dict[str, object]] = []
        self.last_registration_source_uri: str | None = None

    def start_training_run(self, **_kwargs):  # noqa: ANN003
        return SimpleNamespace(
            experiment_id="exp-test",
            experiment_name="cv-platform/test",
            run_id="mlf-run-test",
        )

    def finish_run(self, **_kwargs) -> None:  # noqa: ANN003
        return None

    def log_metrics(self, **kwargs) -> None:  # noqa: ANN003
        self.logged_metrics.append(kwargs)

    def log_artifacts(self, **kwargs) -> str:  # noqa: ANN003
        self.logged_artifacts.append(kwargs)
        return str(kwargs["artifact_path"])

    def register_model_version(self, **kwargs):  # noqa: ANN003
        self.last_registration_source_uri = str(kwargs["source_uri"])
        return SimpleNamespace(
            registered_model_name=kwargs["registered_model_name"],
            model_version="1",
        )

    def sync_registered_model_metadata(self, **_kwargs) -> None:  # noqa: ANN003
        return None


class FakeDvcAdapter:
    def verify_runtime_prerequisites(self) -> None:
        return None

    def current_code_revision(self, _workspace_root: Path) -> str:
        return "git-test-rev"

    def build_reproducibility_metadata(self, **kwargs):  # noqa: ANN003
        return {
            "training_job_id": kwargs["training_job_id"],
            "code_revision": "git-test-rev",
            "git_dirty": False,
        }

    def capture_model_artifact_bundle(self, **_kwargs):  # noqa: ANN003
        return {
            "bundle_dvc_file": "registry/training-jobs/test-job.dvc",
            "git_commit": "git-test-rev",
            "tracked_files": {},
        }


class BrokenDvcAdapter:
    def verify_runtime_prerequisites(self) -> None:
        raise RuntimeError("DVC CLI is required for DVC integration but is not available.")


def test_training_runtime_setting_uses_safe_defaults_for_legacy_runtime_objects() -> None:
    legacy_settings = SimpleNamespace(training=SimpleNamespace())

    assert _training_runtime_setting(legacy_settings, "trainer_mode") == "real"
    assert _training_runtime_setting(legacy_settings, "gpu_devices") == ["cuda:0"]

    configured_settings = SimpleNamespace(training=SimpleNamespace(trainer_mode="simulation"))
    assert _training_runtime_setting(configured_settings, "trainer_mode") == "simulation"


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def override_session_scope(engine):
    @contextmanager
    def _scope(_settings) -> Generator[Session, None, None]:
        session = Session(engine, expire_on_commit=False)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return _scope


@pytest.fixture()
def fake_queue():
    return FakeQueue()


@pytest.fixture()
def route_client(monkeypatch, override_session_scope, fake_queue):
    monkeypatch.setattr("training_orchestrator.api.routes.session_scope", override_session_scope)
    monkeypatch.setattr("training_orchestrator.api.routes.training_queue", lambda settings: fake_queue)
    with TestClient(app) as client:
        yield client


def seed_ready_snapshot(session: Session, *, task_type: TaskType = TaskType.CLASSIFICATION) -> tuple[str, str]:
    dataset = Dataset(
        id="dst_training",
        slug="training-dataset",
        name="Training Dataset",
        task_type=task_type.value,
        label_schema_version="1",
        status=DatasetStatus.SNAPSHOTTED.value,
        created_by="tester",
    )
    snapshot = DatasetSnapshot(
        id="dss_training_ready",
        dataset_id=dataset.id,
        label_project_id=None,
        snapshot_no=1,
        status=SnapshotStatus.READY.value,
        item_count=24,
        annotation_count=24,
        manifest_uri="snapshot_manifest.json",
        content_sha256="abc123",
        dvc_rev="rev-1",
        created_by="tester",
        export_format="JSON",
        label_schema_version="1",
        split_metadata={},
        validation_report={},
        validation_status="passed",
        statistics={"item_count": 24},
        idempotency_key="snapshot-training-key",
    )
    session.add_all([dataset, snapshot])
    session.commit()
    return dataset.id, snapshot.id


def build_training_payload(snapshot_id: str) -> dict[str, object]:
    return {
        "model_family_slug": "widget-inspection-det",
        "dataset_snapshot_id": snapshot_id,
        "task_type": "detection",
        "architecture": architecture_summary(
            task_type=TaskType.DETECTION,
            family="yolo",
            version="v8",
            size="n",
        ).model_dump(mode="json"),
        "recipe_name": "baseline_det",
        "recipe_version": "1.1.0",
        "max_epochs": 5,
        "checkpoint_interval_steps": 5,
        "parameter_overrides": {"epochs": 5, "checkpoint_interval_steps": 5},
        "best_checkpoint_rule": {
            "metric_name": "map_50_95",
            "mode": "max",
            "phase": "validation",
            "minimum_epoch": 1,
        },
        "promotion_criteria": [
            {
                "metric_name": "map_50_95",
                "comparison": "gte",
                "threshold": 0.9,
                "phase": "evaluation",
            }
        ],
        "allow_duplicate_snapshot_training": False,
        "idempotency_key": "create-training-job-1",
    }


def test_training_job_create_api_queues_background_work(route_client, engine, fake_queue) -> None:
    with Session(engine, expire_on_commit=False) as session:
        _, snapshot_id = seed_ready_snapshot(session)

    response = route_client.post("/api/v1/training-jobs", json=build_training_payload(snapshot_id))

    assert response.status_code == 202
    body = response.json()
    assert body["queue"] == QueueName.TRAINING.value
    assert fake_queue.enqueued[0][0] == "training_worker.jobs.run_training_job"
    assert fake_queue.enqueued[0][1]["job_timeout"] == 86400

    with Session(engine, expire_on_commit=False) as session:
        job = get_training_job_detail(session, body["job_id"])

    assert job is not None
    assert job.status == TrainingJobStatus.QUEUED
    assert job.dataset_snapshot_id == snapshot_id


def test_duplicate_submission_reuses_same_training_job(route_client, engine, fake_queue) -> None:
    with Session(engine, expire_on_commit=False) as session:
        _, snapshot_id = seed_ready_snapshot(session)
    payload = build_training_payload(snapshot_id)

    first = route_client.post(
        "/api/v1/training-jobs",
        json=payload,
        headers={"Idempotency-Key": "training-request-key"},
    )
    second = route_client.post(
        "/api/v1/training-jobs",
        json=payload,
        headers={"Idempotency-Key": "training-request-key"},
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert len(fake_queue.enqueued) == 1


def test_cancel_route_marks_active_job_for_graceful_stop(route_client, engine) -> None:
    with Session(engine, expire_on_commit=False) as session:
        _, snapshot_id = seed_ready_snapshot(session)
        job = create_training_job_request(
            session,
            payload=build_training_request(snapshot_id),
            requested_by="tester",
            source_service="test-suite",
            idempotency_key="cancel-job-key",
        )
        update_training_job_state(
            session,
            training_job_id=job.id,
            status=TrainingJobStatus.TRAINING,
            source_service="test-suite",
            actor_type="system",
            actor_id="tester",
            progress={"current_epoch": 1, "current_step": 3, "total_epochs": 5, "total_steps": 10, "progress_percent": 30},
        )
        session.commit()

    response = route_client.post(
        f"/api/v1/training-jobs/{job.id}:cancel",
        json={"reason": "Stop at next checkpoint boundary."},
    )

    assert response.status_code == 202
    with Session(engine, expire_on_commit=False) as session:
        detail = get_training_job_detail(session, job.id)

    assert detail is not None
    assert detail.cancel_requested is True
    assert detail.status == TrainingJobStatus.CANCEL_REQUESTED


def test_cancel_route_finishes_stale_training_job(route_client, engine) -> None:
    with Session(engine, expire_on_commit=False) as session:
        _, snapshot_id = seed_ready_snapshot(session)
        job = create_training_job_request(
            session,
            payload=build_training_request(snapshot_id),
            requested_by="tester",
            source_service="test-suite",
            idempotency_key="cancel-stale-job-key",
        )
        claim_training_job(
            session,
            training_job_id=job.id,
            worker_instance_id="dead-worker",
            source_service="test-suite",
        )
        update_training_job_state(
            session,
            training_job_id=job.id,
            status=TrainingJobStatus.TRAINING,
            source_service="test-suite",
            actor_type="system",
            actor_id="tester",
            gpu_device="cuda:0",
            progress={
                "current_epoch": 2,
                "current_step": 7,
                "total_epochs": 5,
                "total_steps": 10,
                "progress_percent": 70,
            },
        )
        row = session.get(TrainingJob, job.id)
        assert row is not None
        row.worker_heartbeat_at = utc_now() - timedelta(seconds=120)
        session.commit()

    response = route_client.post(
        f"/api/v1/training-jobs/{job.id}:cancel",
        json={"reason": "Worker is stale; stop showing this job as active."},
    )

    assert response.status_code == 202
    with Session(engine, expire_on_commit=False) as session:
        detail = get_training_job_detail(session, job.id)

    assert detail is not None
    assert detail.status == TrainingJobStatus.CANCELLED
    assert detail.cancel_requested is True
    assert detail.worker_instance_id is None
    assert detail.gpu_device is None
    assert detail.finished_at is not None
    assert detail.runtime_metadata["cancellation"]["stale"] is True


def test_cancel_route_force_finishes_cancel_requested_job(route_client, engine) -> None:
    with Session(engine, expire_on_commit=False) as session:
        _, snapshot_id = seed_ready_snapshot(session)
        job = create_training_job_request(
            session,
            payload=build_training_request(snapshot_id),
            requested_by="tester",
            source_service="test-suite",
            idempotency_key="force-cancel-job-key",
        )
        update_training_job_state(
            session,
            training_job_id=job.id,
            status=TrainingJobStatus.CANCEL_REQUESTED,
            source_service="test-suite",
            actor_type="system",
            actor_id="tester",
            gpu_device="cuda:0",
        )
        session.commit()

    response = route_client.post(
        f"/api/v1/training-jobs/{job.id}:cancel",
        json={"reason": "Operator force-cancelled a stuck cancellation.", "force": True},
    )

    assert response.status_code == 202
    with Session(engine, expire_on_commit=False) as session:
        detail = get_training_job_detail(session, job.id)

    assert detail is not None
    assert detail.status == TrainingJobStatus.CANCELLED
    assert detail.gpu_device is None
    assert detail.runtime_metadata["cancellation"]["force"] is True


def test_resume_route_requeues_from_latest_checkpoint(route_client, engine, fake_queue, tmp_path: Path) -> None:
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint_path.write_text("{}", encoding="utf-8")

    with Session(engine, expire_on_commit=False) as session:
        _, snapshot_id = seed_ready_snapshot(session)
        job = create_training_job_request(
            session,
            payload=build_training_request(snapshot_id),
            requested_by="tester",
            source_service="test-suite",
            idempotency_key="resume-job-key",
        )
        checkpoint = record_training_checkpoint(
            session,
            training_job_id=job.id,
            epoch_no=1,
            step_no=5,
            storage_uri=str(checkpoint_path),
            sha256="checkpoint-sha",
            checkpoint_kind="interval",
            metric_primary=0.72,
            resume_safe=True,
            is_best=True,
            metrics={"accuracy_top1": 0.72},
        )
        update_training_job_state(
            session,
            training_job_id=job.id,
            status=TrainingJobStatus.FAILED,
            source_service="test-suite",
            actor_type="system",
            actor_id="tester",
            latest_checkpoint_id=checkpoint.id,
            best_checkpoint_id=checkpoint.id,
            reason="Simulated training crash",
        )
        session.commit()

    response = route_client.post(
        f"/api/v1/training-jobs/{job.id}:resume",
        json={"reason": "Resume from saved checkpoint."},
    )

    assert response.status_code == 202
    assert fake_queue.enqueued[-1][0] == "training_worker.jobs.resume_training_job"
    assert fake_queue.enqueued[-1][1]["job_timeout"] == 86400
    with Session(engine, expire_on_commit=False) as session:
        detail = get_training_job_detail(session, job.id)

    assert detail is not None
    assert detail.status == TrainingJobStatus.RESUMING
    assert detail.resume_count == 1


def test_checkpoint_pruning_keeps_best_latest_and_newest_retained_checkpoint(
    engine,
    tmp_path: Path,
) -> None:
    with Session(engine, expire_on_commit=False) as session:
        _, snapshot_id = seed_ready_snapshot(session)
        job = create_training_job_request(
            session,
            payload=build_training_request(snapshot_id),
            requested_by="tester",
            source_service="test-suite",
            idempotency_key="checkpoint-prune-key",
        )
        checkpoint_ids: list[str] = []
        checkpoint_paths: list[Path] = []
        for step_no in range(1, 7):
            checkpoint_path = tmp_path / f"checkpoint-{step_no}.pth"
            checkpoint_path.write_bytes(f"checkpoint-{step_no}".encode())
            checkpoint_paths.append(checkpoint_path)
            checkpoint = record_training_checkpoint(
                session,
                training_job_id=job.id,
                epoch_no=step_no,
                step_no=step_no,
                storage_uri=str(checkpoint_path),
                sha256=f"sha-{step_no}",
                checkpoint_kind="epoch",
                metric_primary=float(step_no),
                resume_safe=True,
                is_best=step_no == 1,
                metrics={"accuracy_top1": float(step_no)},
            )
            checkpoint_ids.append(checkpoint.id)
        session.flush()

        pruned_paths = prune_training_checkpoints(
            session,
            training_job_id=job.id,
            keep_count=3,
        )
        session.commit()

    assert set(pruned_paths) == {str(path) for path in checkpoint_paths[1:4]}

    with Session(engine, expire_on_commit=False) as session:
        detail = get_training_job_detail(session, job.id)

    assert detail is not None
    assert {checkpoint.checkpoint_id for checkpoint in detail.checkpoints} == {
        checkpoint_ids[0],
        checkpoint_ids[4],
        checkpoint_ids[5],
    }
    assert detail.best_checkpoint_id == checkpoint_ids[0]
    assert detail.latest_checkpoint_id == checkpoint_ids[5]


def test_pruned_checkpoint_file_deletion_stays_inside_job_root(tmp_path: Path) -> None:
    job_root = tmp_path / "job"
    checkpoint_path = job_root / "checkpoints" / "stale.pth"
    outside_path = tmp_path / "outside.pth"
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint_path.write_bytes(b"stale")
    outside_path.write_bytes(b"outside")

    deleted, errors = _delete_pruned_checkpoint_files(
        paths=[str(checkpoint_path), str(outside_path)],
        job_root=job_root,
    )

    assert deleted == [str(checkpoint_path)]
    assert not checkpoint_path.exists()
    assert outside_path.exists()
    assert errors == [f"Skipped checkpoint outside job root: {outside_path}"]


def test_worker_crash_recovery_resumes_from_latest_good_checkpoint(
    monkeypatch,
    override_session_scope,
    engine,
    tmp_path: Path,
) -> None:
    settings = build_test_settings(tmp_path)
    fake_redis = FakeRedis()
    trainer = FlakyTrainer()
    mlflow_adapter = FakeMlflowAdapter()
    manifest_path = settings.storage.snapshot_root("dss_training_ready") / "snapshot_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(manifest_path, {"item_count": 24})
    write_json(settings.storage.snapshot_root("dss_training_ready") / "label_map.json", ["accepted", "rework"])

    monkeypatch.setattr("training_worker.runtime._load_worker_settings", lambda: settings)
    monkeypatch.setattr("training_worker.runtime.session_scope", override_session_scope)
    monkeypatch.setattr("training_worker.runtime.Redis.from_url", lambda url: fake_redis)
    monkeypatch.setattr(
        "training_worker.runtime.get_default_trainer",
        lambda task_type, **kwargs: trainer,
    )
    monkeypatch.setattr("training_worker.runtime.build_mlflow_adapter", lambda settings: mlflow_adapter)
    monkeypatch.setattr("training_worker.runtime.build_dvc_adapter", lambda settings: FakeDvcAdapter())
    monkeypatch.setattr(
        "training_worker.runtime.get_current_job",
        lambda: SimpleNamespace(retries_left=1),
    )

    with Session(engine, expire_on_commit=False) as session:
        _, snapshot_id = seed_ready_snapshot(session)
        job = create_training_job_request(
            session,
            payload=build_training_request(snapshot_id),
            requested_by="tester",
            source_service="test-suite",
            idempotency_key="crash-recovery-key",
        )
        session.commit()

    with pytest.raises(RetryableTrainingError):
        run_training_workflow(job.id)

    with Session(engine, expire_on_commit=False) as session:
        after_crash = get_training_job_detail(session, job.id)

    assert after_crash is not None
    assert after_crash.latest_checkpoint_id is not None
    assert after_crash.status == TrainingJobStatus.QUEUED

    run_training_workflow(job.id)

    with Session(engine, expire_on_commit=False) as session:
        recovered = get_training_job_detail(session, job.id)

    assert recovered is not None
    assert recovered.status == TrainingJobStatus.COMPLETED
    assert recovered.model_version_id is not None
    assert recovered.metrics_summary["map_50_95"] >= 0.9
    assert recovered.last_error is None
    assert recovered.failure_class is None

    paths = build_training_job_paths(settings, job.id)
    export_manifest = read_json(paths.export_manifest_file)
    engine_build_manifest = read_json(paths.engine_build_manifest_file)

    assert export_manifest["io_schema"]["input_tensors"][0]["name"] == "input__image"
    assert export_manifest["hardware_compatibility"]["target_gpu_profile"] == settings.triton.realtime.gpu_profile
    assert export_manifest["class_count"] == 2
    assert export_manifest["postprocessing"]["class_map"] == ["accepted", "rework"]
    assert engine_build_manifest["precision"] == "fp16"
    assert mlflow_adapter.last_registration_source_uri == "runs:/mlf-run-test/registration-bundle"
    assert any(
        call["step"] == 5
        and call["metrics"]["training/loss"] == 0.5
        and call["metrics"]["validation/map_50_95"] == 0.7
        for call in mlflow_adapter.logged_metrics
    )
    assert any(
        call["step"] > 5
        and call["metrics"]["training/loss"] == 0.08
        and call["metrics"]["validation/map_50_95"] == 0.94
        for call in mlflow_adapter.logged_metrics
    )


def test_training_fails_fast_when_dvc_cli_is_missing(
    monkeypatch,
    override_session_scope,
    engine,
    tmp_path: Path,
) -> None:
    settings = build_test_settings(tmp_path)
    fake_redis = FakeRedis()
    manifest_path = settings.storage.snapshot_root("dss_training_ready") / "snapshot_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(manifest_path, {"item_count": 24})
    write_json(settings.storage.snapshot_root("dss_training_ready") / "label_map.json", ["accepted", "rework"])

    class _TrainerMustNotRun:
        def run(self, **_kwargs):  # noqa: ANN003
            raise AssertionError("trainer.run should not be called when DVC prerequisites are missing")

    monkeypatch.setattr("training_worker.runtime._load_worker_settings", lambda: settings)
    monkeypatch.setattr("training_worker.runtime.session_scope", override_session_scope)
    monkeypatch.setattr("training_worker.runtime.Redis.from_url", lambda url: fake_redis)
    monkeypatch.setattr(
        "training_worker.runtime.get_default_trainer",
        lambda task_type, **kwargs: _TrainerMustNotRun(),
    )
    monkeypatch.setattr(
        "training_worker.runtime.build_mlflow_adapter",
        lambda settings: FakeMlflowAdapter(),
    )
    monkeypatch.setattr(
        "training_worker.runtime.build_dvc_adapter",
        lambda settings: BrokenDvcAdapter(),
    )

    with Session(engine, expire_on_commit=False) as session:
        _, snapshot_id = seed_ready_snapshot(session)
        job = create_training_job_request(
            session,
            payload=build_training_request(snapshot_id),
            requested_by="tester",
            source_service="test-suite",
            idempotency_key="missing-dvc-key",
        )
        session.commit()

    with pytest.raises(
        TerminalTrainingError,
        match="Training runtime prerequisites are missing: DVC CLI is required for DVC integration but is not available.",
    ):
        run_training_workflow(job.id)

    with Session(engine, expire_on_commit=False) as session:
        detail = get_training_job_detail(session, job.id)

    assert detail is not None
    assert detail.status == TrainingJobStatus.FAILED
    assert (
        detail.last_error
        == "Training runtime prerequisites are missing: DVC CLI is required for DVC integration but is not available."
    )
    assert fake_redis.store == {}


def build_training_request(snapshot_id: str):
    from cv_contracts import TrainingJobCreateRequest

    return TrainingJobCreateRequest.model_validate(build_training_payload(snapshot_id))


def build_test_settings(tmp_path: Path) -> PlatformSettings:
    return PlatformSettings(
        service=ServiceRuntimeConfig(name="training-worker"),
        storage=StorageRoots(
            state_root=str(tmp_path / "state"),
            training_root=str(tmp_path / "state" / "training"),
            serving_root=str(tmp_path / "state" / "serving"),
            audit_root=str(tmp_path / "state" / "audit"),
            logs_root=str(tmp_path / "state" / "logs"),
        ),
        training=TrainingRuntimeConfig(
            max_parallel_jobs=1,
            heartbeat_interval_seconds=1,
            stale_after_seconds=30,
            checkpoint_interval_steps=5,
            safe_retry_limit=2,
            gpu_devices=["cuda:0"],
            registration_hook="postgres_model_registry",
            log_tail_limit=200,
            default_step_duration_ms=0,
            cleanup_temp_on_failure=True,
            resume_from_latest_good_checkpoint=True,
        ),
    )
