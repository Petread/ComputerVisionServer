"""Shared causal temporal feature preparation and calibrated model output decoding."""

# ruff: noqa: ANN401 - Versioned temporal feature rows contain heterogeneous values.
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv_contracts import RealtimeTemporalSequenceRequest
from cv_status import TaskType
from cv_utils import PlatformError


@dataclass(frozen=True)
class TemporalModelContext:
    manifest: dict[str, Any]


@dataclass(frozen=True)
class TemporalOutput:
    values: list[float]


def _prepare_temporal_model_tensor(
    *,
    route: TemporalModelContext,
    payload: RealtimeTemporalSequenceRequest,
) -> tuple[Any, dict[str, dict[str, Any]]]:
    try:
        import numpy as np
    except ImportError as exc:
        raise PlatformError(
            code="dependency_missing",
            message="NumPy is required for temporal feature preprocessing.",
            status_code=503,
        ) from exc
    contracts = {
        name: _load_temporal_contract_artifact(route, name)
        for name in (
            "temporal_contract",
            "scaler",
            "angle_contract",
            "head_class_order",
            "confidence_calibration",
        )
    }
    contract = contracts["temporal_contract"]
    scaler = contracts["scaler"]
    angle = contracts["angle_contract"]
    head_order = contracts["head_class_order"]
    if not bool((route.manifest.get("temporal") or {}).get("runtime_contract_implemented")):
        raise PlatformError(
            code="temporal_runtime_contract_unavailable",
            message=(
                "The selected model artifact is not approved for the temporal runtime contract."
            ),
            status_code=409,
        )
    invariants = {
        "task_type": TaskType.TEMPORAL_SEQUENCE.value,
        "causal": True,
        "post_decision_frames_allowed": False,
        "input_end": "decision_frame_or_earlier",
    }
    for key, expected in invariants.items():
        if contract.get(key) != expected:
            raise PlatformError(
                code="temporal_contract_incompatible",
                message="The deployed temporal contract violates a causal runtime invariant.",
                detail=key,
                status_code=409,
            )
    compatibility = {
        "feature_set_id": (
            payload.feature_set_id,
            contract.get("feature_set_id"),
        ),
        "rotation_viewpoint_convention": (
            payload.rotation_viewpoint_convention,
            angle.get("rotation_viewpoint_convention"),
        ),
        "positive_rotation_direction": (
            payload.positive_rotation_direction,
            angle.get("positive_rotation_direction"),
        ),
    }
    if contract.get("rotation_viewpoint_convention") != angle.get(
        "rotation_viewpoint_convention"
    ) or contract.get("positive_rotation_direction") != angle.get("positive_rotation_direction"):
        raise PlatformError(
            code="temporal_direction_contract_incompatible",
            message="The packaged feature and angle direction conventions differ.",
            status_code=500,
        )
    for name, (actual, expected) in compatibility.items():
        if actual != expected:
            raise PlatformError(
                code="temporal_contract_incompatible",
                message="The live temporal sequence does not match the deployed model contract.",
                detail=f"{name}: expected={expected!r}, actual={actual!r}",
                status_code=409,
            )
    for name, actual, contract_key in (
        ("feature_schema_version", payload.feature_schema_version, "source_feature_schemas"),
        ("rotation_schema_version", payload.rotation_schema_version, "rotation_schemas"),
        ("calibration_schema_version", payload.calibration_schema_version, "calibration_schemas"),
    ):
        allowed = contract.get(contract_key)
        if not isinstance(allowed, list) or actual not in allowed:
            raise PlatformError(
                code="temporal_schema_incompatible",
                message="The live temporal schema is not present in the training contract.",
                detail=f"{name}: {actual!r}",
                status_code=409,
            )
    if not payload.calibration_valid:
        raise PlatformError(
            code="temporal_calibration_invalid",
            message="Temporal angle inference requires a valid rotation calibration.",
            status_code=409,
        )
    if not payload.trace_continuous:
        raise PlatformError(
            code="temporal_trace_discontinuous",
            message="Temporal angle inference requires a continuous rotation trace.",
            status_code=409,
        )
    minimum_steps = int(contract.get("minimum_window_steps") or 0)
    maximum_steps = int(contract.get("maximum_window_steps") or 0)
    if maximum_steps > 4096:
        raise PlatformError(
            code="temporal_contract_incompatible",
            message="The deployed temporal window exceeds the runtime contract limit.",
            detail=f"maximum={maximum_steps}, runtime_limit=4096",
            status_code=409,
        )
    if len(payload.frames) < minimum_steps:
        raise PlatformError(
            code="temporal_sequence_too_short",
            message="The causal sequence is shorter than the deployed model minimum.",
            detail=f"minimum={minimum_steps}, actual={len(payload.frames)}",
            status_code=409,
        )
    if maximum_steps < 1 or len(payload.frames) > maximum_steps:
        raise PlatformError(
            code="temporal_sequence_too_long",
            message=(
                "The causal sequence exceeds the deployed model window; truncation is forbidden."
            ),
            detail=f"maximum={maximum_steps}, actual={len(payload.frames)}",
            status_code=409,
        )
    feature_specs = contract.get("feature_specs")
    raw_feature_order = scaler.get("feature_order")
    means = scaler.get("mean")
    scales = scaler.get("scale")
    if not all(
        isinstance(value, list) for value in (feature_specs, raw_feature_order, means, scales)
    ):
        raise PlatformError(
            code="temporal_contract_invalid",
            message="The deployed temporal feature/scaler artifact is incomplete.",
            status_code=500,
        )
    expected_raw_count = int(contract.get("raw_feature_count") or 0)
    if not (
        len(raw_feature_order) == len(means) == len(scales) == expected_raw_count
        and str(scaler.get("schema_version")) == "temporal-scaler.v1"
    ):
        raise PlatformError(
            code="temporal_scaler_incompatible",
            message="The deployed scaler dimensions do not match the temporal contract.",
            status_code=500,
        )
    if any(_finite_float(value) is None for value in [*means, *scales]) or any(
        abs(float(value)) <= 1e-12 for value in scales
    ):
        raise PlatformError(
            code="temporal_scaler_incompatible",
            message="The packaged temporal scaler contains invalid means or scales.",
            status_code=500,
        )
    expanded_feature_order = [
        (
            str(spec.get("name") or "")
            if int(spec.get("length") or 1) == 1
            else f"{str(spec.get('name') or '')}[{index}]"
        )
        for spec in feature_specs
        if isinstance(spec, dict)
        for index in range(int(spec.get("length") or 1))
    ]
    model_feature_order = [
        *expanded_feature_order,
        *(f"{name}__valid" for name in expanded_feature_order),
        "__sequence_present",
    ]
    scaler_descriptor = (route.manifest.get("source_manifests") or {}).get("scaler") or {}
    contract_scaler_sha256 = str(contract.get("scaler_sha256") or "").removeprefix("sha256:")
    packaged_scaler_sha256 = str(scaler_descriptor.get("sha256") or "").removeprefix("sha256:")
    if (
        expanded_feature_order != [str(item) for item in raw_feature_order]
        or contract.get("feature_order") != model_feature_order
        or contract_scaler_sha256 != packaged_scaler_sha256
        or contract.get("padding") != {"direction": "left", "value": 0.0}
        or contract.get("sequence_presence_feature") != "__sequence_present"
    ):
        raise PlatformError(
            code="temporal_feature_contract_incompatible",
            message="The packaged feature order, scaler, or padding differs from training.",
            status_code=500,
        )
    if (
        angle.get("representation") != ["sin_2e", "cos_2e"]
        or float(angle.get("periodicity_deg") or 0.0) != 180.0
        or angle.get("wrap_interval_deg") != [-90.0, 90.0]
        or not bool(angle.get("wrap_upper_bound_exclusive"))
        or _finite_float(angle.get("deadband_deg")) is None
        or not 0.0 <= float(angle.get("deadband_deg")) < 90.0
    ):
        raise PlatformError(
            code="temporal_angle_contract_incompatible",
            message="The packaged temporal angle contract is not the trained modulo-180 contract.",
            status_code=500,
        )
    if (
        head_order.get("schema_version") != "temporal-head-order.v1"
        or head_order.get("bow_relevance") != ["nearly_straight", "relevant"]
        or head_order.get("target_observed") != ["no", "yes"]
        or head_order.get("final_orientation_quality") != ["bad", "acceptable", "good"]
        or head_order.get("angle_vector") != ["sin_2e", "cos_2e"]
    ):
        raise PlatformError(
            code="temporal_head_contract_incompatible",
            message="The packaged temporal head order differs from training.",
            status_code=500,
        )
    raw_rows: list[list[float]] = []
    valid_rows: list[list[bool]] = []
    denominator = max(len(payload.frames) - 1, 1)
    for position, frame in enumerate(payload.frames):
        root = {
            **dict(frame.values),
            "$elapsed_seconds": frame.timestamp_seconds - payload.frames[0].timestamp_seconds,
            "$normalized_sequence_progress": position / denominator,
        }
        values: list[float] = []
        valid: list[bool] = []
        for spec in feature_specs:
            if not isinstance(spec, dict):
                raise PlatformError(
                    code="temporal_contract_invalid",
                    message="The deployed temporal feature specification is invalid.",
                    status_code=500,
                )
            extracted, present = _extract_temporal_feature(root, spec)
            values.extend(extracted)
            valid.extend(present)
        if len(values) != expected_raw_count:
            raise PlatformError(
                code="temporal_feature_dimension_mismatch",
                message="The extracted live feature order differs from training.",
                status_code=409,
            )
        raw_rows.append(values)
        valid_rows.append(valid)
    normalized_rows = [
        [
            ((value - float(means[index])) / float(scales[index])) if valid[index] else 0.0
            for index, value in enumerate(row)
        ]
        + [1.0 if present else 0.0 for present in valid]
        + [1.0]
        for row, valid in zip(raw_rows, valid_rows, strict=True)
    ]
    model_feature_count = int(contract.get("model_feature_count") or 0)
    if any(len(row) != model_feature_count for row in normalized_rows):
        raise PlatformError(
            code="temporal_feature_dimension_mismatch",
            message="The scaled live feature tensor differs from the training contract.",
            status_code=409,
        )
    padded = [[0.0] * model_feature_count for _ in range(maximum_steps - len(normalized_rows))]
    padded.extend(normalized_rows)
    tensor = np.asarray([padded], dtype=np.float32)
    if not bool(np.isfinite(tensor).all()):
        raise PlatformError(
            code="temporal_feature_nonfinite",
            message="The temporal feature tensor contains a non-finite value.",
            status_code=422,
        )
    return tensor, contracts


