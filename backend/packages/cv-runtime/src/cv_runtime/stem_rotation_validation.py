from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import random
import statistics
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from cv_contracts.runtime import RuntimeNodeSpec, RuntimeStemRotationPriorityControl
from cv_status import RuntimeMode

from cv_runtime.context import RuntimeExecutionContext
from cv_runtime.nodes.stem_rotation_optimizer import StemRotationOptimizerHandler
from cv_runtime.payloads import (
    ResultPayload,
    StemProjectionObservationPayload,
    StemRotationOptimizationPayload,
    StemSliceFitDiagnosticsPayload,
    StemSliceGeometryPayload,
    StemSliceModelPayload,
)
from cv_runtime.registry import BUILTIN_NODE_TYPE_BY_KEY
from cv_runtime.stem_rotation_priority import resolve_stem_rotation_priority
from cv_runtime.stem_rotation_types import (
    STEM_ROTATION_GEOMETRY_VERSION,
    STEM_ROTATION_OPTIMIZER_VERSION,
    STEM_SLICE_PREPROCESSING_VERSION,
    StemSliceGeometry,
    StemSliceModel,
)
from cv_runtime.stem_slice_measurement import (
    STEM_PROJECTION_OBSERVATION_SCHEMA,
    StemSliceModelBuildResult,
    replay_projection_observation_document,
)
from cv_runtime.stem_slice_preprocessing import (
    StemSlicePreprocessingConfig,
    preprocess_stem_slice_model,
)

STEM_ROTATION_DIAGNOSTIC_PACKAGE_SCHEMA = "stem.rotation-diagnostic-package.v1"
STEM_ROTATION_VALIDATION_REPORT_SCHEMA = "stem.rotation-validation-report.v1"
STEM_ROTATION_MONTE_CARLO_SCHEMA = "stem.rotation-monte-carlo.v1"
STEM_ROTATION_PARAMETER_SWEEP_SCHEMA = "stem.rotation-parameter-sweep.v1"


class SyntheticStemCase(StrEnum):
    STRAIGHT_ROUND = "straight_round"
    CURVED_ROUND = "curved_round"
    CENTERED_ELLIPSE = "centered_ellipse"
    CURVED_ELLIPSE = "curved_ellipse"
    VARYING_OVALITY_ANGLE = "varying_ovality_angle"
    VARYING_CURVATURE_DIRECTION = "varying_curvature_direction"
    TAPER = "taper"
    LOCAL_BOTTLENECK = "local_bottleneck"


@dataclass(frozen=True)
class SyntheticStemConfig:
    """Reproducible geometry generator in canonical machine mm/deg units."""

    case: SyntheticStemCase = SyntheticStemCase.CURVED_ELLIPSE
    seed: int = 20260821
    length_mm: float = 4_000.0
    slice_spacing_mm: float = 50.0
    diameter_start_mm: float = 240.0
    diameter_end_mm: float | None = None
    axis_ratio: float = 0.75
    ellipse_angle_deg: float = 25.0
    ellipse_angle_variation_deg: float = 0.0
    curvature_amplitude_mm: float = 35.0
    curvature_direction_deg: float = 90.0
    curvature_direction_variation_deg: float = 0.0
    bottleneck_x_mm: float | None = None
    bottleneck_diameter_reduction_mm: float = 45.0
    center_noise_std_mm: float = 0.0
    axis_noise_std_mm: float = 0.0
    angle_noise_std_deg: float = 0.0
    outlier_count: int = 0
    outlier_offset_mm: float = 45.0
    short_gap_slices: int = 0
    long_gap_slices: int = 0
    model_quality: float = 0.95

    def validate(self) -> SyntheticStemConfig:
        positive = {
            "length_mm": self.length_mm,
            "slice_spacing_mm": self.slice_spacing_mm,
            "diameter_start_mm": self.diameter_start_mm,
        }
        if not all(math.isfinite(value) and value > 0 for value in positive.values()):
            raise ValueError("synthetic length, spacing, and start diameter must be positive")
        if self.diameter_end_mm is not None and (
            not math.isfinite(self.diameter_end_mm) or self.diameter_end_mm <= 0
        ):
            raise ValueError("diameter_end_mm must be positive when supplied")
        if not math.isfinite(self.axis_ratio) or not 0 < self.axis_ratio <= 1:
            raise ValueError("axis_ratio must be in (0, 1]")
        for name in (
            "ellipse_angle_deg",
            "ellipse_angle_variation_deg",
            "curvature_amplitude_mm",
            "curvature_direction_deg",
            "curvature_direction_variation_deg",
            "bottleneck_diameter_reduction_mm",
            "center_noise_std_mm",
            "axis_noise_std_mm",
            "angle_noise_std_deg",
            "outlier_offset_mm",
        ):
            if not math.isfinite(float(getattr(self, name))):
                raise ValueError(f"{name} must be finite")
        if (
            min(
                self.bottleneck_diameter_reduction_mm,
                self.center_noise_std_mm,
                self.axis_noise_std_mm,
                self.angle_noise_std_deg,
                self.outlier_offset_mm,
            )
            < 0
        ):
            raise ValueError("synthetic noise, bottleneck, and outlier values must be non-negative")
        if min(self.outlier_count, self.short_gap_slices, self.long_gap_slices) < 0:
            raise ValueError("synthetic counts must be non-negative")
        if not 0 <= self.model_quality <= 1:
            raise ValueError("model_quality must be between zero and one")
        return self


@dataclass(frozen=True)
class StemRotationValidationInput:
    source_kind: str
    model_payload: StemSliceModelPayload
    source_model: StemSliceModel
    priority_control: RuntimeStemRotationPriorityControl
    optimizer_config: dict[str, Any]
    original_result: dict[str, Any] | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class _OfflineRuntimeExecutionContext(RuntimeExecutionContext):
    deterministic_timestamp: str = "1970-01-01T00:00:00Z"

    def now_iso(self) -> str:
        return self.deterministic_timestamp


