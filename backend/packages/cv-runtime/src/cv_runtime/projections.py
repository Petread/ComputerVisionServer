from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cv_contracts.edge import EdgePlantResponsePolicy
from cv_contracts.runtime import (
    RuntimeDecisionRule,
    RuntimeNodeSpec,
    RuntimeOutputPolicy,
    RuntimeProjectionAggregate,
    RuntimeProjectionField,
    RuntimeProjectionFilter,
    RuntimeResultProjection,
    RuntimeValueSelector,
)
from cv_status import RuntimeNodeCategory

from cv_runtime.context import RuntimeExecutionContext
from cv_runtime.payloads import (
    AnomalyPayload,
    AnyRuntimePayload,
    ClassificationPayload,
    ClearanceZonesPayload,
    CoordinateFramePayload,
    CvEdgesPayload,
    DetectionPayload,
    EventsPayload,
    FeatureMeasurementsPayload,
    FreeRegionPayload,
    MeasurementsPayload,
    PositionsPayload,
    QualityDecisionPayload,
    ReferenceEdgesPayload,
    RegionSetPayload,
    ResultPayload,
    SegmentPayload,
    StemCurvaturePhaseSolutionPayload,
    StemHorizontalCurvaturePayload,
    StemSurfacePhasePayload,
)


@dataclass(frozen=True)
class RuntimeProjectionEvaluation:
    output_node_key: str
    by_destination: dict[str, dict[str, Any]]
    projections: list[dict[str, Any]] = field(default_factory=list)

    @property
    def response(self) -> dict[str, Any]:
        return dict(self.by_destination.get("response") or {})

    @property
    def history(self) -> dict[str, Any]:
        return dict(self.by_destination.get("history") or {})


_MISSING = object()


def edge_http_response_policy(nodes: list[RuntimeNodeSpec]) -> EdgePlantResponsePolicy:
    """All Result outputs share one HTTP envelope; legacy graphs keep the full report."""
    policies = [
        EdgePlantResponsePolicy(
            mode=node.config.get("edge_response_mode", "full"),
            metadata_fields=node.config.get("edge_response_metadata_fields", []),
        )
        for node in nodes
        if node.node_type == "output.result"
    ]
    if policies and any(policy != policies[0] for policy in policies[1:]):
        raise ValueError("All Result output nodes must use the same Edge HTTP response settings.")
    return policies[0] if policies else EdgePlantResponsePolicy()


def normalize_output_result_config(
    config: dict[str, Any] | None,
    *,
    output_node_key: str | None = None,
) -> dict[str, Any]:
    normalized = dict(config or {})
    if normalized.get("projections") or normalized.get("output_policy"):
        return normalized

    projections: list[dict[str, Any]] = []
    mappings = normalized.get("result_fields") or normalized.get("response_fields")
    if isinstance(mappings, list):
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            source_field = str(mapping.get("source_field") or "").strip()
            output_key = str(
                mapping.get("output_key") or mapping.get("response_key") or source_field
            ).strip()
            if not source_field or not output_key:
                continue
            source = {"path": output_key}
            if output_node_key:
                source["node_key"] = output_node_key
            projection: dict[str, Any] = {
                "key": output_key,
                "source": source,
                "destinations": ["response", "history"],
                "required": bool(mapping.get("required", False)),
            }
            if mapping.get("default_value") is not None:
                projection["default_value"] = mapping.get("default_value")
            projections.append(projection)

    if not projections:
        for field_name in _field_list(normalized.get("fields")):
            source = {"path": field_name}
            if output_node_key:
                source["node_key"] = output_node_key
            projections.append(
                {
                    "key": field_name,
                    "source": source,
                    "destinations": ["response", "history"],
                }
            )

    if projections and bool(normalized.get("include_decision", True)):
        existing_keys = {str(item.get("key")) for item in projections if isinstance(item, dict)}
        for decision_key in ("result", "decision"):
            if decision_key in existing_keys:
                continue
            source = {"path": decision_key}
            if output_node_key:
                source["node_key"] = output_node_key
            projections.append(
                {
                    "key": decision_key,
                    "source": source,
                    "destinations": ["response", "history"],
                }
            )

    if projections:
        normalized["projections"] = projections
    return normalized


