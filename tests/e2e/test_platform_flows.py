from __future__ import annotations

from pathlib import Path

from admin_api.main import app as admin_app
from batch_worker import jobs as batch_jobs
from control_worker.jobs.deployment import promote_model_release, stage_model_release
from cv_db import Dataset, DatasetSnapshot, get_training_job_detail
from cv_db.models import ModelVersion
from cv_status import DatasetStatus, SnapshotStatus, TargetPath, TaskType
from fastapi.testclient import TestClient
from inference_gateway.main_realtime import app as realtime_app
from inference_gateway.runtime.service import RealtimeGatewayService
from sqlalchemy.orm import Session
from tests.conftest import build_test_settings, configure_sqlite_factories
from tests.support.scenario_helpers import (
    FakeDvcAdapter,
    FakeMlflowAdapter,
    FakeRedis,
    login_admin,
    seed_release,
)
from training_worker.runtime import run_training_workflow


class _NoopQueue:
    def __init__(self, *_args, **_kwargs) -> None:
        self.enqueued: list[tuple[str, dict[str, object]]] = []

    def enqueue(self, func: str, **kwargs) -> None:
        self.enqueued.append((func, kwargs))


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


class _SuccessfulTrainer:
    trainer_backend = "unit-test-backend"

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
        tensorrt_path = config.tensorrt_dir / "model.plan"
        tensorrt_path.parent.mkdir(parents=True, exist_ok=True)
        tensorrt_path.write_bytes(b"unit-test-tensorrt")
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
                    "accuracy_top1": 0.91,
                    "precision_macro": 0.9,
                    "recall_macro": 0.89,
                    "f1_macro": 0.895,
                },
            )
        )
        control.on_checkpoint(
            TrainerCheckpointArtifact(
                path=str(checkpoint_path),
                epoch_no=1,
                step_no=5,
                metrics={
                    "accuracy_top1": 0.91,
                    "precision_macro": 0.9,
                    "recall_macro": 0.89,
                    "f1_macro": 0.895,
                },
                checkpoint_kind="interval",
                is_best=True,
            )
        )
        return TrainerResult(
            final_epoch=config.max_epochs,
            final_step=config.max_epochs * config.steps_per_epoch,
            best_metric_name="accuracy_top1",
            best_metric_value=0.94,
            best_metrics={
                "accuracy_top1": 0.94,
                "precision_macro": 0.92,
                "recall_macro": 0.91,
                "f1_macro": 0.915,
            },
            final_validation_metrics={
                "accuracy_top1": 0.94,
                "precision_macro": 0.92,
                "recall_macro": 0.91,
                "f1_macro": 0.915,
            },
            export_artifacts=(
                TrainerExportArtifact(
                    kind="onnx",
                    path=str(onnx_path),
                    generated=True,
                    status="ready",
                ),
                TrainerExportArtifact(
                    kind="tensorrt",
                    path=str(tensorrt_path),
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


def test_training_registration_deployment_and_realtime_inference(
    sqlite_engine,
    tmp_path: Path,
    monkeypatch,
    sample_png_base64: str,
) -> None:
    configure_sqlite_factories(
        sqlite_engine,
        "admin-api",
        "control-worker",
        "training-worker",
        "inference-gateway-rt",
    )
    admin_settings = build_test_settings("admin-api", tmp_path)
    control_settings = build_test_settings("control-worker", tmp_path)
    training_settings = build_test_settings("training-worker", tmp_path)
    realtime_settings = build_test_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[
            (
                "rt-client",
                "rt-secret",
                ["realtime.predict"],
                "inference_client",
                "realtime-e2e",
            )
        ],
    )
    admin_app.state.settings = admin_settings
    realtime_app.state.settings = realtime_settings

    _seed_ready_snapshot(training_settings, sqlite_engine)

    training_queue = _NoopQueue()
    deploy_queue = _NoopQueue()
    fake_triton = _FakeTritonClient()
    fake_redis = FakeRedis()

    monkeypatch.setattr("admin_api.api.routes.training._training_queue", lambda _settings: training_queue)
    monkeypatch.setattr("admin_api.api.routes.deployments._deploy_queue", lambda _settings: deploy_queue)
    monkeypatch.setattr("training_worker.runtime._load_worker_settings", lambda: training_settings)
    monkeypatch.setattr("training_worker.runtime.Redis.from_url", lambda _url: fake_redis)
    monkeypatch.setattr(
        "training_worker.runtime.get_default_trainer",
        lambda _task_type, **_kwargs: _SuccessfulTrainer(),
    )
    monkeypatch.setattr("training_worker.runtime.build_mlflow_adapter", lambda _settings: FakeMlflowAdapter())
    monkeypatch.setattr("training_worker.runtime.build_dvc_adapter", lambda _settings: FakeDvcAdapter())
    monkeypatch.setattr(
        "control_worker.jobs.deployment.load_platform_settings",
        lambda _service: control_settings,
    )
    monkeypatch.setattr("control_worker.jobs.deployment._triton_client", lambda _settings, _target: fake_triton)

    with TestClient(admin_app) as client:
        csrf_token = login_admin(client)
        training_response = client.post(
            "/api/v1/training-jobs",
            json={
                "model_family_slug": "widget-inspection-cls",
                "dataset_snapshot_id": "dss_training_ready",
                "task_type": "classification",
                "recipe_name": "baseline_cls",
                "recipe_version": "1.0.0",
                "max_epochs": 5,
                "checkpoint_interval_steps": 5,
                "parameter_overrides": {"epochs": 5, "checkpoint_interval_steps": 5},
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
        assert training_response.status_code == 202
        training_job_id = str(training_response.json()["job_id"])

        run_training_workflow(training_job_id)

        with Session(sqlite_engine, expire_on_commit=False) as session:
            training_detail = get_training_job_detail(session, training_job_id)
            assert training_detail is not None
            model_version_id = str(training_detail.model_version_id)

        detail_response = client.get(f"/api/v1/training-jobs/{training_job_id}")
        assert detail_response.status_code == 200
        assert detail_response.json()["status"] == "completed"

        registry_response = client.get(f"/api/v1/registry/model-versions/{model_version_id}")
        assert registry_response.status_code == 200
        registry_body = registry_response.json()
        assert registry_body["lineage"]["training_job_id"] == training_job_id
        assert registry_body["metrics_summary"]["accuracy_top1"] >= 0.9

        approve_staging = client.post(
            f"/api/v1/registry/model-versions/{model_version_id}:approve",
            json={
                "target_path": "realtime",
                "approval_scope": "staging",
                "notes": "stage for operator test",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert approve_staging.status_code == 200

        stage_request = client.post(
            "/api/v1/deployments/promotions",
            json={
                "model_version_id": model_version_id,
                "target_path": "realtime",
                "requested_state": "staged",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert stage_request.status_code == 202
        promotion_id = str(stage_request.json()["job_id"])

        stage_result = stage_model_release(promotion_id)
        assert stage_result["status"] == "staged"

        approve_production = client.post(
            f"/api/v1/registry/model-versions/{model_version_id}:approve",
            json={
                "target_path": "realtime",
                "approval_scope": "production",
                "notes": "promote to production",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert approve_production.status_code == 200

        production_request = client.post(
            "/api/v1/deployments/promotions",
            json={
                "model_version_id": model_version_id,
                "target_path": "realtime",
                "requested_state": "production",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert production_request.status_code == 202
        assert str(production_request.json()["job_id"]) == promotion_id

    promote_result = promote_model_release(promotion_id)
    assert promote_result["status"] == "production"

    with TestClient(realtime_app) as client:
        realtime_app.state.realtime_gateway_service = RealtimeGatewayService(
            realtime_settings,
            service_name="inference-gateway-rt",
            target_path=TargetPath.REALTIME,
        )
        inference_response = client.post(
            "/api/v1/realtime/classify",
            json={
                "deployment_target": "widget-inspection-rt",
                "image_base64": sample_png_base64,
            },
            headers={"Authorization": "Bearer rt-client.rt-secret"},
        )

    assert inference_response.status_code == 200
    assert inference_response.json()["schema_version"] == "inference.classification.response.v1"
    assert inference_response.json()["route"]["deployment_release_id"] == stage_result["deployment_release_id"]

    with Session(sqlite_engine, expire_on_commit=False) as session:
        version = session.get(ModelVersion, model_version_id)
        assert version is not None
        assert version.serving_manifest_uri is not None
        assert Path(version.serving_manifest_uri).exists()


def test_batch_submission_completes_and_results_are_retrievable(
    sqlite_engine,
    tmp_path: Path,
    monkeypatch,
    sample_png_base64: str,
) -> None:
    configure_sqlite_factories(
        sqlite_engine,
        "admin-api",
        "batch-worker",
        "inference-gateway-batch",
    )
    admin_settings = build_test_settings("admin-api", tmp_path)
    batch_worker_settings = build_test_settings("batch-worker", tmp_path)
    admin_app.state.settings = admin_settings

    with Session(sqlite_engine, expire_on_commit=False) as session:
        seed_release(
            session,
            tmp_path=tmp_path,
            target_path=TargetPath.BATCH,
            task_type=TaskType.CLASSIFICATION,
            release_suffix="batch_e2e",
            active=True,
        )
        session.commit()

    monkeypatch.setattr("inference_gateway.runtime.service.Redis.from_url", lambda _url: object())
    monkeypatch.setattr("inference_gateway.runtime.service.Queue", _NoopQueue)
    monkeypatch.setattr(batch_jobs, "load_platform_settings", lambda _service: batch_worker_settings)

    with TestClient(admin_app) as client:
        csrf_token = login_admin(client)
        submit_response = client.post(
            "/api/v1/batch-jobs",
            json={
                "deployment_target": "widget-inspection-batch",
                "inputs": [
                    {
                        "item_id": "img-1",
                        "image_base64": sample_png_base64,
                        "external_ref": "img-1.png",
                        "metadata": {},
                    },
                    {
                        "item_id": "img-2",
                        "image_base64": sample_png_base64,
                        "external_ref": "img-2.png",
                        "metadata": {},
                    },
                ],
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert submit_response.status_code == 202
        batch_job_id = str(submit_response.json()["batch_job_id"])

        materialized = batch_jobs.materialize_batch_input(batch_job_id)
        assert materialized["status"] == "queued"

        chunk_result = batch_jobs.run_batch_chunk(batch_job_id, 1)
        assert chunk_result["status"] == "completed"

        detail_response = client.get(f"/api/v1/batch-jobs/{batch_job_id}")
        results_response = client.get(f"/api/v1/batch-jobs/{batch_job_id}/results")

    assert detail_response.status_code == 200
    assert detail_response.json()["status"] == "completed"
    assert Path(detail_response.json()["artifacts"]["bundle_uri"]).exists()
    assert results_response.status_code == 200
    assert results_response.json()["metrics"]["sample_count"] == 2
    assert len(results_response.json()["items"]) == 2


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
