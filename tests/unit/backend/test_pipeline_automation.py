from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from cv_db import Base, Dataset, DatasetSnapshot, LabelProject, ModelFamily, TrainingJob
from cv_status import DatasetStatus, LabelProjectStatus, SnapshotStatus, TaskType
from cv_utils import utc_now
from control_worker.jobs.pipeline_automation import (
    request_snapshot_after_project_sync,
    request_training_after_snapshot_ready,
)


def test_request_snapshot_after_project_sync_creates_snapshot_for_ready_project() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as session:
        dataset = Dataset(
            id="dst_sync",
            slug="cars",
            name="Cars",
            task_type=TaskType.DETECTION.value,
            label_schema_version="1",
            status=DatasetStatus.READY.value,
        )
        project = LabelProject(
            id="lpr_sync",
            dataset_id=dataset.id,
            label_studio_project_id=11,
            title="Cars Inspection",
            task_type=TaskType.DETECTION.value,
            label_config={"labels": ["car"]},
            label_schema_version="1",
            training_pipeline_slug="cars-inspection",
            status=LabelProjectStatus.ACTIVE.value,
            auto_snapshot_enabled=True,
            minimum_annotation_count=1,
            required_classes=["car"],
            train_ratio=80,
            val_ratio=10,
            test_ratio=10,
            qualifying_actions=["ANNOTATION_CREATED"],
            task_count=5,
            annotated_task_count=5,
            annotation_count=5,
            eligible_task_count=5,
            submitted_labeled_task_count=5,
            class_distribution={"car": 5},
            last_activity_at=utc_now(),
        )
        session.add_all([dataset, project])
        session.commit()

        first_snapshot_id = request_snapshot_after_project_sync(
            session,
            label_project=project,
            trigger="reconcile",
            reason="Automatic snapshot after project reconciliation",
            created_by="tester",
            actor_id="test-suite",
        )
        second_snapshot_id = request_snapshot_after_project_sync(
            session,
            label_project=project,
            trigger="reconcile",
            reason="Automatic snapshot after project reconciliation",
            created_by="tester",
            actor_id="test-suite",
        )
        session.commit()

        snapshot_count = session.scalar(select(func.count(DatasetSnapshot.id)))

    assert first_snapshot_id is not None
    assert first_snapshot_id == second_snapshot_id
    assert snapshot_count == 1


def test_request_snapshot_after_project_sync_skips_when_annotations_not_ready() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as session:
        dataset = Dataset(
            id="dst_sync",
            slug="cars",
            name="Cars",
            task_type=TaskType.DETECTION.value,
            label_schema_version="1",
            status=DatasetStatus.READY.value,
        )
        project = LabelProject(
            id="lpr_sync",
            dataset_id=dataset.id,
            label_studio_project_id=11,
            title="Cars Inspection",
            task_type=TaskType.DETECTION.value,
            label_config={"labels": ["car"]},
            label_schema_version="1",
            training_pipeline_slug="cars-inspection",
            status=LabelProjectStatus.ACTIVE.value,
            auto_snapshot_enabled=True,
            minimum_annotation_count=3,
            required_classes=["car"],
            train_ratio=80,
            val_ratio=10,
            test_ratio=10,
            qualifying_actions=["ANNOTATION_CREATED"],
            task_count=5,
            annotated_task_count=1,
            annotation_count=1,
            eligible_task_count=1,
            submitted_labeled_task_count=1,
            class_distribution={"car": 1},
            last_activity_at=utc_now(),
        )
        session.add_all([dataset, project])
        session.commit()

        snapshot_id = request_snapshot_after_project_sync(
            session,
            label_project=project,
            trigger="reconcile",
            reason="Automatic snapshot after project reconciliation",
            created_by="tester",
            actor_id="test-suite",
        )
        session.commit()

        snapshot_count = session.scalar(select(func.count(DatasetSnapshot.id)))

    assert snapshot_id is None
    assert snapshot_count == 0