def output_policy_from_config(
    config: dict[str, Any] | None,
    *,
    output_node_key: str | None = None,
) -> RuntimeOutputPolicy | None:
    normalized = normalize_output_result_config(config, output_node_key=output_node_key)
    raw_policy = normalized.get("output_policy")
    if isinstance(raw_policy, dict):
        return RuntimeOutputPolicy.model_validate(raw_policy)
    projections = normalized.get("projections")
    if not isinstance(projections, list) or not projections:
        return None
    return RuntimeOutputPolicy(
        projections=[RuntimeResultProjection.model_validate(item) for item in projections],
        include_runtime_metadata=bool(normalized.get("include_runtime_metadata", True)),
    )


def evaluate_output_projections(
    graph_nodes: list[RuntimeNodeSpec],
    executed_payloads: list[tuple[RuntimeNodeSpec, str, AnyRuntimePayload]],
    context: RuntimeExecutionContext,
) -> RuntimeProjectionEvaluation | None:
    policies = _projection_policies(graph_nodes)
    if not policies:
        return None

    entries = _projection_entries(executed_payloads)
    by_destination: dict[str, dict[str, Any]] = {}
    projection_entries: list[dict[str, Any]] = []
    first_output_node_key = policies[0][0].node_key

    for output_node, policy in policies:
        node_config = output_node.config or {}
        projected: dict[str, Any] = {}
        for projection in policy.projections:
            value = _evaluate_projection(projection, entries)
            if value is _MISSING:
                if projection.default_value is not None:
                    value = projection.default_value
                elif projection.required:
                    value = None
                else:
                    continue
            if projection.decision is not None:
                value = _decision_value(value, projection.decision)
            projected[projection.key] = value
            destinations = projection.destinations or policy.default_destinations
            for destination in destinations:
                by_destination.setdefault(destination, {})[projection.key] = value
            projection_entries.append(
                {
                    "node_key": output_node.node_key,
                    "projection_key": projection.key,
                    "label": projection.label,
                    "destinations": list(destinations),
                    "value": value,
                }
            )

        wrap_key = str(node_config.get("wrap_key") or "").strip()
        if wrap_key and projected:
            for destination in list(by_destination):
                selected = {
                    key: value
                    for key, value in by_destination[destination].items()
                    if key in projected
                }
                if selected:
                    by_destination[destination] = {wrap_key: selected}

        if policy.include_runtime_metadata and "history" in by_destination:
            history = by_destination["history"]
            history.setdefault("correlation_id", context.correlation_id)
            history.setdefault("channel_id", context.channel_id)
            history.setdefault("pipeline_version_id", context.pipeline_version_id)

    return RuntimeProjectionEvaluation(
        output_node_key=first_output_node_key,
        by_destination=by_destination,
        projections=projection_entries,
    )


def _projection_policies(
    graph_nodes: list[RuntimeNodeSpec],
) -> list[tuple[RuntimeNodeSpec, RuntimeOutputPolicy]]:
    policies: list[tuple[RuntimeNodeSpec, RuntimeOutputPolicy]] = []
    for node in graph_nodes:
        if node.node_type != "output.result":
            continue
        policy = output_policy_from_config(node.config or {}, output_node_key=node.node_key)
        if policy is not None:
            policies.append((node, policy))
    return policies


def _evaluate_projection(
    projection: RuntimeResultProjection,
    entries: list[dict[str, Any]],
) -> Any:
    selected = _select_values(projection.source, entries)
    if selected is _MISSING:
        return _MISSING
    items = _as_items(selected)
    for item_filter in projection.filters:
        items = [item for item in items if _filter_matches(item, item_filter)]
    if not items and _selected_was_collection(selected):
        if projection.aggregate.op == "count":
            return 0
        return [] if projection.aggregate.op == "list" else _MISSING
    if projection.fields:
        return _aggregate_projected_fields(items, projection.fields, projection.aggregate)
    return _aggregate_values(items, projection.aggregate, _selected_was_collection(selected))


def _select_values(selector: RuntimeValueSelector, entries: list[dict[str, Any]]) -> Any:
    candidates = []
    for entry in entries:
        if selector.node_key and entry.get("node_key") != selector.node_key:
            continue
        if selector.output_port and entry.get("output_port") != selector.output_port:
            continue
        candidates.append(entry)
    if not candidates:
        return _MISSING

    values = []
    for entry in candidates:
        value = entry if not selector.path else _lookup_path(entry, selector.path)
        if value is _MISSING:
            continue
        values.append(value)
    if not values:
        return _MISSING
    if len(values) == 1:
        return values[0]
    merged: list[Any] = []
    for value in values:
        if isinstance(value, list):
            merged.extend(value)
        else:
            merged.append(value)
    return merged


