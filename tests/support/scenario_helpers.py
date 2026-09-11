from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cv_contracts import TrainingJobCreateRequest
from cv_db import (
    Dataset,
    DatasetSnapshot,
    create_promotion_request,
    create_training_job_request,
    record_deployment_approval,
    register_trained_model_version,
)
from cv_db.models import DeploymentRelease, DeploymentTarget, ModelFamily, ModelVersion
from cv_status import (
    DatasetStatus,
    DeploymentApprovalScope,
    DeploymentApprovalStatus,
    SnapshotStatus,
    TargetPath,
    TaskType,
)


def login_admin(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "adminadmin"},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        _ = ex
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def get(self, key: str) -> bytes | None:
        value = self.store.get(key)
        return value.encode("utf-8") if value is not None else None

    def delete(self, key: str) -> None:
        self.store.pop(key, None)


class FakeQueue:
    def __init__(self, *_args, **_kwargs) -> None:
        self.enqueued: list[tuple[str, dict[str, object]]] = []

    def enqueue(self, job_name: str, **kwargs) -> None:
        self.enqueued.append((job_name, kwargs))

    def enqueue_in(self, *_args, **kwargs) -> None:
        job_name = str(kwargs.pop("func", "") or kwargs.pop("job_name", ""))
        self.enqueued.append((job_name, kwargs))


