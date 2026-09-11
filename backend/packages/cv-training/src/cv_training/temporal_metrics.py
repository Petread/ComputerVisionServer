from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

from cv_training.temporal_data import wrap_to_half_turn


def temporal_angle_error_deg(predicted: float, expected: float) -> float:
    return abs(wrap_to_half_turn(float(predicted) - float(expected)))


def evaluate_temporal_predictions(
    records: Sequence[Mapping[str, Any]],
    *,
    direction_deadband_deg: float,
    confidence_thresholds: Sequence[float] = (0.0, 0.25, 0.5, 0.75, 0.9),
) -> dict[str, Any]:
    materialized = [dict(record) for record in records]
    report = {
        "sample_count": len(materialized),
        "all_valid": _angle_metrics(
            materialized,
            direction_deadband_deg=direction_deadband_deg,
            confidence_thresholds=confidence_thresholds,
        ),
        "gold": _angle_metrics(
            [record for record in materialized if record.get("label_quality_tier") == "gold"],
            direction_deadband_deg=direction_deadband_deg,
            confidence_thresholds=confidence_thresholds,
        ),
        "silver": _angle_metrics(
            [record for record in materialized if record.get("label_quality_tier") == "silver"],
            direction_deadband_deg=direction_deadband_deg,
            confidence_thresholds=confidence_thresholds,
        ),
        "bow_relevance": _binary_metrics(materialized, "bow_relevance"),
        "target_observed": _binary_metrics(materialized, "target_observed"),
        "final_orientation_quality": _quality_metrics(materialized),
        "slices": _slice_metrics(
            materialized,
            direction_deadband_deg=direction_deadband_deg,
            confidence_thresholds=confidence_thresholds,
        ),
    }
    return report


def flatten_temporal_metrics(report: Mapping[str, Any]) -> dict[str, float]:
    flattened: dict[str, float] = {}
    for namespace in ("all_valid", "gold", "silver"):
        values = report.get(namespace)
        if not isinstance(values, Mapping):
            continue
        for name in (
            "target_count",
            "prediction_count",
            "prediction_coverage",
            "count",
            "mean_absolute_error_deg",
            "median_absolute_error_deg",
            "p90_absolute_error_deg",
            "p95_absolute_error_deg",
            "within_5_deg",
            "within_10_deg",
            "within_15_deg",
            "within_20_deg",
            "direction_accuracy",
            "dangerous_opposite_direction_rate",
            "severe_error_rate_gt_30_deg",
            "no_rotation_false_positive_rate",
        ):
            value = values.get(name)
            if isinstance(value, int | float) and math.isfinite(float(value)):
                flattened[f"{namespace}_{name}"] = float(value)
    for namespace in ("bow_relevance", "target_observed"):
        values = report.get(namespace)
        if not isinstance(values, Mapping):
            continue
        for name in ("count", "accuracy", "precision", "recall", "f1"):
            value = values.get(name)
            if isinstance(value, int | float) and math.isfinite(float(value)):
                flattened[f"{namespace}_{name}"] = float(value)
    quality = report.get("final_orientation_quality")
    if isinstance(quality, Mapping):
        for name in ("count", "accuracy", "ordinal_mae"):
            value = quality.get(name)
            if isinstance(value, int | float) and math.isfinite(float(value)):
                flattened[f"final_quality_{name}"] = float(value)
    return flattened


def fit_confidence_calibration(
    records: Sequence[Mapping[str, Any]],
    *,
    tolerance_deg: float,
    bin_count: int = 10,
) -> dict[str, Any]:
    valid = [
        record
        for record in records
        if bool(record.get("angle_target_valid"))
        and _finite(record.get("predicted_angle_deg")) is not None
        and _finite(record.get("expected_angle_deg")) is not None
        and _bounded(record.get("raw_angle_confidence")) is not None
    ]
    if not valid:
        return {
            "schema_version": "temporal-confidence-calibration.v1",
            "method": "validation_equal_width_isotonic",
            "valid": False,
            "fit_split": "val",
            "tolerance_deg": float(tolerance_deg),
            "sample_count": 0,
            "bins": [],
            "reason": "no_valid_validation_angle_targets",
        }
    bin_count = max(2, min(int(bin_count), 100))
    buckets: list[list[float]] = [[] for _ in range(bin_count)]
    for record in valid:
        confidence = float(record["raw_angle_confidence"])
        predicted = float(record["predicted_angle_deg"])
        expected = float(record["expected_angle_deg"])
        index = min(int(confidence * bin_count), bin_count - 1)
        buckets[index].append(
            1.0 if temporal_angle_error_deg(predicted, expected) <= tolerance_deg else 0.0
        )
    blocks: list[dict[str, Any]] = []
    for index, values in enumerate(buckets):
        if not values:
            continue
        blocks.append(
            {
                "start": index,
                "end": index,
                "count": len(values),
                "successes": sum(values),
            }
        )
        while len(blocks) >= 2:
            previous = blocks[-2]
            current = blocks[-1]
            previous_rate = previous["successes"] / previous["count"]
            current_rate = current["successes"] / current["count"]
            if previous_rate <= current_rate:
                break
            blocks[-2:] = [
                {
                    "start": previous["start"],
                    "end": current["end"],
                    "count": previous["count"] + current["count"],
                    "successes": previous["successes"] + current["successes"],
                }
            ]
    bins = [
        {
            "raw_lower": block["start"] / bin_count,
            "raw_upper": (block["end"] + 1) / bin_count,
            "sample_count": block["count"],
            "calibrated_confidence": block["successes"] / block["count"],
        }
        for block in blocks
    ]
    return {
        "schema_version": "temporal-confidence-calibration.v1",
        "method": "validation_equal_width_isotonic",
        "valid": True,
        "fit_split": "val",
        "tolerance_deg": float(tolerance_deg),
        "sample_count": len(valid),
        "bins": bins,
    }


