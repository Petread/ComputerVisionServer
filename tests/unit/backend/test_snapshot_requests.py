from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from cv_db import Base, Dataset, DatasetSnapshot, LabelProject, create_or_reuse_dataset_snapshot_request
from cv_status import DatasetStatus, LabelProjectStatus, TaskType
from cv_utils import utc_now


def test_snapshot_request_is_idempotent_and_reuses_existing_record() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as session:
        dataset = Dataset(
            id="dst_test",
            slug="widgets",
            name="Widgets",
            task_type=TaskType.CLASSIFICATION.value,
            label_schema_version="1",
            status=DatasetStatus.READY.value,
        )
        project = LabelProject(
            id="lpr_test",
            dataset_id=dataset.id,
            label_studio_project_id=101,
            title="Widget Project",
            task_type=TaskType.CLASSIFICATION.value,
            label_config={"labels": ["ok", "defect"]},
            label_schema_version="1",
            training_pipeline_slug="widget-cls-v1",
            status=LabelProjectStatus.ACTIVE.value,
            auto_snapshot_enabled=True,
            minimum_annotation_count=1,
            required_classes=["ok", "defect"],
            train_ratio=80,
            val_ratio=10,
            test_ratio=10,
            qualifying_actions=["ANNOTATION_CREATED"],
            task_count=10,
            annotated_task_count=8,
            annotation_count=12,
            class_distribution={"ok": 7, "defect": 5},
            last_activity_at=utc_now(),
        )
        session.add_all([dataset, project])
        session.commit()

        first = create_or_reuse_dataset_snapshot_request(
            session,
            dataset_id=dataset.id,
            label_project_id=project.id,
            export_format="JSON",
            trigger="manual",
            reason="Operator request",
            created_by="tester",
        )
        second = create_or_reuse_dataset_snapshot_request(
            session,
            dataset_id=dataset.id,
            label_project_id=project.id,
            export_format="JSON",
            trigger="manual",
            reason="Operator request",
            created_by="tester",
        )
        session.commit()

        snapshot_count = session.scalar(select(func.count(DatasetSnapshot.id)))

    assert first.snapshot_id == second.snapshot_id
    assert first.snapshot_no == 1
    assert snapshot_count == 1


def test_snapshot_request_creates_new_record_after_project_state_changes() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as session:
        dataset = Dataset(
            id="dst_test",
            slug="widgets",
            name="Widgets",
            task_type=TaskType.DETECTION.value,
            label_schema_version="2",
            status=DatasetStatus.READY.value,
        )
        project = LabelProject(
            id="lpr_test",
            dataset_id=dataset.id,
            label_studio_project_id=202,
            title="Widget Detector",
            task_type=TaskType.DETECTION.value,
            label_config={"labels": ["defect"]},
            label_schema_version="2",
            training_pipeline_slug="widget-det-v2",
            status=LabelProjectStatus.ACTIVE.value,
            auto_snapshot_enabled=True,
            minimum_annotation_count=1,
            required_classes=["defect"],
            train_ratio=80,
            val_ratio=10,
            test_ratio=10,
            qualifying_actions=["ANNOTATION_CREATED"],
            task_count=10,
            annotated_task_count=5,
            annotation_count=5,
            class_distribution={"defect": 5},
            last_activity_at=utc_now(),
        )
        session.add_all([dataset, project])
        session.commit()

        first = create_or_reuse_dataset_snapshot_request(
            session,
            dataset_id=dataset.id,
            label_project_id=project.id,
            export_format="JSON",
            trigger="manual",
            reason="First request",
            created_by="tester",
        )
        project.annotation_count = 7
        project.annotated_task_count = 7
        project.last_activity_at = utc_now()
        second = create_or_reuse_dataset_snapshot_request(
            session,
            dataset_id=dataset.id,
            label_project_id=project.id,
            export_format="JSON",
            trigger="manual",
            reason="First request",
            created_by="tester",
        )
        session.commit()

    assert first.snapshot_id != second.snapshot_id
    assert second.snapshot_no == 2
