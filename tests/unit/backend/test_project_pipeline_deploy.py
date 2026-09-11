from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from admin_api.main import app as admin_app
from cv_db import Dataset, DatasetSnapshot, DeploymentRelease, DeploymentTarget, LabelProject
from cv_db.models import ModelFamily, ModelVersion, TrainingJob
from cv_db.orchestration_store import (
    get_project_model_workspace_detail,
    get_project_pipeline_detail,
    resolve_project_active_realtime_deployment,
    resolve_project_latest_approved_model_version,
)
from cv_status import (
    DatasetStatus,
    DeploymentApprovalScope,
    DeploymentReleaseStatus,
    LabelProjectStatus,
    QueueName,
    SnapshotStatus,
    TargetPath,
    TaskType,
)
from cv_utils import utc_now
from tests.conftest import build_test_settings, configure_sqlite_factories
from tests.support.scenario_helpers import login_admin


class _FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict[str, object]]] = []

    def enqueue(self, job_name: str, **kwargs) -> None:
        self.enqueued.append((job_name, kwargs))


class _FakeLabelStudioClient:
    created_payloads: list[dict[str, object]] = []

    def __init__(self, **_kwargs) -> None:
        pass

    def list_ml_backends(self, _project_id: int) -> list[dict[str, object]]:
        return []

    def create_ml_backend(self, **payload) -> dict[str, object]:
        self.created_payloads.append(payload)
        return {
            "id": 44,
            "project": payload["project_id"],
            "url": payload["url"],
            "title": payload["title"],
        }

    def update_ml_backend(self, backend_id: int, **payload) -> dict[str, object]:
        return {"id": backend_id, **payload}


