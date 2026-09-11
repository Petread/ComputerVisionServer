from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from cv_contracts import RealtimeTemporalSequenceRequest, TemporalSequenceFrame
from cv_contracts.runtime import RuntimeNodeSpec
from cv_status import TaskType

from cv_runtime.context import NodeInputs, RuntimeExecutionContext
from cv_runtime.errors import RuntimeNodeExecutionError
from cv_runtime.handlers import NodeExecutionResult
from cv_runtime.payloads import (
    OverlayPayload,
    OverlayPrimitive,
    ResultPayload,
    TemporalPredictionPayload,
    TemporalSequencePayload,
)


class TemporalSequenceInferenceHandler:
    node_type = "inference.temporal_sequence"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        sequence = inputs.get("sequence")
        if not isinstance(sequence, TemporalSequencePayload) or not sequence.frames:
            return NodeExecutionResult(
                outputs={
                    "prediction": _invalid_prediction(
                        sequence if isinstance(sequence, TemporalSequencePayload) else None,
                        "temporal_sequence_input_missing",
                    )
                }
            )
        if node.logical_model_ref is None:
            return NodeExecutionResult(
                outputs={
                    "prediction": _invalid_prediction(
                        sequence,
                        "temporal_model_reference_missing",
                    )
                }
            )
        try:
            binding = context.resolve_model(node.logical_model_ref)
            if binding.task_type != TaskType.TEMPORAL_SEQUENCE:
                raise RuntimeNodeExecutionError(
                    code="temporal_model_task_mismatch",
                    message="The resolved model is not a temporal_sequence model.",
                )
            if context.inference_client is None:
                raise RuntimeNodeExecutionError(
                    code="inference_client_missing",
                    message="Temporal inference requires the configured inference gateway client.",
                )
            timeout_ms = _bounded_int(
                (node.config or {}).get(
                    "timeout_ms",
                    context.metadata.get("default_inference_timeout_ms", 1500),
                ),
                default=1500,
                minimum=50,
                maximum=30_000,
            )
            response = context.inference_client.temporal_sequence(
                RealtimeTemporalSequenceRequest(
                    deployment_target=binding.deployment_target,
                    deployment_release_id=binding.deployment_release_id,
                    timeout_ms=timeout_ms,
                    sequence_id=sequence.sequence_id,
                    track_id=sequence.track_id,
                    feature_set_id=sequence.feature_set_id,
                    feature_schema_version=sequence.feature_schema_version,
                    rotation_schema_version=sequence.rotation_schema_version,
                    calibration_schema_version=sequence.calibration_schema_version,
                    rotation_calibration_id=sequence.rotation_calibration_id,
                    rotation_calibration_hash=sequence.rotation_calibration_hash,
                    rotation_viewpoint_convention=sequence.rotation_viewpoint_convention,
                    positive_rotation_direction=sequence.positive_rotation_direction,
                    frames=[
                        TemporalSequenceFrame(
                            frame_index=item.frame_index,
                            timestamp_seconds=item.timestamp_seconds,
                            source_timestamp=item.source_timestamp,
                            values=item.values,
                        )
                        for item in sequence.frames
                    ],
                    current_frame_index=int(sequence.current_frame_index or 0),
                    decision_frame_index=sequence.decision_frame_index,
                    trace_continuous=sequence.trace_continuous,
                    calibration_valid=sequence.calibration_valid,
                )
            )
        except RuntimeNodeExecutionError as exc:
            return NodeExecutionResult(
                outputs={
                    "prediction": _invalid_prediction(
                        sequence,
                        exc.error.code,
                    )
                }
            )
        except (TypeError, ValueError):
            return NodeExecutionResult(
                outputs={
                    "prediction": _invalid_prediction(
                        sequence,
                        "temporal_sequence_contract_invalid",
                    )
                }
            )
        return NodeExecutionResult(
            outputs={
                "prediction": TemporalPredictionPayload(
                    valid=True,
                    sequence_id=response.sequence_id,
                    track_id=response.track_id,
                    sequence_length=response.sequence_length,
                    last_frame_index=response.last_frame_index,
                    decision_frame_index=response.decision_frame_index,
                    feature_set_id=response.feature_set_id,
                    feature_schema_digest=response.feature_schema_digest,
                    scaler_sha256=response.scaler_sha256,
                    snapshot_id=response.snapshot_id,
                    feature_schema_version=response.feature_schema_version,
                    rotation_schema_version=response.rotation_schema_version,
                    calibration_schema_version=response.calibration_schema_version,
                    rotation_calibration_id=response.rotation_calibration_id,
                    rotation_calibration_hash=response.rotation_calibration_hash,
                    rotation_viewpoint_convention=response.rotation_viewpoint_convention,
                    positive_rotation_direction=response.positive_rotation_direction,
                    bow_relevance_probability=response.bow_relevance_probability,
                    target_observed_probability=response.target_observed_probability,
                    angle_sin_2e=response.angle_sin_2e,
                    angle_cos_2e=response.angle_cos_2e,
                    signed_correction_deg=response.signed_correction_deg,
                    raw_angle_confidence=response.raw_angle_confidence,
                    angle_confidence=response.angle_confidence,
                    confidence_calibration_valid=response.confidence_calibration_valid,
                    final_quality_probabilities=response.final_quality_probabilities,
                    deadband_deg=response.deadband_deg,
                    estimate_method=response.estimate_method,
                    model_family_slug=response.route.model_family_slug,
                    model_version_id=response.route.model_version_id,
                    deployment_release_id=response.route.deployment_release_id,
                    reason_codes=response.reason_codes,
                    timing=response.timing.model_dump(mode="json"),
                )
            }
        )


