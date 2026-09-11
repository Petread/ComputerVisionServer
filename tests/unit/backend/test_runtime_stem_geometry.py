from __future__ import annotations

import numpy as np
from cv_contracts import (
    InferenceTiming,
    ModelArchitectureSummary,
    ModelRouteRef,
    RealtimeSegmentRequest,
    SegmentationInferenceResponse,
    SegmentationPoint,
    SegmentationPrediction,
)
from cv_contracts.runtime import (
    RuntimeGraphEdge,
    RuntimeLogicalModelRef,
    RuntimeNodeSpec,
    RuntimePipelineGraph,
)
from cv_runtime import BUILTIN_NODE_TYPE_BY_KEY, ExecutablePipelineGraph
from cv_runtime.context import RuntimeExecutionContext
from cv_runtime.loader import load_runtime_handler
from cv_runtime.nodes.stem_orientation import StemMaskGeometryHandler
from cv_runtime.payloads import (
    FramePayload,
    OverlayPayload,
    RuntimeObjectReference,
    SegmentPayload,
    StemGeometryPayload,
)
from cv_status import DeploymentReleaseStatus, RuntimeMode, TargetPath, TaskType


def _curved_polygon(
    *,
    x_start: float = 0.15,
    x_end: float = 0.85,
    center_y: float = 0.5,
    amplitude: float = 0.07,
    half_width: float = 0.035,
) -> list[SegmentationPoint]:
    normalized_s = np.linspace(0.0, 1.0, 81)
    x = x_start + (x_end - x_start) * normalized_s
    center = center_y + amplitude * 4.0 * normalized_s * (1.0 - normalized_s)
    upper = [
        SegmentationPoint(x=float(point_x), y=float(point_y - half_width))
        for point_x, point_y in zip(x, center, strict=True)
    ]
    lower = [
        SegmentationPoint(x=float(point_x), y=float(point_y + half_width))
        for point_x, point_y in zip(x[::-1], center[::-1], strict=True)
    ]
    return [*upper, *lower]


def _segments_payload(
    polygons: list[list[SegmentationPoint]],
    *,
    labels: list[str] | None = None,
) -> SegmentPayload:
    resolved_labels = labels or ["stem"] * len(polygons)
    segments = [
        SegmentationPrediction(label=label, score=0.96, polygon=polygon)
        for label, polygon in zip(resolved_labels, polygons, strict=True)
    ]
    objects = [
        RuntimeObjectReference(
            object_id=f"obj_stem_{index}",
            object_type="segment",
            source_image_context_id="ctx_stem_frame",
            geometry={
                "points": [point.model_dump(mode="json") for point in polygon],
            },
            coordinate_system="normalized_polygon",
            label=label,
            score=0.96,
            track_id=f"track_{index}",
            producer_node_key="segment",
        )
        for index, (label, polygon) in enumerate(
            zip(resolved_labels, polygons, strict=True),
            start=1,
        )
    ]
    return SegmentPayload(
        segments=segments,
        image_width=640,
        image_height=480,
        image_context_id="ctx_stem_frame",
        objects=objects,
        route={"model_version_id": "mvr_stem_segmentation"},
    )


def _runtime_node(
    node_key: str,
    node_type: str,
    *,
    config: dict[str, object] | None = None,
    logical_model_ref: RuntimeLogicalModelRef | None = None,
) -> RuntimeNodeSpec:
    definition = BUILTIN_NODE_TYPE_BY_KEY[node_type]
    return RuntimeNodeSpec(
        node_key=node_key,
        node_type=node_type,
        node_category=definition.node_category,
        node_contract_version=definition.contract_version,
        input_ports=definition.input_ports,
        output_ports=definition.output_ports,
        config={**definition.default_config, **(config or {})},
        logical_model_ref=logical_model_ref,
    )


def _context(*, inference_client: object | None = None) -> RuntimeExecutionContext:
    return RuntimeExecutionContext(
        correlation_id="corr_stem_geometry",
        pipeline_version_id="piv_stem_geometry",
        mode=RuntimeMode.REALTIME,
        inference_client=inference_client,  # type: ignore[arg-type]
        metadata={"live_frame_sequence": 17, "authoring_preview": True},
    )


