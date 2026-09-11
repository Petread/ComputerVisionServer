from __future__ import annotations

from typing import Any

from cv_contracts.modeling import (
    ModelArchitectureSummary,
    ModelTemplateDefaults,
    ModelTemplateSummary,
    SupportedModelArchitecture,
    TrainingDefaults,
)
from cv_contracts.promotion import PromotionCriteriaPolicy, PromotionCriterion
from cv_status import TaskType

SIZE_LABELS = {
    "n": "Nano",
    "s": "Small",
    "m": "Medium",
    "l": "Large",
    "x": "X-Large",
    "18": "18",
    "50": "50",
    "b0": "B0",
    "large": "Large",
    "resnet50": "ResNet-50",
    "wide_resnet50_2": "WideResNet-50-2",
}

YOLO_CLASSIFICATION_VERSIONS = ("v11", "v26")
YOLO_ULTRALYTICS_VERSIONS = ("v11", "v26")
YOLO_DETECTION_VERSIONS = ("v8", *YOLO_ULTRALYTICS_VERSIONS)
YOLO_ORIENTED_DETECTION_VERSIONS = ("v26",)
YOLO_SEGMENTATION_VERSIONS = ("v8", *YOLO_ULTRALYTICS_VERSIONS)
YOLO_MODEL_SIZES = ("n", "s", "m", "l", "x")
YOLO_DETECTION_SIZES = YOLO_MODEL_SIZES
TRANSFER_LEARNING_POLICY = "auto_pretrained_le_2000"
TEMPORAL_TRAINING_ARCHITECTURES = {
    ("tcn", "v1", "small"): "causal-tcn-small-v1",
    ("gru", "v1", "small"): "causal-gru-small-v1",
}

ANOMALY_METHOD_SUPPORT_MATRIX: dict[tuple[str, str, str], dict[str, Any]] = {
    ("patchcore", "anomalib", "wide_resnet50_2"): {
        "method": "patchcore",
        "training_backend": "anomalib-patchcore",
        "framework": "anomalib",
        "backbone": "wide_resnet50_2",
        "export_format": "onnx",
        "runtime_backend": "anomalib-openvino-inferencer",
        "dataset_format": "anomalib_folder_image_level",
        "heatmap_support": True,
        "score_output_support": True,
        "thresholding_support": True,
        "mask_support": False,
        "localization_masks_from_label_studio": False,
        "status_flags": [
            "real_training",
            "real_checkpoint",
            "real_onnx_export",
            "deployment_packaging",
            "realtime_inference",
            "heatmap_output",
            "score_output",
            "thresholding",
            "image_level_good_bad_dataset",
        ],
    }
}

SUPPORTED_ARCHITECTURE_MATRIX: dict[TaskType, list[tuple[str, str, str]]] = {
    TaskType.CLASSIFICATION: [
        ("resnet", "v1", "18"),
        ("resnet", "v1", "50"),
        ("mobilenet", "v3", "small"),
        ("mobilenet", "v3", "large"),
        ("efficientnet", "v1", "b0"),
        *[
            ("yolo", version, size)
            for version in YOLO_CLASSIFICATION_VERSIONS
            for size in YOLO_MODEL_SIZES
        ],
    ],
    TaskType.DETECTION: [
        ("yolo", version, size)
        for version in YOLO_DETECTION_VERSIONS
        for size in YOLO_DETECTION_SIZES
    ],
    TaskType.ORIENTED_DETECTION: [
        ("yolo", version, size)
        for version in YOLO_ORIENTED_DETECTION_VERSIONS
        for size in YOLO_DETECTION_SIZES
    ],
    TaskType.SEGMENTATION: [
        ("deeplab", "v3", "resnet50"),
        ("fcn", "v1", "resnet50"),
        ("lraspp", "v3", "large"),
        *[
            ("yolo", version, size)
            for version in YOLO_SEGMENTATION_VERSIONS
            for size in YOLO_MODEL_SIZES
        ],
    ],
    TaskType.ANOMALY: list(ANOMALY_METHOD_SUPPORT_MATRIX.keys()),
    TaskType.TEMPORAL_SEQUENCE: list(TEMPORAL_TRAINING_ARCHITECTURES.keys()),
}

TORCHVISION_CLASSIFICATION_MODEL_KEYS = {
    ("resnet", "v1", "18"): "resnet18",
    ("resnet", "v1", "50"): "resnet50",
    ("mobilenet", "v3", "small"): "mobilenet_v3_small",
    ("mobilenet", "v3", "large"): "mobilenet_v3_large",
    ("efficientnet", "v1", "b0"): "efficientnet_b0",
}