def calibrated_confidence(raw_confidence: float, calibration: Mapping[str, Any]) -> float:
    raw = min(max(float(raw_confidence), 0.0), 1.0)
    if not bool(calibration.get("valid")):
        return raw
    bins = calibration.get("bins")
    if not isinstance(bins, list) or not bins:
        return raw
    for entry in bins:
        if not isinstance(entry, Mapping):
            continue
        lower = _finite(entry.get("raw_lower"))
        upper = _finite(entry.get("raw_upper"))
        calibrated = _bounded(entry.get("calibrated_confidence"))
        if lower is None or upper is None or calibrated is None:
            continue
        if lower <= raw < upper or (raw == 1.0 and upper >= 1.0):
            return calibrated
    nearest = min(
        (
            entry
            for entry in bins
            if isinstance(entry, Mapping)
            and _bounded(entry.get("calibrated_confidence")) is not None
        ),
        key=lambda entry: abs(
            raw
            - (
                float(entry.get("raw_lower") or 0.0)
                + float(entry.get("raw_upper") or 1.0)
            )
            / 2.0
        ),
        default=None,
    )
    return (
        float(nearest["calibrated_confidence"])
        if nearest is not None
        else raw
    )


def apply_confidence_calibration(
    records: Sequence[Mapping[str, Any]], calibration: Mapping[str, Any]
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        raw = _bounded(item.get("raw_angle_confidence"))
        item["angle_confidence"] = (
            calibrated_confidence(raw, calibration) if raw is not None else 0.0
        )
        output.append(item)
    return output


def _angle_metrics(
    records: Sequence[Mapping[str, Any]],
    *,
    direction_deadband_deg: float,
    confidence_thresholds: Sequence[float],
) -> dict[str, Any]:
    targets = [
        record
        for record in records
        if bool(record.get("angle_target_valid"))
        and _finite(record.get("expected_angle_deg")) is not None
    ]
    valid = [
        record
        for record in targets
        if _finite(record.get("predicted_angle_deg")) is not None
    ]
    errors = [
        temporal_angle_error_deg(
            float(record["predicted_angle_deg"]),
            float(record["expected_angle_deg"]),
        )
        for record in valid
    ]
    direction_records = [
        record
        for record in valid
        if abs(float(record["expected_angle_deg"])) > direction_deadband_deg
    ]
    direction_correct = sum(
        _direction(float(record["predicted_angle_deg"]), direction_deadband_deg)
        == _direction(float(record["expected_angle_deg"]), direction_deadband_deg)
        or temporal_angle_error_deg(
            float(record["predicted_angle_deg"]),
            float(record["expected_angle_deg"]),
        )
        <= direction_deadband_deg
        for record in direction_records
    )
    opposite = sum(
        _direction(float(record["predicted_angle_deg"]), direction_deadband_deg)
        == -_direction(float(record["expected_angle_deg"]), direction_deadband_deg)
        and temporal_angle_error_deg(
            float(record["predicted_angle_deg"]),
            float(record["expected_angle_deg"]),
        )
        > 30.0
        for record in direction_records
    )
    near_straight = [record for record in records if bool(record.get("nearly_straight"))]
    no_rotation_false = sum(
        abs(float(record.get("predicted_angle_deg") or 0.0)) > direction_deadband_deg
        for record in near_straight
    )
    return {
        "target_count": len(targets),
        "prediction_count": len(valid),
        "prediction_coverage": len(valid) / len(targets) if targets else None,
        "count": len(valid),
        "mean_absolute_error_deg": statistics.fmean(errors) if errors else None,
        "median_absolute_error_deg": statistics.median(errors) if errors else None,
        "p90_absolute_error_deg": _percentile(errors, 0.90),
        "p95_absolute_error_deg": _percentile(errors, 0.95),
        "within_5_deg": _fraction(errors, lambda value: value <= 5.0),
        "within_10_deg": _fraction(errors, lambda value: value <= 10.0),
        "within_15_deg": _fraction(errors, lambda value: value <= 15.0),
        "within_20_deg": _fraction(errors, lambda value: value <= 20.0),
        "direction_count": len(direction_records),
        "direction_accuracy": (
            direction_correct / len(direction_records) if direction_records else None
        ),
        "dangerous_opposite_direction_rate": (
            opposite / len(direction_records) if direction_records else None
        ),
        "severe_error_rate_gt_30_deg": _fraction(errors, lambda value: value > 30.0),
        "nearly_straight_count": len(near_straight),
        "no_rotation_false_positive_rate": (
            no_rotation_false / len(near_straight) if near_straight else None
        ),
        "confidence_coverage": _confidence_coverage(
            valid,
            thresholds=confidence_thresholds,
        ),
    }


def _confidence_coverage(
    records: Sequence[Mapping[str, Any]], *, thresholds: Sequence[float]
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for threshold in thresholds:
        accepted = [
            record
            for record in records
            if float(record.get("angle_confidence", record.get("raw_angle_confidence", 0.0)))
            >= float(threshold)
        ]
        errors = [
            temporal_angle_error_deg(
                float(record["predicted_angle_deg"]),
                float(record["expected_angle_deg"]),
            )
            for record in accepted
        ]
        output.append(
            {
                "threshold": float(threshold),
                "accepted_count": len(accepted),
                "coverage": len(accepted) / len(records) if records else 0.0,
                "mean_absolute_error_deg": statistics.fmean(errors) if errors else None,
                "p95_absolute_error_deg": _percentile(errors, 0.95),
            }
        )
    return output


def _binary_metrics(records: Sequence[Mapping[str, Any]], name: str) -> dict[str, Any]:
    true_key = f"expected_{name}"
    probability_key = f"predicted_{name}_probability"
    valid = [
        record
        for record in records
        if _finite(record.get(true_key)) is not None
        and _bounded(record.get(probability_key)) is not None
    ]
    pairs = [
        (int(float(record[true_key]) >= 0.5), int(float(record[probability_key]) >= 0.5))
        for record in valid
    ]
    true_positive = sum(expected == 1 and predicted == 1 for expected, predicted in pairs)
    false_positive = sum(expected == 0 and predicted == 1 for expected, predicted in pairs)
    false_negative = sum(expected == 1 and predicted == 0 for expected, predicted in pairs)
    correct = sum(expected == predicted for expected, predicted in pairs)
    precision = (
        true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else 0.0
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if true_positive + false_negative
        else 0.0
    )
    f1 = None
    if pairs:
        f1 = (
            2.0 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
    return {
        "count": len(pairs),
        "accuracy": correct / len(pairs) if pairs else None,
        "precision": precision if pairs else None,
        "recall": recall if pairs else None,
        "f1": f1,
    }


def _quality_metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    valid = [
        record
        for record in records
        if bool(record.get("final_quality_valid"))
        and _finite(record.get("expected_final_quality_ordinal")) is not None
        and _finite(record.get("predicted_final_quality_ordinal")) is not None
    ]
    errors = [
        abs(
            int(record["predicted_final_quality_ordinal"])
            - int(record["expected_final_quality_ordinal"])
        )
        for record in valid
    ]
    return {
        "count": len(valid),
        "accuracy": _fraction(errors, lambda value: value == 0),
        "ordinal_mae": statistics.fmean(errors) if errors else None,
    }


def _slice_metrics(
    records: Sequence[Mapping[str, Any]],
    *,
    direction_deadband_deg: float,
    confidence_thresholds: Sequence[float],
) -> dict[str, Any]:
    definitions = {
        "visibility": ((0.0, 0.5), (0.5, 0.8), (0.8, math.inf)),
        "curvature_strength": ((0.0, 0.05), (0.05, 0.15), (0.15, math.inf)),
        "rotation_trace_quality": ((0.0, 0.6), (0.6, 0.8), (0.8, math.inf)),
        "stem_size": ((0.0, 0.1), (0.1, 0.3), (0.3, math.inf)),
        "sequence_length": ((0.0, 32.0), (32.0, 128.0), (128.0, math.inf)),
    }
    output: dict[str, Any] = {}
    for name, bounds in definitions.items():
        buckets: dict[str, Any] = {}
        for lower, upper in bounds:
            key = f"[{lower:g},{'inf' if math.isinf(upper) else f'{upper:g}'})"
            selected = [
                record
                for record in records
                if (value := _finite(_metadata(record).get(name))) is not None
                and lower <= value < upper
            ]
            buckets[key] = _angle_metrics(
                selected,
                direction_deadband_deg=direction_deadband_deg,
                confidence_thresholds=confidence_thresholds,
            )
        output[name] = buckets
    return output


def _metadata(record: Mapping[str, Any]) -> Mapping[str, Any]:
    value = record.get("metadata")
    return value if isinstance(value, Mapping) else {}


def _direction(value: float, deadband: float) -> int:
    if value > deadband:
        return 1
    if value < -deadband:
        return -1
    return 0


def _fraction(values: Sequence[float | int], predicate: Any) -> float | None:
    return sum(bool(predicate(value)) for value in values) / len(values) if values else None


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _bounded(value: Any) -> float | None:
    number = _finite(value)
    return number if number is not None and 0.0 <= number <= 1.0 else None


__all__ = [
    "apply_confidence_calibration",
    "calibrated_confidence",
    "evaluate_temporal_predictions",
    "fit_confidence_calibration",
    "flatten_temporal_metrics",
    "temporal_angle_error_deg",
]
