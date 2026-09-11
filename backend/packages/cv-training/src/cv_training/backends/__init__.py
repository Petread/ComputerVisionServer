from cv_training.backends.anomalib_anomaly import (
    ANOMALIB_PATCHCORE_SUPPORTED_ARCHITECTURES,
    AnomalibPatchCoreTrainer,
)
from cv_training.backends.openmmlab_detection import OpenMMLabDetectionTrainer
from cv_training.backends.simulation import SimulationTrainer
from cv_training.backends.temporal_sequence import (
    TEMPORAL_SEQUENCE_SUPPORTED_ARCHITECTURES,
    TEMPORAL_TRAINER_BACKEND,
    TemporalSequenceTrainer,
)
from cv_training.backends.torchvision_trainers import (
    CLASSIFICATION_SUPPORTED_ARCHITECTURES,
    SEGMENTATION_SUPPORTED_ARCHITECTURES,
    TorchvisionClassificationTrainer,
    TorchvisionSegmentationTrainer,
)
from cv_training.backends.ultralytics_yolo import (
    ULTRALYTICS_YOLO_SIZES,
    ULTRALYTICS_YOLO_VERSIONS,
    UltralyticsYoloTrainer,
)

__all__ = [
    "CLASSIFICATION_SUPPORTED_ARCHITECTURES",
    "ANOMALIB_PATCHCORE_SUPPORTED_ARCHITECTURES",
    "AnomalibPatchCoreTrainer",
    "OpenMMLabDetectionTrainer",
    "SEGMENTATION_SUPPORTED_ARCHITECTURES",
    "SimulationTrainer",
    "TEMPORAL_SEQUENCE_SUPPORTED_ARCHITECTURES",
    "TEMPORAL_TRAINER_BACKEND",
    "TemporalSequenceTrainer",
    "TorchvisionClassificationTrainer",
    "TorchvisionSegmentationTrainer",
    "ULTRALYTICS_YOLO_SIZES",
    "ULTRALYTICS_YOLO_VERSIONS",
    "UltralyticsYoloTrainer",
]