def generate_synthetic_stem(
    config: SyntheticStemConfig | None = None,
) -> StemSliceModel:
    """Generate geometry only; all preparation and optimization stay production APIs."""

    resolved = (config or SyntheticStemConfig()).validate()
    random_source = random.Random(resolved.seed)
    count = int(math.floor(resolved.length_mm / resolved.slice_spacing_mm + 1.0e-9)) + 1
    gap_indices = _synthetic_gap_indices(count, resolved)
    outlier_indices = set(
        random_source.sample(
            [index for index in range(1, max(count - 1, 1)) if index not in gap_indices],
            min(resolved.outlier_count, max(count - 2 - len(gap_indices), 0)),
        )
    )
    bottleneck_x = (
        resolved.bottleneck_x_mm
        if resolved.bottleneck_x_mm is not None
        else resolved.length_mm * 0.55
    )
    slices: list[StemSliceGeometry] = []
    for index in range(count):
        x_mm = min(index * resolved.slice_spacing_mm, resolved.length_mm)
        progress = x_mm / resolved.length_mm
        diameter = _synthetic_diameter(resolved, progress)
        axis_ratio = _synthetic_axis_ratio(resolved)
        a_mm = diameter / 2.0
        b_mm = a_mm * axis_ratio
        phi_deg = _synthetic_phi_deg(resolved, progress)
        cy_mm, cz_mm = _synthetic_center(resolved, progress)

        if (
            resolved.case is SyntheticStemCase.LOCAL_BOTTLENECK
            and abs(x_mm - bottleneck_x) <= resolved.slice_spacing_mm * 0.5
        ):
            reduction_radius = resolved.bottleneck_diameter_reduction_mm / 2.0
            a_mm = max(5.0, a_mm - reduction_radius)
            b_mm = max(5.0, b_mm - reduction_radius)

        cy_mm += random_source.gauss(0.0, resolved.center_noise_std_mm)
        cz_mm += random_source.gauss(0.0, resolved.center_noise_std_mm)
        a_mm += random_source.gauss(0.0, resolved.axis_noise_std_mm)
        b_mm += random_source.gauss(0.0, resolved.axis_noise_std_mm)
        phi_deg += random_source.gauss(0.0, resolved.angle_noise_std_deg)
        quality = 0.95
        reason_codes: tuple[str, ...] = ()
        if index in outlier_indices:
            direction = -1.0 if index % 2 else 1.0
            cy_mm += direction * resolved.outlier_offset_mm
            cz_mm -= direction * resolved.outlier_offset_mm * 0.7
            a_mm += resolved.outlier_offset_mm * 0.35
            b_mm -= resolved.outlier_offset_mm * 0.25
            quality = 0.45
            reason_codes = ("SYNTHETIC_OUTLIER",)
        major = max(a_mm, b_mm, 5.0)
        minor = max(min(a_mm, b_mm), 5.0)
        valid = index not in gap_indices
        slices.append(
            StemSliceGeometry(
                x_mm=x_mm,
                cy_mm=cy_mm,
                cz_mm=cz_mm,
                a_mm=major,
                b_mm=minor,
                phi_rad=math.radians(phi_deg),
                quality=quality if valid else 0.0,
                valid=valid,
                reason_codes=(reason_codes if valid else ("SYNTHETIC_MEASUREMENT_GAP",)),
            ).validate()
        )
    return StemSliceModel(
        slices=tuple(slices),
        model_quality=resolved.model_quality,
        x_eval_start_mm=0.0,
        x_eval_end_mm=resolved.length_mm,
        reason_codes=(f"SYNTHETIC_CASE:{resolved.case.value}",),
    ).validate()


def default_optimizer_node_config() -> dict[str, Any]:
    definition = BUILTIN_NODE_TYPE_BY_KEY["analysis.stem_rotation_optimizer"]
    return dict(definition.default_config)


def default_preprocessing_config(
    optimizer_config: Mapping[str, Any] | None = None,
) -> StemSlicePreprocessingConfig:
    values = {**default_optimizer_node_config(), **dict(optimizer_config or {})}
    return StemSlicePreprocessingConfig(
        slice_spacing_mm=float(values["slice_spacing_mm"]),
        max_interpolation_gap_mm=float(values["max_interpolation_gap_mm"]),
        smoothing_window_slices=int(values["smoothing_window_slices"]),
        x_eval_start_mm=_optional_float(values.get("x_eval_start_mm")),
        x_eval_end_mm=_optional_float(values.get("x_eval_end_mm")),
    ).validate()


def model_payload_from_domain(
    model: StemSliceModel,
    *,
    preprocessing_config: StemSlicePreprocessingConfig | None = None,
    session_id: str = "offline-validation",
    rotation_reference_view: str = "looking_along_positive_x",
    positive_rotation: str = "counter_clockwise_in_y_z_view",
) -> StemSliceModelPayload:
    source = model.validate()
    preprocessing = (
        preprocess_stem_slice_model(source, config=preprocessing_config)
        if preprocessing_config is not None
        else None
    )
    processed = preprocessing.model if preprocessing is not None else source
    valid_raw = sum(item.valid for item in source.slices)
    valid_processed = sum(item.valid for item in processed.slices)
    model_hash = _model_hash(processed)
    return StemSliceModelPayload(
        state="FROZEN" if valid_processed > 0 else "INVALID",
        valid=valid_processed > 0,
        frozen=valid_processed > 0,
        session_id=session_id,
        track_id=f"{session_id}-track",
        timestamp="1970-01-01T00:00:00Z",
        current_pose_phase_deg=0.0,
        rotation_reference_view=rotation_reference_view,
        positive_rotation=positive_rotation,
        model_hash=model_hash if valid_processed > 0 else None,
        model_quality=processed.model_quality,
        requested_station_count=len(processed.slices),
        valid_raw_slice_count=valid_raw,
        valid_processed_slice_count=valid_processed,
        valid_raw_slice_ratio=valid_raw / len(source.slices) if source.slices else 0.0,
        maximum_invalid_gap_mm=_maximum_invalid_gap(processed.slices),
        raw_slices=_geometry_payloads(source.slices),
        processed_slices=_geometry_payloads(processed.slices),
        reason_codes=list(
            dict.fromkeys(
                (
                    *source.reason_codes,
                    *(preprocessing.reason_codes if preprocessing is not None else ()),
                )
            )
        ),
        metadata={
            "offline_validation": True,
            "preprocessing_applied": preprocessing is not None,
        },
    )