class FlakyTrainer:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, *, config, control, resume_state=None):  # noqa: ANN001
        from cv_training.trainer import TrainerProgress, TrainerResult
        from training_worker.runtime import RetryableTrainingError

        self.calls += 1
        control.on_progress(
            TrainerProgress(
                epoch_no=1,
                step_no=5,
                total_epochs=config.max_epochs,
                total_steps=config.max_epochs * config.steps_per_epoch,
                training_metrics={"loss": 0.5},
                validation_metrics={
                    "accuracy_top1": 0.7,
                    "precision_macro": 0.68,
                    "recall_macro": 0.67,
                    "f1_macro": 0.675,
                },
            )
        )
        control.on_checkpoint(
            epoch_no=1,
            step_no=5,
            metrics={
                "accuracy_top1": 0.7,
                "precision_macro": 0.68,
                "recall_macro": 0.67,
                "f1_macro": 0.675,
            },
            checkpoint_kind="interval",
            is_best=True,
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
                validation_metrics={
                    "accuracy_top1": 0.94,
                    "precision_macro": 0.92,
                    "recall_macro": 0.91,
                    "f1_macro": 0.915,
                },
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


class FakeLabelStudioClient:
    def __init__(
        self,
        *,
        projects: list[dict[str, object]] | None = None,
        export_snapshot: dict[str, object] | None = None,
        export_payload: list[dict[str, object]] | None = None,
    ) -> None:
        self.projects = projects or []
        self.export_snapshot = export_snapshot or {"id": 1, "status": "completed"}
        self.export_payload = export_payload or []
        self.imported_tasks: list[tuple[int, list[dict[str, object]]]] = []

    def list_projects(self) -> list[dict[str, object]]:
        return list(self.projects)

    def create_export_snapshot(self, project_id: int) -> dict[str, object]:
        return {"id": int(self.export_snapshot["id"]), "status": "created", "project_id": project_id}

    def get_export_snapshot(self, project_id: int, export_id: int) -> dict[str, object]:
        return {
            **self.export_snapshot,
            "id": export_id,
            "project_id": project_id,
        }

    def download_export_snapshot(self, project_id: int, export_id: int, export_type: str) -> bytes:
        _ = (project_id, export_id, export_type)
        return json.dumps(self.export_payload).encode("utf-8")

    def import_tasks(self, project_id: int, tasks: list[dict[str, object]]) -> dict[str, object]:
        self.imported_tasks.append((project_id, tasks))
        return {"task_count": len(tasks), "project_id": project_id}


def seed_release(
    session: Session,
    *,
    tmp_path: Path,
    target_path: TargetPath,
    task_type: TaskType,
    release_suffix: str,
    active: bool,
    previous_release_id: str | None = None,
) -> str:
    family = ModelFamily(
        id=f"mfy_{release_suffix}",
        slug=f"{release_suffix}-{task_type.value}",
        task_type=task_type.value,
        contract_major=1,
        input_schema_ref=f"contracts/{task_type.value}/input.v1.json",
        output_schema_ref=f"contracts/{task_type.value}/output.v1.json",
        active=True,
    )
    labels = ["accepted", "rework"] if task_type == TaskType.CLASSIFICATION else ["defect", "scratch"]
    package_root = tmp_path / f"package_{release_suffix}"
    class_map_path = package_root / "source" / "class_map.json"
    class_map_path.parent.mkdir(parents=True, exist_ok=True)
    class_map_path.write_text(json.dumps(labels), encoding="utf-8")
    version = ModelVersion(
        id=f"mvr_{release_suffix}",
        model_family_id=family.id,
        version_no=1,
        status="production" if active else "staged",
        serving_manifest_uri=str(package_root / "manifest.json"),
        serving_manifest={
            "package_root": str(package_root),
            "class_count": len(labels),
            "confidence_threshold_default": 0.5,
            "compatible_targets": [target_path.value],
            "preprocessing": {"resize": {"width": 224, "height": 224}},
            "postprocessing": {"class_map": labels},
            "io_schema": {
                "input_schema_ref": family.input_schema_ref,
                "output_schema_ref": family.output_schema_ref,
                "contract_major": 1,
                "schema_digest": f"schema-{release_suffix}",
            },
            "hardware_compatibility": {
                "max_batch_size": {"realtime": 1, "batch": 8},
            },
            "source_manifests": {
                "class_map": {"path": "source/class_map.json"},
            },
        },
    )
    release = DeploymentRelease(
        id=f"rel_{release_suffix}",
        target_path=target_path.value,
        model_family_id=family.id,
        model_version_id=version.id,
        triton_model_name=f"{target_path.value}_{release_suffix}",
        triton_model_version=1,
        status="activated",
    )
    session.add_all([family, version, release])
    session.flush()
    target = session.get(DeploymentTarget, target_path.value)
    if target is None:
        target = DeploymentTarget(
            target_path=target_path.value,
            active_release_id=release.id if active else None,
            previous_release_id=previous_release_id,
            updated_at=release.created_at,
        )
        session.add(target)
    if active:
        target.active_release_id = release.id
        target.previous_release_id = previous_release_id
        target.updated_at = release.created_at
    elif previous_release_id is None and target is not None:
        target.previous_release_id = release.id
        target.updated_at = release.created_at
    session.flush()
    return release.id


def seed_registered_model_version(
    session: Session,
    *,
    tmp_path: Path,
    suffix: str,
    gpu_profile: str = "nvidia-rtx-4500-ada",
):
    dataset = Dataset(
        id=f"dst_{suffix}",
        slug=f"deployment-{suffix}",
        name=f"Deployment Dataset {suffix}",
        task_type=TaskType.CLASSIFICATION.value,
        label_schema_version="1",
        status=DatasetStatus.SNAPSHOTTED.value,
        created_by="tester",
    )
    snapshot = DatasetSnapshot(
        id=f"dss_deploy_{suffix}",
        dataset_id=dataset.id,
        label_project_id=None,
        snapshot_no=1,
        status=SnapshotStatus.READY.value,
        item_count=24,
        annotation_count=24,
        manifest_uri=str(tmp_path / f"snapshot_{suffix}" / "snapshot_manifest.json"),
        content_sha256=f"checksum-{suffix}",
        dvc_rev=f"dvc-{suffix}",
        created_by="tester",
        export_format="JSON",
        label_schema_version="1",
        split_metadata={},
        validation_report={},
        validation_status="passed",
        statistics={"item_count": 24},
        idempotency_key=f"snapshot-key-{suffix}",
    )
    session.add_all([dataset, snapshot])
    session.flush()

    job = create_training_job_request(
        session,
        payload=TrainingJobCreateRequest.model_validate(
            {
                "model_family_slug": "widget-inspection-cls",
                "dataset_snapshot_id": snapshot.id,
                "task_type": "classification",
                "recipe_name": "baseline_cls",
                "recipe_version": "1.0.0",
                "max_epochs": 5,
                "checkpoint_interval_steps": 5,
                "parameter_overrides": {"epochs": 5},
                "best_checkpoint_rule": {
                    "metric_name": "accuracy_top1",
                    "mode": "max",
                    "phase": "evaluation",
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
            }
        ),
        requested_by="tester",
        source_service="test-suite",
        idempotency_key=f"training-{suffix}",
    )

    artifact_root = tmp_path / f"artifacts_{suffix}"
    artifact_root.mkdir(parents=True, exist_ok=True)
    onnx_path = artifact_root / "model.onnx"
    plan_path = artifact_root / "model.plan"
    artifact_manifest_path = artifact_root / "artifact_manifest.json"
    export_manifest_path = artifact_root / "export_manifest.json"

    onnx_path.write_text(json.dumps({"format": "onnx", "suffix": suffix}), encoding="utf-8")
    plan_path.write_text(json.dumps({"format": "tensorrt", "suffix": suffix}), encoding="utf-8")

    artifact_manifest = {
        "training_job_id": job.id,
        "artifacts": {
            "onnx": {"path": str(onnx_path), "sha256": f"onnx-{suffix}"},
            "tensorrt": {"path": str(plan_path), "sha256": f"plan-{suffix}"},
        },
    }
    export_manifest = {
        "training_job_id": job.id,
        "model_family_slug": job.model_family_slug,
        "task_type": "classification",
        "recipe_name": "baseline_cls",
        "recipe_version": "1.0.0",
        "class_map_version": f"{snapshot.id}:classes",
        "class_map_sha256": f"class-map-{suffix}",
        "class_count": 2,
        "confidence_threshold_default": 0.5,
        "preprocessing": {"resize": {"width": 224, "height": 224}},
        "postprocessing": {
            "class_map": ["accepted", "rework"],
            "confidence_threshold_default": 0.5,
        },
        "nms": {"enabled": False},
        "io_schema": {
            "input_tensors": [
                {
                    "name": "input__image",
                    "data_type": "fp32",
                    "dims": [3, 224, 224],
                    "layout": "NCHW",
                }
            ],
            "output_tensors": [
                {
                    "name": "output__probabilities",
                    "data_type": "fp32",
                    "dims": [2],
                }
            ],
            "input_schema_ref": "contracts/classification/input.v1.json",
            "output_schema_ref": "contracts/classification/output.v1.json",
            "contract_major": 1,
            "schema_digest": f"schema-{suffix}",
        },
        "hardware_compatibility": {
            "accelerator": "nvidia_gpu",
            "target_gpu_profile": gpu_profile,
            "min_compute_capability": "8.9",
            "cuda_version": "12.4",
            "tensorrt_version": "10.0",
            "precision": "fp16",
            "max_batch_size": {"realtime": 1, "batch": 64},
            "validated_devices": ["cuda:0"],
        },
        "onnx": {
            "uri": str(onnx_path),
            "sha256": f"onnx-{suffix}",
            "size_bytes": onnx_path.stat().st_size,
            "format": "onnx",
            "file_name": onnx_path.name,
        },
        "tensorrt": {
            "uri": str(plan_path),
            "sha256": f"plan-{suffix}",
            "size_bytes": plan_path.stat().st_size,
            "format": "tensorrt_plan",
            "file_name": plan_path.name,
        },
        "build_manifest": {
            "precision": "fp16",
            "workspace_size_mb": 4096,
        },
    }
    artifact_manifest_path.write_text(json.dumps(artifact_manifest, indent=2), encoding="utf-8")
    export_manifest_path.write_text(json.dumps(export_manifest, indent=2), encoding="utf-8")

    return register_trained_model_version(
        session,
        training_job_id=job.id,
        metrics_summary={"accuracy_top1": 0.95, "f1_macro": 0.94},
        evaluation_summary={
            "primary_metric_name": "accuracy_top1",
            "primary_metric_value": 0.95,
            "metrics": {"accuracy_top1": 0.95, "f1_macro": 0.94},
            "promotion_ready": True,
            "promotion_results": [],
        },
        artifact_manifest_uri=str(artifact_manifest_path),
        artifact_manifest=artifact_manifest,
        export_manifest_uri=str(export_manifest_path),
        export_manifest=export_manifest,
        onnx_uri=str(onnx_path),
        tensorrt_uri=str(plan_path),
        dvc_rev=snapshot.dvc_rev,
        dvc_references={"bundle_dvc_file": f"registry/{job.id}.dvc"},
        reproducibility_metadata={"code_revision": "git-test-rev"},
        code_revision="git-test-rev",
        mlflow_run_id=f"mlf-{suffix}",
        mlflow_registered_model=job.model_family_slug,
        mlflow_model_version="1",
        alias_names=["latest-trained", "candidate"],
        registry_tags={"cv_platform.lifecycle": "trained"},
        immutable_metadata={"dataset_snapshot_id": snapshot.id, "training_job_id": job.id},
        immutable_metadata_sha256=f"sha-{suffix}",
    )


def approve_and_request_staging(session: Session, *, model_version_id: str, target_path: str = "realtime") -> str:
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
