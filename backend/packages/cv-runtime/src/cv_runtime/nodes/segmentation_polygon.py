from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np
from cv_contracts import SegmentationPoint, SegmentationPrediction
from cv_contracts.runtime import RuntimeNodeSpec

from cv_runtime.context import NodeInputs, RuntimeExecutionContext
from cv_runtime.handlers import NodeExecutionResult
from cv_runtime.payloads import OverlayPayload, OverlayPrimitive, RuntimeObjectReference, SegmentPayload


@dataclass(frozen=True)
class SegmentationPolygonSimplificationHandler:
    node_type: str = "analysis.segmentation_polygon_simplification"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        payload = inputs.get("segments")
        if not isinstance(payload, SegmentPayload):
            return NodeExecutionResult()

        config = node.config or {}
        epsilon_ratio = _bounded_float(config.get("epsilon_ratio"), 0.005, 0.00001, 0.25)
        maximum_points = max(4, min(int(config.get("maximum_points") or 12), 12))
        minimum_area_ratio = _bounded_float(
            config.get("minimum_area_ratio"), 0.0005, 0.0, 1.0
        )
        debug_overlay = bool(config.get("debug_overlay", True))

        try:
            accepted: list[SegmentationPrediction] = []
            accepted_indices: list[int] = []
            rejected: list[dict[str, Any]] = []
            primitives: list[OverlayPrimitive] = []
            for index, segment in enumerate(payload.segments):
                simplified, reason = _simplify_prediction(
                    segment,
                    epsilon_ratio=epsilon_ratio,
                    maximum_points=maximum_points,
                    minimum_area_ratio=minimum_area_ratio,
                )
                if simplified is None:
                    rejected.append(
                        {"segment_index": index, "label": segment.label, "reason_code": reason}
                    )
                    continue
                accepted.append(simplified)
                accepted_indices.append(index)
                if debug_overlay:
                    primitives.append(
                        OverlayPrimitive(
                            primitive_type="polygon",
                            layer="simplified_segmentation",
                            label=simplified.label,
                            confidence=simplified.score,
                            geometry={
                                "points": [
                                    point.model_dump(mode="json") for point in simplified.polygon
                                ],
                                "coordinate_system": "normalized_xy",
                            },
                            image_context_id=payload.image_context_id,
                            style={"stroke": "#44d17a", "fill_alpha": 0.08},
                            metadata={"source_segment_index": index},
                        )
                    )

            if rejected and debug_overlay:
                primitives.append(
                    _warning_primitive(
                        payload.image_context_id,
                        "invalid_polygon",
                        f"{len(rejected)} invalid segmentation polygon(s) ignored",
                        node_key=node.node_key,
                    )
                )
            metadata = {
                **payload.metadata,
                "polygon_simplification": {
                    "valid": bool(accepted),
                    "reason_code": None if accepted else "invalid_polygon",
                    "input_count": len(payload.segments),
                    "output_count": len(accepted),
                    "rejected": rejected,
                    "epsilon_ratio": epsilon_ratio,
                    "maximum_points": maximum_points,
                    "minimum_area_ratio": minimum_area_ratio,
                },
            }
            simplified_payload = SegmentPayload(
                segments=accepted,
                image_width=payload.image_width,
                image_height=payload.image_height,
                image_context_id=payload.image_context_id,
                objects=_simplified_objects(payload.objects, accepted_indices, accepted),
                route=dict(payload.route),
                metadata=metadata,
            )
            outputs: dict[str, Any] = {"segments": simplified_payload}
            if debug_overlay:
                outputs["overlay"] = OverlayPayload(
                    primitives=primitives,
                    display_context_id=payload.image_context_id,
                )
            return NodeExecutionResult(outputs=outputs)
        except Exception as exc:
            detail = str(exc)[:240]
            failed = SegmentPayload(
                image_width=payload.image_width,
                image_height=payload.image_height,
                image_context_id=payload.image_context_id,
                route=dict(payload.route),
                metadata={
                    **payload.metadata,
                    "polygon_simplification": {
                        "valid": False,
                        "reason_code": "geometry_internal_error",
                        "detail": detail,
                    },
                },
            )
            outputs = {"segments": failed}
            if debug_overlay:
                outputs["overlay"] = OverlayPayload(
                    primitives=[
                        _warning_primitive(
                            payload.image_context_id,
                            "geometry_internal_error",
                            "Polygon simplification failed",
                            detail=detail,
                            node_key=node.node_key,
                        )
                    ],
                    display_context_id=payload.image_context_id,
                )
            return NodeExecutionResult(outputs=outputs)


