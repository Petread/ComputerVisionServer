from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from admin_api.main import app as admin_app
from control_worker.jobs.deployment import promote_model_release, rollback_model_release, stage_model_release
from cv_db import (
    Dataset,
    DatasetSnapshot,
    create_promotion_request,
    get_training_job_detail,
    record_deployment_approval,
)
from cv_db.models import DeploymentTarget
from cv_status import (
    DatasetStatus,
    DeploymentApprovalScope,
    DeploymentApprovalStatus,
    SnapshotStatus,
    TargetPath,
    TaskType,
)
from inference_gateway.main_realtime import app as realtime_app
from inference_gateway.runtime.service import RealtimeGatewayService
from tests.conftest import build_test_settings, configure_sqlite_factories
from tests.support.scenario_helpers import (
    FakeDvcAdapter,
    FakeMlflowAdapter,
    FakeRedis,
    login_admin,
    seed_registered_model_version,
)
from training_worker.runtime import run_training_workflow


class _NoopQueue:
    def __init__(self, *_args, **_kwargs) -> None:
        return None

    def enqueue(self, _func: str, **_kwargs) -> None:
        return None


class _FakeTritonClient:
    def load_model(self, _model_name: str) -> None:
        return None

    def unload_model(self, _model_name: str) -> None:
        return None

    def repository_index(self, *, ready_only: bool = False):  # noqa: ANN001
        _ = ready_only
        return []

    def is_server_ready(self) -> bool:
        return True

    def is_model_ready(self, model_name: str, model_version: int) -> bool:
        _ = (model_name, model_version)
        return True


class _BlockingTrainer:
    def __init__(self, started: Event, continue_run: Event) -> None:
        self.started = started
        self.continue_run = continue_run
        self.trainer_backend = "unit-test-backend"

    def run(self, *, config, control, resume_state=None):  # noqa: ANN001
        from cv_training.trainer import (
            TrainerArtifactValidation,
            TrainerCheckpointArtifact,
            TrainerExportArtifact,
            TrainerProgress,
            TrainerResult,
        )

        assert resume_state is None
        checkpoint_path = config.checkpoint_dir / "iter_5.pth"
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path.write_bytes(b"unit-test-checkpoint")
        onnx_path = config.onnx_dir / "model.onnx"
        onnx_path.parent.mkdir(parents=True, exist_ok=True)
        onnx_path.write_bytes(b"unit-test-onnx")
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
                validation_metrics={
                    "accuracy_top1": 0.9,
                    "precision_macro": 0.89,
                    "recall_macro": 0.88,
                    "f1_macro": 0.885,
                },
            )
        )
        control.on_checkpoint(
            TrainerCheckpointArtifact(
                path=str(checkpoint_path),
                epoch_no=1,
                step_no=5,
                metrics={
                    "accuracy_top1": 0.9,
                    "precision_macro": 0.89,
                    "recall_macro": 0.88,
                    "f1_macro": 0.885,
                },
                checkpoint_kind="interval",
                is_best=True,
            )
        )
        self.started.set()
        self.continue_run.wait(timeout=10)
        return TrainerResult(
            final_epoch=config.max_epochs,
            final_step=config.max_epochs * config.steps_per_epoch,
            best_metric_name="accuracy_top1",
            best_metric_value=0.93,
            best_metrics={
                "accuracy_top1": 0.93,
                "precision_macro": 0.91,
                "recall_macro": 0.9,
                "f1_macro": 0.905,
            },
            final_validation_metrics={
                "accuracy_top1": 0.93,
                "precision_macro": 0.91,
                "recall_macro": 0.9,
                "f1_macro": 0.905,
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
                details={
                    "checkpoint": {"size_bytes": checkpoint_path.stat().st_size},
                    "onnx": {"size_bytes": onnx_path.stat().st_size},
                },
            ),
            runtime_metadata={
                "trainer_mode": "real",
                "trainer_backend": self.trainer_backend,
            },
            best_checkpoint_path=str(checkpoint_path),
            latest_checkpoint_path=str(checkpoint_path),
        )