def _seed_project_training_state(
    session: Session,
    *,
    include_older_approved_version: bool,
) -> str:
    dataset = Dataset(
        id="dst_pipeline",
        slug="thumbs",
        name="Thumbs",
        task_type=TaskType.CLASSIFICATION.value,
        label_schema_version="1",
        status=DatasetStatus.READY.value,
    )
    project = LabelProject(
        id="lpr_pipeline",
        dataset_id=dataset.id,
        label_studio_project_id=7,
        title="Daumen hoch runter",
        task_type=TaskType.CLASSIFICATION.value,
        label_config={"labels": ["gut", "schlecht"]},
        label_schema_version="1",
        training_pipeline_slug="daumenobenunten",
        status=LabelProjectStatus.ACTIVE.value,
        auto_deploy_enabled=False,
        minimum_annotation_count=1,
        required_classes=["gut", "schlecht"],
        train_ratio=80,
        val_ratio=10,
        test_ratio=10,
        qualifying_actions=["ANNOTATION_UPDATED"],
        task_count=10,
        annotated_task_count=10,
        annotation_count=10,
        class_distribution={"gut": 5, "schlecht": 5},
    )
    snapshot = DatasetSnapshot(
        id="dss_pipeline_ready",
        dataset_id=dataset.id,
        label_project_id=project.id,
        snapshot_no=1,
        status=SnapshotStatus.READY.value,
        item_count=10,
        annotation_count=10,
        manifest_uri="snapshot_manifest.json",
        content_sha256="snapshot-sha",
        dvc_rev="rev-1",
        created_by="tester",
        export_format="JSON",
        label_schema_version="1",
        split_metadata={"train_count": 8, "val_count": 1, "test_count": 1},
        validation_report={},
        validation_status="passed",
        statistics={"item_count": 10},
        idempotency_key="snapshot-pipeline-key",
    )
    family = ModelFamily(
        id="mfy_pipeline",
        slug="daumenobenunten",
        label_project_id=project.id,
        display_name="Daumen oben unten",
        template_key="classifier-small",
        training_profile_key="safe-default",
        task_type=TaskType.CLASSIFICATION.value,
        contract_major=1,
        input_schema_ref="contracts/classification/input.v1.json",
        output_schema_ref="contracts/classification/output.v1.json",
        active=True,
    )
    session.add_all([dataset, project, snapshot, family])
    session.commit()

    if include_older_approved_version:
        approved_job = TrainingJob(
            id="trn_pipeline_approved",
            model_family_id=family.id,
            model_family_slug=family.slug,
            dataset_snapshot_id=snapshot.id,
            task_type=TaskType.CLASSIFICATION.value,
            status="completed",
            queue_name=QueueName.TRAINING.value,
            requested_by="tester",
            recipe_name="baseline_cls",
            recipe_version="1.0.0",
            best_checkpoint_rule={"metric_name": "accuracy_top1", "mode": "max", "phase": "validation", "minimum_epoch": 1},
            promotion_criteria=[{"metric_name": "accuracy_top1", "comparison": "gte", "threshold": 0.9, "phase": "evaluation"}],
            config_snapshot={},
            progress={"progress_percent": 100},
            evaluation_summary={
                "primary_metric_name": "accuracy_top1",
                "primary_metric_value": 0.96,
                "metrics": {"accuracy_top1": 0.96},
                "promotion_ready": True,
                "promotion_results": [{"metric_name": "accuracy_top1", "comparison": "gte", "threshold": 0.9, "phase": "evaluation", "actual_value": 0.96, "passed": True}],
            },
            metrics_summary={"accuracy_top1": 0.96},
            model_version_id="mvr_pipeline_approved",
        )
        approved_version = ModelVersion(
            id="mvr_pipeline_approved",
            model_family_id=family.id,
            version_no=1,
            training_job_id=approved_job.id,
            dataset_snapshot_id=snapshot.id,
            status="validated",
            metrics_summary={"accuracy_top1": 0.96},
            evaluation_summary=approved_job.evaluation_summary,
        )
        session.add_all([approved_job, approved_version])
        session.commit()

    latest_job = TrainingJob(
        id="trn_pipeline_latest",
        model_family_id=family.id,
        model_family_slug=family.slug,
        dataset_snapshot_id=snapshot.id,
        task_type=TaskType.CLASSIFICATION.value,
        status="completed",
        queue_name=QueueName.TRAINING.value,
        requested_by="tester",
        recipe_name="baseline_cls",
        recipe_version="1.0.0",
        best_checkpoint_rule={"metric_name": "accuracy_top1", "mode": "max", "phase": "validation", "minimum_epoch": 1},
        promotion_criteria=[{"metric_name": "accuracy_top1", "comparison": "gte", "threshold": 0.9, "phase": "evaluation"}],
        config_snapshot={},
        progress={"progress_percent": 100},
        evaluation_summary={
            "primary_metric_name": "accuracy_top1",
            "primary_metric_value": 0.0,
            "metrics": {"accuracy_top1": 0.0},
            "promotion_ready": False,
            "promotion_results": [{"metric_name": "accuracy_top1", "comparison": "gte", "threshold": 0.9, "phase": "evaluation", "actual_value": 0.0, "passed": False}],
        },
        metrics_summary={"accuracy_top1": 0.0},
        model_version_id="mvr_pipeline_latest",
    )
    latest_version = ModelVersion(
        id="mvr_pipeline_latest",
        model_family_id=family.id,
        version_no=2,
        training_job_id=latest_job.id,
        dataset_snapshot_id=snapshot.id,
        status="validated",
        metrics_summary={"accuracy_top1": 0.0},
        evaluation_summary=latest_job.evaluation_summary,
    )
    session.add_all([latest_job, latest_version])
    session.commit()
    return project.id


def test_resolve_project_latest_approved_model_version_skips_failed_latest(sqlite_engine) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        project_id = _seed_project_training_state(
            session,
            include_older_approved_version=True,
        )

        approved_version = resolve_project_latest_approved_model_version(
            session,
            label_project_id=project_id,
        )

    assert approved_version is not None
    assert approved_version.id == "mvr_pipeline_approved"


