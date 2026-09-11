from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any, Literal, cast

from cv_contracts.runtime import RuntimeNodeSpec

from cv_runtime.context import NodeInputs, RuntimeExecutionContext
from cv_runtime.handlers import NodeExecutionResult
from cv_runtime.payloads import (
    ResultPayload,
    StemRotationAngleScorePayload,
    StemRotationOptimizationPayload,
    StemRotationPriorityPayload,
    StemRotationReplaySnapshotPayload,
    StemSliceModelPayload,
)
from cv_runtime.stem_rotation_optimizer import (
    StemRotationOptimizationConfig,
    optimize_stem_rotation,
)
from cv_runtime.stem_rotation_types import (
    STEM_ROTATION_GEOMETRY_VERSION,
    STEM_ROTATION_OPTIMIZER_VERSION,
    STEM_SLICE_PREPROCESSING_VERSION,
    PriorityMode,
    RotationConstraints,
    StemRotationOptimizationResult,
    StemSliceGeometry,
    StemSliceModel,
)
from cv_runtime.stem_rotation_view_model import build_stem_rotation_view_model

RuntimeOptimizationState = Literal[
    "IDLE",
    "MEASURING",
    "MODEL_BUILDING",
    "OPTIMIZING",
    "RESULT_READY",
    "DONE",
    "APPLIED",
    "INVALID",
    "ERROR",
]


class RotationConstraintConfigurationError(ValueError):
    """Raised when signed mechanical limits cannot contain a valid candidate set."""


