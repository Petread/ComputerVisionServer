from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from cv_contracts import TrainingJobCreateRequest
from cv_db import (
    Base,
    Dataset,
    DatasetSnapshot,
    activate_deployment_model_release,
    create_deployment_release_for_promotion,
    create_promotion_request,
    create_training_job_request,
    deactivate_deployment_model_releases,
    get_deployment_release_detail,
    get_model_version_detail,
    list_deployment_releases,
    mark_promotion_staged,
    mark_rollback_candidate,
    promote_staged_release_to_production,
    record_deployment_approval,
    register_trained_model_version,
    rollback_target_to_previous_release,
    update_deployment_release_status,
    validate_deployment_model_serving_command,
)
from cv_db.model_catalog import architecture_summary
from cv_db.models import DeploymentRelease, DeploymentTarget, RuntimeExecution
from cv_db.registry_store import compare_model_versions as compare_registry_versions
from cv_status import (
    DatasetStatus,
    DeploymentApprovalScope,
    DeploymentApprovalStatus,
    DeploymentReleaseStatus,
    ModelLifecycleStatus,
    RuntimeExecutionStatus,
    SnapshotStatus,
    TaskType,
)
from cv_utils import utc_now_iso


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def seed_training_job(session: Session, *, snapshot_id: str = "dss_registry_ready"):
    dataset_id = f"dst_{snapshot_id}"
    dataset = Dataset(
        id=dataset_id,
        slug=f"registry-{snapshot_id}",
        name=f"Registry Dataset {snapshot_id}",
        task_type=TaskType.CLASSIFICATION.value,
        label_schema_version="1",
        status=DatasetStatus.SNAPSHOTTED.value,
        created_by="tester",
    )
    snapshot = DatasetSnapshot(
        id=snapshot_id,
        dataset_id=dataset.id,
        label_project_id=None,
        snapshot_no=1,
        status=SnapshotStatus.READY.value,
        item_count=100,
        annotation_count=100,
        manifest_uri=f"{snapshot_id}/manifest.json",
        content_sha256=f"checksum-{snapshot_id}",
        dvc_rev=f"dvc-{snapshot_id}",
        created_by="tester",
        export_format="JSON",
        label_schema_version="1",
        split_metadata={"train_ratio": 80, "val_ratio": 10, "test_ratio": 10},
        validation_report={},
        validation_status="passed",
        statistics={"item_count": 100},
        idempotency_key=f"key-{snapshot_id}",
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
                "architecture": architecture_summary(
                    task_type=TaskType.CLASSIFICATION,
                    family="resnet",
                    version="v1",
                    size="18",
                ).model_dump(mode="json"),
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
        idempotency_key=f"training-{snapshot_id}",
    )
    return dataset, snapshot, job


def register_version(session: Session, *, snapshot_id: str = "dss_registry_ready", accuracy: float = 0.94):
    _dataset, snapshot, job = seed_training_job(session, snapshot_id=snapshot_id)
    version = register_trained_model_version(
        session,
        training_job_id=job.id,
        metrics_summary={"accuracy_top1": accuracy, "f1_macro": accuracy - 0.01},
        evaluation_summary={
            "primary_metric_name": "accuracy_top1",
            "primary_metric_value": accuracy,
            "metrics": {"accuracy_top1": accuracy, "f1_macro": accuracy - 0.01},
            "promotion_ready": accuracy >= 0.9,
            "promotion_results": [],
            "trainer_mode": "real",
            "trainer_backend": "unit-test-backend",
        },
        artifact_manifest_uri=f"/artifacts/{job.id}/artifact_manifest.json",
        artifact_manifest={"artifacts": {"onnx": {"sha256": "onnx-sha"}}},
        export_manifest_uri=f"/artifacts/{job.id}/export_manifest.json",
        export_manifest={
            "exports": {"tensorrt": {"sha256": "plan-sha"}},
            "trainer_mode": "real",
            "trainer_backend": "unit-test-backend",
            "artifact_validation": {
                "status": "passed",
                "checks": {"checkpoint_valid": True, "onnx_valid": True},
                "details": {},
            },
            "onnx": {"uri": f"/artifacts/{job.id}/model.onnx"},
        },
        onnx_uri=f"/artifacts/{job.id}/model.onnx",
        tensorrt_uri=f"/artifacts/{job.id}/model.plan",
        dvc_rev=snapshot.dvc_rev,
        dvc_references={"bundle_dvc_file": f"registry/{job.id}.dvc"},
        reproducibility_metadata={"code_revision": "git-test-rev", "recorded_at": utc_now_iso()},
        code_revision="git-test-rev",
        mlflow_run_id=f"mlf-{job.id}",
        mlflow_registered_model=job.model_family_slug,
        mlflow_model_version="1",
        alias_names=["latest-trained", "candidate"],
        registry_tags={
            "cv_platform.lifecycle": "trained",
            "cv_platform.trainer_mode": "real",
            "cv_platform.trainer_backend": "unit-test-backend",
        },
        immutable_metadata={
            "dataset_snapshot_id": snapshot.id,
            "training_job_id": job.id,
            "code_revision": "git-test-rev",
            "trainer_mode": "real",
            "trainer_backend": "unit-test-backend",
            "artifact_validation": {
                "status": "passed",
                "checks": {"checkpoint_valid": True, "onnx_valid": True},
                "details": {},
            },
        },
        immutable_metadata_sha256=f"sha-{job.id}",
    )
    session.flush()
    return snapshot, job, version


