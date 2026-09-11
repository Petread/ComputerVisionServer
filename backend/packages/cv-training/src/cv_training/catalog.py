from __future__ import annotations

from cv_status import TaskType


def metric_definitions_for_task(task_type: TaskType) -> dict[str, list[str]]:
    if task_type == TaskType.TEMPORAL_SEQUENCE:
        angle_metrics = [
            "all_valid_mean_absolute_error_deg",
            "all_valid_median_absolute_error_deg",
            "all_valid_p90_absolute_error_deg",
            "all_valid_p95_absolute_error_deg",
            "all_valid_prediction_coverage",
            "all_valid_within_5_deg",
            "all_valid_within_10_deg",
            "all_valid_within_15_deg",
            "all_valid_within_20_deg",
            "all_valid_direction_accuracy",
            "all_valid_dangerous_opposite_direction_rate",
            "all_valid_severe_error_rate_gt_30_deg",
            "gold_mean_absolute_error_deg",
            "silver_mean_absolute_error_deg",
        ]
        return {
            "training": [
                "loss",
                "loss_bow_relevance",
                "loss_target_observed",
                "loss_angle_circular",
                "loss_angle_confidence",
                "loss_final_quality_ordinal",
            ],
            "validation": angle_metrics
            + ["bow_relevance_f1", "target_observed_f1"],
            "evaluation": angle_metrics
            + ["bow_relevance_f1", "target_observed_f1"],
        }
    if task_type == TaskType.ANOMALY:
        return {
            "training": ["memory_bank_size"],
            "validation": ["image_auroc", "image_f1", "precision", "recall"],
            "evaluation": ["image_auroc", "image_f1", "precision", "recall", "threshold"],
        }
    if task_type in {TaskType.DETECTION, TaskType.ORIENTED_DETECTION}:
        return {
            "training": ["loss_cls", "loss_bbox"],
            "validation": ["map_50", "map_50_95", "recall_at_100"],
            "evaluation": ["map_50", "map_50_95", "recall_at_100"],
        }
    if task_type == TaskType.SEGMENTATION:
        return {
            "training": ["loss_cls", "loss_mask"],
            "validation": ["mask_map_50", "mask_map_50_95", "recall_at_100"],
            "evaluation": ["mask_map_50", "mask_map_50_95", "recall_at_100"],
        }
    return {
        "training": ["loss"],
        "validation": ["accuracy_top1", "precision_macro", "recall_macro", "f1_macro"],
        "evaluation": ["accuracy_top1", "precision_macro", "recall_macro", "f1_macro"],
    }


def default_best_checkpoint_rule(task_type: TaskType) -> dict[str, str | int]:
    if task_type == TaskType.TEMPORAL_SEQUENCE:
        return {
            "metric_name": "all_valid_mean_absolute_error_deg",
            "mode": "min",
            "phase": "validation",
            "minimum_epoch": 1,
        }
    if task_type == TaskType.ANOMALY:
        return {
            "metric_name": "image_auroc",
            "mode": "max",
            "phase": "validation",
            "minimum_epoch": 1,
        }
    if task_type in {TaskType.DETECTION, TaskType.ORIENTED_DETECTION}:
        return {
            "metric_name": "map_50_95",
            "mode": "max",
            "phase": "validation",
            "minimum_epoch": 1,
        }
    if task_type == TaskType.SEGMENTATION:
        return {
            "metric_name": "mask_map_50_95",
            "mode": "max",
            "phase": "validation",
            "minimum_epoch": 1,
        }
    return {
        "metric_name": "accuracy_top1",
        "mode": "max",
        "phase": "validation",
        "minimum_epoch": 1,
    }


def default_promotion_criteria(task_type: TaskType) -> list[dict[str, str | float]]:
    if task_type == TaskType.TEMPORAL_SEQUENCE:
        # Gold coverage and promotion thresholds are deliberately not guessed. Temporal
        # jobs remain shadow-only even when callers provide evaluation criteria.
        return []
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


def trainer_framework_for_task(task_type: TaskType) -> str:
    if task_type == TaskType.TEMPORAL_SEQUENCE:
        return "pytorch"
    if task_type == TaskType.ANOMALY:
        return "anomalib"
    if task_type == TaskType.DETECTION:
        return "openmmlab-mmyolo"
    if task_type == TaskType.ORIENTED_DETECTION:
        return "ultralytics"
    if task_type in {TaskType.CLASSIFICATION, TaskType.SEGMENTATION}:
        return "torchvision"
    return "unknown"
