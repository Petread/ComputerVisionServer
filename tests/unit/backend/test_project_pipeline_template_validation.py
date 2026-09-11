from __future__ import annotations

import pytest
from cv_contracts import (
    ModelTemplateCreateRequest,
    ProjectAutomationPolicy,
    ProjectModelCreateRequest,
    ProjectTrainRequest,
)
from cv_contracts.modeling import TrainingDefaults, training_defaults_from_persisted
from cv_contracts.promotion import PromotionCriteriaPolicy
from cv_contracts.training import BestCheckpointRule, PromotionCriterion, TrainingJobCreateRequest
from cv_db.model_catalog import architecture_summary, recipe_for_architecture
from cv_db.models import Dataset, DatasetSnapshot, LabelProject, ModelFamily, ModelTemplate
from cv_db.orchestration_store import (
    build_project_training_request,
    create_model_template,
    create_project_model_family,
    ensure_model_templates_seeded,
    list_model_templates,
    list_project_pipeline_summaries,
    update_project_automation_policy,
)
from cv_db.training_store import create_training_job_request
from cv_status import DatasetStatus, SnapshotStatus, TaskType, ValidationStatus
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session


def seed_detection_project(session: Session, *, project_id: str = "lpr_test_det") -> LabelProject:
    project = LabelProject(
        id=project_id,
        label_studio_project_id=101,
        dataset_id=None,
        title="Detector Project",
        task_type=TaskType.DETECTION.value,
        label_config={},
        label_schema_version="ls-test",
        training_pipeline_slug="detector-project",
        status="active",
        default_model_template="classification-resnet-18",  # intentionally wrong for detection
        default_training_profile="safe-default",
        preferred_endpoint_mode="realtime",
    )
    session.add(project)
    session.flush()
    return project