class StemOrientationModelDecisionHandler:
    node_type = "analysis.stem_orientation_model_decision"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        baseline = inputs.get("baseline")
        prediction = inputs.get("prediction")
        baseline_result = (
            dict(baseline.result)
            if isinstance(baseline, ResultPayload)
            and baseline.result.get("schema_version") == "stem.orientation.v2"
            else {}
        )
        state, frozen = _model_decision_state(context, node, baseline_result)
        if frozen is not None:
            verification = baseline_result.get("post_rotation_verification")
            if verification is not None:
                frozen["post_rotation_verification"] = deepcopy(verification)
            return _model_result_outputs(frozen)
        if not baseline_result:
            result = _manual_result(
                context,
                baseline_result,
                prediction if isinstance(prediction, TemporalPredictionPayload) else None,
                ["stem_orientation_baseline_missing"],
            )
            return _model_result_outputs(result)
        prediction_payload = (
            prediction if isinstance(prediction, TemporalPredictionPayload) else None
        )
        reasons = list(baseline_result.get("reason_codes") or [])
        decision_ready = bool(baseline_result.get("decision_ready"))
        if prediction_payload is None:
            reasons.append("temporal_model_prediction_missing")
        elif not prediction_payload.valid:
            reasons.extend(prediction_payload.reason_codes or ["temporal_model_prediction_invalid"])
            reasons.append("temporal_model_prediction_invalid")
        else:
            reasons.extend(prediction_payload.reason_codes)
            if prediction_payload.sequence_id != baseline_result.get("session_id"):
                reasons.append("temporal_model_session_mismatch")
            if prediction_payload.track_id != baseline_result.get("track_id"):
                reasons.append("temporal_model_track_mismatch")
            decision_frame = baseline_result.get("decision_frame")
            baseline_frame = (
                decision_frame.get("frame_sequence")
                if isinstance(decision_frame, Mapping)
                else None
            )
            if decision_ready and prediction_payload.last_frame_index != baseline_frame:
                reasons.append("temporal_model_decision_frame_mismatch")
            if decision_ready and prediction_payload.decision_frame_index != baseline_frame:
                reasons.append("temporal_model_decision_frame_mismatch")
            direction = baseline_result.get("direction_provenance") or {}
            if (
                prediction_payload.rotation_viewpoint_convention
                != direction.get("rotation_viewpoint_convention")
            ):
                reasons.append("temporal_model_viewpoint_mismatch")
            if (
                prediction_payload.positive_rotation_direction
                != direction.get("positive_rotation_direction")
            ):
                reasons.append("temporal_model_positive_direction_mismatch")
            versions = baseline_result.get("versions") or {}
            if prediction_payload.feature_schema_version != versions.get(
                "temporal_feature_schema_version",
                versions.get("feature_schema_version"),
            ):
                reasons.append("temporal_model_feature_schema_mismatch")
            if prediction_payload.rotation_schema_version != versions.get(
                "rotation_schema_version"
            ):
                reasons.append("temporal_model_rotation_schema_mismatch")
            if (
                prediction_payload.calibration_schema_version
                != versions.get("rotation_calibration_version")
            ):
                reasons.append("temporal_model_calibration_schema_mismatch")
            if (
                prediction_payload.rotation_calibration_id
                != versions.get("rotation_calibration_id")
                or prediction_payload.rotation_calibration_hash
                != versions.get("rotation_calibration_hash")
            ):
                reasons.append("temporal_model_rotation_calibration_mismatch")
            if not prediction_payload.confidence_calibration_valid:
                reasons.append("temporal_model_confidence_calibration_invalid")

        config = node.config or {}
        bow_probability = _probability(prediction_payload, "bow_relevance_probability")
        target_probability = _probability(prediction_payload, "target_observed_probability")
        angle_confidence = _probability(prediction_payload, "angle_confidence")
        minimum_bow = _bounded_float(config.get("minimum_bow_relevance_probability"), 0.6)
        maximum_straight = _bounded_float(
            config.get("maximum_bow_relevance_probability_for_no_rotation"), 0.35
        )
        minimum_target = _bounded_float(config.get("minimum_target_observed_probability"), 0.6)
        minimum_confidence = _bounded_float(config.get("minimum_angle_confidence"), 0.5)
        candidate = prediction_payload.signed_correction_deg if prediction_payload else None
        status = "manual_required"
        correction: float | None = None
        if not decision_ready:
            reasons.append("temporal_model_decision_not_ready")
            status = "observing"
        elif reasons and any(_blocking_reason(item) for item in reasons):
            status = "manual_required"
        elif bow_probability is None or (
            bow_probability > maximum_straight and bow_probability < minimum_bow
        ):
            reasons.append("temporal_model_bow_relevance_ambiguous")
        elif bow_probability <= maximum_straight:
            correction = 0.0
            status = "no_rotation"
        elif target_probability is None or target_probability < minimum_target:
            reasons.append("temporal_model_target_not_observed")
        elif angle_confidence is None or angle_confidence < minimum_confidence:
            reasons.append("temporal_model_high_uncertainty")
        elif candidate is None:
            reasons.append("temporal_model_angle_missing")
        else:
            baseline_angle = baseline_result.get("recommended_rotation_deg_signed")
            maximum_disagreement = _bounded_nonnegative(
                config.get("maximum_baseline_disagreement_deg"), 20.0
            )
            if isinstance(baseline_angle, int | float) and _angle_distance(
                float(candidate), float(baseline_angle)
            ) > maximum_disagreement:
                reasons.append("temporal_model_baseline_disagreement")
            else:
                deadband = float(prediction_payload.deadband_deg or 0.0)
                correction = 0.0 if abs(float(candidate)) <= deadband else float(candidate)
                status = "no_rotation" if correction == 0.0 else "pre_entry_angle_ready"

        if status == "manual_required":
            correction = None
        result = dict(baseline_result)
        result.update(
            {
                "decision_ready": decision_ready,
                "decision_frame_index": _decision_frame_index(baseline_result),
                "recommended_rotation_deg_signed": correction,
                "recommended_rotation_deg_abs": abs(correction) if correction is not None else None,
                "recommended_direction": _recommended_direction(
                    correction,
                    prediction_payload.positive_rotation_direction
                    if prediction_payload
                    else None,
                ),
                "rotation_viewpoint_convention": (
                    prediction_payload.rotation_viewpoint_convention
                    if prediction_payload
                    else (baseline_result.get("direction_provenance") or {}).get(
                        "rotation_viewpoint_convention"
                    )
                ),
                "bow_relevance_probability": bow_probability,
                "target_observed_probability": target_probability,
                "angle_confidence": angle_confidence or 0.0,
                "confidence": angle_confidence or 0.0,
                "final_quality_probabilities": (
                    prediction_payload.final_quality_probabilities
                    if prediction_payload
                    else {}
                ),
                "estimate_method": (
                    str(config.get("estimate_method") or prediction_payload.estimate_method)
                    if prediction_payload and prediction_payload.estimate_method
                    else "deterministic"
                ),
                "reason_codes": list(dict.fromkeys(reasons)),
                "deterministic_baseline": baseline_result,
                "post_rotation_verification": baseline_result.get(
                    "post_rotation_verification"
                ),
            }
        )
        decision = dict(result.get("decision") or {})
        decision.update(
            {
                "ready": decision_ready,
                "status": status,
                "valid_until": _valid_until(
                    decision.get("timestamp"),
                    _bounded_nonnegative(config.get("validity_seconds"), 2.0),
                )
                if decision_ready
                else None,
            }
        )
        result["decision"] = decision
        recommendation = dict(result.get("recommendation") or {})
        recommendation.update(
            {
                "rotate_required": correction is not None and correction != 0.0,
                "signed_rotation_deg": correction,
                "absolute_rotation_deg": abs(correction) if correction is not None else None,
                "direction": result["recommended_direction"],
                "deadband_deg": prediction_payload.deadband_deg if prediction_payload else None,
            }
        )
        result["recommendation"] = recommendation
        versions = dict(result.get("versions") or {})
        if prediction_payload is not None:
            versions.update(
                {
                    "temporal_model_version_id": prediction_payload.model_version_id,
                    "temporal_model_family_slug": prediction_payload.model_family_slug,
                    "temporal_deployment_release_id": prediction_payload.deployment_release_id,
                    "temporal_snapshot_id": prediction_payload.snapshot_id,
                    "temporal_feature_set_id": prediction_payload.feature_set_id,
                    "temporal_feature_schema_digest": prediction_payload.feature_schema_digest,
                    "temporal_scaler_sha256": prediction_payload.scaler_sha256,
                }
            )
        result["versions"] = versions
        result["model_prediction"] = (
            prediction_payload.model_dump(mode="json") if prediction_payload else None
        )
        result["valid_until"] = decision.get("valid_until")
        result["validity_seconds"] = (
            _bounded_nonnegative(config.get("validity_seconds"), 2.0)
            if decision_ready
            else None
        )
        if decision_ready:
            state["frozen_result"] = deepcopy(result)
        return _model_result_outputs(result)