def test_registry_and_handler_loader_expose_stem_mask_geometry_node() -> None:
    definition = BUILTIN_NODE_TYPE_BY_KEY["analysis.stem_mask_geometry"]

    assert [(port.key, port.data_type, port.required) for port in definition.input_ports] == [
        ("segments", "segments", True),
        ("frame", "frame", False),
    ]
    assert [(port.key, port.data_type, port.required) for port in definition.output_ports] == [
        ("stem_geometry", "stem_geometry", True),
        ("overlay", "overlay", False),
    ]
    assert definition.runtime_handler_ref == (
        "cv_runtime.nodes.stem_orientation:analysis_stem_mask_geometry"
    )
    fields = {field.key: field for field in definition.property_schema}
    assert {
        "analysis_mode",
        "class_filter",
        "minimum_score",
        "selection_strategy",
        "centerline_sample_count",
        "width_sample_count",
        "smoothing_passes",
        "end_trim_percent",
        "clipped_end_window_percent",
        "debug_overlay",
    }.issubset(fields)
    assert {field.field_type for field in fields.values()} <= {
        "text",
        "number",
        "integer",
        "boolean",
        "select",
    }
    handler = load_runtime_handler(
        definition.runtime_handler_ref,
        expected_node_type=definition.node_type,
    )
    assert isinstance(handler, StemMaskGeometryHandler)


def test_phase_tracking_fast_geometry_keeps_axis_contract_with_reduced_workload() -> None:
    node = _runtime_node(
        "stem_geometry",
        "analysis.stem_mask_geometry",
        config={
            "class_filter": "stem",
            "analysis_mode": "phase_tracking_fast",
            "debug_overlay": False,
        },
    )

    result = StemMaskGeometryHandler().execute(
        _context(),
        node,
        {"segments": _segments_payload([_curved_polygon()])},
    )
    payload = result.outputs["stem_geometry"]

    assert isinstance(payload, StemGeometryPayload)
    assert payload.valid is True
    assert payload.reference_axis is not None
    assert payload.median_width_px is not None
    assert len(payload.normalized_s) == 8
    assert len(payload.width_profile_px) == 8
    assert payload.metadata["analysis_mode"] == "phase_tracking_fast"
    assert payload.metadata["polygon_rasterization_mode"] == "oriented_roi"
    assert "overlay" not in result.outputs


def test_handler_emits_serializable_typed_geometry_and_debug_overlay() -> None:
    node = _runtime_node(
        "stem_geometry",
        "analysis.stem_mask_geometry",
        config={"class_filter": "stem"},
    )
    result = StemMaskGeometryHandler().execute(
        _context(),
        node,
        {"segments": _segments_payload([_curved_polygon()])},
    )

    payload = result.outputs["stem_geometry"]
    overlay = result.outputs["overlay"]
    assert isinstance(payload, StemGeometryPayload)
    assert payload.valid is True
    assert payload.track_id == "track_1"
    assert payload.source_object_id == "obj_stem_1"
    assert payload.image_context_id == "ctx_stem_frame"
    assert payload.frame_sequence == 17
    assert len(payload.normalized_s) == 32
    assert len(payload.centerline_image_xy) == 32
    assert len(payload.residual_profile_px) == 32
    assert len(payload.residual_valid_mask) == 32
    assert len(payload.width_profile_px) == 16
    assert payload.signed_bow_normalized == payload.signed_curvature
    assert payload.metadata["projected_2d_only"] is True
    assert payload.metadata["metric_calibration_applied"] is False
    assert StemGeometryPayload.model_validate_json(payload.model_dump_json()) == payload
    assert isinstance(overlay, OverlayPayload)
    assert {primitive.primitive_type for primitive in overlay.primitives} >= {
        "polygon",
        "axis",
        "line",
        "point",
    }
    assert any(primitive.layer == "measurements" for primitive in overlay.primitives)


def test_handler_fails_closed_for_ambiguous_or_nonmatching_instances() -> None:
    ambiguous_node = _runtime_node(
        "stem_geometry",
        "analysis.stem_mask_geometry",
        config={
            "class_filter": "stem",
            "selection_strategy": "single_matching_segment",
        },
    )
    segments = _segments_payload(
        [
            _curved_polygon(x_start=0.08, x_end=0.48),
            _curved_polygon(x_start=0.52, x_end=0.92, amplitude=-0.05),
        ]
    )

    ambiguous = (
        StemMaskGeometryHandler()
        .execute(
            _context(),
            ambiguous_node,
            {"segments": segments},
        )
        .outputs["stem_geometry"]
    )
    assert isinstance(ambiguous, StemGeometryPayload)
    assert ambiguous.valid is False
    assert ambiguous.reason_codes == ["multiple_segments_without_selection"]
    assert ambiguous.reasons[0].message

    no_match_node = _runtime_node(
        "stem_geometry",
        "analysis.stem_mask_geometry",
        config={"class_filter": "log"},
    )
    no_match = (
        StemMaskGeometryHandler()
        .execute(
            _context(),
            no_match_node,
            {"segments": _segments_payload([_curved_polygon()])},
        )
        .outputs["stem_geometry"]
    )
    assert isinstance(no_match, StemGeometryPayload)
    assert no_match.valid is False
    assert no_match.reason_codes == ["no_matching_segment"]


