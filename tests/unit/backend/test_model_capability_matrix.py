from __future__ import annotations

import pytest
from cv_db.model_catalog import (
    supported_model_architectures,
    supported_model_capability,
)
from cv_db.orchestration_store import list_project_model_templates
from cv_status import TaskType
from cv_training.trainer import get_default_trainer, resolve_trainer_architecture

YOLO_SIZES = ("n", "s", "m", "l", "x")


def _matrix_keys(task_type: TaskType) -> set[tuple[str, str, str]]:
    return {
        (
            item.architecture.family,
            item.architecture.version,
            item.architecture.size,
        )
        for item in supported_model_architectures(task_type=task_type)
        if item.supported and item.status == "supported"
    }


def test_supported_capability_matrix_matches_required_production_models() -> None:
    assert _matrix_keys(TaskType.CLASSIFICATION) == {
        ("resnet", "v1", "18"),
        ("resnet", "v1", "50"),
        ("mobilenet", "v3", "small"),
        ("mobilenet", "v3", "large"),
        ("efficientnet", "v1", "b0"),
        *{("yolo", version, size) for version in ("v11", "v26") for size in YOLO_SIZES},
    }
    assert _matrix_keys(TaskType.DETECTION) == {
        ("yolo", version, size)
        for version in ("v8", "v11", "v26")
        for size in YOLO_SIZES
    }
    assert _matrix_keys(TaskType.ORIENTED_DETECTION) == {
        ("yolo", "v26", size) for size in YOLO_SIZES
    }
    assert _matrix_keys(TaskType.SEGMENTATION) == {
        ("deeplab", "v3", "resnet50"),
        ("fcn", "v1", "resnet50"),
        ("lraspp", "v3", "large"),
        *{("yolo", version, size) for version in ("v8", "v11", "v26") for size in YOLO_SIZES},
    }
    assert _matrix_keys(TaskType.ANOMALY) == {
        ("patchcore", "anomalib", "wide_resnet50_2"),
    }


@pytest.mark.parametrize("capability", supported_model_architectures())
def test_every_supported_capability_is_full_lifecycle(capability) -> None:  # noqa: ANN001
    assert capability.supported is True
    assert capability.export_support is True
    assert capability.deployment_support is True
    assert capability.inference_support is True
    assert capability.trainer_backend != "unknown"
    assert capability.concrete_architecture != "unknown"
    if capability.architecture.task_type == TaskType.TEMPORAL_SEQUENCE:
        assert capability.status == "experimental"
        assert capability.transfer_learning_policy == "none"
        assert "shadow_only" in capability.status_flags
    else:
        assert capability.status == "supported"
        assert capability.transfer_learning_policy == "auto_pretrained_le_2000"


def test_operator_templates_are_subset_of_backend_capabilities() -> None:
    capability_keys = {
        (
            item.architecture.task_type,
            item.architecture.family,
            item.architecture.version,
            item.architecture.size,
        )
        for item in supported_model_architectures()
        if item.supported and item.status == "supported"
    }
    template_keys = {
        (
            template.task_type,
            template.architecture.family,
            template.architecture.version,
            template.architecture.size,
        )
        for template in list_project_model_templates()
    }

    assert template_keys <= capability_keys
    assert (TaskType.CLASSIFICATION, "yolo", "v8", "n") not in template_keys
    assert (TaskType.ORIENTED_DETECTION, "yolo", "v26", "n") in template_keys
    assert (TaskType.SEGMENTATION, "yolo", "v8", "n") in template_keys
    assert (TaskType.ANOMALY, "patchcore", "anomalib", "wide_resnet50_2") in template_keys


def test_anomaly_capability_exposes_only_end_to_end_patchcore_support() -> None:
    capability = supported_model_capability(
        task_type=TaskType.ANOMALY,
        family="patchcore",
        version="anomalib",
        size="wide_resnet50_2",
    )

    assert capability is not None
    assert capability.architecture.display_name == "PatchCore WideResNet-50-2"
    assert capability.capability_metadata["method"] == "patchcore"
    assert capability.capability_metadata["training_backend"] == "anomalib-patchcore"
    assert capability.capability_metadata["runtime_backend"] == "anomalib-openvino-inferencer"
    assert capability.capability_metadata["dataset_format"] == "anomalib_folder_image_level"
    assert capability.capability_metadata["heatmap_support"] is True
    assert capability.capability_metadata["score_output_support"] is True
    assert capability.capability_metadata["thresholding_support"] is True
    assert capability.capability_metadata["mask_support"] is False


@pytest.mark.parametrize(
    ("task_type", "family", "version", "size", "backend", "concrete"),
    [
        (TaskType.CLASSIFICATION, "resnet", "v1", "18", "torchvision-classification", "resnet18"),
        (TaskType.CLASSIFICATION, "yolo", "v26", "x", "ultralytics-yolo", "yolo26x-cls"),
        (
            TaskType.DETECTION,
            "yolo",
            "v8",
            "x",
            "openmmlab-mmyolo",
            "yolov8_x_syncbn_fast_8xb16-500e_coco",
        ),
        (TaskType.DETECTION, "yolo", "v11", "m", "ultralytics-yolo", "yolo11m"),
        (TaskType.ORIENTED_DETECTION, "yolo", "v26", "m", "ultralytics-yolo", "yolo26m-obb"),
        (TaskType.SEGMENTATION, "deeplab", "v3", "resnet50", "torchvision-segmentation", "deeplabv3_resnet50"),
        (TaskType.SEGMENTATION, "yolo", "v8", "n", "ultralytics-yolo", "yolov8n-seg"),
        (TaskType.SEGMENTATION, "yolo", "v26", "l", "ultralytics-yolo", "yolo26l-seg"),
        (TaskType.ANOMALY, "patchcore", "anomalib", "wide_resnet50_2", "anomalib-patchcore", "anomalib:patchcore:wide_resnet50_2"),
    ],
)
def test_capability_routing_resolves_exact_backend_and_architecture(
    task_type: TaskType,
    family: str,
    version: str,
    size: str,
    backend: str,
    concrete: str,
) -> None:
    capability = supported_model_capability(
        task_type=task_type,
        family=family,
        version=version,
        size=size,
    )
    assert capability is not None
    assert capability.trainer_backend == backend
    assert capability.concrete_architecture == concrete

    architecture = resolve_trainer_architecture(
        task_type=task_type,
        family=family,
        version=version,
        size=size,
    )
    assert architecture.supported is True
    assert architecture.backend_model_key == concrete
    assert get_default_trainer(
        task_type,
        trainer_mode="real",
        architecture=architecture,
    ).trainer_backend == backend