TORCHVISION_SEGMENTATION_MODEL_KEYS = {
    ("deeplab", "v3", "resnet50"): "deeplabv3_resnet50",
    ("fcn", "v1", "resnet50"): "fcn_resnet50",
    ("lraspp", "v3", "large"): "lraspp_mobilenet_v3_large",
}


def is_supported_architecture(
    *,
    task_type: TaskType,
    family: str,
    version: str,
    size: str,
) -> bool:
    supported = SUPPORTED_ARCHITECTURE_MATRIX.get(task_type, [])
    return (
        family.lower(),
        version.lower(),
        size.lower(),
    ) in supported


def is_trainable_architecture(
    *,
    task_type: TaskType,
    family: str,
    version: str,
    size: str,
) -> bool:
    if is_supported_architecture(
        task_type=task_type,
        family=family,
        version=version,
        size=size,
    ):
        return True
    return task_type == TaskType.TEMPORAL_SEQUENCE and (
        family.lower(),
        version.lower(),
        size.lower(),
    ) in TEMPORAL_TRAINING_ARCHITECTURES


def is_supported_template_definition(template_key: str) -> bool:
    definition = DEFAULT_TEMPLATE_DEFINITIONS[template_key]
    return is_supported_architecture(
        task_type=definition["task_type"],
        family=str(definition["family"]),
        version=str(definition["version"]),
        size=str(definition["size"]),
    )


def architecture_display_name(*, family: str, version: str, size: str) -> str:
    normalized_family = family.lower()
    normalized_size = size.lower()
    size_label = SIZE_LABELS.get(normalized_size, size.upper())
    if normalized_family == "yolo":
        return f"YOLO{version.lower()} {size_label}"
    if normalized_family == "resnet":
        return f"ResNet-{size_label}"
    if normalized_family == "mobilenet":
        return f"MobileNet{version.upper()} {size_label}"
    if normalized_family == "efficientnet":
        return f"EfficientNet-{size_label}"
    if normalized_family == "deeplab":
        return f"DeepLab{version.upper()} {size_label}"
    if normalized_family == "fcn":
        return f"FCN {size_label}"
    if normalized_family == "lraspp":
        return f"LRASPP {size_label}"
    if normalized_family == "patchcore":
        return f"PatchCore {size_label}"
    if normalized_family == "tcn":
        return f"Causal TCN {size_label}"
    if normalized_family == "gru":
        return f"Causal GRU {size_label}"
    return f"{family.upper()}{version.upper()} {size_label}"


def architecture_internal_key(*, task_type: TaskType, family: str, version: str, size: str) -> str:
    return f"{task_type.value}:{family.lower()}:{version.lower()}:{size.lower()}"


def architecture_summary(
    *,
    task_type: TaskType,
    family: str,
    version: str,
    size: str,
) -> ModelArchitectureSummary:
    return ModelArchitectureSummary(
        task_type=task_type,
        family=family.lower(),
        version=version.lower(),
        size=size.lower(),
        display_name=architecture_display_name(family=family, version=version, size=size),
        internal_key=architecture_internal_key(
            task_type=task_type,
            family=family,
            version=version,
            size=size,
        ),
    )


def supported_model_architectures(
    *,
    task_type: TaskType | None = None,
) -> list[SupportedModelArchitecture]:
    task_types = (
        [task_type] if task_type is not None else list(SUPPORTED_ARCHITECTURE_MATRIX.keys())
    )
    items: list[SupportedModelArchitecture] = []
    for current_task_type in task_types:
        for family, version, size in SUPPORTED_ARCHITECTURE_MATRIX.get(current_task_type, []):
            capability_metadata = architecture_capability_metadata(
                task_type=current_task_type,
                family=family,
                version=version,
                size=size,
            )
            items.append(
                SupportedModelArchitecture(
                    architecture=architecture_summary(
                        task_type=current_task_type,
                        family=family,
                        version=version,
                        size=size,
                    ),
                    supported=True,
                    support_reason=(
                        "Causal temporal training, export, shadow deployment packaging, "
                        "and runtime inference contracts are implemented."
                        if current_task_type == TaskType.TEMPORAL_SEQUENCE
                        else "End-to-end training, export, deployment packaging, and "
                        "inference contracts are implemented."
                    ),
                    trainer_backend=trainer_backend_for_architecture(
                        task_type=current_task_type,
                        family=family,
                        version=version,
                        size=size,
                    ),
                    concrete_architecture=concrete_trainer_architecture(
                        task_type=current_task_type,
                        family=family,
                        version=version,
                        size=size,
                    ),
                    export_support=True,
                    deployment_support=True,
                    inference_support=True,
                    transfer_learning_policy=(
                        "none"
                        if current_task_type == TaskType.TEMPORAL_SEQUENCE
                        else TRANSFER_LEARNING_POLICY
                    ),
                    status=(
                        "experimental"
                        if current_task_type == TaskType.TEMPORAL_SEQUENCE
                        else "supported"
                    ),
                    status_flags=(
                        list(capability_metadata.get("status_flags") or [])
                        if capability_metadata.get("status_flags")
                        else [
                            "real_training",
                            "real_checkpoint",
                            "real_onnx_export",
                            "deployment_packaging",
                            "realtime_inference",
                            "batch_inference",
                        ]
                    ),
                    capability_metadata=capability_metadata,
                )
            )
    return items