def seed_ready_snapshot(session: Session, *, snapshot_id: str, project_id: str) -> DatasetSnapshot:
    snapshot = DatasetSnapshot(
        id=snapshot_id,
        dataset_id="dst_test",
        label_project_id=project_id,
        snapshot_no=1,
        status=SnapshotStatus.READY.value,
        validation_status=ValidationStatus.PASSED.value,
        idempotency_key=f"snapshot:{snapshot_id}",
        label_schema_version="ls-test",
        split_metadata={},
        validation_report={},
        statistics={},
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def test_training_defaults_require_image_size_divisible_by_32() -> None:
    assert TrainingDefaults(image_size=1_216).image_size == 1_216
    assert training_defaults_from_persisted({"image_size": 1_212}).image_size == 1_216

    with pytest.raises(ValidationError, match="multiple of 32"):
        TrainingDefaults(image_size=1_200)


def test_training_job_request_requires_image_size_divisible_by_32() -> None:
    payload = {
        "model_family_slug": "stride-validation",
        "dataset_snapshot_id": "dss_stride_validation",
        "task_type": TaskType.DETECTION,
        "architecture": {
            "task_type": TaskType.DETECTION,
            "family": "yolo",
            "version": "v8",
            "size": "n",
            "display_name": "YOLOv8 Nano",
            "internal_key": "detection:yolo:v8:n",
        },
        "recipe_name": "baseline_det",
        "recipe_version": "1.0.0",
        "image_size": 1_200,
        "best_checkpoint_rule": {
            "metric_name": "map_50_95",
            "mode": "max",
            "phase": "evaluation",
        },
        "promotion_criteria": [
            {
                "metric_name": "map_50_95",
                "comparison": "gte",
                "threshold": 0.5,
                "phase": "evaluation",
            }
        ],
    }

    with pytest.raises(ValidationError, match="multiple of 32"):
        TrainingJobCreateRequest.model_validate(payload)


def seed_classification_dataset(session: Session, *, dataset_id: str = "dst_test") -> Dataset:
    dataset = Dataset(
        id=dataset_id,
        slug=f"{dataset_id}-slug",
        name=f"{dataset_id} Dataset",
        task_type=TaskType.CLASSIFICATION.value,
        label_schema_version="ls-test",
        status=DatasetStatus.SNAPSHOTTED.value,
    )
    session.add(dataset)
    session.flush()
    return dataset


def seed_project_model_family(
    session: Session,
    *,
    family_id: str,
    project_id: str,
    slug: str,
    template_key: str,
    task_type: TaskType,
) -> ModelFamily:
    defaults = {
        TaskType.CLASSIFICATION: ("resnet", "v1", "18", "ResNet-18"),
        TaskType.DETECTION: ("yolo", "v8", "n", "YOLOv8 Nano"),
        TaskType.SEGMENTATION: ("deeplab", "v3", "resnet50", "DeepLabV3 ResNet-50"),
        TaskType.ANOMALY: ("patchcore", "anomalib", "wide_resnet50_2", "PatchCore WideResNet50-2"),
    }[task_type]
    family = ModelFamily(
        id=family_id,
        slug=slug,
        label_project_id=project_id,
        display_name="Test Family",
        template_key=template_key,
        training_profile_key="safe-default",
        task_type=task_type.value,
        architecture_family=defaults[0],
        architecture_version=defaults[1],
        architecture_size=defaults[2],
        architecture_display_name=defaults[3],
        contract_major=1,
        input_schema_ref=f"contracts/{task_type.value}/input.v1.json",
        output_schema_ref=f"contracts/{task_type.value}/output.v1.json",
        active=True,
    )
    session.add(family)
    session.flush()
    return family


def test_create_project_model_family_rejects_incompatible_template(sqlite_engine) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        project = seed_detection_project(session, project_id="lpr_create_model_guard")

        with pytest.raises(ValueError, match="request declared detection"):
            create_project_model_family(
                session,
                label_project_id=project.id,
                payload=ProjectModelCreateRequest(
                    model_template_key="classification-resnet-18",
                    task_type=TaskType.DETECTION,
                    architecture_family="resnet",
                    architecture_version="v1",
                    architecture_size="18",
                    display_name="Bad model",
                ),
            )


def test_create_project_model_family_applies_supported_architecture_and_training_overrides(
    sqlite_engine,
) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        project = seed_detection_project(session, project_id="lpr_create_model_override")
        seed_ready_snapshot(
            session,
            snapshot_id="dss_create_model_override",
            project_id=project.id,
        )
        model = create_project_model_family(
            session,
            label_project_id=project.id,
            payload=ProjectModelCreateRequest(
                model_template_key="detection-yolov8-n",
                task_type=TaskType.DETECTION,
                architecture_family="yolo",
                architecture_version="v11",
                architecture_size="s",
                display_name="Override model",
                training_overrides=TrainingDefaults(
                    epochs=7,
                    learning_rate=0.002,
                    image_size=512,
                    batch_size=4,
                    optimizer="sgd",
                    checkpoint_interval_steps=3,
                ),
            ),
        )
        request = build_project_training_request(
            session,
            label_project_id=project.id,
            payload=None,
        )

    assert model.architecture.version == "v11"
    assert model.architecture.size == "s"
    assert model.training_overrides is not None
    assert request.max_epochs == 7
    assert request.learning_rate == 0.002
    assert request.image_size == 512
    assert request.batch_size == 4


def test_update_project_automation_policy_rejects_incompatible_default_template(sqlite_engine) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        project = seed_detection_project(session, project_id="lpr_automation_guard")

        with pytest.raises(ValueError, match="project pipeline is detection"):
            update_project_automation_policy(
                session,
                label_project_id=project.id,
                payload=ProjectAutomationPolicy(
                    default_model_template="classification-resnet-18",
                ),
            )


def test_update_project_automation_policy_requires_concrete_default_model(sqlite_engine) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        project = seed_detection_project(session, project_id="lpr_automation_model_guard")

        with pytest.raises(ValueError, match="default project model"):
            update_project_automation_policy(
                session,
                label_project_id=project.id,
                payload=ProjectAutomationPolicy(auto_train_enabled=True),
            )


def test_update_project_automation_policy_persists_default_project_model(sqlite_engine) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        project = seed_detection_project(session, project_id="lpr_automation_model")
        family = seed_project_model_family(
            session,
            family_id="mfy_automation_model",
            project_id=project.id,
            slug="automation-detector",
            template_key="detection-yolov8-n",
            task_type=TaskType.DETECTION,
        )
        session.commit()

        policy = update_project_automation_policy(
            session,
            label_project_id=project.id,
            payload=ProjectAutomationPolicy(
                auto_train_enabled=True,
                default_model_family_id=family.id,
                default_model_template="detection-yolov8-n",
            ),
        )

    assert policy.auto_train_enabled is True
    assert policy.default_model_family_id == family.id


def test_build_project_training_request_coerces_incompatible_template_key(sqlite_engine) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        project = seed_detection_project(session, project_id="lpr_training_template_guard")
        seed_ready_snapshot(session, snapshot_id="dss_training_template_guard", project_id=project.id)
        seed_project_model_family(
            session,
            family_id="mfy_training_template_guard",
            project_id=project.id,
            slug="detector-family",
            template_key="classification-resnet-18",
            task_type=TaskType.DETECTION,
        )
        session.commit()

        request = build_project_training_request(session, label_project_id=project.id)

        assert request.task_type == TaskType.DETECTION
        assert request.recipe_name == "baseline_det_mmyolo"
        assert request.parameter_overrides["template_key"] == "detection-yolov8-n"


@pytest.mark.parametrize(
    ("task_type", "family", "version", "recipe_name"),
    [
        (TaskType.CLASSIFICATION, "resnet", "v1", "baseline_cls_torchvision"),
        (TaskType.CLASSIFICATION, "yolo", "v11", "baseline_cls_yolo"),
        (TaskType.DETECTION, "yolo", "v8", "baseline_det_mmyolo"),
        (TaskType.DETECTION, "yolo", "v11", "baseline_det_yolo"),
        (TaskType.SEGMENTATION, "deeplab", "v3", "baseline_seg_torchvision"),
        (TaskType.SEGMENTATION, "yolo", "v8", "baseline_seg_yolo"),
    ],
)
def test_recipe_for_architecture_uses_backend_specific_recipes(
    task_type: TaskType,
    family: str,
    version: str,
    recipe_name: str,
) -> None:
    assert recipe_for_architecture(
        task_type=task_type,
        family=family,
        version=version,
    ) == (recipe_name, "1.2.0")


def test_yolo_sgd_templates_use_ultralytics_learning_rate_default() -> None:
    from cv_db.model_catalog import default_template_summary

    for template_key in ("detection-yolov8-n", "detection-yolov8-s", "segmentation-yolov8-n"):
        template = default_template_summary(template_key)
        assert template.defaults.training.optimizer == "sgd"
        assert template.defaults.training.learning_rate == 0.01


def test_seeded_system_templates_sync_training_defaults(sqlite_engine) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        ensure_model_templates_seeded(session)
        row = session.scalar(select(ModelTemplate).where(ModelTemplate.template_key == "detection-yolov8-n"))
        assert row is not None
        row.training_defaults = {
            **dict(row.training_defaults or {}),
            "training": {
                **dict((row.training_defaults or {}).get("training") or {}),
                "learning_rate": 0.001,
            },
        }
        row.template_version = 1
        row.created_by = "system"
        row.updated_by = "system"
        session.flush()

        ensure_model_templates_seeded(session)

        assert row.training_defaults["training"]["learning_rate"] == 0.01
        assert row.template_version == 2


def test_model_template_summary_normalizes_legacy_persisted_image_size(sqlite_engine) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        ensure_model_templates_seeded(session)
        row = session.scalar(
            select(ModelTemplate).where(
                ModelTemplate.template_key == "segmentation-deeplab-v3-resnet50"
            )
        )
        assert row is not None
        row.training_defaults = {
            **dict(row.training_defaults or {}),
            "training": {
                **dict((row.training_defaults or {}).get("training") or {}),
                "image_size": 1_212,
            },
        }
        row.updated_by = "legacy-operator"
        session.flush()

        templates = list_model_templates(
            session,
            task_type=TaskType.SEGMENTATION,
        )

    template = next(
        item
        for item in templates
        if item.template_key == "segmentation-deeplab-v3-resnet50"
    )
    assert template.defaults.training.image_size == 1_216


def test_pipeline_summary_normalizes_legacy_model_image_size(sqlite_engine) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        project = seed_detection_project(
            session,
            project_id="lpr_legacy_model_image_size",
        )
        seed_ready_snapshot(
            session,
            snapshot_id="dss_legacy_model_image_size",
            project_id=project.id,
        )
        family = seed_project_model_family(
            session,
            family_id="mfy_legacy_model_image_size",
            project_id=project.id,
            slug="legacy-image-size-detector",
            template_key="detection-yolov8-n",
            task_type=TaskType.DETECTION,
        )
        family.training_overrides = {"image_size": 1_212}
        session.commit()

        summary = next(
            item
            for item in list_project_pipeline_summaries(session)
            if item.label_project_id == project.id
        )
        request = build_project_training_request(
            session,
            label_project_id=project.id,
            payload=ProjectTrainRequest(model_family_id=family.id),
        )

    assert summary.models[0].training_overrides is not None
    assert summary.models[0].training_overrides.image_size == 1_216
    assert request.image_size == 1_216


def test_build_project_training_request_supports_segmentation(sqlite_engine) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        project = LabelProject(
            id="lpr_segmentation_training_guard",
            label_studio_project_id=202,
            dataset_id=None,
            title="Segmenter Project",
            task_type=TaskType.SEGMENTATION.value,
            label_config={},
            label_schema_version="ls-test",
            training_pipeline_slug="segmenter-project",
            status="active",
            default_model_template="segmentation-deeplab-v3-resnet50",
            default_training_profile="safe-default",
            preferred_endpoint_mode="realtime",
        )
        session.add(project)
        seed_ready_snapshot(
            session,
            snapshot_id="dss_segmentation_training_guard",
            project_id=project.id,
        )
        seed_project_model_family(
            session,
            family_id="mfy_segmentation_training_guard",
            project_id=project.id,
            slug="segmenter-family",
            template_key="segmentation-deeplab-v3-resnet50",
            task_type=TaskType.SEGMENTATION,
        )
        session.commit()

        request = build_project_training_request(session, label_project_id=project.id)

        assert request.task_type == TaskType.SEGMENTATION
        assert request.recipe_name == "baseline_seg_torchvision"
        assert request.parameter_overrides["template_key"] == "segmentation-deeplab-v3-resnet50"


def test_segmentation_project_supports_class_scoped_instance_and_semantic_models(
    sqlite_engine,
) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        project = LabelProject(
            id="lpr_multi_segmentation",
            label_studio_project_id=207,
            dataset_id=None,
            title="Board Segmentation",
            task_type=TaskType.SEGMENTATION.value,
            label_config={"polygonlabels": ["board", "edge", "defect"]},
            label_schema_version="ls-test",
            training_pipeline_slug="board-segmentation",
            status="active",
            default_model_template="segmentation-yolov26-n",
            default_training_profile="safe-default",
            preferred_endpoint_mode="realtime",
        )
        session.add(project)
        seed_ready_snapshot(
            session,
            snapshot_id="dss_multi_segmentation",
            project_id=project.id,
        )

        instance_model = create_project_model_family(
            session,
            label_project_id=project.id,
            payload=ProjectModelCreateRequest(
                model_template_key="segmentation-yolov26-n",
                task_type=TaskType.SEGMENTATION,
                architecture_family="yolo",
                architecture_version="v26",
                architecture_size="n",
                display_name="Defect instances",
                class_names=["defect", "edge"],
                segmentation_mode="instance",
            ),
        )
        semantic_model = create_project_model_family(
            session,
            label_project_id=project.id,
            payload=ProjectModelCreateRequest(
                model_template_key="segmentation-deeplab-v3-resnet50",
                task_type=TaskType.SEGMENTATION,
                architecture_family="deeplab",
                architecture_version="v3",
                architecture_size="resnet50",
                display_name="Board surface",
                class_names=["board"],
                segmentation_mode="semantic",
            ),
        )
        session.commit()

        instance_request = build_project_training_request(
            session,
            label_project_id=project.id,
            payload=ProjectTrainRequest(model_family_id=instance_model.model_family_id),
        )
        semantic_request = build_project_training_request(
            session,
            label_project_id=project.id,
            payload=ProjectTrainRequest(model_family_id=semantic_model.model_family_id),
        )
        create_training_job_request(
            session,
            instance_request,
            requested_by="tester",
            source_service="unit-test",
        )
        create_training_job_request(
            session,
            semantic_request,
            requested_by="tester",
            source_service="unit-test",
        )

        with pytest.raises(ValueError, match="already has active training job"):
            create_training_job_request(
                session,
                instance_request,
                requested_by="tester",
                source_service="unit-test",
            )

    assert instance_model.segmentation_mode == "instance"
    assert instance_model.class_names == ["defect", "edge"]
    assert instance_model.uses_all_classes is False
    assert semantic_model.segmentation_mode == "semantic"
    assert semantic_model.class_names == ["board"]
    assert instance_request.parameter_overrides["class_names"] == ["defect", "edge"]
    assert instance_request.parameter_overrides["segmentation_mode"] == "instance"
    assert semantic_request.parameter_overrides["class_names"] == ["board"]
    assert semantic_request.parameter_overrides["segmentation_mode"] == "semantic"


def test_project_model_rejects_unknown_classes_and_incompatible_segmentation_mode(
    sqlite_engine,
) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        project = LabelProject(
            id="lpr_multi_segmentation_validation",
            label_studio_project_id=208,
            dataset_id=None,
            title="Board Segmentation Validation",
            task_type=TaskType.SEGMENTATION.value,
            label_config={"polygonlabels": ["board", "defect"]},
            label_schema_version="ls-test",
            training_pipeline_slug="board-segmentation-validation",
            status="active",
            default_model_template="segmentation-yolov26-n",
            default_training_profile="safe-default",
            preferred_endpoint_mode="realtime",
        )
        session.add(project)
        session.flush()

        with pytest.raises(ValueError, match="not part of the Label Studio project"):
            create_project_model_family(
                session,
                label_project_id=project.id,
                payload=ProjectModelCreateRequest(
                    model_template_key="segmentation-yolov26-n",
                    task_type=TaskType.SEGMENTATION,
                    architecture_family="yolo",
                    architecture_version="v26",
                    architecture_size="n",
                    class_names=["missing"],
                    segmentation_mode="instance",
                ),
            )

        with pytest.raises(ValueError, match="not semantic segmentation"):
            create_project_model_family(
                session,
                label_project_id=project.id,
                payload=ProjectModelCreateRequest(
                    model_template_key="segmentation-yolov26-n",
                    task_type=TaskType.SEGMENTATION,
                    architecture_family="yolo",
                    architecture_version="v26",
                    architecture_size="n",
                    class_names=["defect"],
                    segmentation_mode="semantic",
                ),
            )


def test_classification_training_request_uses_macro_f1_and_class_balance(sqlite_engine) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        project = LabelProject(
            id="lpr_classification_balance",
            label_studio_project_id=303,
            dataset_id=None,
            title="Classifier Project",
            task_type=TaskType.CLASSIFICATION.value,
            label_config={},
            label_schema_version="ls-test",
            training_pipeline_slug="classifier-project",
            status="active",
            default_model_template="classification-resnet-18",
            default_training_profile="safe-default",
            preferred_endpoint_mode="realtime",
            class_balance_strategy="weighted_loss",
        )
        session.add(project)
        seed_ready_snapshot(
            session,
            snapshot_id="dss_classification_balance",
            project_id=project.id,
        )
        seed_project_model_family(
            session,
            family_id="mfy_classification_balance",
            project_id=project.id,
            slug="classifier-family",
            template_key="classification-resnet-18",
            task_type=TaskType.CLASSIFICATION,
        )
        session.commit()

        request = build_project_training_request(session, label_project_id=project.id)

        assert request.best_checkpoint_rule.metric_name == "f1_macro"
        assert request.optimizer_settings["class_balance_strategy"] == "weighted_loss"
        assert request.parameter_overrides["class_balance_strategy"] == "weighted_loss"


def test_project_model_inherits_template_promotion_policy(sqlite_engine) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        project = seed_detection_project(session, project_id="lpr_promotion_policy_inherit")
        seed_ready_snapshot(session, snapshot_id="dss_promotion_policy_inherit", project_id=project.id)
        template = create_model_template(
            session,
            payload=ModelTemplateCreateRequest(
                template_key="detection-yolov8-n-strict",
                label="YOLOv8 Nano Strict Detector",
                description="Strict detector template for promotion policy inheritance tests.",
                task_type=TaskType.DETECTION,
                architecture_family="yolo",
                architecture_version="v8",
                architecture_size="n",
                display_name_suffix="Detector",
                default_training_profile="safe-default",
                default_promotion_policy=PromotionCriteriaPolicy(
                    allow_model_override=False,
                    criteria=[
                        PromotionCriterion(
                            metric_name="map_50_95",
                            comparison="gte",
                            threshold=0.8,
                            phase="evaluation",
                        )
                    ],
                ),
            ),
            created_by="tester",
        )
        model = create_project_model_family(
            session,
            label_project_id=project.id,
            payload=ProjectModelCreateRequest(
                model_template_key=template.template_key,
                task_type=TaskType.DETECTION,
                architecture_family="yolo",
                architecture_version="v8",
                architecture_size="n",
                display_name="Strict detector",
            ),
        )
        session.commit()

        request = build_project_training_request(
            session,
            label_project_id=project.id,
            payload=None,
        )

    assert model.promotion_policy.allow_model_override is False
    assert model.promotion_policy.criteria[0].threshold == 0.8
    assert request.promotion_policy is not None
    assert request.promotion_policy.criteria[0].threshold == 0.8
    assert request.promotion_criteria[0].threshold == 0.8


def test_create_project_model_family_retargets_choice_project_to_anomaly(sqlite_engine) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        dataset = seed_classification_dataset(session)
        project = LabelProject(
            id="lpr_choice_anomaly",
            label_studio_project_id=404,
            dataset_id=dataset.id,
            title="Choice Anomaly",
            task_type=TaskType.CLASSIFICATION.value,
            label_config={"choices": ["normal", "anomaly"]},
            label_schema_version="ls-test",
            training_pipeline_slug="choice-anomaly",
            status="active",
            default_model_template="classification-resnet-18",
            default_training_profile="safe-default",
            preferred_endpoint_mode="realtime",
        )
        session.add(project)
        snapshot = seed_ready_snapshot(
            session,
            snapshot_id="dss_choice_anomaly_old",
            project_id=project.id,
        )
        session.commit()

        model = create_project_model_family(
            session,
            label_project_id=project.id,
            payload=ProjectModelCreateRequest(
                model_template_key="anomaly-patchcore-wide-resnet50-2",
                task_type=TaskType.ANOMALY,
                architecture_family="patchcore",
                architecture_version="anomalib",
                architecture_size="wide_resnet50_2",
                display_name="Choice anomaly detector",
            ),
        )
        session.commit()

        session.refresh(project)
        session.refresh(dataset)
        session.refresh(snapshot)

    assert model.task_type == TaskType.ANOMALY
    assert project.task_type == TaskType.ANOMALY.value
    assert project.default_model_template == "anomaly-patchcore-wide-resnet50-2"
    assert dataset.task_type == TaskType.ANOMALY.value
    assert dataset.status == DatasetStatus.READY.value
    assert snapshot.status == SnapshotStatus.SUPERSEDED.value


def test_build_project_training_request_retargets_stale_project_from_anomaly_model(sqlite_engine) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        dataset = seed_classification_dataset(session)
        project = LabelProject(
            id="lpr_stale_anomaly_training",
            label_studio_project_id=405,
            dataset_id=dataset.id,
            title="Stale Anomaly Training",
            task_type=TaskType.CLASSIFICATION.value,
            label_config={"choices": ["normal", "anomaly"]},
            label_schema_version="ls-test",
            training_pipeline_slug="stale-anomaly-training",
            status="active",
            default_model_template="classification-resnet-18",
            default_training_profile="safe-default",
            preferred_endpoint_mode="realtime",
        )
        session.add(project)
        snapshot = seed_ready_snapshot(
            session,
            snapshot_id="dss_stale_anomaly_training_old",
            project_id=project.id,
        )
        seed_project_model_family(
            session,
            family_id="mfy_stale_anomaly_training",
            project_id=project.id,
            slug="stale-anomaly-detector",
            template_key="anomaly-patchcore-wide-resnet50-2",
            task_type=TaskType.ANOMALY,
        )
        session.commit()

        with pytest.raises(ValueError, match="valid immutable dataset snapshot"):
            build_project_training_request(session, label_project_id=project.id)

        session.flush()
        session.refresh(project)
        session.refresh(dataset)
        session.refresh(snapshot)

    assert project.task_type == TaskType.ANOMALY.value
    assert project.default_model_template == "anomaly-patchcore-wide-resnet50-2"
    assert dataset.task_type == TaskType.ANOMALY.value
    assert snapshot.status == SnapshotStatus.SUPERSEDED.value


def test_build_project_training_request_supports_anomaly_after_fresh_snapshot(sqlite_engine) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        dataset = Dataset(
            id="dst_anomaly_ready",
            slug="dst-anomaly-ready",
            name="Anomaly Ready Dataset",
            task_type=TaskType.ANOMALY.value,
            label_schema_version="ls-test",
            status=DatasetStatus.SNAPSHOTTED.value,
        )
        project = LabelProject(
            id="lpr_anomaly_training_ready",
            label_studio_project_id=406,
            dataset_id=dataset.id,
            title="Anomaly Training Ready",
            task_type=TaskType.ANOMALY.value,
            label_config={"choices": ["normal", "anomaly"]},
            label_schema_version="ls-test",
            training_pipeline_slug="anomaly-training-ready",
            status="active",
            default_model_template="anomaly-patchcore-wide-resnet50-2",
            default_training_profile="safe-default",
            preferred_endpoint_mode="realtime",
        )
        session.add_all([dataset, project])
        snapshot = DatasetSnapshot(
            id="dss_anomaly_training_ready",
            dataset_id=dataset.id,
            label_project_id=project.id,
            snapshot_no=1,
            status=SnapshotStatus.READY.value,
            validation_status=ValidationStatus.PASSED.value,
            idempotency_key="snapshot:dss_anomaly_training_ready",
            label_schema_version="ls-test",
            split_metadata={},
            validation_report={},
            statistics={},
        )
        session.add(snapshot)
        seed_project_model_family(
            session,
            family_id="mfy_anomaly_training_ready",
            project_id=project.id,
            slug="anomaly-training-ready-detector",
            template_key="anomaly-patchcore-wide-resnet50-2",
            task_type=TaskType.ANOMALY,
        )
        session.commit()

        request = build_project_training_request(session, label_project_id=project.id)

    assert request.task_type == TaskType.ANOMALY
    assert request.recipe_name == "baseline_anomaly"
    assert request.parameter_overrides["template_key"] == "anomaly-patchcore-wide-resnet50-2"


def test_project_model_rejects_promotion_override_when_template_disallows_it(sqlite_engine) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        project = seed_detection_project(session, project_id="lpr_promotion_policy_locked")
        template = create_model_template(
            session,
            payload=ModelTemplateCreateRequest(
                template_key="detection-yolov8-s-locked",
                label="YOLOv8 Small Locked Detector",
                description="Locked detector template for promotion policy override tests.",
                task_type=TaskType.DETECTION,
                architecture_family="yolo",
                architecture_version="v8",
                architecture_size="s",
                display_name_suffix="Detector",
                default_training_profile="safe-default",
                default_promotion_policy=PromotionCriteriaPolicy(
                    allow_model_override=False,
                    criteria=[
                        PromotionCriterion(
                            metric_name="map_50_95",
                            comparison="gte",
                            threshold=0.75,
                            phase="evaluation",
                        )
                    ],
                ),
            ),
            created_by="tester",
        )

        with pytest.raises(ValueError, match="does not allow model-level promotion"):
            create_project_model_family(
                session,
                label_project_id=project.id,
                payload=ProjectModelCreateRequest(
                    model_template_key=template.template_key,
                    task_type=TaskType.DETECTION,
                    architecture_family="yolo",
                    architecture_version="v8",
                    architecture_size="s",
                    display_name="Override detector",
                    promotion_policy=PromotionCriteriaPolicy(
                        criteria=[
                            PromotionCriterion(
                                metric_name="map_50_95",
                                comparison="gte",
                                threshold=0.1,
                                phase="evaluation",
                            )
                        ],
                    ),
                ),
            )


def test_training_job_request_rejects_existing_model_family_with_other_task_type(sqlite_engine) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        project = seed_detection_project(session, project_id="lpr_training_family_mismatch")
        snapshot = seed_ready_snapshot(session, snapshot_id="dss_training_family_mismatch", project_id=project.id)
        seed_project_model_family(
            session,
            family_id="mfy_family_mismatch",
            project_id=project.id,
            slug="shared-family",
            template_key="classification-resnet-18",
            task_type=TaskType.CLASSIFICATION,
        )
        session.commit()

        payload = TrainingJobCreateRequest(
            model_family_slug="shared-family",
            dataset_snapshot_id=snapshot.id,
            task_type=TaskType.DETECTION,
            architecture=architecture_summary(
                task_type=TaskType.DETECTION,
                family="yolo",
                version="v8",
                size="n",
            ),
            recipe_name="baseline_det",
            recipe_version="1.0.0",
            max_epochs=3,
            checkpoint_interval_steps=5,
            parameter_overrides={"epochs": 3, "checkpoint_interval_steps": 5},
            best_checkpoint_rule=BestCheckpointRule(
                metric_name="map_50_95",
                mode="max",
                phase="validation",
                minimum_epoch=1,
            ),
            promotion_criteria=[
                PromotionCriterion(
                    metric_name="map_50_95",
                    comparison="gte",
                    threshold=0.5,
                    phase="evaluation",
                )
            ],
        )

        with pytest.raises(ValueError, match="already exists with task type classification"):
            create_training_job_request(
                session,
                payload,
                requested_by="tester",
                source_service="tests",
            )