def test_request_snapshot_after_project_sync_force_new_snapshot_after_failure() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as session:
        dataset = Dataset(
            id="dst_sync",
            slug="cars",
            name="Cars",
            task_type=TaskType.DETECTION.value,
            label_schema_version="1",
            status=DatasetStatus.READY.value,
        )
        project = LabelProject(
            id="lpr_sync",
            dataset_id=dataset.id,
            label_studio_project_id=11,
            title="Cars Inspection",
            task_type=TaskType.DETECTION.value,
            label_config={"labels": ["car"]},
            label_schema_version="1",
            training_pipeline_slug="cars-inspection",
            status=LabelProjectStatus.ACTIVE.value,
            auto_snapshot_enabled=True,
            minimum_annotation_count=1,
            required_classes=["car"],
            train_ratio=80,
            val_ratio=10,
            test_ratio=10,
            qualifying_actions=["ANNOTATION_CREATED"],
            task_count=5,
            annotated_task_count=5,
            annotation_count=5,
            eligible_task_count=5,
            submitted_labeled_task_count=5,
            class_distribution={"car": 5},
            last_activity_at=utc_now(),
        )
        failed_snapshot = DatasetSnapshot(
            id="dss_failed",
            dataset_id=dataset.id,
            label_project_id=project.id,
            snapshot_no=1,
            status=SnapshotStatus.FAILED.value,
            item_count=5,
            annotation_count=5,
            created_by="tester",
            export_format="JSON",
            label_schema_version="1",
            validation_status="failed",
            idempotency_key="snapshot:failed",
            processing_error="validation failed",
        )
        session.add_all([dataset, project, failed_snapshot])
        session.commit()

        snapshot_id = request_snapshot_after_project_sync(
            session,
            label_project=project,
            trigger="reconcile",
            reason="Automatic snapshot after project reconciliation",
            created_by="tester",
            actor_id="test-suite",
            force_new_snapshot=True,
        )
        session.commit()

        snapshots = session.scalars(
            select(DatasetSnapshot).order_by(DatasetSnapshot.snapshot_no.asc())
        ).all()

    assert snapshot_id is not None
    assert [snapshot.snapshot_no for snapshot in snapshots] == [1, 2]
    assert snapshots[-1].id != "dss_failed"


def test_request_snapshot_after_project_sync_force_new_snapshot_reuses_active_snapshot() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as session:
        dataset = Dataset(
            id="dst_sync",
            slug="cars",
            name="Cars",
            task_type=TaskType.DETECTION.value,
            label_schema_version="1",
            status=DatasetStatus.READY.value,
        )
        project = LabelProject(
            id="lpr_sync",
            dataset_id=dataset.id,
            label_studio_project_id=11,
            title="Cars Inspection",
            task_type=TaskType.DETECTION.value,
            label_config={"labels": ["car"]},
            label_schema_version="1",
            training_pipeline_slug="cars-inspection",
            status=LabelProjectStatus.ACTIVE.value,
            auto_snapshot_enabled=True,
            minimum_annotation_count=1,
            required_classes=["car"],
            train_ratio=80,
            val_ratio=10,
            test_ratio=10,
            qualifying_actions=["ANNOTATION_CREATED"],
            task_count=5,
            annotated_task_count=5,
            annotation_count=5,
            eligible_task_count=5,
            submitted_labeled_task_count=5,
            class_distribution={"car": 5},
            last_activity_at=utc_now(),
        )
        requested_snapshot = DatasetSnapshot(
            id="dss_requested",
            dataset_id=dataset.id,
            label_project_id=project.id,
            snapshot_no=1,
            status=SnapshotStatus.REQUESTED.value,
            item_count=0,
            annotation_count=5,
            created_by="tester",
            export_format="JSON",
            label_schema_version="1",
            validation_status="pending",
            idempotency_key="snapshot:requested",
        )
        session.add_all([dataset, project, requested_snapshot])
        session.commit()

        snapshot_id = request_snapshot_after_project_sync(
            session,
            label_project=project,
            trigger="reconcile",
            reason="Automatic snapshot after project reconciliation",
            created_by="tester",
            actor_id="test-suite",
            force_new_snapshot=True,
        )
        session.commit()

        snapshot_count = session.scalar(select(func.count(DatasetSnapshot.id)))

    assert snapshot_id == "dss_requested"
    assert snapshot_count == 1


