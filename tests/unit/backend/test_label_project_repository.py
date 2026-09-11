from __future__ import annotations

from cv_db import (
    Dataset,
    LabelProject,
    get_label_project_by_external_id,
    list_label_projects,
    upsert_discovered_label_project,
)
from cv_status import DatasetStatus, LabelProjectStatus, TaskType
from cv_utils import utc_now
from sqlalchemy.orm import Session


def test_discovered_label_project_upsert_restores_soft_deleted_external_project(
    sqlite_engine,
) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        dataset = Dataset(
            id="dst_project_21",
            slug="old-project-classification",
            name="Old Project",
            task_type=TaskType.CLASSIFICATION.value,
            label_schema_version="ls-old",
            status=DatasetStatus.READY.value,
        )
        project = LabelProject(
            id="lpr_project_21",
            dataset_id=dataset.id,
            label_studio_project_id=21,
            title="Old Project",
            task_type=TaskType.CLASSIFICATION.value,
            label_config={"labels": ["ok"]},
            label_schema_version="ls-old",
            training_pipeline_slug="old-project",
            status=LabelProjectStatus.ACTIVE.value,
            deleted_at=utc_now(),
            deleted_by="admin",
            delete_reason="operator cleanup",
        )
        session.add_all([dataset, project])
        session.commit()

        assert get_label_project_by_external_id(session, 21) is None
        assert get_label_project_by_external_id(session, 21, include_deleted=True) == project
        assert list_label_projects(session) == []

        restored = upsert_discovered_label_project(
            session,
            project_payload={
                "id": 21,
                "title": "Restored Project",
                "label_config": "<View><Image name=\"image\" value=\"$image\"/><Choices name=\"label\" toName=\"image\"><Choice value=\"ok\"/></Choices></View>",
                "task_number": 12,
                "finished_task_number": 8,
                "useful_annotation_number": 8,
            },
            automatic_snapshot_actions=["ANNOTATION_CREATED"],
            minimum_annotation_count=1,
            required_classes=[],
            split_ratios=(80, 10, 10),
        )
        session.commit()

        assert restored.id == project.id
        assert restored.dataset_id == dataset.id
        assert restored.title == "Restored Project"
        assert restored.deleted_at is None
        assert restored.deleted_by is None
        assert restored.delete_reason is None
        assert restored.task_count == 12
        assert restored.annotated_task_count == 8
        assert get_label_project_by_external_id(session, 21) == restored
        assert [item.label_project_id for item in list_label_projects(session)] == [project.id]


def test_labelstudio_choice_template_does_not_downgrade_mapped_anomaly_project(
    sqlite_engine,
) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        dataset = Dataset(
            id="dst_anomaly_project",
            slug="surface-anomaly",
            name="Surface Anomaly",
            task_type=TaskType.ANOMALY.value,
            label_schema_version="ls-anomaly",
            status=DatasetStatus.READY.value,
        )
        project = LabelProject(
            id="lpr_anomaly_project",
            dataset_id=dataset.id,
            label_studio_project_id=42,
            title="Surface Anomaly",
            task_type=TaskType.ANOMALY.value,
            label_config={"choices": ["good", "bad"]},
            label_schema_version="ls-anomaly",
            training_pipeline_slug="surface-anomaly",
            status=LabelProjectStatus.ACTIVE.value,
            default_model_template="anomaly-patchcore-wide-resnet50-2",
        )
        session.add_all([dataset, project])
        session.commit()

        restored = upsert_discovered_label_project(
            session,
            project_payload={
                "id": 42,
                "title": "Surface Anomaly",
                "label_config": (
                    "<View><Image name=\"image\" value=\"$image\"/>"
                    "<Choices name=\"label\" toName=\"image\">"
                    "<Choice value=\"good\"/><Choice value=\"bad\"/>"
                    "</Choices></View>"
                ),
                "task_number": 20,
                "finished_task_number": 12,
                "useful_annotation_number": 12,
            },
            automatic_snapshot_actions=["ANNOTATION_CREATED"],
            minimum_annotation_count=1,
            required_classes=[],
            split_ratios=(70, 15, 15),
        )
        session.commit()

        assert restored.id == project.id
        assert restored.task_type == TaskType.ANOMALY.value
        assert restored.default_model_template == "anomaly-patchcore-wide-resnet50-2"
        assert restored.task_count == 20
        assert restored.annotated_task_count == 12