def test_operator_ui_restart_does_not_interrupt_training(
    sqlite_engine,
    tmp_path: Path,
    monkeypatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "training-worker")
    admin_settings = build_test_settings("admin-api", tmp_path)
    training_settings = build_test_settings("training-worker", tmp_path)
    admin_app.state.settings = admin_settings

    _seed_ready_snapshot(training_settings, sqlite_engine)

    started = Event()
    continue_run = Event()
    trainer = _BlockingTrainer(started, continue_run)
    exceptions: list[BaseException] = []
    fake_redis = FakeRedis()

    monkeypatch.setattr("admin_api.api.routes.training._training_queue", lambda _settings: _NoopQueue())
    monkeypatch.setattr("training_worker.runtime._load_worker_settings", lambda: training_settings)
    monkeypatch.setattr("training_worker.runtime.Redis.from_url", lambda _url: fake_redis)
    monkeypatch.setattr(
        "training_worker.runtime.get_default_trainer",
        lambda _task_type, **_kwargs: trainer,
    )
    monkeypatch.setattr("training_worker.runtime.build_mlflow_adapter", lambda _settings: FakeMlflowAdapter())
    monkeypatch.setattr("training_worker.runtime.build_dvc_adapter", lambda _settings: FakeDvcAdapter())

    with TestClient(admin_app) as first_client:
        csrf_token = login_admin(first_client)
        create_response = first_client.post(
            "/api/v1/training-jobs",
            json={
                "model_family_slug": "widget-inspection-cls",
                "dataset_snapshot_id": "dss_training_ready",
                "task_type": "classification",
                "recipe_name": "baseline_cls",
                "recipe_version": "1.0.0",
                "max_epochs": 5,
                "checkpoint_interval_steps": 5,
                "parameter_overrides": {"epochs": 5},
                "best_checkpoint_rule": {
                    "metric_name": "accuracy_top1",
                    "mode": "max",
                    "phase": "validation",
                    "minimum_epoch": 1,
                },
                "promotion_criteria": [
                    {
                        "metric_name": "accuracy_top1",
                        "comparison": "gte",
                        "threshold": 0.9,
                        "phase": "evaluation",
                    }
                ],
                "allow_duplicate_snapshot_training": False,
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert create_response.status_code == 202
        training_job_id = str(create_response.json()["job_id"])

        worker = Thread(
            target=_run_training_thread,
            args=(training_job_id, exceptions),
            daemon=True,
        )
        worker.start()
        assert started.wait(timeout=10)

    with TestClient(admin_app) as second_client:
        csrf_token = login_admin(second_client)
        detail_during_restart = second_client.get(f"/api/v1/training-jobs/{training_job_id}")
        assert detail_during_restart.status_code == 200
        continue_run.set()
        worker.join(timeout=15)
        detail_after_restart = second_client.get(
            f"/api/v1/training-jobs/{training_job_id}",
            headers={"X-CSRF-Token": csrf_token},
        )

    assert not exceptions
    assert detail_after_restart.status_code == 200
    assert detail_after_restart.json()["status"] == "completed"

    with Session(sqlite_engine, expire_on_commit=False) as session:
        detail = get_training_job_detail(session, training_job_id)

    assert detail is not None
    assert detail.status.value == "completed"


def test_failed_deployment_attempt_keeps_current_production_release(
    sqlite_engine,
    tmp_path: Path,
    monkeypatch,
    sample_png_base64: str,
) -> None:
    configure_sqlite_factories(sqlite_engine, "control-worker", "inference-gateway-rt")
    control_settings = build_test_settings("control-worker", tmp_path)
    realtime_settings = build_test_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "rt-acceptance")],
    )
    fake_triton = _FakeTritonClient()

    monkeypatch.setattr("control_worker.jobs.deployment.load_platform_settings", lambda _service: control_settings)
    monkeypatch.setattr("control_worker.jobs.deployment._triton_client", lambda _settings, _target: fake_triton)

    with Session(sqlite_engine, expire_on_commit=False) as session:
        good_version = seed_registered_model_version(session, tmp_path=tmp_path, suffix="good")
        good_promotion_id = _approve_stage_and_promote(session, good_version.id, target_path="realtime")
        session.commit()

    good_stage = stage_model_release(good_promotion_id)
    _approve_production(sqlite_engine, good_version.id, target_path="realtime")
    promote_model_release(good_promotion_id)

    initial_release_id = good_stage["deployment_release_id"]
    before_failure_release_id = _classify_active_release(realtime_settings, sample_png_base64)
    assert before_failure_release_id == initial_release_id

    with Session(sqlite_engine, expire_on_commit=False) as session:
        bad_version = seed_registered_model_version(
            session,
            tmp_path=tmp_path,
            suffix="badgpu",
            gpu_profile="different-gpu-profile",
        )
        bad_promotion_id = _approve_stage_and_promote(session, bad_version.id, target_path="realtime")
        session.commit()

    with pytest.raises(ValueError):
        stage_model_release(bad_promotion_id)

    after_failure_release_id = _classify_active_release(realtime_settings, sample_png_base64)
    assert after_failure_release_id == initial_release_id

    with Session(sqlite_engine, expire_on_commit=False) as session:
        target = session.get(DeploymentTarget, "realtime")

    assert target is not None
    assert target.active_release_id == initial_release_id