def model_payload_from_build_result(
    build: StemSliceModelBuildResult,
    *,
    session_id: str,
    rotation_reference_view: str,
    positive_rotation: str,
) -> StemSliceModelPayload:
    valid = build.valid_processed_slice_count > 0 and build.status.value != "INVALID"
    return StemSliceModelPayload(
        state="FROZEN" if valid else "INVALID",
        valid=valid,
        frozen=valid,
        session_id=session_id,
        track_id=f"{session_id}-track",
        timestamp="1970-01-01T00:00:00Z",
        current_pose_phase_deg=build.reference_phase_deg if valid else None,
        rotation_reference_view=rotation_reference_view if valid else None,
        positive_rotation=positive_rotation if valid else None,
        model_hash=_model_hash(build.processed_model) if valid else None,
        model_quality=build.model_quality,
        requested_station_count=build.requested_station_count,
        valid_raw_slice_count=build.valid_raw_slice_count,
        valid_processed_slice_count=build.valid_processed_slice_count,
        valid_raw_slice_ratio=build.valid_raw_slice_ratio,
        maximum_invalid_gap_mm=build.maximum_invalid_gap_mm,
        raw_slices=_geometry_payloads(build.raw_model.slices),
        processed_slices=_geometry_payloads(build.processed_model.slices),
        fit_diagnostics=[
            StemSliceFitDiagnosticsPayload(**asdict(item.diagnostics))
            for item in build.station_fits
        ],
        reason_codes=list(build.reason_codes),
        metadata={"offline_validation": True, "source": "projection_observations"},
    )


def load_validation_document(
    document: Mapping[str, Any],
) -> StemRotationValidationInput:
    """Load production result, slice-model, or calibrated observation JSON."""

    optimization_result = _extract_optimization_result(document)
    if optimization_result is not None:
        replay = _mapping(optimization_result, "replay")
        model_payload = _model_payload_from_replay(replay, optimization_result)
        control_values = _mapping(replay, "priority_control")
        # The persisted snapshot deliberately stores the resolved runtime payload
        # schema, while replay needs the semantically equivalent channel control.
        control_values.pop("schema_version", None)
        control = RuntimeStemRotationPriorityControl.model_validate(control_values)
        optimizer_config = {
            **default_optimizer_node_config(),
            **_mapping(replay, "optimizer_config"),
        }
        constraints = _mapping(replay, "rotation_constraints")
        optimizer_config.update(
            {
                "rotation_negative_limit_deg": constraints.get("negative_limit_deg", -90.0),
                "rotation_positive_limit_deg": constraints.get("positive_limit_deg", 90.0),
            }
        )
        warnings = _version_warnings(optimization_result)
        return StemRotationValidationInput(
            source_kind="production_optimization_result",
            model_payload=model_payload,
            source_model=_domain_model_from_payload(model_payload),
            priority_control=control,
            optimizer_config=optimizer_config,
            original_result=dict(optimization_result),
            warnings=warnings,
        )

    if document.get("schema_version") == "stem.slice-model.v1":
        model_payload = StemSliceModelPayload.model_validate(document)
        if model_payload.valid and not model_payload.frozen:
            model_payload = model_payload.model_copy(update={"state": "FROZEN", "frozen": True})
        return StemRotationValidationInput(
            source_kind="production_slice_model",
            model_payload=model_payload,
            source_model=_domain_model_from_payload(model_payload),
            priority_control=RuntimeStemRotationPriorityControl(
                priority_mode="VORSCHNITT_KRITISCH"
            ),
            optimizer_config=default_optimizer_node_config(),
            warnings=("NO_ORIGINAL_OPTIMIZATION_RESULT",),
        )

    if document.get("schema_version") == STEM_PROJECTION_OBSERVATION_SCHEMA:
        build = replay_projection_observation_document(document)
        reference_view = str(document.get("rotation_reference_view") or "looking_along_positive_x")
        positive_rotation = str(
            document.get("positive_rotation") or "counter_clockwise_in_y_z_view"
        )
        model_payload = model_payload_from_build_result(
            build,
            session_id=str(document.get("session_id") or "offline-observation-replay"),
            rotation_reference_view=reference_view,
            positive_rotation=positive_rotation,
        )
        return StemRotationValidationInput(
            source_kind="production_projection_observations",
            model_payload=model_payload,
            source_model=build.raw_model,
            priority_control=RuntimeStemRotationPriorityControl(
                priority_mode="VORSCHNITT_KRITISCH"
            ),
            optimizer_config=default_optimizer_node_config(),
            warnings=("NO_ORIGINAL_OPTIMIZATION_RESULT",),
        )
    raise ValueError(
        "expected stem.rotation-optimization.v1, stem.slice-model.v1, or "
        "stem.projection-observations.v1"
    )