def supported_model_capability(
    *,
    task_type: TaskType,
    family: str,
    version: str,
    size: str,
) -> SupportedModelArchitecture | None:
    normalized = (
        family.lower(),
        version.lower(),
        size.lower(),
    )
    for capability in supported_model_architectures(task_type=task_type):
        architecture = capability.architecture
        if (
            architecture.family,
            architecture.version,
            architecture.size,
        ) == normalized:
            return capability
    return None


def trainable_model_capability(
    *,
    task_type: TaskType,
    family: str,
    version: str,
    size: str,
) -> SupportedModelArchitecture | None:
    capability = supported_model_capability(
        task_type=task_type,
        family=family,
        version=version,
        size=size,
    )
    if capability is not None:
        return capability
    normalized = (family.lower(), version.lower(), size.lower())
    concrete = TEMPORAL_TRAINING_ARCHITECTURES.get(normalized)
    if task_type != TaskType.TEMPORAL_SEQUENCE or concrete is None:
        return None
    return SupportedModelArchitecture(
        architecture=architecture_summary(
            task_type=task_type,
            family=normalized[0],
            version=normalized[1],
            size=normalized[2],
        ),
        supported=True,
        support_reason=(
            "Training, checkpointing, evaluation, ONNX export, shadow deployment, "
            "and causal runtime inference are implemented."
        ),
        trainer_backend="pytorch-temporal-sequence",
        concrete_architecture=concrete,
        export_support=True,
        deployment_support=True,
        inference_support=True,
        transfer_learning_policy="none",
        status="experimental",
        status_flags=[
            "real_training",
            "real_checkpoint",
            "real_onnx_export",
            "deployment_packaging",
            "realtime_inference",
            "shadow_only",
            "no_automatic_production_promotion",
        ],
        capability_metadata={
            "causal": True,
            "input_kind": "feature_sequence",
            "angle_representation": "sin_2e_cos_2e",
            "angle_wrap_range_deg": "[-90,90)",
        },
    )


def trainer_backend_for_architecture(
    *,
    task_type: TaskType,
    family: str,
    version: str,
    size: str,
) -> str:
    normalized = (family.lower(), version.lower(), size.lower())
    if task_type == TaskType.TEMPORAL_SEQUENCE and normalized in TEMPORAL_TRAINING_ARCHITECTURES:
        return "pytorch-temporal-sequence"
    if task_type == TaskType.ANOMALY and normalized in ANOMALY_METHOD_SUPPORT_MATRIX:
        return str(ANOMALY_METHOD_SUPPORT_MATRIX[normalized]["training_backend"])
    if family.lower() == "yolo":
        if task_type == TaskType.ORIENTED_DETECTION:
            return "ultralytics-yolo"
        if task_type == TaskType.DETECTION and version.lower() == "v8":
            return "openmmlab-mmyolo"
        return "ultralytics-yolo"
    if task_type == TaskType.CLASSIFICATION and normalized in TORCHVISION_CLASSIFICATION_MODEL_KEYS:
        return "torchvision-classification"
    if task_type == TaskType.SEGMENTATION and normalized in TORCHVISION_SEGMENTATION_MODEL_KEYS:
        return "torchvision-segmentation"
    return "unknown"