def test_request_training_after_snapshot_ready_is_idempotent_for_default_model() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as session:
        dataset = Dataset(
            id="dst_auto_train",
            slug="cars-auto-train",
            name="Cars Auto Train",
            task_type=TaskType.DETECTION.value,
            label_schema_version="1",
            status=DatasetStatus.SNAPSHOTTED.value,
        )
        project = LabelProject(
            id="lpr_auto_train",
            dataset_id=dataset.id,
            label_studio_project_id=81,
            title="Cars Auto Train",
            task_type=TaskType.DETECTION.value,
            label_config={"labels": ["car"]},
            label_schema_version="1",
            training_pipeline_slug="cars-auto-train",
            status=LabelProjectStatus.ACTIVE.value,
            auto_train_enabled=True,
            default_model_family_id="mfy_auto_train",
            default_model_template="detection-yolov8-n",
            default_training_profile="safe-default",
            minimum_annotation_count=1,
            required_classes=["car"],
            train_ratio=80,
            val_ratio=10,
            test_ratio=10,
            qualifying_actions=["ANNOTATION_CREATED"],
        )
        snapshot = DatasetSnapshot(
            id="dss_auto_train",
            dataset_id=dataset.id,
            label_project_id=project.id,
            snapshot_no=1,
            status=SnapshotStatus.READY.value,
            item_count=4,
            annotation_count=4,
            eligible_item_count=4,
            manifest_uri="snapshot_manifest.json",
            content_sha256="auto-train-sha",
            dvc_rev="auto-train-rev",
            created_by="tester",
            export_format="JSON",
            label_schema_version="1",
            split_metadata={"train_count": 3, "val_count": 1, "test_count": 0},
            validation_report={},
            validation_status="passed",
            statistics={"class_distribution": {"car": 4}},
            idempotency_key="snapshot:auto-train",
        )
        family = ModelFamily(
            id="mfy_auto_train",
            slug="cars-auto-train-yolo",
            label_project_id=project.id,
            display_name="Cars Detector",
            template_key="detection-yolov8-n",
            template_version=1,
            training_profile_key="safe-default",
            task_type=TaskType.DETECTION.value,
            architecture_family="yolo",
            architecture_version="v8",
            architecture_size="n",
            architecture_display_name="YOLOv8 Nano",
            promotion_policy={},
            contract_major=1,
            input_schema_ref="contracts/detection/input.v1.json",
            output_schema_ref="contracts/detection/output.v1.json",
            active=True,
        )
        session.add_all([dataset, project, snapshot, family])
        session.commit()

        first_job_id = request_training_after_snapshot_ready(
            session,
            snapshot_id=snapshot.id,
        )
        second_job_id = request_training_after_snapshot_ready(
            session,
            snapshot_id=snapshot.id,
        )
        session.commit()
        jobs = session.scalars(select(TrainingJob)).all()

    assert first_job_id is not None
    assert second_job_id == first_job_id
    assert len(jobs) == 1
    assert jobs[0].model_family_id == family.id
    assert jobs[0].dataset_snapshot_id == snapshot.id
    assert jobs[0].idempotency_key == f"auto-train:{snapshot.id}:{family.id}"


def test_request_training_after_snapshot_ready_skips_incomplete_policy() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as session:
        dataset = Dataset(
            id="dst_auto_train_off",
            slug="auto-train-off",
            name="Auto Train Off",
            task_type=TaskType.CLASSIFICATION.value,
            label_schema_version="1",
            status=DatasetStatus.SNAPSHOTTED.value,
        )
        project = LabelProject(
            id="lpr_auto_train_off",
            dataset_id=dataset.id,
            label_studio_project_id=82,
            title="Auto Train Off",
            task_type=TaskType.CLASSIFICATION.value,
            label_config={"labels": ["ok"]},
            training_pipeline_slug="auto-train-off",
            status=LabelProjectStatus.ACTIVE.value,
            auto_train_enabled=True,
            default_model_family_id=None,
        )
        snapshot = DatasetSnapshot(
            id="dss_auto_train_off",
            dataset_id=dataset.id,
            label_project_id=project.id,
            snapshot_no=1,
            status=SnapshotStatus.READY.value,
            item_count=1,
            annotation_count=1,
            created_by="tester",
            export_format="JSON",
            label_schema_version="1",
            validation_status="passed",
            idempotency_key="snapshot:auto-train-off",
        )
        session.add_all([dataset, project, snapshot])
        session.commit()

        job_id = request_training_after_snapshot_ready(session, snapshot_id=snapshot.id)

    assert job_id is None