def test_project_pipeline_detail_keeps_deployed_endpoint_when_global_target_moves(
    sqlite_engine,
) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        _seed_project_training_state(
            session,
            include_older_approved_version=True,
        )
        now = utc_now()
        project_release = DeploymentRelease(
            id="dpr_pipeline_rt",
            target_path=TargetPath.REALTIME.value,
            model_family_id="mfy_pipeline",
            model_version_id="mvr_pipeline_approved",
            triton_model_name="rt__daumenobenunten_v1",
            triton_model_version=1,
            status=DeploymentReleaseStatus.ACTIVATED.value,
            activated_at=now,
        )
        other_family = ModelFamily(
            id="mfy_other_pipeline",
            slug="other-pipeline",
            display_name="Other Pipeline",
            task_type=TaskType.CLASSIFICATION.value,
            contract_major=1,
            input_schema_ref="contracts/classification/input.v1.json",
            output_schema_ref="contracts/classification/output.v1.json",
            active=True,
        )
        other_version = ModelVersion(
            id="mvr_other_pipeline",
            model_family_id=other_family.id,
            version_no=1,
            status="production",
        )
        other_release = DeploymentRelease(
            id="dpr_other_rt",
            target_path=TargetPath.REALTIME.value,
            model_family_id=other_family.id,
            model_version_id=other_version.id,
            triton_model_name="rt__other_pipeline_v1",
            triton_model_version=1,
            status=DeploymentReleaseStatus.ACTIVATED.value,
            activated_at=now,
        )
        target = DeploymentTarget(
            target_path=TargetPath.REALTIME.value,
            active_release_id=other_release.id,
            previous_release_id=project_release.id,
            updated_at=now,
        )
        session.add_all([project_release, other_family, other_version, other_release, target])
        session.commit()

        detail = get_project_pipeline_detail(session, label_project_id="lpr_pipeline")
        active = resolve_project_active_realtime_deployment(
            session,
            label_project_id="lpr_pipeline",
        )

    assert detail is not None
    endpoint = next(
        item for item in detail.current_endpoints if item.model_family_id == "mfy_pipeline"
    )
    assert endpoint.realtime_available is True
    assert endpoint.realtime_release_id == "dpr_pipeline_rt"
    assert endpoint.current_model_version_id == "mvr_pipeline_approved"
    assert detail.next_recommended_action == "Test inference"
    assert active is not None
    assert active[3].id == "dpr_pipeline_rt"


def test_project_model_workspace_projects_gate_package_activation_and_labelstudio_state(
    sqlite_engine,
) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        _seed_project_training_state(session, include_older_approved_version=False)
        project = session.get(LabelProject, "lpr_pipeline")
        version = session.get(ModelVersion, "mvr_pipeline_latest")
        assert project is not None
        assert version is not None
        version.serving_package_status = "ready"
        version.serving_package_built_at = utc_now()
        version.serving_manifest = {"serving_artifact_format": "tensorrt"}
        project.prediction_backend_enabled = True
        project.prediction_model_family_id = "mfy_pipeline"
        project.prediction_model_version_id = version.id
        project.prediction_linked_at = utc_now()
        session.add(
            DeploymentRelease(
                id="dpr_workspace_rt",
                target_path=TargetPath.REALTIME.value,
                model_family_id="mfy_pipeline",
                model_version_id=version.id,
                triton_model_name="rt__daumenobenunten_v2",
                triton_model_version=2,
                status=DeploymentReleaseStatus.ACTIVATED.value,
                activated_at=utc_now(),
            )
        )
        session.commit()

        detail = get_project_model_workspace_detail(
            session,
            label_project_id=project.id,
            model_family_id="mfy_pipeline",
        )

    assert detail is not None
    stages = {stage.key: stage for stage in detail.lifecycle}
    assert stages["training"].state == "success"
    assert stages["evaluation"].state == "blocked"
    assert stages["package"].state == "success"
    assert stages["deployment"].state == "success"
    assert stages["activation"].state == "success"
    assert stages["labelstudio"].state == "success"