def concrete_trainer_architecture(
    *,
    task_type: TaskType,
    family: str,
    version: str,
    size: str,
) -> str:
    normalized = (family.lower(), version.lower(), size.lower())
    if task_type == TaskType.TEMPORAL_SEQUENCE and normalized in TEMPORAL_TRAINING_ARCHITECTURES:
        return TEMPORAL_TRAINING_ARCHITECTURES[normalized]
    if task_type == TaskType.ANOMALY and normalized in ANOMALY_METHOD_SUPPORT_MATRIX:
        support = ANOMALY_METHOD_SUPPORT_MATRIX[normalized]
        return f"{str(support['framework']).lower()}:{family.lower()}:{support['backbone']}"
    if family.lower() == "yolo":
        normalized_version = version.lower()
        version_number = (
            normalized_version
            if normalized_version == "v8"
            else normalized_version.lstrip("v")
        )
        suffix = ""
        if task_type == TaskType.CLASSIFICATION:
            suffix = "-cls"
        elif task_type == TaskType.ORIENTED_DETECTION:
            suffix = "-obb"
        elif task_type == TaskType.SEGMENTATION:
            suffix = "-seg"
        if task_type == TaskType.DETECTION and normalized_version == "v8":
            return f"yolov8_{size.lower()}_syncbn_fast_8xb16-500e_coco"
        return f"yolo{version_number}{size.lower()}{suffix}"
    if task_type == TaskType.CLASSIFICATION:
        return TORCHVISION_CLASSIFICATION_MODEL_KEYS.get(normalized, "unknown")
    if task_type == TaskType.SEGMENTATION:
        return TORCHVISION_SEGMENTATION_MODEL_KEYS.get(normalized, "unknown")
    return "unknown"


def anomaly_capability_metadata(
    *,
    task_type: TaskType,
    family: str,
    version: str,
    size: str,
) -> dict[str, Any]:
    if task_type != TaskType.ANOMALY:
        return {}
    support = ANOMALY_METHOD_SUPPORT_MATRIX.get(
        (family.lower(), version.lower(), size.lower()),
        {},
    )
    return dict(support)


def architecture_capability_metadata(
    *,
    task_type: TaskType,
    family: str,
    version: str,
    size: str,
) -> dict[str, Any]:
    metadata = anomaly_capability_metadata(
        task_type=task_type,
        family=family,
        version=version,
        size=size,
    )
    if task_type == TaskType.TEMPORAL_SEQUENCE:
        metadata.update(
            {
                "causal": True,
                "input_kind": "feature_sequence",
                "angle_representation": "sin_2e_cos_2e",
                "angle_wrap_range_deg": "[-90,90)",
                "status_flags": [
                    "real_training",
                    "real_checkpoint",
                    "real_onnx_export",
                    "deployment_packaging",
                    "realtime_inference",
                    "shadow_only",
                    "no_automatic_production_promotion",
                ],
            }
        )
    if task_type == TaskType.SEGMENTATION:
        metadata["segmentation_mode"] = (
            "instance" if family.lower() == "yolo" else "semantic"
        )
        metadata["annotation_source"] = "polygons"
    return metadata


def default_promotion_policy(task_type: TaskType) -> PromotionCriteriaPolicy:
    criteria = [
        PromotionCriterion.model_validate(item)
        for item in default_promotion_criteria(task_type)
    ]
    return PromotionCriteriaPolicy(criteria=criteria, allow_model_override=True)


def default_promotion_criteria(task_type: TaskType) -> list[dict[str, str | float]]:
    if task_type == TaskType.ANOMALY:
        return [
            {
                "metric_name": "image_auroc",
                "comparison": "gte",
                "threshold": 0.9,
                "phase": "evaluation",
            },
            {
                "metric_name": "image_f1",
                "comparison": "gte",
                "threshold": 0.8,
                "phase": "evaluation",
            },
        ]
    if task_type in {TaskType.DETECTION, TaskType.ORIENTED_DETECTION}:
        return [
            {
                "metric_name": "map_50_95",
                "comparison": "gte",
                "threshold": 0.55,
                "phase": "evaluation",
            },
            {
                "metric_name": "map_50",
                "comparison": "gte",
                "threshold": 0.72,
                "phase": "evaluation",
            },
        ]
    if task_type == TaskType.SEGMENTATION:
        return [
            {
                "metric_name": "mask_map_50_95",
                "comparison": "gte",
                "threshold": 0.5,
                "phase": "evaluation",
            },
            {
                "metric_name": "mask_map_50",
                "comparison": "gte",
                "threshold": 0.7,
                "phase": "evaluation",
            },
        ]
    return [
        {
            "metric_name": "accuracy_top1",
            "comparison": "gte",
            "threshold": 0.9,
            "phase": "evaluation",
        },
        {
            "metric_name": "f1_macro",
            "comparison": "gte",
            "threshold": 0.85,
            "phase": "evaluation",
        },
    ]