def synthetic_validation_input(
    config: SyntheticStemConfig,
    *,
    priority_mode: str = "VORSCHNITT_KRITISCH",
    optimizer_config: Mapping[str, Any] | None = None,
) -> StemRotationValidationInput:
    node_config = {**default_optimizer_node_config(), **dict(optimizer_config or {})}
    source = generate_synthetic_stem(config)
    payload = model_payload_from_domain(
        source,
        preprocessing_config=default_preprocessing_config(node_config),
        session_id=f"synthetic-{config.case.value}-{config.seed}",
    )
    return StemRotationValidationInput(
        source_kind=f"synthetic:{config.case.value}",
        model_payload=payload,
        source_model=source,
        priority_control=RuntimeStemRotationPriorityControl(
            priority_mode=priority_mode,
            revision=1,
            updated_by="offline-simulator",
        ),
        optimizer_config=node_config,
        warnings=("SYNTHETIC_INPUT_NOT_PRODUCTION_MEASUREMENT",),
    )


def run_runtime_optimization(
    model: StemSliceModelPayload,
    control: RuntimeStemRotationPriorityControl | Mapping[str, Any],
    *,
    optimizer_config: Mapping[str, Any] | None = None,
    correlation_id: str = "offline-stem-rotation-validation",
) -> StemRotationOptimizationPayload:
    """Run the actual priority resolver, runtime node, optimizer, and HMI builder."""

    priority = resolve_stem_rotation_priority(control)
    definition = BUILTIN_NODE_TYPE_BY_KEY["analysis.stem_rotation_optimizer"]
    node = RuntimeNodeSpec(
        node_key="stem_rotation_optimizer",
        node_type=definition.node_type,
        node_category=definition.node_category,
        node_contract_version=definition.contract_version,
        input_ports=definition.input_ports,
        output_ports=definition.output_ports,
        config={**definition.default_config, **dict(optimizer_config or {})},
    )
    context = _OfflineRuntimeExecutionContext(
        correlation_id=correlation_id,
        pipeline_version_id="offline-production-api-validation",
        runtime_execution_id="offline-production-api-validation",
        channel_id="offline-validation",
        mode=RuntimeMode.REALTIME,
        metadata={"offline_validation": True},
        deterministic_timestamp=model.timestamp or "1970-01-01T00:00:00Z",
    )
    output = (
        StemRotationOptimizerHandler()
        .execute(
            context,
            node,
            {"slice_model": model, "priority": priority},
        )
        .outputs["result"]
    )
    if not isinstance(output, ResultPayload):
        raise TypeError("production optimizer did not emit a ResultPayload")
    return cast(
        StemRotationOptimizationPayload,
        StemRotationOptimizationPayload.model_validate(output.result),
    )