def test_register_trained_model_version_persists_immutable_lineage_metadata(engine) -> None:
    with Session(engine, expire_on_commit=False) as session:
        snapshot, job, version = register_version(session)
        session.commit()

    with Session(engine, expire_on_commit=False) as session:
        detail = get_model_version_detail(session, version.id)

    assert detail is not None
    assert detail.status == ModelLifecycleStatus.TRAINED
    assert detail.dataset_snapshot_id == snapshot.id
    assert detail.lineage.training_job_id == job.id
    assert detail.lineage.code_revision == "git-test-rev"
    assert detail.lineage.dvc_references["bundle_dvc_file"] == f"registry/{job.id}.dvc"
    assert detail.export_manifest_uri == f"/artifacts/{job.id}/export_manifest.json"
    assert detail.immutable_metadata_sha256 == f"sha-{job.id}"


def test_promotion_requires_approval_before_request_is_created(engine) -> None:
    with Session(engine, expire_on_commit=False) as session:
        _snapshot, _job, version = register_version(session)

        with pytest.raises(ValueError):
            create_promotion_request(
                session,
                model_version_id=version.id,
                target_path="realtime",
                requested_by="tester",
                requested_state="staged",
            )

        record_deployment_approval(
            session,
            model_version_id=version.id,
            target_path="realtime",
            approval_scope=DeploymentApprovalScope.STAGING.value,
            status=DeploymentApprovalStatus.APPROVED.value,
            decided_by="tester",
            notes="approved",
        )
        promotion_id = create_promotion_request(
            session,
            model_version_id=version.id,
            target_path="realtime",
            requested_by="tester",
            requested_state="staged",
        )
        session.commit()

    assert promotion_id.startswith("prm_")


def test_deployment_approval_respects_promotion_gate(engine) -> None:
    with Session(engine, expire_on_commit=False) as session:
        _snapshot, _job, version = register_version(
            session,
            snapshot_id="dss_registry_failed_gate",
            accuracy=0.1,
        )

        with pytest.raises(ValueError, match="resolved deployment promotion criteria"):
            record_deployment_approval(
                session,
                model_version_id=version.id,
                target_path="realtime",
                approval_scope=DeploymentApprovalScope.STAGING.value,
                status=DeploymentApprovalStatus.APPROVED.value,
                decided_by="tester",
                notes="should fail",
            )

        approval = record_deployment_approval(
            session,
            model_version_id=version.id,
            target_path="realtime",
            approval_scope=DeploymentApprovalScope.STAGING.value,
            status=DeploymentApprovalStatus.APPROVED.value,
            decided_by="tester",
            notes="testing override",
            allow_quality_gate_bypass=True,
        )

    assert approval.status == DeploymentApprovalStatus.APPROVED.value