def _yolo_template_definitions() -> dict[str, dict[str, Any]]:
    task_suffix = {
        TaskType.CLASSIFICATION: "Classifier",
        TaskType.DETECTION: "Detector",
        TaskType.ORIENTED_DETECTION: "OBB Detector",
        TaskType.SEGMENTATION: "Segmenter",
    }
    task_description = {
        TaskType.CLASSIFICATION: "image classification",
        TaskType.DETECTION: "object detection",
        TaskType.ORIENTED_DETECTION: "oriented object detection",
        TaskType.SEGMENTATION: "instance segmentation",
    }
    defaults_by_task_size = {
        TaskType.CLASSIFICATION: {
            "n": {
                "profile": "safe-default",
                "epochs": 60,
                "learning_rate": 0.001,
                "image_size": 640,
                "batch_size": 24,
                "checkpoint_interval_steps": 20,
            },
            "s": {
                "profile": "quality-guarded",
                "epochs": 70,
                "learning_rate": 0.00085,
                "image_size": 960,
                "batch_size": 12,
                "checkpoint_interval_steps": 25,
            },
            "m": {
                "profile": "quality-guarded",
                "epochs": 80,
                "learning_rate": 0.0007,
                "image_size": 960,
                "batch_size": 8,
                "checkpoint_interval_steps": 25,
            },
            "l": {
                "profile": "high-capacity",
                "epochs": 90,
                "learning_rate": 0.0005,
                "image_size": 960,
                "batch_size": 6,
                "checkpoint_interval_steps": 30,
            },
            "x": {
                "profile": "high-capacity",
                "epochs": 100,
                "learning_rate": 0.0004,
                "image_size": 960,
                "batch_size": 4,
                "checkpoint_interval_steps": 35,
            },
        },
        TaskType.DETECTION: {
            "n": {
                "profile": "safe-default",
                "epochs": 40,
                "learning_rate": 0.01,
                "image_size": 640,
                "batch_size": 16,
                "checkpoint_interval_steps": 25,
            },
            "s": {
                "profile": "quality-guarded",
                "epochs": 55,
                "learning_rate": 0.01,
                "image_size": 768,
                "batch_size": 12,
                "checkpoint_interval_steps": 30,
            },
            "m": {
                "profile": "quality-guarded",
                "epochs": 65,
                "learning_rate": 0.01,
                "image_size": 768,
                "batch_size": 8,
                "checkpoint_interval_steps": 30,
            },
            "l": {
                "profile": "high-capacity",
                "epochs": 80,
                "learning_rate": 0.01,
                "image_size": 896,
                "batch_size": 6,
                "checkpoint_interval_steps": 35,
            },
            "x": {
                "profile": "high-capacity",
                "epochs": 90,
                "learning_rate": 0.01,
                "image_size": 1024,
                "batch_size": 4,
                "checkpoint_interval_steps": 40,
            },
        },
        TaskType.ORIENTED_DETECTION: {
            "n": {
                "profile": "safe-default",
                "epochs": 60,
                "learning_rate": 0.01,
                "image_size": 1024,
                "batch_size": 8,
                "checkpoint_interval_steps": 25,
            },
            "s": {
                "profile": "quality-guarded",
                "epochs": 70,
                "learning_rate": 0.01,
                "image_size": 1024,
                "batch_size": 6,
                "checkpoint_interval_steps": 30,
            },
            "m": {
                "profile": "quality-guarded",
                "epochs": 80,
                "learning_rate": 0.01,
                "image_size": 1024,
                "batch_size": 4,
                "checkpoint_interval_steps": 30,
            },
            "l": {
                "profile": "high-capacity",
                "epochs": 90,
                "learning_rate": 0.01,
                "image_size": 1024,
                "batch_size": 3,
                "checkpoint_interval_steps": 35,
            },
            "x": {
                "profile": "high-capacity",
                "epochs": 100,
                "learning_rate": 0.01,
                "image_size": 1024,
                "batch_size": 2,
                "checkpoint_interval_steps": 40,
            },
        },
        TaskType.SEGMENTATION: {
            "n": {
                "profile": "safe-default",
                "epochs": 45,
                "learning_rate": 0.01,
                "image_size": 640,
                "batch_size": 8,
                "checkpoint_interval_steps": 25,
            },
            "s": {
                "profile": "quality-guarded",
                "epochs": 55,
                "learning_rate": 0.01,
                "image_size": 768,
                "batch_size": 6,
                "checkpoint_interval_steps": 30,
            },
            "m": {
                "profile": "quality-guarded",
                "epochs": 65,
                "learning_rate": 0.01,
                "image_size": 768,
                "batch_size": 4,
                "checkpoint_interval_steps": 30,
            },
            "l": {
                "profile": "high-capacity",
                "epochs": 80,
                "learning_rate": 0.01,
                "image_size": 896,
                "batch_size": 3,
                "checkpoint_interval_steps": 35,
            },
            "x": {
                "profile": "high-capacity",
                "epochs": 90,
                "learning_rate": 0.01,
                "image_size": 1024,
                "batch_size": 2,
                "checkpoint_interval_steps": 40,
            },
        },
    }
    optimizer_by_task = {
        TaskType.CLASSIFICATION: ("adamw", {"weight_decay": 0.0005}),
        TaskType.DETECTION: ("sgd", {"momentum": 0.937, "weight_decay": 0.0005}),
        TaskType.ORIENTED_DETECTION: ("sgd", {"momentum": 0.937, "weight_decay": 0.0005}),
        TaskType.SEGMENTATION: ("sgd", {"momentum": 0.937, "weight_decay": 0.0005}),
    }
    versions_by_task = {
        TaskType.CLASSIFICATION: YOLO_CLASSIFICATION_VERSIONS,
        TaskType.DETECTION: YOLO_DETECTION_VERSIONS,
        TaskType.ORIENTED_DETECTION: YOLO_ORIENTED_DETECTION_VERSIONS,
        TaskType.SEGMENTATION: YOLO_SEGMENTATION_VERSIONS,
    }
    items: dict[str, dict[str, Any]] = {}
    for task_type, versions in versions_by_task.items():
        optimizer, optimizer_settings = optimizer_by_task[task_type]
        for version in versions:
            for size in YOLO_MODEL_SIZES:
                display_name = architecture_display_name(family="yolo", version=version, size=size)
                defaults = defaults_by_task_size[task_type][size]
                suffix = task_suffix[task_type]
                items[f"{task_type.value}-yolo{version}-{size}"] = {
                    "label": f"{display_name} {suffix}",
                    "description": (
                        f"{display_name} {task_description[task_type]} preset for "
                        "operator-managed training templates."
                    ),
                    "task_type": task_type,
                    "family": "yolo",
                    "version": version,
                    "size": size,
                    "display_name_suffix": suffix,
                    "default_training_profile": defaults["profile"],
                    "defaults": TrainingDefaults(
                        epochs=defaults["epochs"],
                        learning_rate=defaults["learning_rate"],
                        image_size=defaults["image_size"],
                        batch_size=defaults["batch_size"],
                        optimizer=optimizer,
                        optimizer_settings=dict(optimizer_settings),
                        checkpoint_interval_steps=defaults["checkpoint_interval_steps"],
                    ),
                    "recommended": (
                        (task_type == TaskType.DETECTION and version == "v8" and size == "n")
                        or (
                            task_type == TaskType.ORIENTED_DETECTION
                            and version == "v26"
                            and size == "n"
                        )
                    ),
                }
    return items