class StemRotationOptimizerHandler:
    node_type = "analysis.stem_rotation_optimizer"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        model = inputs.get("slice_model")
        priority = inputs.get("priority")
        try:
            config, constraints, config_snapshot = _configuration(node.config or {})
        except RotationConstraintConfigurationError as exc:
            return _output(
                _safe_payload(
                    context,
                    model,
                    priority,
                    state="INVALID",
                    reason_code="ROTATION_CONSTRAINT_CONFLICT",
                    config_snapshot={"error": str(exc)[:240]},
                )
            )
        except (TypeError, ValueError) as exc:
            return _output(
                _safe_payload(
                    context,
                    model,
                    priority,
                    state="ERROR",
                    reason_code="CONFIGURATION_INVALID",
                    config_snapshot={"error": str(exc)[:240]},
                )
            )

        if not isinstance(model, StemSliceModelPayload):
            return _output(
                _safe_payload(
                    context,
                    model,
                    priority,
                    state="MEASURING",
                    reason_code="STEM_SLICE_MODEL_MISSING",
                    config_snapshot=config_snapshot,
                    constraints=constraints,
                )
            )
        if not isinstance(priority, StemRotationPriorityPayload):
            return _output(
                _safe_payload(
                    context,
                    model,
                    priority,
                    state="INVALID",
                    reason_code="PRIORITY_INPUT_MISSING",
                    config_snapshot=config_snapshot,
                    constraints=constraints,
                    result_ready=model.frozen,
                )
            )
        if not priority.valid:
            return _output(
                _safe_payload(
                    context,
                    model,
                    priority,
                    state="INVALID",
                    reason_code=priority.reason_code,
                    config_snapshot=config_snapshot,
                    constraints=constraints,
                    result_ready=model.frozen,
                )
            )
        if not model.frozen:
            state = cast(
                RuntimeOptimizationState,
                {
                    "RESET": "IDLE",
                    "COLLECTING": "MEASURING",
                    "READY": "MODEL_BUILDING",
                    "DEGRADED": "MODEL_BUILDING",
                    "INVALID": "INVALID",
                }.get(model.state, "MODEL_BUILDING"),
            )
            return _output(
                _safe_payload(
                    context,
                    model,
                    priority,
                    state=state,
                    reason_code=(
                        "INVALID_STEM_MODEL" if model.state == "INVALID" else "MODEL_NOT_FROZEN"
                    ),
                    config_snapshot=config_snapshot,
                    constraints=constraints,
                    result_ready=model.state == "INVALID",
                )
            )
        if not model.valid:
            return _output(
                _safe_payload(
                    context,
                    model,
                    priority,
                    state="INVALID",
                    reason_code=_measurement_reason(model),
                    config_snapshot=config_snapshot,
                    constraints=constraints,
                    result_ready=True,
                )
            )
        valid_evaluation_count, evaluated_slice_count = _evaluation_slice_counts(
            model,
            config_snapshot,
        )
        if evaluated_slice_count == 0 or valid_evaluation_count == 0:
            return _output(
                _safe_payload(
                    context,
                    model,
                    priority,
                    state="INVALID",
                    reason_code="NO_VALID_SLICES_IN_EVALUATION_RANGE",
                    config_snapshot=config_snapshot,
                    constraints=constraints,
                    result_ready=True,
                    valid_slice_count=valid_evaluation_count,
                    evaluated_slice_count=evaluated_slice_count,
                )
            )
        if valid_evaluation_count < config.minimum_valid_slice_count:
            return _output(
                _safe_payload(
                    context,
                    model,
                    priority,
                    state="INVALID",
                    reason_code="VALID_SLICE_COUNT_INSUFFICIENT",
                    config_snapshot=config_snapshot,
                    constraints=constraints,
                    result_ready=True,
                    valid_slice_count=valid_evaluation_count,
                    evaluated_slice_count=evaluated_slice_count,
                )
            )
        if (
            valid_evaluation_count / evaluated_slice_count
            < config.minimum_valid_slice_ratio
        ):
            return _output(
                _safe_payload(
                    context,
                    model,
                    priority,
                    state="INVALID",
                    reason_code="VALID_SLICE_RATIO_LOW",
                    config_snapshot=config_snapshot,
                    constraints=constraints,
                    result_ready=True,
                    valid_slice_count=valid_evaluation_count,
                    evaluated_slice_count=evaluated_slice_count,
                )
            )

        try:
            domain_model = _domain_model(model, config_snapshot)
            optimized = optimize_stem_rotation(
                domain_model,
                PriorityMode(priority.priority_mode),
                constraints=constraints,
                config=config,
            )
        except (ArithmeticError, TypeError, ValueError):
            return _output(
                _safe_payload(
                    context,
                    model,
                    priority,
                    state="ERROR",
                    reason_code="OPTIMIZER_FAILED",
                    config_snapshot=config_snapshot,
                    constraints=constraints,
                    result_ready=True,
                )
            )
        payload = _result_payload(
            context,
            model,
            priority,
            optimized,
            config_snapshot=config_snapshot,
            constraints=constraints,
            valid_slice_count=valid_evaluation_count,
            evaluated_slice_count=evaluated_slice_count,
        )
        return _output(payload)


def _configuration(
    raw: Mapping[str, Any],
) -> tuple[StemRotationOptimizationConfig, RotationConstraints, dict[str, Any]]:
    snapshot: dict[str, Any] = {
        "slice_spacing_mm": _number(raw, "slice_spacing_mm", 50.0),
        "x_eval_start_mm": _optional_number(raw.get("x_eval_start_mm")),
        "x_eval_end_mm": _optional_number(raw.get("x_eval_end_mm")),
        "smoothing_window_slices": _integer(raw, "smoothing_window_slices", 3),
        "coarse_angle_step_deg": _number(raw, "coarse_angle_step_deg", 1.0),
        "fine_angle_step_deg": _number(raw, "fine_angle_step_deg", 0.1),
        "fine_search_half_width_deg": _number(raw, "fine_search_half_width_deg", 2.0),
        "score_tolerance_mm": _number(raw, "score_tolerance_mm", 1.0),
        "min_gain_mm": _number(raw, "min_gain_mm", 3.0),
        "min_rotation_deg": _number(raw, "min_rotation_deg", 1.0),
        "min_valid_slice_ratio": _number(raw, "min_valid_slice_ratio", 0.6),
        "min_valid_slice_count": _integer(raw, "min_valid_slice_count", 3),
        "min_model_quality": _number(raw, "min_model_quality", 0.25),
        "max_interpolation_gap_mm": _number(raw, "max_interpolation_gap_mm", 100.0),
        "rotation_negative_limit_deg": _number(raw, "rotation_negative_limit_deg", -90.0),
        "rotation_positive_limit_deg": _number(raw, "rotation_positive_limit_deg", 90.0),
    }
    start = snapshot["x_eval_start_mm"]
    end = snapshot["x_eval_end_mm"]
    if start is not None and end is not None and end < start:
        raise ValueError("x evaluation range must not be reversed")
    config = StemRotationOptimizationConfig(
        coarse_angle_step_deg=snapshot["coarse_angle_step_deg"],
        fine_angle_step_deg=snapshot["fine_angle_step_deg"],
        fine_search_half_width_deg=snapshot["fine_search_half_width_deg"],
        score_tolerance_mm=snapshot["score_tolerance_mm"],
        min_gain_mm=snapshot["min_gain_mm"],
        min_rotation_deg=snapshot["min_rotation_deg"],
        minimum_model_quality=snapshot["min_model_quality"],
        minimum_valid_slice_ratio=snapshot["min_valid_slice_ratio"],
        minimum_valid_slice_count=snapshot["min_valid_slice_count"],
    ).validate()
    try:
        constraints = RotationConstraints(
            negative_limit_deg=snapshot["rotation_negative_limit_deg"],
            positive_limit_deg=snapshot["rotation_positive_limit_deg"],
        ).validate()
    except ValueError as exc:
        raise RotationConstraintConfigurationError(str(exc)) from exc
    return config, constraints, snapshot