def test_project_model_workspace_reads_training_progress_from_progress_payload(
    sqlite_engine,
) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        _seed_project_training_state(session, include_older_approved_version=False)
        job = session.get(TrainingJob, "trn_pipeline_latest")
        assert job is not None
        job.status = "training"
        job.progress = {
            "progress_percent": 37.5,
            "current_epoch": 15,
            "current_step": 420,
            "total_epochs": 40,
        }
        job.evaluation_summary = {}
        session.commit()

        detail = get_project_model_workspace_detail(
            session,
            label_project_id="lpr_pipeline",
            model_family_id="mfy_pipeline",
        )

    assert detail is not None
    training = next(stage for stage in detail.lifecycle if stage.key == "training")
    assert training.state == "running"
    assert training.progress_percent == 37.5
    assert training.detail == "Epoche 15/40 · training"


def test_project_pipeline_deploy_route_blocks_unapproved_latest_by_default(
    sqlite_engine,
    tmp_path,
    monkeypatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    fake_queue = _FakeQueue()
    monkeypatch.setattr("admin_api.api.routes.project_pipelines._queue", lambda *_args, **_kwargs: fake_queue)

    with Session(sqlite_engine, expire_on_commit=False) as session:
        _seed_project_training_state(
            session,
            include_older_approved_version=False,
        )

    with TestClient(admin_app) as client:
        csrf_token = login_admin(client)
        response = client.post(
            "/api/v1/project-pipelines/lpr_pipeline:deploy-latest",
            headers={"X-CSRF-Token": csrf_token},
        )

    assert response.status_code == 422
    assert response.json()["message"] == "The latest trained model has not passed the deployment quality gates yet."
    assert fake_queue.enqueued == []


def test_project_pipeline_deploy_route_allows_explicit_testing_override(
    sqlite_engine,
    tmp_path,
    monkeypatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    fake_queue = _FakeQueue()
    monkeypatch.setattr("admin_api.api.routes.project_pipelines._queue", lambda *_args, **_kwargs: fake_queue)

    with Session(sqlite_engine, expire_on_commit=False) as session:
        _seed_project_training_state(
            session,
            include_older_approved_version=False,
        )

    with TestClient(admin_app) as client:
        csrf_token = login_admin(client)
        response = client.post(
            "/api/v1/project-pipelines/lpr_pipeline:deploy-latest",
            json={"allow_quality_gate_bypass": True},
            headers={"X-CSRF-Token": csrf_token},
        )

    assert response.status_code == 202
    assert response.json()["job_id"] == "mvr_pipeline_latest"
    assert fake_queue.enqueued == [
        (
            "control_worker.jobs.orchestration.deploy_project_latest_model",
            {
                "label_project_id": "lpr_pipeline",
                "preferred_endpoint_mode": None,
                "model_family_id": None,
                "model_version_id": "mvr_pipeline_latest",
                "allow_quality_gate_bypass": True,
                "requested_by": "admin",
                "job_timeout": admin_settings.training.job_timeout_seconds,
                "retry": fake_queue.enqueued[0][1]["retry"],
            },
        )
    ]


def test_project_model_deploy_route_queues_only_selected_model_family(
    sqlite_engine,
    tmp_path,
    monkeypatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    fake_queue = _FakeQueue()
    monkeypatch.setattr("admin_api.api.routes.project_pipelines._queue", lambda *_args, **_kwargs: fake_queue)

    with Session(sqlite_engine, expire_on_commit=False) as session:
        _seed_project_training_state(session, include_older_approved_version=True)
        family = ModelFamily(
            id="mfy_selected",
            slug="selected-model",
            label_project_id="lpr_pipeline",
            display_name="Selected Model",
            task_type=TaskType.CLASSIFICATION.value,
            contract_major=1,
            input_schema_ref="contracts/classification/input.v1.json",
            output_schema_ref="contracts/classification/output.v1.json",
            active=True,
        )
        version = ModelVersion(
            id="mvr_selected",
            model_family_id=family.id,
            version_no=1,
            status="validated",
            evaluation_summary={"promotion_ready": True},
        )
        session.add_all([family, version])
        session.commit()

    with TestClient(admin_app) as client:
        csrf_token = login_admin(client)
        response = client.post(
            "/api/v1/project-pipelines/lpr_pipeline/models/mfy_selected:deploy",
            headers={"X-CSRF-Token": csrf_token},
        )

    assert response.status_code == 202
    assert response.json()["job_id"] == "mvr_selected"
    job_name, kwargs = fake_queue.enqueued[0]
    assert job_name == "control_worker.jobs.orchestration.deploy_project_latest_model"
    assert kwargs["model_family_id"] == "mfy_selected"
    assert kwargs["model_version_id"] == "mvr_selected"


def test_model_production_approval_queues_project_auto_deploy(
    sqlite_engine,
    tmp_path,
    monkeypatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    fake_queue = _FakeQueue()
    monkeypatch.setattr("admin_api.api.routes.registry._deploy_queue", lambda _settings: fake_queue)

    with Session(sqlite_engine, expire_on_commit=False) as session:
        _seed_project_training_state(
            session,
            include_older_approved_version=True,
        )
        project = session.get(LabelProject, "lpr_pipeline")
        assert project is not None
        project.auto_deploy_enabled = True
        session.commit()

    with TestClient(admin_app) as client:
        csrf_token = login_admin(client)
        response = client.post(
            "/api/v1/registry/model-versions/mvr_pipeline_approved:approve",
            json={
                "target_path": TargetPath.REALTIME.value,
                "approval_scope": DeploymentApprovalScope.PRODUCTION.value,
                "notes": "approve and auto deploy",
            },
            headers={"X-CSRF-Token": csrf_token},
        )

    assert response.status_code == 200
    assert fake_queue.enqueued == [
        (
            "control_worker.jobs.orchestration.deploy_project_latest_model",
            {
                "label_project_id": "lpr_pipeline",
                "preferred_endpoint_mode": TargetPath.REALTIME.value,
                "allow_quality_gate_bypass": False,
                "requested_by": "admin",
                "job_timeout": admin_settings.training.job_timeout_seconds,
                "retry": fake_queue.enqueued[0][1]["retry"],
            },
        )
    ]


def test_project_pipeline_links_labelstudio_predictions_for_active_realtime_deployment(
    sqlite_engine,
    tmp_path,
    monkeypatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    admin_settings.label_studio.ml_backend_adapter_url = "http://adapter"
    admin_app.state.settings = admin_settings
    _FakeLabelStudioClient.created_payloads = []
    monkeypatch.setattr("cv_db.orchestration_store.LabelStudioClient", _FakeLabelStudioClient)

    with Session(sqlite_engine, expire_on_commit=False) as session:
        _seed_project_training_state(
            session,
            include_older_approved_version=True,
        )
        now = utc_now()
        release = DeploymentRelease(
            id="dpr_pipeline_rt",
            target_path=TargetPath.REALTIME.value,
            model_family_id="mfy_pipeline",
            model_version_id="mvr_pipeline_approved",
            triton_model_name="daumenobenunten",
            triton_model_version=1,
            status=DeploymentReleaseStatus.ACTIVATED.value,
            activated_at=now,
        )
        target = DeploymentTarget(
            target_path=TargetPath.REALTIME.value,
            active_release_id=release.id,
            previous_release_id=None,
            updated_at=now,
        )
        session.add_all([release, target])
        session.commit()

    with TestClient(admin_app) as client:
        csrf_token = login_admin(client)
        response = client.post(
            "/api/v1/project-pipelines/lpr_pipeline:link-labelstudio-predictions?model_family_id=mfy_pipeline",
            headers={"X-CSRF-Token": csrf_token},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["backend_id"] == 44
    assert payload["backend_url"] == "http://adapter/api/v1/projects/lpr_pipeline"
    assert payload["model_version_id"] == "mvr_pipeline_approved"
    assert payload["model_version_no"] == 1
    assert _FakeLabelStudioClient.created_payloads[0]["project_id"] == 7
    assert _FakeLabelStudioClient.created_payloads[0]["url"] == "http://adapter/api/v1/projects/lpr_pipeline"

    with Session(sqlite_engine, expire_on_commit=False) as session:
        project = session.get(LabelProject, "lpr_pipeline")
        assert project is not None
        assert project.prediction_backend_enabled is True
        assert project.prediction_backend_id == 44
        assert project.prediction_model_family_id == "mfy_pipeline"
        assert project.prediction_model_version_id == "mvr_pipeline_approved"