def _aggregate_projected_fields(
    items: list[Any],
    fields: list[RuntimeProjectionField],
    aggregate: RuntimeProjectionAggregate,
) -> Any:
    mapped = [_map_fields(item, fields) for item in items]
    mapped = [item for item in mapped if item is not _MISSING]
    if aggregate.op == "count":
        return len(mapped)
    if aggregate.op == "first":
        return mapped[0] if mapped else _MISSING
    if aggregate.op in {"none", "list"}:
        return mapped
    return _aggregate_values(mapped, aggregate, selected_was_collection=True)


def _map_fields(item: Any, fields: list[RuntimeProjectionField]) -> Any:
    output: dict[str, Any] = {}
    for field_spec in fields:
        value = _lookup_path(item, field_spec.source_path)
        if value is _MISSING:
            if field_spec.default_value is not None:
                value = field_spec.default_value
            elif field_spec.required:
                value = None
            else:
                continue
        output[field_spec.output_key] = value
    return output if output else _MISSING


def _aggregate_values(
    items: list[Any],
    aggregate: RuntimeProjectionAggregate,
    selected_was_collection: bool,
) -> Any:
    values = [_lookup_path(item, aggregate.path) for item in items] if aggregate.path else items
    values = [value for value in values if value is not _MISSING]
    if aggregate.op == "count":
        return len(values)
    if aggregate.op == "list":
        return values
    if aggregate.op == "first":
        return values[0] if values else _MISSING
    if aggregate.op == "any":
        return any(bool(value) for value in values)
    if aggregate.op == "all":
        return bool(values) and all(bool(value) for value in values)
    if aggregate.op in {"sum", "avg", "min", "max"}:
        numbers = [_coerce_number(value) for value in values]
        numbers = [value for value in numbers if value is not None]
        if not numbers:
            return _MISSING
        if aggregate.op == "sum":
            return sum(numbers)
        if aggregate.op == "avg":
            return sum(numbers) / len(numbers)
        if aggregate.op == "min":
            return min(numbers)
        if aggregate.op == "max":
            return max(numbers)
    if selected_was_collection:
        return values
    return values[0] if values else _MISSING


def _filter_matches(item: Any, item_filter: RuntimeProjectionFilter) -> bool:
    value = _lookup_path(item, item_filter.path) if item_filter.path else item
    return _compare(value, item_filter.op, item_filter.value)


def _decision_value(value: Any, decision: RuntimeDecisionRule) -> Any:
    if _compare(value, decision.op, decision.value):
        return decision.when_true
    return decision.when_false


def _compare(value: Any, op: str, expected: Any) -> bool:
    exists = value is not _MISSING and value is not None
    if op == "exists":
        return exists
    if op == "not_exists":
        return not exists
    if op == "truthy":
        return bool(value) if exists else False
    if op == "falsy":
        return not bool(value) if exists else True
    if not exists:
        return False
    if op == "eq":
        return value == expected
    if op == "ne":
        return value != expected
    if op == "contains":
        return expected in value if isinstance(value, str | list | tuple | set | dict) else False
    if op == "in":
        return value in expected if isinstance(expected, list | tuple | set) else False
    if op == "not_in":
        return value not in expected if isinstance(expected, list | tuple | set) else True
    left = _coerce_number(value)
    right = _coerce_number(expected)
    if left is None or right is None:
        return False
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    return False