def _domain_model(
    payload: StemSliceModelPayload,
    config: Mapping[str, Any],
) -> StemSliceModel:
    slices = []
    for item in payload.processed_slices:
        if not item.valid:
            continue
        if None in (item.cy_mm, item.cz_mm, item.a_mm, item.b_mm, item.phi_rad):
            continue
        slices.append(
            StemSliceGeometry(
                x_mm=item.x_mm,
                cy_mm=cast(float, item.cy_mm),
                cz_mm=cast(float, item.cz_mm),
                a_mm=cast(float, item.a_mm),
                b_mm=cast(float, item.b_mm),
                phi_rad=cast(float, item.phi_rad),
                quality=item.quality,
                valid=True,
                interpolated=item.interpolated,
                reason_codes=tuple(item.reason_codes),
            )
        )
    return StemSliceModel(
        slices=tuple(slices),
        model_quality=payload.model_quality,
        x_eval_start_mm=cast(float | None, config["x_eval_start_mm"]),
        x_eval_end_mm=cast(float | None, config["x_eval_end_mm"]),
        reason_codes=tuple(payload.reason_codes),
    ).validate()


def _evaluation_slice_counts(
    model: StemSliceModelPayload,
    config: Mapping[str, Any],
) -> tuple[int, int]:
    """Count valid and requested stations inside the configured x range.

    Fit diagnostics retain stations whose geometry was rejected, while processed
    slices retain accepted and short-gap-interpolated stations. Counting both views
    prevents an adapter from making the valid ratio look better by dropping invalid
    stations before the pure optimizer is called.
    """

    start = cast(float | None, config["x_eval_start_mm"])
    end = cast(float | None, config["x_eval_end_mm"])

    def included(x_mm: float) -> bool:
        return (start is None or x_mm >= start) and (end is None or x_mm <= end)

    valid_count = sum(item.valid and included(item.x_mm) for item in model.processed_slices)
    station_positions = {
        round(item.x_mm, 9)
        for item in model.fit_diagnostics
        if included(item.x_mm)
    }
    if not station_positions:
        station_positions = {
            round(item.x_mm, 9)
            for item in model.processed_slices
            if included(item.x_mm)
        }
    evaluated_count = max(len(station_positions), valid_count)
    if start is None and end is None:
        evaluated_count = max(evaluated_count, model.requested_station_count)
    return valid_count, evaluated_count