def test_labelstudio_schema_change_updates_existing_dataset_metadata(
    sqlite_engine,
) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        dataset = Dataset(
            id="dst_detector_project",
            slug="detector-project",
            name="Detector Project",
            task_type=TaskType.CLASSIFICATION.value,
            label_schema_version="ls-old",
            status=DatasetStatus.SNAPSHOTTED.value,
        )
        project = LabelProject(
            id="lpr_detector_project",
            dataset_id=dataset.id,
            label_studio_project_id=84,
            title="Detector Project",
            task_type=TaskType.CLASSIFICATION.value,
            label_config={"choices": ["LKW", "Lader"]},
            label_schema_version="ls-old",
            training_pipeline_slug="detector-project",
            status=LabelProjectStatus.ACTIVE.value,
            default_model_template="classification-yolov11-s",
        )
        session.add_all([dataset, project])
        session.commit()

        restored = upsert_discovered_label_project(
            session,
            project_payload={
                "id": 84,
                "title": "Detector Project",
                "label_config": (
                    "<View><Image name=\"image\" value=\"$image\"/>"
                    "<RectangleLabels name=\"label\" toName=\"image\">"
                    "<Label value=\"Lader\"/><Label value=\"LKW\"/>"
                    "</RectangleLabels></View>"
                ),
                "task_number": 195,
                "finished_task_number": 195,
                "useful_annotation_number": 195,
            },
            automatic_snapshot_actions=["ANNOTATION_CREATED"],
            minimum_annotation_count=1,
            required_classes=[],
            split_ratios=(80, 10, 10),
        )
        session.commit()

        updated_dataset = session.get(Dataset, dataset.id)

        assert restored.task_type == TaskType.DETECTION.value
        assert restored.default_model_template == "detection-yolov8-n"
        assert updated_dataset is not None
        assert updated_dataset.task_type == TaskType.DETECTION.value
        assert updated_dataset.label_schema_version == restored.label_schema_version
        assert updated_dataset.status == DatasetStatus.READY.value


def test_labelstudio_sync_preserves_provisioned_temporal_schema_metadata(sqlite_engine) -> None:
    raw_config = (
        '<View><Video name="video" value="$video" frameRate="$frame_rate"/>'
        '<TimelineLabels name="target_orientation" toName="video">'
        '<Label value="target_orientation"/></TimelineLabels></View>'
    )
    with Session(sqlite_engine, expire_on_commit=False) as session:
        dataset = Dataset(
            id="dst_temporal_project",
            slug="temporal-project",
            name="Temporal Project",
            task_type=TaskType.TEMPORAL_SEQUENCE.value,
            label_schema_version="stem_orientation_angle.v1",
            status=DatasetStatus.READY.value,
        )
        project = LabelProject(
            id="lpr_temporal_project",
            dataset_id=dataset.id,
            label_studio_project_id=99,
            title="Temporal Project",
            task_type=TaskType.TEMPORAL_SEQUENCE.value,
            label_config={
                "raw": raw_config,
                "annotation_schema_id": "stem_orientation_angle.v1",
                "config_sha256": "a" * 64,
                "provisioning": {
                    "marker_version": "v1",
                    "marker": "cv-platform-provisioning:v1:test",
                },
            },
            label_schema_version="stem_orientation_angle.v1",
            training_pipeline_slug="temporal-project",
            status=LabelProjectStatus.ACTIVE.value,
        )
        session.add_all([dataset, project])
        session.commit()

        restored = upsert_discovered_label_project(
            session,
            project_payload={
                "id": 99,
                "title": "Temporal Project",
                "label_config": raw_config,
                "parsed_label_config": {
                    "target_orientation": {"type": "TimelineLabels"},
                },
            },
            automatic_snapshot_actions=["ANNOTATION_CREATED"],
            minimum_annotation_count=1,
            required_classes=[],
            split_ratios=(80, 10, 10),
        )
        session.commit()

        assert restored.task_type == TaskType.TEMPORAL_SEQUENCE.value
        assert restored.label_schema_version == "stem_orientation_angle.v1"
        assert restored.label_config["raw"] == raw_config
        assert restored.label_config["config_sha256"] == "a" * 64
        assert dataset.task_type == TaskType.TEMPORAL_SEQUENCE.value
        assert dataset.label_schema_version == "stem_orientation_angle.v1"