def _projection_entries(
    executed_payloads: list[tuple[RuntimeNodeSpec, str, AnyRuntimePayload]],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for node, port, payload in executed_payloads:
        entry = {
            "node_key": node.node_key,
            "node_type": node.node_type,
            "node_category": node.node_category.value
            if isinstance(node.node_category, RuntimeNodeCategory)
            else str(node.node_category),
            "output_port": port,
            "payload_type": payload.payload_type,
            "payload": payload.model_dump(mode="json"),
        }
        entry.update(_payload_projection_values(payload))
        entries.append(entry)
    return entries


def _payload_projection_values(payload: AnyRuntimePayload) -> dict[str, Any]:
    if isinstance(payload, ResultPayload):
        values = dict(payload.result)
        values["decision"] = payload.decision
        if payload.decision is not None:
            values.setdefault("result", payload.decision)
        return values
    if isinstance(payload, DetectionPayload):
        count = len(payload.objects or payload.detections)
        detections = [item.model_dump(mode="json") for item in payload.detections]
        objects = [item.model_dump(mode="json") for item in payload.objects]
        return {
            "count": count,
            "detection": {"count": count, "detections": detections, "objects": objects},
            "detections": detections,
            "objects": objects,
            "image_context_id": payload.image_context_id,
            "route": payload.route,
        }
    if isinstance(payload, SegmentPayload):
        return {
            "count": len(payload.objects or payload.segments),
            "segments": [item.model_dump(mode="json") for item in payload.segments],
            "objects": [item.model_dump(mode="json") for item in payload.objects],
            "image_context_id": payload.image_context_id,
            "route": payload.route,
        }
    if isinstance(payload, StemHorizontalCurvaturePayload):
        curvature = payload.model_dump(mode="json")
        return {
            "stem_horizontal_curvature": curvature,
            "valid": payload.valid,
            "observation_state": payload.observation_state,
            "track_id": payload.track_id,
            "signed_bow_mm": payload.signed_bow_mm,
            "curvature_rms_mm": payload.curvature_rms_mm,
            "curvature_maxabs_mm": payload.curvature_maxabs_mm,
            "curvature_peak_to_peak_mm": payload.curvature_peak_to_peak_mm,
            "geometry_confidence": payload.geometry_confidence,
            "bow_shape_hint": payload.bow_shape_hint,
            "common_segment_comparable": payload.common_segment_descriptor.comparable,
        }
    if isinstance(payload, StemSurfacePhasePayload):
        phase = payload.model_dump(mode="json")
        return {
            "stem_surface_phase": phase,
            "raw_phase_valid": payload.raw_phase_valid,
            "calibrated_phase_valid": payload.calibrated_phase_valid,
            "phase_locked": payload.phase_locked,
            "track_id": payload.track_id,
            "delta_raw_phase": payload.delta_raw_phase,
            "cumulative_raw_phase": payload.cumulative_raw_phase,
            "delta_phase_deg": payload.delta_phase_deg,
            "cumulative_phase_deg": payload.cumulative_phase_deg,
            "tracking_mode": payload.tracking_mode,
            "relock_status": payload.relock_status,
            "confidence": payload.confidence,
            "reason_codes": payload.reason_codes,
        }
    if isinstance(payload, StemCurvaturePhaseSolutionPayload):
        solution = payload.model_dump(mode="json")
        projection = {
            "stem_curvature_phase_solution": solution,
            "session_id": payload.session_id,
            "track_id": payload.track_id,
            "state": payload.state,
            "observation_count": payload.observation_count,
            "observed_phase_span_deg": payload.observed_phase_span_deg,
            "curvature_frozen": payload.curvature_frozen,
            "decision_gate_reached": payload.decision_gate_reached,
            "target_phase_deg": payload.target_phase_deg,
            "maximum_phase_deg": payload.maximum_phase_deg,
            "fitted_min_curvature_mm": payload.fitted_min_curvature_mm,
            "fitted_max_curvature_mm": payload.fitted_max_curvature_mm,
            "curvature_span_mm": payload.curvature_span_mm,
            "minimum_unique": payload.minimum_unique,
            "bow_relevant": payload.bow_relevant,
            "confidence": payload.confidence,
            "reason_codes": payload.reason_codes,
        }
        if payload.orientation_result is not None:
            projection["stem_orientation_result"] = (
                payload.orientation_result.model_dump(mode="json")
            )
        return projection
    if isinstance(payload, ClassificationPayload):
        top_label = payload.labels[0].model_dump(mode="json") if payload.labels else None
        labels = [item.model_dump(mode="json") for item in payload.labels]
        enrichments = [item.model_dump(mode="json") for item in payload.enrichments]
        return {
            "count": len(payload.labels),
            "labels": labels,
            "top_label": top_label,
            "classification": {
                "top1": top_label,
                "labels": labels,
            },
            "class": top_label.get("label") if isinstance(top_label, dict) else None,
            "score": top_label.get("score") if isinstance(top_label, dict) else None,
            "image_context_id": payload.image_context_id,
            "source_object_id": payload.source_object_id,
            "enrichments": enrichments,
            "route": payload.route,
        }
    if isinstance(payload, AnomalyPayload):
        anomaly_decision = "anomalous" if payload.is_anomalous else "normal"
        return {
            "score": payload.anomaly_score,
            "anomaly": {
                "score": payload.anomaly_score,
                "decision": anomaly_decision,
            },
            "anomaly_score": payload.anomaly_score,
            "predicted_label": payload.predicted_label,
            "is_anomalous": payload.is_anomalous,
            "threshold": payload.threshold,
            "heatmap": payload.heatmap.model_dump(mode="json"),
            "mask": payload.mask.model_dump(mode="json") if payload.mask else None,
            "image_context_id": payload.image_context_id,
            "route": payload.route,
        }
    if isinstance(payload, RegionSetPayload):
        regions = [item.model_dump(mode="json") for item in payload.regions]
        return {
            "count": len(regions),
            "regions": regions,
            "objects": regions,
        }
    if isinstance(payload, PositionsPayload):
        positions = [item.model_dump(mode="json") for item in payload.positions]
        return {"count": len(positions), "positions": positions}
    if isinstance(payload, MeasurementsPayload):
        measurements = [item.model_dump(mode="json") for item in payload.measurements]
        return {"count": len(measurements), "measurements": measurements}
    if isinstance(payload, FeatureMeasurementsPayload):
        measurements = [item.model_dump(mode="json") for item in payload.measurements]
        return {
            "feature_measurements": measurements,
            "measurements": measurements,
            "features": list(payload.features),
            "feature_count": len(payload.features),
        }
    if isinstance(payload, ClearanceZonesPayload):
        zones = [item.model_dump(mode="json") for item in payload.zones]
        return {"clearance_zones": zones, "clearance_zone_count": len(zones)}
    if isinstance(payload, FreeRegionPayload):
        region = payload.free_region.model_dump(mode="json")
        return {"free_region": region, **region}
    if isinstance(payload, QualityDecisionPayload):
        decision = payload.decision.model_dump(mode="json")
        status = str(payload.decision.status)
        quality_pass = status.casefold() in {"pass", "passed", "ok", "good", "true"}
        return {
            "status": payload.decision.status,
            "result_label": payload.decision.result_label,
            "evaluated_rules": payload.decision.evaluated_rules,
            "messages": payload.decision.messages,
            "selected_output_values": payload.decision.selected_output_values,
            "decision": decision,
            "quality": {
                "decision": payload.decision.status,
                "pass": quality_pass,
                "result_label": payload.decision.result_label,
            },
        }
    if isinstance(payload, EventsPayload):
        return {"events": [item.model_dump(mode="json") for item in payload.events]}
    if isinstance(payload, ReferenceEdgesPayload):
        return {
            "reference_edges": [item.model_dump(mode="json") for item in payload.edges],
            "edge_count": len(payload.edges),
            "image_context_id": payload.image_context_id,
        }
    if isinstance(payload, CvEdgesPayload):
        return {
            "cv_edges": payload.model_dump(mode="json"),
            "edge_pixel_count": payload.edge_pixel_count,
            "contour_count": len(payload.contours),
            "line_candidate_count": len(payload.line_candidates),
            "image_context_id": payload.image_context_id,
        }
    if isinstance(payload, CoordinateFramePayload):
        return {"coordinate_frame": payload.frame.model_dump(mode="json")}
    return {}


def _lookup_path(value: Any, path: str | None) -> Any:
    if not path:
        return value
    current = value
    for part in path.split("."):
        if current is _MISSING:
            return _MISSING
        if isinstance(current, list):
            if part.isdigit():
                index = int(part)
                current = current[index] if 0 <= index < len(current) else _MISSING
                continue
            mapped = [_lookup_path(item, part) for item in current]
            current = [item for item in mapped if item is not _MISSING]
            continue
        if isinstance(current, dict):
            current = current.get(part, _MISSING)
            continue
        current = getattr(current, part, _MISSING)
    return current


def _as_items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [value]


def _selected_was_collection(value: Any) -> bool:
    return isinstance(value, list)


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _field_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]