def _result_payload(
    context: RuntimeExecutionContext,
    model: StemSliceModelPayload,
    priority: StemRotationPriorityPayload,
    result: StemRotationOptimizationResult,
    *,
    config_snapshot: dict[str, Any],
    constraints: RotationConstraints,
    valid_slice_count: int,
    evaluated_slice_count: int,
) -> StemRotationOptimizationPayload:
    valid = result.valid
    delta = result.delta_angle_deg if valid else None
    reasons = list(dict.fromkeys([*model.reason_codes, result.reason_code.value]))
    return StemRotationOptimizationPayload(
        algorithm_version=result.algorithm_version,
        geometry_version=STEM_ROTATION_GEOMETRY_VERSION,
        preprocessing_version=STEM_SLICE_PREPROCESSING_VERSION,
        rotation_reference_view=model.rotation_reference_view,
        positive_rotation=model.positive_rotation,
        state="RESULT_READY" if valid else "INVALID",
        result_ready=True,
        result_valid=valid,
        timestamp=context.now_iso(),
        stem_id=model.session_id,
        session_id=model.session_id,
        track_id=model.track_id,
        model_hash=model.model_hash,
        configuration_hash=_configuration_hash(config_snapshot, constraints),
        priority_source=priority.priority_source,
        priority_mode=priority.priority_mode,
        priority_axis=priority.priority_axis,
        priority_revision=priority.revision,
        optimization_enable=priority.optimization_enable,
        should_rotate=result.should_rotate if valid else False,
        rotation_delta_deg=delta,
        delta_angle_deg=delta,
        gain_mm=result.gain_mm if valid else None,
        model_quality=model.model_quality,
        reason_code=result.reason_code.value,
        reason_codes=reasons,
        limiting_x_current_mm=result.limiting_x_current_mm,
        limiting_x_optimal_mm=result.limiting_x_optimal_mm,
        score_current_mm=result.score_current_mm,
        score_optimal_mm=result.score_optimal_mm,
        optimal_candidate_angle_deg=result.optimal_candidate_angle_deg,
        plateau_width_deg=result.plateau_width_deg,
        valid_slice_count=valid_slice_count,
        evaluated_slice_count=evaluated_slice_count,
        score_curve=[
            StemRotationAngleScorePayload(
                angle_deg=item.angle_deg,
                score_mm=item.score_mm,
                limiting_x_mm=item.limiting_x_mm,
                valid=item.valid,
                reason_code=item.reason_code.value,
            )
            for item in result.score_curve
        ],
        replay=_replay(model, priority, config_snapshot, constraints),
    )


def _safe_payload(
    context: RuntimeExecutionContext,
    model: object,
    priority: object,
    *,
    state: RuntimeOptimizationState,
    reason_code: str,
    config_snapshot: dict[str, Any],
    constraints: RotationConstraints | None = None,
    result_ready: bool = False,
    valid_slice_count: int | None = None,
    evaluated_slice_count: int | None = None,
) -> StemRotationOptimizationPayload:
    typed_model = model if isinstance(model, StemSliceModelPayload) else None
    typed_priority = priority if isinstance(priority, StemRotationPriorityPayload) else None
    fallback_priority = typed_priority or StemRotationPriorityPayload()
    resolved_constraints = constraints or RotationConstraints()
    reasons = list(
        dict.fromkeys([*(typed_model.reason_codes if typed_model else []), reason_code])
    )
    return StemRotationOptimizationPayload(
        algorithm_version=STEM_ROTATION_OPTIMIZER_VERSION,
        geometry_version=STEM_ROTATION_GEOMETRY_VERSION,
        preprocessing_version=STEM_SLICE_PREPROCESSING_VERSION,
        rotation_reference_view=typed_model.rotation_reference_view if typed_model else None,
        positive_rotation=typed_model.positive_rotation if typed_model else None,
        state=state,
        result_ready=result_ready,
        result_valid=False,
        timestamp=context.now_iso(),
        stem_id=typed_model.session_id if typed_model else None,
        session_id=typed_model.session_id if typed_model else None,
        track_id=typed_model.track_id if typed_model else None,
        model_hash=typed_model.model_hash if typed_model else None,
        configuration_hash=_configuration_hash(config_snapshot, resolved_constraints),
        priority_source=fallback_priority.priority_source,
        priority_mode=fallback_priority.priority_mode,
        priority_axis=fallback_priority.priority_axis,
        priority_revision=fallback_priority.revision,
        optimization_enable=fallback_priority.optimization_enable,
        model_quality=typed_model.model_quality if typed_model else 0,
        reason_code=reason_code,
        reason_codes=reasons,
        valid_slice_count=(
            valid_slice_count
            if valid_slice_count is not None
            else typed_model.valid_processed_slice_count
            if typed_model
            else 0
        ),
        evaluated_slice_count=(
            evaluated_slice_count
            if evaluated_slice_count is not None
            else typed_model.requested_station_count
            if typed_model
            else 0
        ),
        replay=_replay(typed_model, fallback_priority, config_snapshot, resolved_constraints),
    )