def test_rollback_drill_restores_previous_release(
    sqlite_engine,
    tmp_path: Path,
    monkeypatch,
    sample_png_base64: str,
) -> None:
    configure_sqlite_factories(sqlite_engine, "control-worker", "inference-gateway-rt")
    control_settings = build_test_settings("control-worker", tmp_path)
    realtime_settings = build_test_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "rt-acceptance")],
    )
    fake_triton = _FakeTritonClient()

    monkeypatch.setattr("control_worker.jobs.deployment.load_platform_settings", lambda _service: control_settings)
    monkeypatch.setattr("control_worker.jobs.deployment._triton_client", lambda _settings, _target: fake_triton)

    with Session(sqlite_engine, expire_on_commit=False) as session:
        version_a = seed_registered_model_version(session, tmp_path=tmp_path, suffix="rollback_a")
        promotion_a = _approve_stage_and_promote(session, version_a.id, target_path="realtime")
        session.commit()

    stage_a = stage_model_release(promotion_a)
    _approve_production(sqlite_engine, version_a.id, target_path="realtime")
    promote_model_release(promotion_a)

    with Session(sqlite_engine, expire_on_commit=False) as session:
        version_b = seed_registered_model_version(session, tmp_path=tmp_path, suffix="rollback_b")
        promotion_b = _approve_stage_and_promote(session, version_b.id, target_path="realtime")
        session.commit()

    stage_b = stage_model_release(promotion_b)
    _approve_production(sqlite_engine, version_b.id, target_path="realtime")
    promote_model_release(promotion_b)

    active_before_rollback = _classify_active_release(realtime_settings, sample_png_base64)
    assert active_before_rollback == stage_b["deployment_release_id"]

    rollback_result = rollback_model_release("realtime")
    assert rollback_result["status"] == "rolled_back"

    active_after_rollback = _classify_active_release(realtime_settings, sample_png_base64)
    assert active_after_rollback == stage_a["deployment_release_id"]


def _run_training_thread(training_job_id: str, exceptions: list[BaseException]) -> None:
    try:
        run_training_workflow(training_job_id)
    except BaseException as exc:  # pragma: no cover - helper captures failures for assertion
        exceptions.append(exc)


def _approve_stage_and_promote(session: Session, model_version_id: str, *, target_path: str) -> str:
    record_deployment_approval(
        session,
        model_version_id=model_version_id,
        target_path=target_path,
        approval_scope=DeploymentApprovalScope.STAGING.value,
        status=DeploymentApprovalStatus.APPROVED.value,
        decided_by="tester",
        notes="stage",
    )
    return create_promotion_request(
        session,
        model_version_id=model_version_id,
        target_path=target_path,
        requested_by="tester",
        requested_state="staged",
    )


def _approve_production(engine, model_version_id: str, *, target_path: str) -> None:
    with Session(engine, expire_on_commit=False) as session:
        record_deployment_approval(
            session,
            model_version_id=model_version_id,
            target_path=target_path,
            approval_scope=DeploymentApprovalScope.PRODUCTION.value,
            status=DeploymentApprovalStatus.APPROVED.value,
            decided_by="tester",
            notes="promote",
        )
        create_promotion_request(
            session,
            model_version_id=model_version_id,
            target_path=target_path,
            requested_by="tester",
            requested_state="production",
        )
        session.commit()


def _classify_active_release(settings, sample_png_base64: str) -> str:
    realtime_app.state.settings = settings
    with TestClient(realtime_app) as client:
        realtime_app.state.realtime_gateway_service = RealtimeGatewayService(
            settings,
            service_name="inference-gateway-rt",
            target_path=TargetPath.REALTIME,
        )
        response = client.post(
            "/api/v1/realtime/classify",
            json={
                "deployment_target": "widget-inspection-rt",
                "image_base64": sample_png_base64,
            },
            headers={"Authorization": "Bearer rt-client.rt-secret"},
        )
    assert response.status_code == 200
    return str(response.json()["route"]["deployment_release_id"])


def _seed_ready_snapshot(settings, engine) -> None:
    snapshot_root = settings.storage.snapshot_root("dss_training_ready")
    snapshot_root.mkdir(parents=True, exist_ok=True)
    (snapshot_root / "snapshot_manifest.json").write_text('{"item_count": 24}', encoding="utf-8")
    (snapshot_root / "label_map.json").write_text('["accepted", "rework"]', encoding="utf-8")

    with Session(engine, expire_on_commit=False) as session:
        dataset = Dataset(
            id="dst_training",
            slug="training-dataset",
            name="Training Dataset",
            task_type=TaskType.CLASSIFICATION.value,
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
            manifest_uri=str(snapshot_root / "snapshot_manifest.json"),
            content_sha256="sha256-training",
            dvc_rev="dvc-training",
            created_by="tester",
            export_format="JSON",
            label_schema_version="1",
            split_metadata={},
            validation_report={},
            validation_status="passed",
            statistics={"item_count": 24},
            idempotency_key="training-snapshot-key",
        )
        session.add_all([dataset, snapshot])
        session.commit()