def test_production_promotion_and_rollback_metadata_are_tracked(engine) -> None:
    with Session(engine, expire_on_commit=False) as session:
        _snapshot_a, _job_a, version_a = register_version(session, snapshot_id="dss_registry_a", accuracy=0.93)
        record_deployment_approval(
            session,
            model_version_id=version_a.id,
            target_path="realtime",
            approval_scope=DeploymentApprovalScope.STAGING.value,
            status=DeploymentApprovalStatus.APPROVED.value,
            decided_by="tester",
            notes="stage first",
        )
        promotion_a = create_promotion_request(
            session,
            model_version_id=version_a.id,
            target_path="realtime",
            requested_by="tester",
            requested_state="staged",
        )
        release_a = create_deployment_release_for_promotion(session, promotion_id=promotion_a)
        mark_promotion_staged(session, promotion_id=promotion_a)
        record_deployment_approval(
            session,
            model_version_id=version_a.id,
            target_path="realtime",
            approval_scope=DeploymentApprovalScope.PRODUCTION.value,
            status=DeploymentApprovalStatus.APPROVED.value,
            decided_by="tester",
            notes="go live",
        )
        create_promotion_request(
            session,
            model_version_id=version_a.id,
            target_path="realtime",
            requested_by="tester",
            requested_state="production",
        )
        update_deployment_release_status(
            session,
            deployment_release_id=release_a.id,
            status=DeploymentReleaseStatus.ACTIVATED,
            smoke_report={"passed": True},
        )
        promote_staged_release_to_production(session, promotion_id=promotion_a)
        mark_rollback_candidate(
            session,
            model_version_id=version_a.id,
            target_path="realtime",
            enabled=True,
            marked_by="tester",
            notes="keep as rollback target",
        )

        _snapshot_b, _job_b, version_b = register_version(session, snapshot_id="dss_registry_b", accuracy=0.97)
        record_deployment_approval(
            session,
            model_version_id=version_b.id,
            target_path="realtime",
            approval_scope=DeploymentApprovalScope.STAGING.value,
            status=DeploymentApprovalStatus.APPROVED.value,
            decided_by="tester",
            notes="stage second",
        )
        promotion_b = create_promotion_request(
            session,
            model_version_id=version_b.id,
            target_path="realtime",
            requested_by="tester",
            requested_state="staged",
        )
        release_b = create_deployment_release_for_promotion(session, promotion_id=promotion_b)
        mark_promotion_staged(session, promotion_id=promotion_b)
        record_deployment_approval(
            session,
            model_version_id=version_b.id,
            target_path="realtime",
            approval_scope=DeploymentApprovalScope.PRODUCTION.value,
            status=DeploymentApprovalStatus.APPROVED.value,
            decided_by="tester",
            notes="promote second",
        )
        create_promotion_request(
            session,
            model_version_id=version_b.id,
            target_path="realtime",
            requested_by="tester",
            requested_state="production",
        )
        update_deployment_release_status(
            session,
            deployment_release_id=release_b.id,
            status=DeploymentReleaseStatus.ACTIVATED,
            smoke_report={"passed": True},
        )
        target = promote_staged_release_to_production(session, promotion_id=promotion_b)
        session.commit()

    assert target.previous_release_id == release_a.id

    with Session(engine, expire_on_commit=False) as session:
        rolled_back = rollback_target_to_previous_release(session, target_path="realtime")
        session.commit()

    assert rolled_back.active_release_id == release_a.id
    assert rolled_back.previous_release_id is not None