def _replay(
    model: StemSliceModelPayload | None,
    priority: StemRotationPriorityPayload,
    config: dict[str, Any],
    constraints: RotationConstraints,
) -> StemRotationReplaySnapshotPayload:
    return StemRotationReplaySnapshotPayload(
        model_schema_version=model.schema_version if model else "stem.slice-model.v1",
        model_hash=model.model_hash if model else None,
        current_pose_phase_deg=model.current_pose_phase_deg if model else None,
        rotation_reference_view=model.rotation_reference_view if model else None,
        positive_rotation=model.positive_rotation if model else None,
        trace_generation=model.trace_generation if model else None,
        process_calibration_hash=model.process_calibration_hash if model else None,
        phase_calibration_id=model.phase_calibration_id if model else None,
        phase_calibration_hash=model.phase_calibration_hash if model else None,
        raw_slices=model.raw_slices if model else [],
        processed_slices=model.processed_slices if model else [],
        fit_diagnostics=model.fit_diagnostics if model else [],
        raw_observations=model.raw_observations if model else [],
        model_quality=model.model_quality if model else 0,
        model_reason_codes=model.reason_codes if model else [],
        priority_control={
            "schema_version": priority.schema_version,
            "priority_source": priority.priority_source,
            "priority_mode": priority.priority_mode,
            "optimization_enable": priority.optimization_enable,
            "revision": priority.revision,
            "updated_at": priority.updated_at,
            "updated_by": priority.updated_by,
        },
        optimizer_config=config,
        rotation_constraints={
            "negative_limit_deg": constraints.negative_limit_deg,
            "positive_limit_deg": constraints.positive_limit_deg,
        },
    )


def _output(payload: StemRotationOptimizationPayload) -> NodeExecutionResult:
    try:
        payload = payload.model_copy(
            update={"view_model": build_stem_rotation_view_model(payload)}
        )
    except (ArithmeticError, TypeError, ValueError):
        # Visualization must never turn a fail-closed optimizer result into a runtime failure.
        pass
    return NodeExecutionResult(
        outputs={
            "result": ResultPayload(
                result=payload.model_dump(mode="json"),
                decision=payload.should_rotate if payload.result_valid else False,
            )
        }
    )


def _configuration_hash(config: Mapping[str, Any], constraints: RotationConstraints) -> str:
    encoded = json.dumps(
        {
            "optimizer_config": dict(config),
            "rotation_constraints": {
                "negative_limit_deg": constraints.negative_limit_deg,
                "positive_limit_deg": constraints.positive_limit_deg,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _measurement_reason(model: StemSliceModelPayload) -> str:
    if any("PHASE" in reason or "ANGLE" in reason for reason in model.reason_codes):
        return "INSUFFICIENT_ANGLE_COVERAGE"
    if model.state == "INVALID":
        return "INVALID_STEM_MODEL"
    return "INSUFFICIENT_MEASUREMENT"


def _number(config: Mapping[str, Any], key: str, default: float) -> float:
    raw = config.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise TypeError(f"{key} must be numeric")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{key} must be finite")
    return value


def _integer(config: Mapping[str, Any], key: str, default: int) -> int:
    raw = config.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError(f"{key} must be an integer")
    return int(raw)


def _optional_number(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("optional evaluation range values must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("optional evaluation range values must be finite")
    return result


analysis_stem_rotation_optimizer = StemRotationOptimizerHandler()