def _load_temporal_contract_artifact(route: TemporalModelContext, name: str) -> dict[str, Any]:
    descriptor = (route.manifest.get("source_manifests") or {}).get(name)
    if not isinstance(descriptor, dict):
        raise PlatformError(
            code="temporal_artifact_missing",
            message="The deployment package is missing a temporal contract artifact.",
            detail=name,
            status_code=503,
        )
    package_root = Path(str(route.manifest.get("package_root") or ""))
    relative = str(descriptor.get("path") or "")
    path = (package_root / relative).resolve()
    try:
        path.relative_to(package_root.resolve())
    except ValueError as exc:
        raise PlatformError(
            code="temporal_artifact_invalid",
            message="A temporal contract artifact resolves outside its deployment package.",
            detail=name,
            status_code=500,
        ) from exc
    if not path.is_file():
        raise PlatformError(
            code="temporal_artifact_missing",
            message="A temporal contract artifact is not present on disk.",
            detail=name,
            status_code=503,
        )
    actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    expected_sha256 = str(descriptor.get("sha256") or "").removeprefix("sha256:")
    if actual_sha256 != expected_sha256:
        raise PlatformError(
            code="temporal_artifact_checksum_mismatch",
            message="A temporal contract artifact changed after packaging.",
            detail=name,
            status_code=503,
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlatformError(
            code="temporal_artifact_invalid",
            message="A temporal contract artifact is not valid JSON.",
            detail=name,
            status_code=500,
        ) from exc
    if not isinstance(value, dict):
        raise PlatformError(
            code="temporal_artifact_invalid",
            message="A temporal contract artifact must contain an object.",
            detail=name,
            status_code=500,
        )
    return value


def _extract_temporal_feature(
    root: dict[str, Any],
    spec: dict[str, Any],
) -> tuple[list[float], list[bool]]:
    name = str(spec.get("name") or "")
    kind = str(spec.get("kind") or "scalar")
    length = int(spec.get("length") or 1)
    raw = _temporal_path_value(root, str(spec.get("path") or ""))
    divisor_path = spec.get("divisor_path")
    divisor = _finite_float(_temporal_path_value(root, str(divisor_path))) if divisor_path else 1.0
    divisor_valid = divisor is not None and abs(divisor) > 1e-12
    validity_path = spec.get("validity_path")
    validity = _temporal_path_value(root, str(validity_path)) if validity_path else None
    values: list[float] = []
    present: list[bool] = []
    if kind == "scalar":
        number = float(raw) if isinstance(raw, bool) else _finite_float(raw)
        valid = number is not None and divisor_valid
        if bool(spec.get("required")) and not valid:
            raise PlatformError(
                code="temporal_feature_missing",
                message="A required live temporal feature is missing or invalid.",
                detail=name,
                status_code=409,
            )
        return [number / float(divisor) if valid else 0.0], [valid]
    source = list(raw) if isinstance(raw, list | tuple) else []
    mask = list(validity) if isinstance(validity, list | tuple) else []
    for index in range(length):
        item = source[index] if index < len(source) else None
        if kind in {"point_x", "point_y"}:
            coordinate = "x" if kind == "point_x" else "y"
            item = item.get(coordinate) if isinstance(item, dict) else None
        number = _finite_float(item)
        mask_valid = bool(mask[index]) if index < len(mask) else True
        valid = number is not None and divisor_valid and mask_valid
        values.append(number / float(divisor) if valid else 0.0)
        present.append(valid)
    if bool(spec.get("required")) and len(source) != length:
        raise PlatformError(
            code="temporal_feature_missing",
            message="A required live temporal vector has the wrong length.",
            detail=f"{name}: expected={length}, actual={len(source)}",
            status_code=409,
        )
    return values, present


def _temporal_path_value(root: dict[str, Any], path: str) -> Any:
    if path.startswith("$"):
        return root.get(path)
    value: Any = root
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _temporal_prediction_from_outputs(
    outputs: dict[str, TemporalOutput],
    *,
    contracts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    def scalar(name: str) -> float:
        output = outputs.get(name)
        value = _finite_float(output.values[0]) if output is not None and output.values else None
        if value is None:
            raise PlatformError(
                code="temporal_output_invalid",
                message="The temporal model did not return a finite required output.",
                detail=name,
                status_code=502,
            )
        return value

    bow = _bounded_probability(scalar("output__bow_relevance_probability"))
    target = _bounded_probability(scalar("output__target_observed_probability"))
    sin_2e = scalar("output__angle_sin_2e")
    cos_2e = scalar("output__angle_cos_2e")
    norm = math.hypot(sin_2e, cos_2e)
    if norm <= 1e-8:
        raise PlatformError(
            code="temporal_angle_vector_invalid",
            message="The temporal model returned a degenerate double-angle vector.",
            status_code=502,
        )
    sin_2e /= norm
    cos_2e /= norm
    signed = _wrap_to_half_turn(math.degrees(0.5 * math.atan2(sin_2e, cos_2e)))
    raw_confidence = _bounded_probability(scalar("output__angle_confidence_raw"))
    calibration = contracts["confidence_calibration"]
    confidence = _calibrated_temporal_confidence(raw_confidence, calibration)
    quality_output = outputs.get("output__final_quality_ordinal_logits")
    logits = list(quality_output.values[:2]) if quality_output is not None else []
    quality = _ordinal_quality_probabilities(logits)
    reasons = [] if bool(calibration.get("valid")) else ["confidence_calibration_invalid"]
    return {
        "bow_relevance_probability": bow,
        "target_observed_probability": target,
        "angle_sin_2e": sin_2e,
        "angle_cos_2e": cos_2e,
        "signed_correction_deg": signed,
        "raw_angle_confidence": raw_confidence,
        "angle_confidence": confidence,
        "final_quality_probabilities": quality,
        "reason_codes": reasons,
    }


def _bounded_probability(value: float) -> float:
    if value < -1e-5 or value > 1.0 + 1e-5:
        raise PlatformError(
            code="temporal_output_invalid",
            message="The temporal model returned a probability outside [0, 1].",
            status_code=502,
        )
    return min(max(value, 0.0), 1.0)


def _calibrated_temporal_confidence(raw: float, calibration: dict[str, Any]) -> float:
    if not bool(calibration.get("valid")):
        return raw
    bins = calibration.get("bins") or []
    for item in bins:
        if not isinstance(item, dict):
            continue
        lower = _finite_float(item.get("raw_lower"))
        upper = _finite_float(item.get("raw_upper"))
        calibrated = _finite_float(item.get("calibrated_confidence"))
        if lower is None or upper is None or calibrated is None:
            continue
        if lower <= raw < upper or (raw == 1.0 and upper >= 1.0):
            return _bounded_probability(calibrated)
    valid_bins = [
        item
        for item in bins
        if isinstance(item, dict) and _finite_float(item.get("calibrated_confidence")) is not None
    ]
    nearest = min(
        valid_bins,
        key=lambda item: abs(
            raw - (float(item.get("raw_lower") or 0.0) + float(item.get("raw_upper") or 1.0)) / 2.0
        ),
        default=None,
    )
    return (
        _bounded_probability(float(nearest["calibrated_confidence"]))
        if nearest is not None
        else raw
    )


def _ordinal_quality_probabilities(logits: list[float]) -> dict[str, float]:
    if len(logits) != 2 or any(_finite_float(value) is None for value in logits):
        return {}
    greater_bad = _stable_sigmoid(float(logits[0]))
    greater_acceptable = min(
        greater_bad,
        _stable_sigmoid(float(logits[1])),
    )
    return {
        "bad": 1.0 - greater_bad,
        "acceptable": greater_bad - greater_acceptable,
        "good": greater_acceptable,
    }


def _stable_sigmoid(value: float) -> float:
    if value >= 0.0:
        inverse = math.exp(-value)
        return 1.0 / (1.0 + inverse)
    exponent = math.exp(value)
    return exponent / (1.0 + exponent)


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _wrap_to_half_turn(value: float) -> float:
    wrapped = (float(value) + 90.0) % 180.0 - 90.0
    return -90.0 if wrapped >= 90.0 - 1e-12 else wrapped