class _SegmentationClient:
    def segment(self, payload: RealtimeSegmentRequest) -> SegmentationInferenceResponse:
        return SegmentationInferenceResponse(
            request_id="req_stem_segmentation",
            correlation_id="corr_stem_geometry",
            deployment_target=payload.deployment_target,
            route=ModelRouteRef(
                deployment_release_id=payload.deployment_release_id or "rel_stem_segmentation",
                target_path=TargetPath.REALTIME,
                release_status=DeploymentReleaseStatus.ACTIVATED,
                model_family_slug="stem-segmentation",
                model_version_id="mvr_stem_segmentation",
                task_type=TaskType.SEGMENTATION,
                triton_model_name="rt__stem_segmentation_v1",
                triton_model_version=1,
                contract_major=1,
            ),
            architecture=ModelArchitectureSummary(
                task_type=TaskType.SEGMENTATION,
                family="yolo",
                version="v11",
                size="n",
                display_name="YOLOv11 Nano Segmentation",
                internal_key="yolo-v11-n-seg",
            ),
            warm_model=True,
            timing=InferenceTiming(
                received_at="2026-08-03T10:00:00Z",
                validation_ms=0.1,
                routing_ms=0.1,
                preprocessing_ms=0.2,
                inference_ms=1.0,
                postprocessing_ms=0.2,
                total_ms=1.6,
            ),
            image_width=640,
            image_height=480,
            segments=[
                SegmentationPrediction(
                    label="stem",
                    score=0.96,
                    polygon=_curved_polygon(),
                )
            ],
        )


def test_pipeline_engine_runs_file_segmentation_geometry_overlay_chain() -> None:
    model_ref = RuntimeLogicalModelRef(
        model_family_slug="stem-segmentation",
        target_path=TargetPath.REALTIME,
        required_task_type=TaskType.SEGMENTATION,
        selection_policy="explicit_release",
        deployment_release_id="rel_stem_segmentation",
    )
    nodes = [
        _runtime_node("source", "source.file_test"),
        _runtime_node("segment", "inference.segmentation", logical_model_ref=model_ref),
        _runtime_node("geometry", "analysis.stem_mask_geometry"),
        _runtime_node(
            "overlay",
            "output.overlay",
            config={"layers": ["lines", "polygons", "measurements"]},
        ),
    ]
    graph = RuntimePipelineGraph(
        nodes=nodes,
        edges=[
            RuntimeGraphEdge(
                from_node_key="source",
                from_port="frame",
                to_node_key="segment",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="source",
                from_port="frame",
                to_node_key="geometry",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="segment",
                from_port="segments",
                to_node_key="geometry",
                to_port="segments",
            ),
            RuntimeGraphEdge(
                from_node_key="geometry",
                from_port="overlay",
                to_node_key="overlay",
                to_port="result",
            ),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="piv_stem_geometry",
        graph=graph,
        handler_refs={
            node.node_type: BUILTIN_NODE_TYPE_BY_KEY[node.node_type].runtime_handler_ref
            for node in nodes
        },
    )
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    context = _context(inference_client=_SegmentationClient())
    context.input_frame = FramePayload.from_cv2_frame(
        frame,
        timestamp="2026-08-03T10:00:00Z",
        metadata={"sequence": 17},
    )

    result = executable.execute(context)

    assert result.status == "completed"
    assert result.errors == []
    geometry = result.node_outputs["geometry"]["stem_geometry"]
    assert geometry["payload_type"] == "stem_geometry"
    assert geometry["valid"] is True
    assert len(geometry["centerline_image_xy"]) == 32
    output_overlay = result.node_outputs["overlay"]["overlay"]
    assert output_overlay["payload_type"] == "overlay"
    assert any(item["primitive_type"] == "axis" for item in output_overlay["primitives"])
    assert any(
        item["primitive_type"] == "point" and item["layer"] == "measurements"
        for item in output_overlay["primitives"]
    )
    assert any(item["primitive_type"] == "axis" for item in result.overlay_primitives)
    assert any(
        item["primitive_type"] == "point" and item["layer"] == "measurements"
        for item in result.overlay_primitives
    )
