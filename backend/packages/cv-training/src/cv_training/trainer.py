from __future__ import annotations

from cv_db.model_catalog import trainable_model_capability
from cv_status import TaskType

from cv_training.backends import (
    ULTRALYTICS_YOLO_SIZES,
    ULTRALYTICS_YOLO_VERSIONS,
    AnomalibPatchCoreTrainer,
    OpenMMLabDetectionTrainer,
    SimulationTrainer,
    TemporalSequenceTrainer,
    TorchvisionClassificationTrainer,
    TorchvisionSegmentationTrainer,
    UltralyticsYoloTrainer,
)
from cv_training.trainer_types import (
    BaseTrainer,
    ResolvedTrainerArchitecture,
    TrainerArtifactValidation,
    TrainerCheckpointArtifact,
    TrainerConfig,
    TrainerControl,
    TrainerDatasetArtifact,
    TrainerExportArtifact,
    TrainerMode,
    TrainerProgress,
    TrainerResult,
    TrainerResumeState,
)

YOLO_DETECTION_SIZES = {"n", "s", "m", "l", "x"}


def resolve_trainer_mode(raw_mode: str | None) -> TrainerMode:
    if str(raw_mode or "real").lower() == "simulation":
        return "simulation"
    return "real"


def resolve_trainer_architecture(
    *,
    task_type: TaskType,
    family: str,
    version: str,
    size: str,
    display_name: str | None = None,
) -> ResolvedTrainerArchitecture:
    normalized_family = family.lower()
    normalized_version = version.lower()
    normalized_size = size.lower()
    capability = trainable_model_capability(
        task_type=task_type,
        family=normalized_family,
        version=normalized_version,
        size=normalized_size,
    )
    supported = (
        capability is not None
        and capability.supported
        and capability.export_support
        and (
            capability.status == "supported"
            or (
                task_type == TaskType.TEMPORAL_SEQUENCE
                and capability.status == "experimental"
            )
        )
    )
    support_reason = None
    if not supported:
        if task_type == TaskType.DETECTION:
            support_reason = (
                "Real detection training supports YOLOv8 through MMYOLO and YOLOv11/YOLOv26 "
                "through Ultralytics, in sizes n/s/m/l/x."
            )
        elif task_type == TaskType.ORIENTED_DETECTION:
            support_reason = (
                "Oriented detection real training supports YOLOv26 OBB models "
                "through Ultralytics, in sizes n/s/m/l/x."
            )
        elif task_type == TaskType.CLASSIFICATION:
            support_reason = (
                "Classification real training supports ResNet-18/50, MobileNetV3 small/large, "
                "EfficientNet-B0 torchvision architectures, plus YOLOv11/YOLOv26 classifiers "
                "in sizes n/s/m/l/x through Ultralytics."
            )
        elif task_type == TaskType.SEGMENTATION:
            support_reason = (
                "Segmentation real training supports DeepLabV3-ResNet50, FCN-ResNet50, "
                "LRASPP-MobileNetV3-Large torchvision architectures, plus YOLOv8/YOLOv11/"
                "YOLOv26 segmenters in sizes n/s/m/l/x through Ultralytics."
            )
        elif task_type == TaskType.ANOMALY:
            support_reason = (
                "Anomaly real training currently supports PatchCore through Anomalib with "
                "image-level good/bad datasets, ONNX export, deployment packaging, and "
                "runtime heatmap inference."
            )
        elif task_type == TaskType.TEMPORAL_SEQUENCE:
            support_reason = (
                "Temporal-sequence real training supports the general small causal TCN and "
                "GRU architectures. Artifacts remain experimental/shadow-only until the "
                "separate runtime inference contract is approved."
            )
        else:
            support_reason = f"No real trainer backend is available for {task_type.value}."
    label = display_name or f"{family.upper()}{version.upper()} {size.upper()}"
    return ResolvedTrainerArchitecture(
        task_type=task_type,
        family=normalized_family,
        version=normalized_version,
        size=normalized_size,
        display_name=label,
        backend_model_key=(
            capability.concrete_architecture
            if capability is not None
            else f"{normalized_family}:{normalized_version}:{normalized_size}"
        ),
        supported=supported,
        support_reason=support_reason,
    )


def get_default_trainer(
    task_type: TaskType,
    *,
    trainer_mode: TrainerMode = "real",
    architecture: ResolvedTrainerArchitecture | None = None,
) -> BaseTrainer:
    if trainer_mode == "simulation":
        return SimulationTrainer()
    resolved_architecture = architecture or _default_architecture_for_task(task_type)
    if not resolved_architecture.supported:
        raise ValueError(
            resolved_architecture.support_reason
            or f"No real trainer backend is available for {task_type.value}."
        )
    if _is_ultralytics_yolo_architecture(resolved_architecture):
        return UltralyticsYoloTrainer()
    if task_type == TaskType.ANOMALY:
        return AnomalibPatchCoreTrainer()
    if task_type == TaskType.TEMPORAL_SEQUENCE:
        return TemporalSequenceTrainer()
    if task_type == TaskType.DETECTION:
        return OpenMMLabDetectionTrainer()
    if task_type == TaskType.CLASSIFICATION:
        return TorchvisionClassificationTrainer()
    if task_type == TaskType.SEGMENTATION:
        return TorchvisionSegmentationTrainer()
    raise ValueError(f"No real trainer backend is available for {task_type.value}.")


def _default_architecture_for_task(task_type: TaskType) -> ResolvedTrainerArchitecture:
    if task_type == TaskType.CLASSIFICATION:
        return resolve_trainer_architecture(
            task_type=task_type,
            family="resnet",
            version="v1",
            size="18",
        )
    if task_type == TaskType.DETECTION:
        return resolve_trainer_architecture(
            task_type=task_type,
            family="yolo",
            version="v8",
            size="n",
        )
    if task_type == TaskType.ORIENTED_DETECTION:
        return resolve_trainer_architecture(
            task_type=task_type,
            family="yolo",
            version="v26",
            size="n",
        )
    if task_type == TaskType.SEGMENTATION:
        return resolve_trainer_architecture(
            task_type=task_type,
            family="deeplab",
            version="v3",
            size="resnet50",
        )
    if task_type == TaskType.ANOMALY:
        return resolve_trainer_architecture(
            task_type=task_type,
            family="patchcore",
            version="anomalib",
            size="wide_resnet50_2",
        )
    if task_type == TaskType.TEMPORAL_SEQUENCE:
        return resolve_trainer_architecture(
            task_type=task_type,
            family="tcn",
            version="v1",
            size="small",
        )
    return resolve_trainer_architecture(
        task_type=task_type,
        family="",
        version="",
        size="",
    )


def _is_ultralytics_yolo_architecture(architecture: ResolvedTrainerArchitecture) -> bool:
    return (
        architecture.family.lower() == "yolo"
        and not (
            architecture.task_type == TaskType.DETECTION and architecture.version.lower() == "v8"
        )
        and architecture.version.lower() in {"v8", *ULTRALYTICS_YOLO_VERSIONS}
        and architecture.size.lower() in ULTRALYTICS_YOLO_SIZES
    )


__all__ = [
    "BaseTrainer",
    "ResolvedTrainerArchitecture",
    "TrainerArtifactValidation",
    "TrainerCheckpointArtifact",
    "TrainerConfig",
    "TrainerControl",
    "TrainerDatasetArtifact",
    "TrainerExportArtifact",
    "TrainerMode",
    "TrainerProgress",
    "TrainerResult",
    "TrainerResumeState",
    "get_default_trainer",
    "resolve_trainer_architecture",
    "resolve_trainer_mode",
]