def _simplify_prediction(
    segment: SegmentationPrediction,
    *,
    epsilon_ratio: float,
    maximum_points: int,
    minimum_area_ratio: float,
) -> tuple[SegmentationPrediction | None, str | None]:
    points = _deduplicate_points([(point.x, point.y) for point in segment.polygon])
    if len(points) < 3:
        return None, "invalid_polygon"
    if not _simple_polygon(points):
        return None, "invalid_polygon"
    if abs(_signed_area(points)) < minimum_area_ratio:
        return None, "polygon_too_small"

    contour = np.asarray(points, dtype=np.float32).reshape((-1, 1, 2))
    perimeter = float(cv2.arcLength(contour, True))
    epsilon = max(perimeter * epsilon_ratio, 1e-7)
    approximation = cv2.approxPolyDP(contour, epsilon, True).reshape((-1, 2))
    attempts = 0
    while len(approximation) > maximum_points and attempts < 16:
        epsilon *= 1.5
        approximation = cv2.approxPolyDP(contour, epsilon, True).reshape((-1, 2))
        attempts += 1
    simplified = _deduplicate_points(
        [(float(point[0]), float(point[1])) for point in approximation]
    )
    if len(simplified) < 3 or len(simplified) > maximum_points:
        return None, "polygon_too_complex"
    if not _simple_polygon(simplified) or abs(_signed_area(simplified)) < minimum_area_ratio:
        return None, "invalid_polygon"
    return (
        segment.model_copy(
            update={
                "polygon": [SegmentationPoint(x=x, y=y) for x, y in simplified]
            }
        ),
        None,
    )


def _simplified_objects(
    objects: list[RuntimeObjectReference],
    accepted_indices: list[int],
    segments: list[SegmentationPrediction],
) -> list[RuntimeObjectReference]:
    simplified: list[RuntimeObjectReference] = []
    for source_index, segment in zip(accepted_indices, segments, strict=True):
        if source_index >= len(objects):
            continue
        source = objects[source_index]
        points = [point.model_dump(mode="json") for point in segment.polygon]
        xs = [point.x for point in segment.polygon]
        ys = [point.y for point in segment.polygon]
        simplified.append(
            source.model_copy(
                update={
                    "geometry": {
                        "points": points,
                        "box": {
                            "x_min": min(xs),
                            "y_min": min(ys),
                            "x_max": max(xs),
                            "y_max": max(ys),
                        },
                    },
                    "metadata": {**source.metadata, "polygon_simplified": True},
                }
            )
        )
    return simplified


def _warning_primitive(
    image_context_id: str | None,
    reason_code: str,
    label: str,
    *,
    detail: str | None = None,
    node_key: str | None = None,
) -> OverlayPrimitive:
    return OverlayPrimitive(
        primitive_type="label",
        layer="board_geometry_warning",
        label=label,
        geometry={},
        image_context_id=image_context_id,
        style={"color": "#ffb020", "ttl_ms": 3000},
        metadata={
            "valid": False,
            "reason_code": reason_code,
            "detail": detail,
            "node_key": node_key,
            "notification_cooldown_ms": 3000,
        },
    )


def _bounded_float(value: object, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def _deduplicate_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for point in points:
        normalized = (min(max(point[0], 0.0), 1.0), min(max(point[1], 0.0), 1.0))
        if not result or _distance_squared(result[-1], normalized) > 1e-16:
            result.append(normalized)
    if len(result) > 1 and _distance_squared(result[0], result[-1]) <= 1e-16:
        result.pop()
    return result


def _signed_area(points: list[tuple[float, float]]) -> float:
    return 0.5 * sum(
        x1 * y2 - x2 * y1
        for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1], strict=True)
    )


def _simple_polygon(points: list[tuple[float, float]]) -> bool:
    size = len(points)
    for first in range(size):
        a1 = points[first]
        a2 = points[(first + 1) % size]
        for second in range(first + 1, size):
            if second in {first, (first + 1) % size}:
                continue
            if first == 0 and second == size - 1:
                continue
            b1 = points[second]
            b2 = points[(second + 1) % size]
            if _segments_intersect(a1, a2, b1, b2):
                return False
    return True


def _segments_intersect(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> bool:
    def orientation(
        p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]
    ) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    first = orientation(a1, a2, b1)
    second = orientation(a1, a2, b2)
    third = orientation(b1, b2, a1)
    fourth = orientation(b1, b2, a2)
    return first * second < -1e-12 and third * fourth < -1e-12


def _distance_squared(first: tuple[float, float], second: tuple[float, float]) -> float:
    return (first[0] - second[0]) ** 2 + (first[1] - second[1]) ** 2


analysis_segmentation_polygon_simplification = SegmentationPolygonSimplificationHandler()