DEFAULT_TEMPLATE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "classification-resnet-18": {
        "label": "ResNet-18 Classifier",
        "description": (
            "Transfer-learning friendly classification preset for smaller labeled datasets."
        ),
        "task_type": TaskType.CLASSIFICATION,
        "family": "resnet",
        "version": "v1",
        "size": "18",
        "display_name_suffix": "Classifier",
        "default_training_profile": "safe-default",
        "defaults": TrainingDefaults(
            epochs=30,
            learning_rate=0.0008,
            image_size=224,
            batch_size=32,
            optimizer="adamw",
            optimizer_settings={"weight_decay": 0.0005},
            checkpoint_interval_steps=20,
        ),
        "recommended": True,
    },
    "classification-resnet-50": {
        "label": "ResNet-50 Classifier",
        "description": "Stronger residual classification preset for richer image variation.",
        "task_type": TaskType.CLASSIFICATION,
        "family": "resnet",
        "version": "v1",
        "size": "50",
        "display_name_suffix": "Classifier",
        "default_training_profile": "quality-guarded",
        "defaults": TrainingDefaults(
            epochs=45,
            learning_rate=0.0005,
            image_size=224,
            batch_size=24,
            optimizer="adamw",
            optimizer_settings={"weight_decay": 0.0005},
            checkpoint_interval_steps=25,
        ),
        "recommended": False,
    },
    "classification-mobilenet-v3-small": {
        "label": "MobileNetV3 Small Classifier",
        "description": (
            "Lightweight classification preset for fast iteration and smaller edge targets."
        ),
        "task_type": TaskType.CLASSIFICATION,
        "family": "mobilenet",
        "version": "v3",
        "size": "small",
        "display_name_suffix": "Classifier",
        "default_training_profile": "safe-default",
        "defaults": TrainingDefaults(
            epochs=30,
            learning_rate=0.001,
            image_size=224,
            batch_size=32,
            optimizer="adamw",
            optimizer_settings={"weight_decay": 0.0005},
            checkpoint_interval_steps=20,
        ),
        "recommended": False,
    },
    "classification-mobilenet-v3-large": {
        "label": "MobileNetV3 Large Classifier",
        "description": "Balanced mobile classification preset with stronger capacity.",
        "task_type": TaskType.CLASSIFICATION,
        "family": "mobilenet",
        "version": "v3",
        "size": "large",
        "display_name_suffix": "Classifier",
        "default_training_profile": "quality-guarded",
        "defaults": TrainingDefaults(
            epochs=40,
            learning_rate=0.0008,
            image_size=224,
            batch_size=24,
            optimizer="adamw",
            optimizer_settings={"weight_decay": 0.0005},
            checkpoint_interval_steps=25,
        ),
        "recommended": False,
    },
    "classification-efficientnet-b0": {
        "label": "EfficientNet-B0 Classifier",
        "description": "Efficient classification preset for higher quality without a large model.",
        "task_type": TaskType.CLASSIFICATION,
        "family": "efficientnet",
        "version": "v1",
        "size": "b0",
        "display_name_suffix": "Classifier",
        "default_training_profile": "quality-guarded",
        "defaults": TrainingDefaults(
            epochs=45,
            learning_rate=0.0005,
            image_size=224,
            batch_size=24,
            optimizer="adamw",
            optimizer_settings={"weight_decay": 0.0005},
            checkpoint_interval_steps=25,
        ),
        "recommended": False,
    },
    "detection-yolov8-n": {
        "label": "YOLOv8 Nano Detector",
        "description": "Balanced detection preset for the simplest deployable happy path.",
        "task_type": TaskType.DETECTION,
        "family": "yolo",
        "version": "v8",
        "size": "n",
        "display_name_suffix": "Detector",
        "default_training_profile": "safe-default",
        "defaults": TrainingDefaults(
            epochs=40,
            learning_rate=0.01,
            image_size=640,
            batch_size=16,
            optimizer="sgd",
            optimizer_settings={"momentum": 0.937, "weight_decay": 0.0005},
            checkpoint_interval_steps=25,
        ),
        "recommended": True,
    },
    "detection-yolov8-s": {
        "label": "YOLOv8 Small Detector",
        "description": "Quality-focused detection preset with stronger default image size.",
        "task_type": TaskType.DETECTION,
        "family": "yolo",
        "version": "v8",
        "size": "s",
        "display_name_suffix": "Detector",
        "default_training_profile": "quality-guarded",
        "defaults": TrainingDefaults(
            epochs=55,
            learning_rate=0.01,
            image_size=768,
            batch_size=12,
            optimizer="sgd",
            optimizer_settings={"momentum": 0.937, "weight_decay": 0.0005},
            checkpoint_interval_steps=30,
        ),
        "recommended": False,
    },
    "segmentation-deeplab-v3-resnet50": {
        "label": "DeepLabV3 ResNet-50 Segmenter",
        "description": (
            "Semantic segmentation preset for polygon masks with transfer-learning defaults."
        ),
        "task_type": TaskType.SEGMENTATION,
        "family": "deeplab",
        "version": "v3",
        "size": "resnet50",
        "display_name_suffix": "Segmenter",
        "default_training_profile": "safe-default",
        "defaults": TrainingDefaults(
            epochs=45,
            learning_rate=0.0005,
            image_size=512,
            batch_size=4,
            optimizer="adamw",
            optimizer_settings={"weight_decay": 0.0001},
            checkpoint_interval_steps=25,
        ),
        "recommended": True,
    },
    "segmentation-fcn-v1-resnet50": {
        "label": "FCN ResNet-50 Segmenter",
        "description": "Stable semantic segmentation preset with a compact decoder.",
        "task_type": TaskType.SEGMENTATION,
        "family": "fcn",
        "version": "v1",
        "size": "resnet50",
        "display_name_suffix": "Segmenter",
        "default_training_profile": "quality-guarded",
        "defaults": TrainingDefaults(
            epochs=45,
            learning_rate=0.0005,
            image_size=512,
            batch_size=4,
            optimizer="adamw",
            optimizer_settings={"weight_decay": 0.0001},
            checkpoint_interval_steps=30,
        ),
        "recommended": False,
    },
    "segmentation-lraspp-v3-large": {
        "label": "LRASPP MobileNetV3 Segmenter",
        "description": "Lightweight semantic segmentation preset for smaller deployment targets.",
        "task_type": TaskType.SEGMENTATION,
        "family": "lraspp",
        "version": "v3",
        "size": "large",
        "display_name_suffix": "Segmenter",
        "default_training_profile": "safe-default",
        "defaults": TrainingDefaults(
            epochs=40,
            learning_rate=0.0008,
            image_size=512,
            batch_size=6,
            optimizer="adamw",
            optimizer_settings={"weight_decay": 0.0001},
            checkpoint_interval_steps=25,
        ),
        "recommended": False,
    },
    "anomaly-patchcore-wide-resnet50-2": {
        "label": "PatchCore Anomaly Detector",
        "description": (
            "Image-level good/bad anomaly detection preset with PatchCore heatmap output."
        ),
        "task_type": TaskType.ANOMALY,
        "family": "patchcore",
        "version": "anomalib",
        "size": "wide_resnet50_2",
        "display_name_suffix": "Anomaly Detector",
        "default_training_profile": "safe-default",
        "defaults": TrainingDefaults(
            epochs=1,
            learning_rate=0.001,
            image_size=256,
            batch_size=8,
            optimizer="anomalib-default",
            optimizer_settings={
                "method": "patchcore",
                "backbone": "wide_resnet50_2",
                "threshold_calibration": "adaptive",
                "normal_label": "normal",
                "abnormal_label": "anomalous",
                "heatmap": True,
                "mask_support": False,
            },
            checkpoint_interval_steps=1,
        ),
        "recommended": True,
    },
}