def test_deployment_model_can_be_deactivated_and_activated_without_deletion(engine) -> None:
    with Session(engine, expire_on_commit=False) as session:
        _snapshot_a, _job_a, version_a = register_version(
            session,
            snapshot_id="dss_serving_a",
        )
        _snapshot_b, _job_b, version_b = register_version(
            session,
            snapshot_id="dss_serving_b",
        )
        session.add_all(
            [
                DeploymentRelease(
                    id="rel_serving_a",
                    target_path="realtime",
                    model_family_id=version_a.model_family_id,
                    model_version_id=version_a.id,
                    triton_model_name="rt__serving_toggle",
                    triton_model_version=1,
                    status=DeploymentReleaseStatus.ACTIVATED.value,
                ),
                DeploymentRelease(
                    id="rel_serving_b",
                    target_path="realtime",
                    model_family_id=version_b.model_family_id,
                    model_version_id=version_b.id,
                    triton_model_name="rt__serving_toggle",
                    triton_model_version=2,
                    status=DeploymentReleaseStatus.ACTIVATED.value,
                ),
            ]
        )
        session.flush()

        selected, changed_ids = deactivate_deployment_model_releases(
            session,
            deployment_release_id="rel_serving_b",
        )

        assert selected.id == "rel_serving_b"
        assert changed_ids == ["rel_serving_a", "rel_serving_b"]
        assert {
            item.status
            for item in list_deployment_releases(session)
            if item.triton_model_name == "rt__serving_toggle"
        } == {DeploymentReleaseStatus.DEACTIVATED}
        assert all(
            not item.serving_enabled and item.serving_release_id is None
            for item in list_deployment_releases(session)
            if item.triton_model_name == "rt__serving_toggle"
        )
        detail = get_deployment_release_detail(
            session,
            deployment_release_id="rel_serving_b",
        )

        assert detail is not None
        assert detail.architecture.task_type == TaskType.CLASSIFICATION

        activated = activate_deployment_model_release(
            session,
            deployment_release_id="rel_serving_a",
        )
        summaries = [
            item
            for item in list_deployment_releases(session)
            if item.triton_model_name == "rt__serving_toggle"
        ]

    assert activated.status == DeploymentReleaseStatus.ACTIVATED.value
    assert all(item.serving_enabled for item in summaries)
    assert {item.serving_release_id for item in summaries} == {"rel_serving_a"}


def test_active_target_release_cannot_be_deactivated(engine) -> None:
    with Session(engine, expire_on_commit=False) as session:
        _snapshot, _job, version = register_version(
            session,
            snapshot_id="dss_serving_production",
        )
        session.add(
            DeploymentRelease(
                id="rel_serving_production",
                target_path="realtime",
                model_family_id=version.model_family_id,
                model_version_id=version.id,
                triton_model_name="rt__serving_production",
                triton_model_version=1,
                status=DeploymentReleaseStatus.ACTIVATED.value,
            )
        )
        session.flush()
        session.add(
            DeploymentTarget(
                target_path="realtime",
                active_release_id="rel_serving_production",
            )
        )
        session.flush()

        with pytest.raises(ValueError, match="active production release"):
            validate_deployment_model_serving_command(
                session,
                deployment_release_id="rel_serving_production",
                enable=False,
            )


def test_running_runtime_release_cannot_be_deactivated(engine) -> None:
    with Session(engine, expire_on_commit=False) as session:
        _snapshot, _job, version = register_version(
            session,
            snapshot_id="dss_serving_runtime",
        )
        session.add(
            DeploymentRelease(
                id="rel_serving_runtime",
                target_path="realtime",
                model_family_id=version.model_family_id,
                model_version_id=version.id,
                triton_model_name="rt__serving_runtime",
                triton_model_version=1,
                status=DeploymentReleaseStatus.ACTIVATED.value,
            )
        )
        session.add(
            RuntimeExecution(
                id="rte_serving_runtime",
                channel_id="chn_serving_runtime",
                pipeline_version_id="plv_serving_runtime",
                executor_service="realtime-executor",
                status=RuntimeExecutionStatus.RUNNING.value,
                runtime_plan={
                    "binding_signature": {
                        "model_bindings": [
                            {"deployment_release_id": "rel_serving_runtime"}
                        ]
                    }
                },
            )
        )
        session.flush()

        with pytest.raises(ValueError, match="running runtime channel"):
            validate_deployment_model_serving_command(
                session,
                deployment_release_id="rel_serving_runtime",
                enable=False,
            )


def test_compare_versions_surfaces_metric_and_lineage_differences(engine) -> None:
    with Session(engine, expire_on_commit=False) as session:
        _snapshot_a, _job_a, version_a = register_version(session, snapshot_id="dss_registry_cmp_a", accuracy=0.91)
        _snapshot_b, _job_b, version_b = register_version(session, snapshot_id="dss_registry_cmp_b", accuracy=0.96)
        comparison = compare_registry_versions(
            session,
            model_version_ids=[version_a.id, version_b.id],
        )

    assert len(comparison.metrics) >= 1
    assert any(metric.metric_name == "accuracy_top1" for metric in comparison.metrics)
    assert any(field.field_name == "dataset_snapshot_id" for field in comparison.lineage_fields)