def _invalid_prediction(
    sequence: TemporalSequencePayload | None,
    reason: str,
) -> TemporalPredictionPayload:
    return TemporalPredictionPayload(
        valid=False,
        sequence_id=sequence.sequence_id if sequence else "unavailable",
        track_id=sequence.track_id if sequence else None,
        sequence_length=len(sequence.frames) if sequence else 0,
        last_frame_index=sequence.current_frame_index if sequence else None,
        decision_frame_index=sequence.decision_frame_index if sequence else None,
        feature_set_id=sequence.feature_set_id if sequence else None,
        feature_schema_version=sequence.feature_schema_version if sequence else None,
        rotation_schema_version=sequence.rotation_schema_version if sequence else None,
        calibration_schema_version=sequence.calibration_schema_version if sequence else None,
        rotation_calibration_id=sequence.rotation_calibration_id if sequence else None,
        rotation_calibration_hash=sequence.rotation_calibration_hash if sequence else None,
        rotation_viewpoint_convention=(
            sequence.rotation_viewpoint_convention if sequence else None
        ),
        positive_rotation_direction=(sequence.positive_rotation_direction if sequence else None),
        reason_codes=[reason],
    )


def _model_decision_state(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    baseline: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    raw_state = context.state.setdefault(node.node_key, {})
    if not isinstance(raw_state, dict):
        raw_state = {}
        context.state[node.node_key] = raw_state
    identity = (
        str(baseline.get("session_id") or ""),
        str(baseline.get("track_id") or ""),
    )
    if identity == ("", ""):
        raw_state.clear()
        return raw_state, None
    if tuple(raw_state.get("identity") or ()) != identity:
        raw_state.clear()
        raw_state["identity"] = list(identity)
        return raw_state, None
    frozen = raw_state.get("frozen_result")
    return raw_state, deepcopy(frozen) if isinstance(frozen, Mapping) else None


def _manual_result(
    context: RuntimeExecutionContext,
    baseline: dict[str, Any],
    prediction: TemporalPredictionPayload | None,
    reasons: list[str],
) -> dict[str, Any]:
    direction = baseline.get("direction_provenance") or {}
    versions = dict(baseline.get("versions") or {})
    if prediction is not None:
        versions.update(
            {
                "temporal_model_version_id": prediction.model_version_id,
                "temporal_model_family_slug": prediction.model_family_slug,
                "temporal_deployment_release_id": prediction.deployment_release_id,
                "temporal_snapshot_id": prediction.snapshot_id,
                "temporal_feature_set_id": prediction.feature_set_id,
                "temporal_feature_schema_digest": prediction.feature_schema_digest,
                "temporal_scaler_sha256": prediction.scaler_sha256,
            }
        )
    return {
        **baseline,
        "schema_version": "stem.orientation.v2",
        "channel_id": context.channel_id,
        "pipeline_version_id": context.pipeline_version_id,
        "decision_ready": bool(baseline.get("decision_ready")),
        "decision_frame_index": _decision_frame_index(baseline),
        "recommended_rotation_deg_signed": None,
        "recommended_rotation_deg_abs": None,
        "recommended_direction": "manual_required",
        "rotation_viewpoint_convention": (
            prediction.rotation_viewpoint_convention
            if prediction
            else direction.get("rotation_viewpoint_convention")
        ),
        "bow_relevance_probability": _probability(
            prediction, "bow_relevance_probability"
        ),
        "target_observed_probability": _probability(
            prediction, "target_observed_probability"
        ),
        "angle_confidence": _probability(prediction, "angle_confidence") or 0.0,
        "final_quality_probabilities": (
            prediction.final_quality_probabilities if prediction else {}
        ),
        "estimate_method": (
            prediction.estimate_method
            if prediction is not None and prediction.estimate_method
            else "deterministic"
        ),
        "reason_codes": reasons,
        "deterministic_baseline": baseline or None,
        "post_rotation_verification": baseline.get("post_rotation_verification"),
        "versions": versions,
        "model_prediction": (
            prediction.model_dump(mode="json") if prediction is not None else None
        ),
        "valid_until": None,
        "validity_seconds": None,
    }


def _model_result_outputs(result: dict[str, Any]) -> NodeExecutionResult:
    label = _model_overlay_label(result)
    return NodeExecutionResult(
        outputs={
            "result": ResultPayload(result=result),
            "overlay": OverlayPayload(
                primitives=[
                    OverlayPrimitive(
                        primitive_type="label",
                        layer="stem_temporal_model_decision",
                        label=label,
                        confidence=float(result.get("angle_confidence") or 0.0),
                        geometry={"anchor": "top_left"},
                        metadata={
                            "schema_version": "stem.orientation.v2",
                            "estimate_method": result.get("estimate_method"),
                            "reason_codes": list(result.get("reason_codes") or []),
                            "rotation_viewpoint_convention": result.get(
                                "rotation_viewpoint_convention"
                            ),
                        },
                    )
                ],
                metadata={
                    "schema_version": "stem.orientation.v2",
                    "decision_ready": bool(result.get("decision_ready")),
                },
            ),
        }
    )


def _model_overlay_label(result: Mapping[str, Any]) -> str:
    if not bool(result.get("decision_ready")):
        return "Vor-Einzug-Modell: Beobachtung läuft"
    signed = result.get("recommended_rotation_deg_signed")
    if not isinstance(signed, int | float):
        return "Keine sichere Winkelentscheidung – Bediener erforderlich"
    confidence = f"{float(result.get('angle_confidence') or 0.0):.2f}".replace(".", ",")
    if float(signed) == 0.0:
        return f"Keine Drehung erforderlich – Konfidenz {confidence}"
    direction = result.get("recommended_direction")
    direction_label = (
        "im Uhrzeigersinn"
        if direction == "clockwise_viewed_from_configured_log_end"
        else "gegen den Uhrzeigersinn"
    )
    angle = f"{abs(float(signed)):.1f}".replace(".", ",")
    return f"Drehe {angle}° {direction_label} – Konfidenz {confidence}"


def _probability(payload: TemporalPredictionPayload | None, name: str) -> float | None:
    value = getattr(payload, name, None) if payload else None
    return float(value) if isinstance(value, int | float) else None


def _blocking_reason(reason: str) -> bool:
    if reason == "decision_line_missed_first_roller_reached":
        return True
    return reason.startswith("temporal_model_") and reason not in {
        "temporal_model_decision_not_ready",
    }


def _angle_distance(first: float, second: float) -> float:
    return abs((first - second + 90.0) % 180.0 - 90.0)


def _recommended_direction(correction: float | None, positive: str | None) -> str:
    if correction is None:
        return "manual_required"
    if correction == 0.0:
        return "no_rotation"
    positive_is_clockwise = str(positive or "").startswith("clockwise_")
    clockwise = positive_is_clockwise if correction > 0 else not positive_is_clockwise
    return (
        "clockwise_viewed_from_configured_log_end"
        if clockwise
        else "counter_clockwise_viewed_from_configured_log_end"
    )


def _decision_frame_index(result: Mapping[str, Any]) -> int | None:
    frame = result.get("decision_frame")
    value = frame.get("frame_sequence") if isinstance(frame, Mapping) else None
    return int(value) if isinstance(value, int) else None


def _valid_until(timestamp: object, seconds: float) -> str | None:
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (value + timedelta(seconds=seconds)).isoformat()


def _bounded_float(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, 0.0), 1.0)


def _bounded_nonnegative(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, 0.0)


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


inference_temporal_sequence = TemporalSequenceInferenceHandler()
analysis_stem_orientation_model_decision = StemOrientationModelDecisionHandler()