DEFAULT_TEMPLATE_DEFINITIONS.update(_yolo_template_definitions())


def default_template_summary(template_key: str) -> ModelTemplateSummary:
    definition = DEFAULT_TEMPLATE_DEFINITIONS[template_key]
    task_type = definition["task_type"]
    supported = is_supported_template_definition(template_key)
    architecture = architecture_summary(
        task_type=task_type,
        family=str(definition["family"]),
        version=str(definition["version"]),
        size=str(definition["size"]),
    )
    return ModelTemplateSummary(
        template_key=template_key,
        label=str(definition["label"]),
        description=str(definition["description"]),
        task_type=task_type,
        architecture=architecture,
        display_name_suffix=str(definition["display_name_suffix"]),
        default_training_profile=str(definition["default_training_profile"]),
        defaults=ModelTemplateDefaults(
            training=definition["defaults"],
            promotion=default_promotion_policy(task_type),
        ),
        default_promotion_policy=default_promotion_policy(task_type),
        template_version=1,
        recommended=bool(definition.get("recommended", False)) and supported,
        active=supported,
    )


def recipe_for_architecture(
    *,
    task_type: TaskType,
    family: str,
    version: str,
) -> tuple[str, str]:
    if task_type == TaskType.CLASSIFICATION:
        if (family.lower(), version.lower()) in {
            ("resnet", "v1"),
            ("mobilenet", "v3"),
            ("efficientnet", "v1"),
        }:
            return "baseline_cls_torchvision", "1.2.0"
        if family.lower() == "yolo" and version.lower() in YOLO_CLASSIFICATION_VERSIONS:
            return "baseline_cls_yolo", "1.2.0"
        raise ValueError(
            "Unsupported classification architecture family/version combination: "
            f"{family} {version}."
        )
    if task_type == TaskType.DETECTION:
        if family.lower() == "yolo" and version.lower() == "v8":
            return "baseline_det_mmyolo", "1.2.0"
        if family.lower() == "yolo" and version.lower() in YOLO_ULTRALYTICS_VERSIONS:
            return "baseline_det_yolo", "1.2.0"
        raise ValueError(
            f"Unsupported detection architecture family/version combination: {family} {version}."
        )
    if task_type == TaskType.ORIENTED_DETECTION:
        if family.lower() == "yolo" and version.lower() in YOLO_ORIENTED_DETECTION_VERSIONS:
            return "baseline_obb_yolo", "1.0.0"
        raise ValueError(
            f"Unsupported oriented detection architecture family/version combination: {family} {version}."
        )
    if task_type == TaskType.SEGMENTATION:
        if (family.lower(), version.lower()) in {
            ("deeplab", "v3"),
            ("fcn", "v1"),
            ("lraspp", "v3"),
        }:
            return "baseline_seg_torchvision", "1.2.0"
        if family.lower() == "yolo" and version.lower() in YOLO_SEGMENTATION_VERSIONS:
            return "baseline_seg_yolo", "1.2.0"
        raise ValueError(
            f"Unsupported segmentation architecture family/version combination: {family} {version}."
        )
    if task_type == TaskType.ANOMALY:
        if (family.lower(), version.lower()) == ("patchcore", "anomalib"):
            return "baseline_anomaly", "1.0.0"
        raise ValueError(
            f"Unsupported anomaly architecture family/version combination: {family} {version}."
        )
    if task_type == TaskType.TEMPORAL_SEQUENCE:
        if (family.lower(), version.lower()) == ("tcn", "v1"):
            return "baseline_temporal_tcn", "1.0.0"
        if (family.lower(), version.lower()) == ("gru", "v1"):
            return "baseline_temporal_gru", "1.0.0"
        raise ValueError(
            "Unsupported temporal-sequence architecture family/version combination: "
            f"{family} {version}."
        )
    raise ValueError(f"Unsupported task type {task_type.value}.")