def replay_validation_input(
    source: StemRotationValidationInput,
    *,
    priority_mode: str | None = None,
    config_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    config = {**source.optimizer_config, **dict(config_overrides or {})}
    control = source.priority_control
    if priority_mode is not None and priority_mode != control.priority_mode:
        control = control.model_copy(
            update={
                "priority_source": "FRONTEND",
                "priority_mode": priority_mode,
                "revision": control.revision + 1,
                "updated_by": "offline-replay",
            }
        )
    result = run_runtime_optimization(
        source.model_payload,
        control,
        optimizer_config=config,
    )
    comparison = compare_optimization_results(source.original_result, result)
    warnings = list(source.warnings)
    if config_overrides:
        warnings.append("CONFIGURATION_CHANGED_FOR_REPLAY")
    if priority_mode is not None and priority_mode != source.priority_control.priority_mode:
        warnings.append("PRIORITY_MODE_CHANGED_FOR_REPLAY")
    if source.original_result is not None and (
        source.original_result.get("configuration_hash") != result.configuration_hash
    ):
        warnings.append("CONFIGURATION_HASH_MISMATCH")
    report = {
        "schema_version": STEM_ROTATION_VALIDATION_REPORT_SCHEMA,
        "source_kind": source.source_kind,
        "warnings": list(dict.fromkeys(warnings)),
        "original_result": source.original_result,
        "replayed_result": result.model_dump(mode="json"),
        "difference": comparison,
        "matches_persisted_decision": comparison.get("decision_matches"),
        # Compatibility summary retained for the original replay CLI consumers.
        "priority_source": result.priority_source,
        "priority_mode": result.priority_mode,
        "priority_revision": result.priority_revision,
        "model_hash": result.model_hash,
        "valid": result.result_valid,
        "should_rotate": result.should_rotate,
        "delta_angle_deg": result.delta_angle_deg,
        "gain_mm": result.gain_mm,
        "score_current_mm": result.score_current_mm,
        "score_optimal_mm": result.score_optimal_mm,
        "limiting_x_current_mm": result.limiting_x_current_mm,
        "limiting_x_optimal_mm": result.limiting_x_optimal_mm,
        "reason_code": result.reason_code,
    }
    return report


def compare_optimization_results(
    original: Mapping[str, Any] | None,
    replayed: StemRotationOptimizationPayload,
    *,
    floating_tolerance: float = 1.0e-7,
) -> dict[str, Any]:
    if original is None:
        return {
            "original_available": False,
            "decision_matches": None,
            "delta_angle_deg_difference": None,
            "gain_mm_difference": None,
            "limiting_x_changed": None,
        }
    angle_difference = _numeric_difference(
        replayed.delta_angle_deg,
        original.get("delta_angle_deg"),
    )
    gain_difference = _numeric_difference(replayed.gain_mm, original.get("gain_mm"))
    decision_matches = (
        replayed.result_valid == bool(original.get("result_valid"))
        and replayed.should_rotate == bool(original.get("should_rotate"))
        and _numbers_match(
            replayed.delta_angle_deg,
            original.get("delta_angle_deg"),
            floating_tolerance,
        )
        and _numbers_match(
            replayed.gain_mm,
            original.get("gain_mm"),
            floating_tolerance,
        )
    )
    return {
        "original_available": True,
        "decision_matches": decision_matches,
        "result_valid_changed": replayed.result_valid != bool(original.get("result_valid")),
        "should_rotate_changed": replayed.should_rotate != bool(original.get("should_rotate")),
        "delta_angle_deg_difference": angle_difference,
        "gain_mm_difference": gain_difference,
        "score_current_mm_difference": _numeric_difference(
            replayed.score_current_mm,
            original.get("score_current_mm"),
        ),
        "score_optimal_mm_difference": _numeric_difference(
            replayed.score_optimal_mm,
            original.get("score_optimal_mm"),
        ),
        "limiting_x_changed": (
            replayed.limiting_x_current_mm != original.get("limiting_x_current_mm")
            or replayed.limiting_x_optimal_mm != original.get("limiting_x_optimal_mm")
        ),
        "reason_code_changed": replayed.reason_code != original.get("reason_code"),
    }


def build_diagnostic_package(
    result: StemRotationOptimizationPayload,
    *,
    source_kind: str,
    original_result: Mapping[str, Any] | None = None,
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "schema_version": STEM_ROTATION_DIAGNOSTIC_PACKAGE_SCHEMA,
        "source_kind": source_kind,
        "warnings": list(dict.fromkeys(warnings)),
        "coordinate_system": result.coordinate_system,
        "rotation_axis": result.rotation_axis,
        "software_versions": {
            "algorithm_version": result.algorithm_version,
            "geometry_version": result.geometry_version,
            "preprocessing_version": result.preprocessing_version,
        },
        "priority": {
            "source": result.priority_source,
            "mode": result.priority_mode,
            "axis": result.priority_axis,
            "revision": result.priority_revision,
        },
        "stem_model": result.replay.model_dump(mode="json"),
        "configuration": {
            "optimizer": result.replay.optimizer_config,
            "rotation_constraints": result.replay.rotation_constraints,
            "configuration_hash": result.configuration_hash,
        },
        "quality_metadata": {
            "model_quality": result.model_quality,
            "valid_slice_count": result.valid_slice_count,
            "evaluated_slice_count": result.evaluated_slice_count,
            "fit_diagnostics": [
                item.model_dump(mode="json") for item in result.replay.fit_diagnostics
            ],
        },
        "result": result.model_dump(mode="json"),
        "score_curve": [item.model_dump(mode="json") for item in result.score_curve],
        "hmi_view_model": (
            result.view_model.model_dump(mode="json") if result.view_model is not None else None
        ),
        "original_result": dict(original_result) if original_result is not None else None,
    }


def export_diagnostic_package(
    package: Mapping[str, Any],
    directory: Path,
) -> tuple[Path, Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    package_path = directory / "stem-rotation-diagnostic-package.json"
    slices_path = directory / "stem-slices.csv"
    curve_path = directory / "angle-score-curve.csv"
    package_path.write_text(
        json.dumps(package, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    stem_model = _mapping(package, "stem_model")
    _write_csv(
        slices_path,
        [
            dict(item)
            for item in stem_model.get("processed_slices", [])
            if isinstance(item, Mapping)
        ],
    )
    _write_csv(
        curve_path,
        [dict(item) for item in package.get("score_curve", []) if isinstance(item, Mapping)],
    )
    return package_path, slices_path, curve_path


def add_slice_model_noise(
    model: StemSliceModelPayload,
    *,
    seed: int,
    center_noise_std_mm: float,
    axis_noise_std_mm: float,
    angle_noise_std_deg: float,
) -> StemSliceModelPayload:
    random_source = random.Random(seed)
    slices: list[StemSliceGeometryPayload] = []
    for item in model.processed_slices:
        if not item.valid:
            slices.append(item)
            continue
        values = _geometry_values(item)
        if values is None:
            slices.append(item)
            continue
        cy_mm, cz_mm, a_mm, b_mm, phi_rad = values
        major = a_mm + random_source.gauss(0.0, axis_noise_std_mm)
        minor = b_mm + random_source.gauss(0.0, axis_noise_std_mm)
        slices.append(
            item.model_copy(
                update={
                    "cy_mm": cy_mm + random_source.gauss(0.0, center_noise_std_mm),
                    "cz_mm": cz_mm + random_source.gauss(0.0, center_noise_std_mm),
                    "a_mm": max(major, minor, 5.0),
                    "b_mm": max(min(major, minor), 5.0),
                    "phi_rad": phi_rad
                    + math.radians(random_source.gauss(0.0, angle_noise_std_deg)),
                }
            )
        )
    domain = _domain_model_from_geometry_payloads(slices, model.model_quality)
    return cast(
        StemSliceModelPayload,
        model.model_copy(
            update={
                "processed_slices": slices,
                "model_hash": _model_hash(domain),
            }
        ),
    )


def monte_carlo_robustness(
    source: StemRotationValidationInput,
    *,
    iterations: int = 100,
    seed: int = 20260821,
    center_noise_std_mm: float = 1.0,
    axis_noise_std_mm: float = 1.5,
    angle_noise_std_deg: float = 1.0,
    priority_mode: str | None = None,
    config_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be positive")
    config = {**source.optimizer_config, **dict(config_overrides or {})}
    control = source.priority_control
    if priority_mode is not None:
        control = control.model_copy(
            update={
                "priority_source": "FRONTEND",
                "priority_mode": priority_mode,
                "revision": control.revision + 1,
            }
        )
    angles: list[float] = []
    gains: list[float] = []
    limiting_positions: Counter[str] = Counter()
    rotate_count = 0
    valid_count = 0
    for iteration in range(iterations):
        noisy = add_slice_model_noise(
            source.model_payload,
            seed=seed + iteration,
            center_noise_std_mm=center_noise_std_mm,
            axis_noise_std_mm=axis_noise_std_mm,
            angle_noise_std_deg=angle_noise_std_deg,
        )
        result = run_runtime_optimization(
            noisy,
            control,
            optimizer_config=config,
            correlation_id=f"monte-carlo-{seed}-{iteration}",
        )
        if not result.result_valid:
            continue
        valid_count += 1
        if result.delta_angle_deg is not None:
            angles.append(result.delta_angle_deg)
        if result.gain_mm is not None:
            gains.append(result.gain_mm)
        if result.limiting_x_optimal_mm is not None:
            limiting_positions[f"{result.limiting_x_optimal_mm:.3f}"] += 1
        rotate_count += int(result.should_rotate)
    dominant_count = max(limiting_positions.values(), default=0)
    return {
        "schema_version": STEM_ROTATION_MONTE_CARLO_SCHEMA,
        "source_kind": source.source_kind,
        "iterations": iterations,
        "valid_result_count": valid_count,
        "invalid_result_count": iterations - valid_count,
        "noise": {
            "center_noise_std_mm": center_noise_std_mm,
            "axis_noise_std_mm": axis_noise_std_mm,
            "angle_noise_std_deg": angle_noise_std_deg,
            "seed": seed,
        },
        "delta_angle_deg": _distribution(angles),
        "gain_mm": _distribution(gains),
        "should_rotate_ratio": rotate_count / valid_count if valid_count else 0.0,
        "limiting_x_optimal_frequency": dict(
            sorted(limiting_positions.items(), key=lambda item: (-item[1], item[0]))
        ),
        "limiting_slice_switch_ratio": (1.0 - dominant_count / valid_count if valid_count else 0.0),
    }


def parameter_sweep(
    source: StemRotationValidationInput,
    sweep: Mapping[str, Sequence[int | float]],
    *,
    priority_mode: str | None = None,
) -> dict[str, Any]:
    allowed = {
        "slice_spacing_mm",
        "smoothing_window_slices",
        "coarse_angle_step_deg",
        "fine_angle_step_deg",
        "score_tolerance_mm",
        "min_gain_mm",
        "min_rotation_deg",
    }
    unknown = set(sweep) - allowed
    if unknown:
        raise ValueError(f"unsupported sweep parameters: {sorted(unknown)}")
    control = source.priority_control
    if priority_mode is not None:
        control = control.model_copy(
            update={
                "priority_source": "FRONTEND",
                "priority_mode": priority_mode,
                "revision": control.revision + 1,
            }
        )
    baseline_config = dict(source.optimizer_config)
    cases: list[dict[str, Any]] = [{"label": "baseline", "overrides": {}}]
    for key, values in sweep.items():
        for value in values:
            cases.append({"label": f"{key}={value}", "overrides": {key: value}})
    rows: list[dict[str, Any]] = []
    for case in cases:
        overrides = case["overrides"]
        config = {**baseline_config, **overrides}
        model_payload = source.model_payload
        preprocessing_changed = bool(
            {"slice_spacing_mm", "smoothing_window_slices"} & set(overrides)
        )
        if preprocessing_changed:
            preprocessing_source = source.source_model
            if source.model_payload.raw_slices:
                preprocessing_source = _domain_model_from_geometry_payloads(
                    source.model_payload.raw_slices,
                    source.model_payload.model_quality,
                )
            model_payload = model_payload_from_domain(
                preprocessing_source,
                preprocessing_config=default_preprocessing_config(config),
                session_id=source.model_payload.session_id or "parameter-sweep",
                rotation_reference_view=(
                    source.model_payload.rotation_reference_view or "looking_along_positive_x"
                ),
                positive_rotation=(
                    source.model_payload.positive_rotation or "counter_clockwise_in_y_z_view"
                ),
            )
        started = time.perf_counter()
        result = run_runtime_optimization(
            model_payload,
            control,
            optimizer_config=config,
            correlation_id=f"parameter-sweep-{case['label']}",
        )
        rows.append(
            {
                "label": case["label"],
                "overrides": overrides,
                "result_valid": result.result_valid,
                "should_rotate": result.should_rotate,
                "delta_angle_deg": result.delta_angle_deg,
                "gain_mm": result.gain_mm,
                "limiting_x_current_mm": result.limiting_x_current_mm,
                "limiting_x_optimal_mm": result.limiting_x_optimal_mm,
                "reason_code": result.reason_code,
                "model_quality": result.model_quality,
                "valid_slice_count": result.valid_slice_count,
                "elapsed_ms": (time.perf_counter() - started) * 1_000.0,
                "preprocessing_replayed": preprocessing_changed,
            }
        )
    warnings = list(source.warnings)
    if (
        source.source_kind == "production_optimization_result"
        and not source.model_payload.raw_slices
        and any(row["preprocessing_replayed"] for row in rows)
    ):
        warnings.append("PREPROCESSING_SWEEP_USES_STORED_PROCESSED_SLICES_AS_INPUT")
    return {
        "schema_version": STEM_ROTATION_PARAMETER_SWEEP_SCHEMA,
        "source_kind": source.source_kind,
        "priority_mode": control.priority_mode,
        "baseline_configuration": baseline_config,
        "warnings": list(dict.fromkeys(warnings)),
        "rows": rows,
    }


def _synthetic_diameter(config: SyntheticStemConfig, progress: float) -> float:
    end = config.diameter_end_mm
    if config.case is SyntheticStemCase.TAPER and end is None:
        end = config.diameter_start_mm * 0.72
    end = end if end is not None else config.diameter_start_mm
    return config.diameter_start_mm + progress * (end - config.diameter_start_mm)


def _synthetic_axis_ratio(config: SyntheticStemConfig) -> float:
    if config.case in {SyntheticStemCase.STRAIGHT_ROUND, SyntheticStemCase.CURVED_ROUND}:
        return 1.0
    return config.axis_ratio


def _synthetic_phi_deg(config: SyntheticStemConfig, progress: float) -> float:
    if config.case in {SyntheticStemCase.STRAIGHT_ROUND, SyntheticStemCase.CURVED_ROUND}:
        return 0.0
    variation = config.ellipse_angle_variation_deg
    if config.case is SyntheticStemCase.VARYING_OVALITY_ANGLE and variation == 0:
        variation = 70.0
    return config.ellipse_angle_deg + variation * (progress - 0.5)


def _synthetic_center(
    config: SyntheticStemConfig,
    progress: float,
) -> tuple[float, float]:
    if config.case in {
        SyntheticStemCase.STRAIGHT_ROUND,
        SyntheticStemCase.CENTERED_ELLIPSE,
        SyntheticStemCase.TAPER,
        SyntheticStemCase.LOCAL_BOTTLENECK,
        SyntheticStemCase.VARYING_OVALITY_ANGLE,
    }:
        return (0.0, 0.0)
    direction_variation = config.curvature_direction_variation_deg
    if config.case is SyntheticStemCase.VARYING_CURVATURE_DIRECTION and direction_variation == 0:
        direction_variation = 120.0
    direction_rad = math.radians(
        config.curvature_direction_deg + direction_variation * (progress - 0.5)
    )
    displacement = config.curvature_amplitude_mm * math.sin(math.pi * progress)
    return (
        displacement * math.cos(direction_rad),
        displacement * math.sin(direction_rad),
    )


def _synthetic_gap_indices(
    count: int,
    config: SyntheticStemConfig,
) -> set[int]:
    output: set[int] = set()
    for size, center_fraction in (
        (config.short_gap_slices, 0.33),
        (config.long_gap_slices, 0.72),
    ):
        if size <= 0:
            continue
        center = int(round((count - 1) * center_fraction))
        start = max(1, center - size // 2)
        end = min(count - 1, start + size)
        output.update(range(start, end))
    return output


def _geometry_payloads(
    slices: Iterable[StemSliceGeometry],
) -> list[StemSliceGeometryPayload]:
    output: list[StemSliceGeometryPayload] = []
    for item in slices:
        if not item.valid:
            output.append(
                StemSliceGeometryPayload(
                    x_mm=item.x_mm,
                    quality=0.0,
                    valid=False,
                    reason_codes=list(item.reason_codes),
                )
            )
            continue
        output.append(
            StemSliceGeometryPayload(
                x_mm=item.x_mm,
                cy_mm=item.cy_mm,
                cz_mm=item.cz_mm,
                a_mm=item.a_mm,
                b_mm=item.b_mm,
                phi_rad=item.phi_rad,
                quality=item.quality,
                valid=True,
                interpolated=item.interpolated,
                reason_codes=list(item.reason_codes),
            )
        )
    return output


def _model_payload_from_replay(
    replay: Mapping[str, Any],
    result: Mapping[str, Any],
) -> StemSliceModelPayload:
    slices = [
        StemSliceGeometryPayload.model_validate(item)
        for item in replay.get("processed_slices", [])
        if isinstance(item, Mapping)
    ]
    fit_diagnostics = [
        StemSliceFitDiagnosticsPayload.model_validate(item)
        for item in replay.get("fit_diagnostics", [])
        if isinstance(item, Mapping)
    ]
    raw_slices = [
        StemSliceGeometryPayload.model_validate(item)
        for item in replay.get("raw_slices", [])
        if isinstance(item, Mapping)
    ]
    raw_observations = [
        StemProjectionObservationPayload.model_validate(item)
        for item in replay.get("raw_observations", [])
        if isinstance(item, Mapping)
    ]
    valid_count = sum(item.valid for item in slices)
    valid_raw_count = sum(item.valid for item in raw_slices) or valid_count
    requested_count = max(len(fit_diagnostics), len(raw_slices), len(slices))
    replayed_pose_phase_deg = _optional_float(replay.get("current_pose_phase_deg"))
    return StemSliceModelPayload(
        state="FROZEN" if valid_count else "INVALID",
        valid=valid_count > 0,
        frozen=valid_count > 0,
        session_id=_optional_string(result.get("session_id") or result.get("stem_id")),
        track_id=_optional_string(result.get("track_id")),
        timestamp=_optional_string(result.get("timestamp")),
        current_pose_phase_deg=(replayed_pose_phase_deg or 0.0) if valid_count else None,
        rotation_reference_view=(
            _optional_string(replay.get("rotation_reference_view"))
            or _optional_string(result.get("rotation_reference_view"))
            or "looking_along_positive_x"
            if valid_count
            else None
        ),
        positive_rotation=(
            _optional_string(replay.get("positive_rotation"))
            or _optional_string(result.get("positive_rotation"))
            or "counter_clockwise_in_y_z_view"
            if valid_count
            else None
        ),
        trace_generation=_optional_int(replay.get("trace_generation")),
        process_calibration_hash=_optional_string(replay.get("process_calibration_hash")),
        phase_calibration_id=_optional_string(replay.get("phase_calibration_id")),
        phase_calibration_hash=_optional_string(replay.get("phase_calibration_hash")),
        model_hash=_optional_string(replay.get("model_hash")) if valid_count else None,
        model_quality=float(replay.get("model_quality", 0.0)),
        requested_station_count=requested_count,
        valid_raw_slice_count=valid_raw_count,
        valid_processed_slice_count=valid_count,
        valid_raw_slice_ratio=(
            valid_raw_count / requested_count if requested_count else 0.0
        ),
        raw_observations=raw_observations,
        raw_slices=raw_slices,
        processed_slices=slices,
        fit_diagnostics=fit_diagnostics,
        reason_codes=[
            str(item)
            for item in replay.get("model_reason_codes", result.get("reason_codes", []))
        ],
        metadata={"offline_validation": True, "source": "persisted_result_replay"},
    )


def _domain_model_from_payload(payload: StemSliceModelPayload) -> StemSliceModel:
    return _domain_model_from_geometry_payloads(
        payload.processed_slices,
        payload.model_quality,
    )


def _domain_model_from_geometry_payloads(
    slices: Iterable[StemSliceGeometryPayload],
    model_quality: float,
) -> StemSliceModel:
    geometry: list[StemSliceGeometry] = []
    for item in slices:
        values = _geometry_values(item) if item.valid else None
        if values is None:
            continue
        cy_mm, cz_mm, a_mm, b_mm, phi_rad = values
        geometry.append(
            StemSliceGeometry(
                x_mm=item.x_mm,
                cy_mm=cy_mm,
                cz_mm=cz_mm,
                a_mm=a_mm,
                b_mm=b_mm,
                phi_rad=phi_rad,
                quality=item.quality,
                valid=True,
                interpolated=item.interpolated,
                reason_codes=tuple(item.reason_codes),
            )
        )
    return StemSliceModel(
        slices=tuple(geometry),
        model_quality=model_quality,
    ).validate()


def _geometry_values(
    item: StemSliceGeometryPayload,
) -> tuple[float, float, float, float, float] | None:
    if (
        item.cy_mm is None
        or item.cz_mm is None
        or item.a_mm is None
        or item.b_mm is None
        or item.phi_rad is None
    ):
        return None
    return (item.cy_mm, item.cz_mm, item.a_mm, item.b_mm, item.phi_rad)


def _model_hash(model: StemSliceModel) -> str:
    document = [
        {
            "x_mm": item.x_mm,
            "cy_mm": item.cy_mm,
            "cz_mm": item.cz_mm,
            "a_mm": item.a_mm,
            "b_mm": item.b_mm,
            "phi_rad": item.phi_rad,
            "quality": item.quality,
            "valid": item.valid,
            "interpolated": item.interpolated,
        }
        for item in model.slices
    ]
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _maximum_invalid_gap(slices: Sequence[StemSliceGeometry]) -> float:
    if len(slices) < 2:
        return 0.0
    ordered = sorted(slices, key=lambda item: item.x_mm)
    spacing = statistics.median(
        right.x_mm - left.x_mm for left, right in zip(ordered, ordered[1:], strict=False)
    )
    longest = current = 0
    for item in ordered:
        if item.valid:
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return float(max(0.0, longest * spacing))


def _version_warnings(result: Mapping[str, Any]) -> tuple[str, ...]:
    expected = {
        "algorithm_version": STEM_ROTATION_OPTIMIZER_VERSION,
        "geometry_version": STEM_ROTATION_GEOMETRY_VERSION,
        "preprocessing_version": STEM_SLICE_PREPROCESSING_VERSION,
    }
    return tuple(
        f"SOFTWARE_VERSION_MISMATCH:{key}:{result.get(key)}!={value}"
        for key, value in expected.items()
        if result.get(key) != value
    )


def _extract_optimization_result(
    document: Mapping[str, Any],
) -> dict[str, Any] | None:
    candidates: list[Mapping[str, Any]] = [document]
    for key in ("result", "derived_preview"):
        value = document.get(key)
        if isinstance(value, Mapping):
            candidates.append(value)
    execution = document.get("execution_result")
    if isinstance(execution, Mapping):
        candidates.append(execution)
    for container in (document, execution if isinstance(execution, Mapping) else {}):
        derived = container.get("derived_outputs")
        if not isinstance(derived, list):
            continue
        for entry in derived:
            if not isinstance(entry, Mapping):
                continue
            payload = entry.get("payload")
            if not isinstance(payload, Mapping):
                continue
            value = payload.get("result")
            if isinstance(value, Mapping):
                candidates.append(value)
    for candidate in candidates:
        if candidate.get("schema_version") == "stem.rotation-optimization.v1":
            return dict(candidate)
    return None


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "minimum": None,
            "p05": None,
            "median": None,
            "p95": None,
            "maximum": None,
            "mean": None,
            "standard_deviation": None,
        }
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "minimum": ordered[0],
        "p05": _percentile(ordered, 0.05),
        "median": statistics.median(ordered),
        "p95": _percentile(ordered, 0.95),
        "maximum": ordered[-1],
        "mean": statistics.mean(ordered),
        "standard_deviation": statistics.pstdev(ordered),
    }


def _percentile(ordered: Sequence[float], quantile: float) -> float:
    position = quantile * (len(ordered) - 1)
    left = int(math.floor(position))
    right = int(math.ceil(position))
    if left == right:
        return ordered[left]
    fraction = position - left
    return ordered[left] + fraction * (ordered[right] - ordered[left])


def _numeric_difference(left: object, right: object) -> float | None:
    if isinstance(left, bool) or isinstance(right, bool):
        return None
    if not isinstance(left, int | float) or not isinstance(right, int | float):
        return None
    return float(left) - float(right)


def _numbers_match(left: object, right: object, tolerance: float) -> bool:
    if left is None or right is None:
        return left is None and right is None
    difference = _numeric_difference(left, right)
    return difference is not None and abs(difference) <= tolerance


def _mapping(document: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = document.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"field {key} must be an object")
    return dict(value)


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("optional numeric value must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("optional numeric value must be finite")
    return result


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("optional integer value must be numeric")
    result = float(value)
    if not math.isfinite(result) or not result.is_integer():
        raise ValueError("optional integer value must be a finite whole number")
    return int(result)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fields = sorted({str(key) for row in rows for key in row})
    buffer = io.StringIO(newline="")
    if fields:
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: (
                        json.dumps(row.get(key), sort_keys=True)
                        if isinstance(row.get(key), list | dict)
                        else row.get(key)
                    )
                    for key in fields
                }
            )
    path.write_text(buffer.getvalue(), encoding="utf-8")
