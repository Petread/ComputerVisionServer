from __future__ import annotations

import cv2
import numpy as np
import cv_runtime.nodes.board_cut as board_cut_nodes
from cv_contracts import (
    DetectionBox,
    DetectionPrediction,
    SegmentationPoint,
    SegmentationPrediction,
)
from cv_contracts.runtime import RuntimeNodeSpec, RuntimeTriggerExecutionRequest
from cv_runtime import BUILTIN_NODE_TYPE_BY_KEY, RuntimeExecutionContext
from cv_runtime.nodes.board_cut import (
    BoardEndCoordinateFrameHandler,
    BoardViewCutConstraintsHandler,
    EndFaceCutConstraintsHandler,
    MultiViewBoardCutFusionHandler,
    SingleBoardCameraTestSourceHandler,
    SingleViewBoardCutDecisionHandler,
    SynchronizedCameraFramesHandler,
    _board_region_corner_bridge_quality,
    _BoardColorRegion,
    _detect_board_color_region,
    _merge_axis_line_segments,
    _merge_free_oriented_line_segments,
    _opencv_line_segments,
    _refine_lines_from_edge_support,
)
from cv_runtime.nodes.segmentation_polygon import SegmentationPolygonSimplificationHandler
from cv_runtime.payloads import (
    BoardCutConstraintsPayload,
    BoardFeatureConstraint,
    CoordinateFramePayload,
    DetectionPayload,
    FramePayload,
    InspectionCoordinateFrame,
    InspectionPoint,
    InspectionVector,
    OverlayPayload,
    ReferenceEdgesPayload,
    SegmentPayload,
    SynchronizedCameraFrame,
    SynchronizedFrameSetPayload,
    TriggerPayload,
)
from cv_status import RuntimeMode, RuntimeNodeCategory


def _context() -> RuntimeExecutionContext:
    return RuntimeExecutionContext(
        correlation_id="plc-cycle-123",
        pipeline_version_id="piv_board_cut",
        mode=RuntimeMode.TRIGGER,
    )


def _node(node_type: str, config: dict) -> RuntimeNodeSpec:
    return RuntimeNodeSpec(
        node_key=node_type.rsplit(".", 1)[-1],
        node_type=node_type,
        node_category=(
            RuntimeNodeCategory.SOURCE
            if node_type.startswith("source.")
            else RuntimeNodeCategory.ANALYSIS
        ),
        config=config,
    )


def _surface(role: str, y_min: float, y_max: float, *, length: float = 400.0) -> dict:
    return {
        "surface_role": role,
        "image_points": [
            {"x": 0.0, "y": y_min},
            {"x": 1.0, "y": y_min},
            {"x": 1.0, "y": y_max},
            {"x": 0.0, "y": y_max},
        ],
        "world_points_mm": [
            {"x": 0.0, "y": 0.0},
            {"x": length, "y": 0.0},
            {"x": length, "y": 100.0},
            {"x": 0.0, "y": 100.0},
        ],
    }


def _rule() -> dict:
    return {
        "class_id": 0,
        "label_snapshot": "knot",
        "class_map_version": "model-1:class-map",
        "confirmed": True,
        "relevant": True,
        "measurement_basis": "max_feret_diameter",
        "clearance_multiplier": 1.0,
        "depth_factor": 1.0,
        "clearance_factor": 1.0,
        "minimum_confidence": 0.35,
    }


def _frame() -> FramePayload:
    return FramePayload(
        image_base64="",
        mime_type="image/png",
        width=100,
        height=100,
        timestamp="2026-07-21T08:00:00Z",
    )


def _segment_payload(*segments: SegmentationPrediction) -> SegmentPayload:
    return SegmentPayload(
        segments=list(segments),
        image_width=1000,
        image_height=500,
        image_context_id="ctx_board_surface",
        route={"deployment_release_id": "release-seg-1"},
        metadata={
            "deployment_release_id": "release-seg-1",
            "capability": {
                "class_labels": ["main", "side", "end"],
                "class_map_version": "surface-map-v1",
            },
        },
    )


def _segmentation(label: str, points: list[tuple[float, float]], score: float = 0.9):
    return SegmentationPrediction(
        label=label,
        score=score,
        polygon=[SegmentationPoint(x=x, y=y) for x, y in points],
    )


def _surface_mapping(*roles: tuple[str, int, str]) -> dict:
    return {
        role: {
            "class_id": class_id,
            "label_snapshot": label,
            "class_map_version": "surface-map-v1",
            "deployment_release_id": "release-seg-1",
            "confirmed": True,
        }
        for role, class_id, label in roles
    }


def test_segmentation_polygon_simplification_preserves_context_and_limits_points() -> None:
    points = [
        (
            0.5 + 0.35 * np.cos(index * 2 * np.pi / 40),
            0.5 + 0.2 * np.sin(index * 2 * np.pi / 40),
        )
        for index in range(40)
    ]
    payload = _segment_payload(_segmentation("main", points))

    result = SegmentationPolygonSimplificationHandler().execute(
        _context(),
        _node(
            "analysis.segmentation_polygon_simplification",
            {
                "epsilon_ratio": 0.005,
                "maximum_points": 256,
                "minimum_area_ratio": 0.0005,
                "debug_overlay": True,
            },
        ),
        {"segments": payload},
    )

    simplified = result.outputs["segments"]
    assert isinstance(simplified, SegmentPayload)
    assert simplified.image_context_id == payload.image_context_id
    assert simplified.route == payload.route
    assert simplified.metadata["deployment_release_id"] == "release-seg-1"
    assert 4 <= len(simplified.segments[0].polygon) <= 12
    assert simplified.metadata["polygon_simplification"]["valid"] is True
    assert isinstance(result.outputs["overlay"], OverlayPayload)


def test_segmentation_polygon_simplification_rejects_self_intersection_without_raising() -> None:
    payload = _segment_payload(
        _segmentation("main", [(0.1, 0.1), (0.9, 0.9), (0.1, 0.9), (0.9, 0.1)])
    )

    result = SegmentationPolygonSimplificationHandler().execute(
        _context(),
        _node("analysis.segmentation_polygon_simplification", {}),
        {"segments": payload},
    )

    simplified = result.outputs["segments"]
    assert simplified.segments == []
    diagnostic = simplified.metadata["polygon_simplification"]
    assert diagnostic["valid"] is False
    assert diagnostic["reason_code"] == "invalid_polygon"


def test_board_end_coordinate_frame_uses_selected_segmentation_surface_labels() -> None:
    payload = _segment_payload(
        _segmentation("main", [(0.2, 0.2), (0.95, 0.2), (0.95, 0.55), (0.2, 0.55)]),
        _segmentation("side", [(0.2, 0.55), (0.95, 0.55), (0.95, 0.72), (0.2, 0.72)]),
    )
    frame = FramePayload.from_cv2_frame(np.zeros((500, 1000, 3), dtype=np.uint8))

    result = BoardEndCoordinateFrameHandler().execute(
        _context(),
        _node(
            "analysis.board_end_coordinate_frame",
            {
                "geometry_source": "segmentation",
                "view_role": "upper",
                "board_end_side": "left",
                "board_surface_class_mapping": _surface_mapping(
                    ("board_main_face", 0, "main"),
                    ("board_side_face", 1, "side"),
                ),
            },
        ),
        {"frame": frame, "segments": payload},
    )

    coordinate = result.outputs["coordinate_frame"].frame
    assert coordinate.source["found"] is True
    assert coordinate.source["method"] == "segmentation_polygons"
    assert set(coordinate.source["segmentation_surface_polygons"]) == {"top", "side_a"}
    assert coordinate.longitudinal_axis.x > 0.9
    assert coordinate.confidence >= 0.5
    assert len(result.outputs["reference_edges"].edges) >= 2


def test_board_end_coordinate_frame_marks_ambiguous_segmentation_invalid_and_continues() -> None:
    payload = _segment_payload(
        _segmentation("main", [(0.2, 0.2), (0.9, 0.2), (0.9, 0.5), (0.2, 0.5)]),
        _segmentation("main", [(0.25, 0.25), (0.8, 0.25), (0.8, 0.45), (0.25, 0.45)]),
        _segmentation("side", [(0.2, 0.5), (0.9, 0.5), (0.9, 0.7), (0.2, 0.7)]),
    )
    frame = FramePayload.from_cv2_frame(np.zeros((500, 1000, 3), dtype=np.uint8))
    node = _node(
        "analysis.board_end_coordinate_frame",
        {
            "geometry_source": "segmentation",
            "board_surface_class_mapping": _surface_mapping(
                ("board_main_face", 0, "main"),
                ("board_side_face", 1, "side"),
            ),
        },
    )

    invalid = BoardEndCoordinateFrameHandler().execute(
        _context(), node, {"frame": frame, "segments": payload}
    )
    valid = BoardEndCoordinateFrameHandler().execute(
        _context(),
        node,
        {
            "frame": frame,
            "segments": _segment_payload(payload.segments[0], payload.segments[2]),
        },
    )

    assert invalid.outputs["coordinate_frame"].frame.confidence == 0
    assert invalid.outputs["coordinate_frame"].frame.source["reason_code"] == "surface_ambiguous"
    assert invalid.outputs["reference_edges"].edges == []
    assert valid.outputs["coordinate_frame"].frame.source["found"] is True


def test_segmented_board_view_never_reuses_a_previous_pose_for_an_invalid_frame() -> None:
    context = RuntimeExecutionContext(
        correlation_id="realtime-board",
        pipeline_version_id="piv_board_cut",
        mode=RuntimeMode.REALTIME,
    )
    frame = _frame()
    main = _segmentation("main", [(0.05, 0.15), (0.98, 0.15), (0.98, 0.3), (0.05, 0.3)])
    side = _segmentation("side", [(0.05, 0.3), (0.98, 0.3), (0.98, 0.42), (0.05, 0.42)])
    coordinate_node = _node(
        "analysis.board_end_coordinate_frame",
        {
            "geometry_source": "segmentation",
            "board_surface_class_mapping": _surface_mapping(
                ("board_main_face", 0, "main"),
                ("board_side_face", 1, "side"),
            ),
        },
    )
    constraints_config = _longitudinal_config("upper")
    constraints_config.update({"board_width_mm": 200, "board_thickness_mm": 40})
    constraints_node = _node("analysis.board_view_cut_constraints", constraints_config)
    features = DetectionPayload(
        detections=[],
        image_width=100,
        image_height=100,
        image_context_id="ctx_board_surface",
    )

    valid_coordinate = BoardEndCoordinateFrameHandler().execute(
        context,
        coordinate_node,
        {"frame": frame, "segments": _segment_payload(main, side)},
    ).outputs["coordinate_frame"]
    valid_result = BoardViewCutConstraintsHandler().execute(
        context,
        constraints_node,
        {"frame": frame, "coordinate_frame": valid_coordinate, "features": features},
    )
    invalid_coordinate = BoardEndCoordinateFrameHandler().execute(
        context,
        coordinate_node,
        {"frame": frame, "segments": _segment_payload(main, main, side)},
    ).outputs["coordinate_frame"]
    invalid_result = BoardViewCutConstraintsHandler().execute(
        context,
        constraints_node,
        {"frame": frame, "coordinate_frame": invalid_coordinate, "features": features},
    )

    assert valid_result.outputs["constraints"].valid is True, (
        valid_result.outputs["constraints"].status,
        valid_result.outputs["constraints"].reason_code,
        [primitive.metadata for primitive in valid_result.outputs.get("overlay", OverlayPayload()).primitives],
    )
    assert invalid_result.outputs["constraints"].valid is False
    assert invalid_result.outputs["constraints"].reason_code == "surface_missing"
    assert invalid_result.outputs["overlay"].primitives[0].metadata["reason_code"] == "surface_missing"


def test_end_face_constraints_derive_homography_from_segment_and_board_format() -> None:
    segments = _segment_payload(
        _segmentation("end", [(0.2, 0.2), (0.8, 0.25), (0.75, 0.75), (0.25, 0.7)])
    )
    features = DetectionPayload(detections=[], image_context_id="ctx_board_surface")
    context = _context()
    context.trigger = TriggerPayload(
        trigger_id="format-trigger",
        body={
            "board_format": {
                "format_id": "board-200x40",
                "format_version": "7",
                "width_mm": 200,
                "thickness_mm": 40,
            }
        },
    )
    result = EndFaceCutConstraintsHandler().execute(
        context,
        _node(
            "analysis.end_face_cut_constraints",
            {
                "inspection_length_mm": 300,
                "class_rules": [_rule()],
                "board_surface_class_mapping": _surface_mapping(
                    ("board_end_face", 2, "end")
                ),
            },
        ),
        {"frame": _frame(), "features": features, "segments": segments},
    )

    constraints = result.outputs["constraints"]
    assert constraints.valid is True
    assert constraints.constraints == []
    assert isinstance(result.outputs["overlay"], OverlayPayload)


def test_trigger_segmentation_requires_a_valid_board_format_for_each_frame() -> None:
    result = EndFaceCutConstraintsHandler().execute(
        _context(),
        _node(
            "analysis.end_face_cut_constraints",
            {
                "class_rules": [_rule()],
                "board_surface_class_mapping": _surface_mapping(
                    ("board_end_face", 2, "end")
                ),
            },
        ),
        {
            "frame": _frame(),
            "features": DetectionPayload(detections=[], image_context_id="ctx_board_surface"),
            "segments": _segment_payload(
                _segmentation(
                    "end",
                    [(0.2, 0.2), (0.8, 0.25), (0.75, 0.75), (0.25, 0.7)],
                )
            ),
        },
    )

    assert result.outputs["constraints"].valid is False
    assert result.outputs["constraints"].reason_code == "invalid_board_format"
    assert result.outputs["overlay"].primitives[0].metadata["reason_code"] == (
        "invalid_board_format"
    )


def test_end_face_internal_geometry_error_is_a_recoverable_invalid_frame(monkeypatch) -> None:
    monkeypatch.setattr(
        board_cut_nodes,
        "_dynamic_end_face_calibration",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("unexpected geometry")),
    )
    result = EndFaceCutConstraintsHandler().execute(
        _context(),
        _node(
            "analysis.end_face_cut_constraints",
            {
                "class_rules": [_rule()],
                "board_surface_class_mapping": _surface_mapping(
                    ("board_end_face", 2, "end")
                ),
            },
        ),
        {
            "frame": _frame(),
            "features": DetectionPayload(detections=[], image_context_id="ctx_board_surface"),
            "segments": _segment_payload(
                _segmentation(
                    "end",
                    [(0.2, 0.2), (0.8, 0.25), (0.75, 0.75), (0.25, 0.7)],
                )
            ),
        },
    )

    constraints = result.outputs["constraints"]
    warning = result.outputs["overlay"].primitives[0]
    assert constraints.valid is False
    assert constraints.reason_code == "geometry_internal_error"
    assert warning.metadata["node_key"] == "end_face_cut_constraints"


def test_board_end_detection_merges_reference_segments_and_emits_debug_overlay() -> None:
    image = np.full((448, 605, 3), 35, dtype=np.uint8)
    cv2.rectangle(image, (242, 107), (604, 155), (215, 215, 215), thickness=-1)
    cv2.rectangle(image, (242, 155), (604, 295), (145, 145, 145), thickness=-1)
    cv2.line(image, (360, 107), (360, 295), (235, 235, 235), thickness=4)
    cv2.rectangle(image, (0, 296), (604, 447), (80, 80, 80), thickness=-1)
    frame = FramePayload.from_cv2_frame(image, mime_type="image/jpeg")
    result = BoardEndCoordinateFrameHandler().execute(
        _context(),
        _node(
            "analysis.board_end_coordinate_frame",
            {
                "board_end_side": "left",
                "blur_kernel_size": 3,
                "lower_threshold": 30,
                "upper_threshold": 100,
                "hough_threshold": 15,
                "minimum_line_length_px": 15,
                "max_line_gap_px": 30,
                "debug_overlay": True,
            },
        ),
        {"frame": frame},
    )

    coordinate = result.outputs["coordinate_frame"]
    assert isinstance(coordinate, CoordinateFramePayload)
    assert coordinate.frame.source["found"] is True
    assert coordinate.frame.source["end_candidate_uses_image_vertical"] is True
    assert coordinate.frame.confidence >= 0.5
    assert abs(coordinate.frame.origin.x - 242) <= 8
    assert coordinate.frame.longitudinal_axis.x > 0.9
    assert coordinate.frame.source["calibration_candidates"]
    overlay = result.outputs["overlay"]
    assert isinstance(overlay, OverlayPayload)
    assert any(primitive.layer == "board_end" for primitive in overlay.primitives)
    assert any(primitive.layer == "board_longitudinal_axis" for primitive in overlay.primitives)
    reference_edges = result.outputs["reference_edges"]
    assert isinstance(reference_edges, ReferenceEdgesPayload)
    assert {edge.role for edge in reference_edges.edges} >= {
        "start_edge",
        "side_edge",
    }


def test_board_end_detection_downscales_analysis_and_restores_source_coordinates() -> None:
    image = np.full((896, 1210, 3), 35, dtype=np.uint8)
    cv2.rectangle(image, (484, 214), (1209, 310), (215, 215, 215), thickness=-1)
    cv2.rectangle(image, (484, 310), (1209, 590), (145, 145, 145), thickness=-1)
    cv2.rectangle(image, (0, 592), (1209, 895), (80, 80, 80), thickness=-1)
    frame = FramePayload.from_cv2_frame(image, mime_type="image/jpeg")

    result = BoardEndCoordinateFrameHandler().execute(
        _context(),
        _node(
            "analysis.board_end_coordinate_frame",
            {
                "board_end_side": "left",
                "blur_kernel_size": 3,
                "lower_threshold": 30,
                "upper_threshold": 100,
                "hough_threshold": 15,
                "minimum_line_length_px": 30,
                "max_line_gap_px": 60,
                "processing_max_dimension_px": 448,
            },
        ),
        {"frame": frame},
    )

    coordinate = result.outputs["coordinate_frame"]
    assert coordinate.frame.source["found"] is True
    assert abs(coordinate.frame.origin.x - 484) <= 16
    assert coordinate.frame.source["processing_width"] == 448
    assert coordinate.frame.source["source_width"] == 1210
    assert coordinate.frame.source["processing_scale"] < 0.5


def test_board_end_detection_accepts_board_end_anywhere_in_image() -> None:
    image = np.full((400, 800, 3), 30, dtype=np.uint8)
    cv2.rectangle(image, (610, 70), (799, 180), (215, 215, 215), thickness=-1)
    cv2.rectangle(image, (610, 180), (799, 340), (135, 135, 135), thickness=-1)
    frame = FramePayload.from_cv2_frame(image, mime_type="image/jpeg")

    result = BoardEndCoordinateFrameHandler().execute(
        _context(),
        _node(
            "analysis.board_end_coordinate_frame",
            {
                "board_end_side": "left",
                "blur_kernel_size": 3,
                "lower_threshold": 30,
                "upper_threshold": 100,
                "hough_threshold": 15,
                "minimum_line_length_px": 15,
                "max_line_gap_px": 20,
                "minimum_longitudinal_span_percent": 20,
            },
        ),
        {"frame": frame},
    )

    coordinate = result.outputs["coordinate_frame"]
    assert coordinate.frame.source["found"] is True
    assert abs(coordinate.frame.origin.x - 610) <= 8
    assert coordinate.frame.origin.x > image.shape[1] * 0.72


def test_board_end_detection_is_deterministic_for_identical_frame_bytes() -> None:
    image = np.full((400, 700, 3), 35, dtype=np.uint8)
    cv2.rectangle(image, (170, 65), (699, 175), (215, 215, 215), thickness=-1)
    cv2.rectangle(image, (170, 175), (699, 335), (135, 135, 135), thickness=-1)
    frame = FramePayload.from_cv2_frame(image, mime_type="image/png")
    node = _node(
        "analysis.board_end_coordinate_frame",
        {
            "board_end_side": "left",
            "line_detector_mode": "robust_lsd",
            "blur_kernel_size": 3,
            "lower_threshold": 25,
            "upper_threshold": 75,
            "hough_threshold": 15,
            "minimum_line_length_px": 35,
            "max_line_gap_px": 15,
            "maximum_reference_join_gap_px": 25,
        },
    )

    snapshots = []
    for _ in range(8):
        result = BoardEndCoordinateFrameHandler().execute(
            _context(),
            node,
            {"frame": frame},
        )
        coordinate = result.outputs["coordinate_frame"].frame
        snapshots.append(
            (
                coordinate.source["found"],
                round(coordinate.origin.x, 6),
                round(coordinate.origin.y, 6),
                round(coordinate.confidence, 6),
                tuple(round(value, 6) for value in coordinate.source["start_edge"]),
                tuple(round(value, 6) for value in coordinate.source["side_edge"]),
            )
        )

    assert len(set(snapshots)) == 1
    assert coordinate.source["method"] == "deterministic_lsd_reference_pair"
    assert coordinate.source["angle_handling"] == "soft_quality_score"


def test_opencv_line_output_is_normalized_across_array_layouts() -> None:
    nested = np.asarray([[[10, 20, 30, 40]], [[50, 60, 70, 80]]], dtype=np.float32)
    flat = nested.reshape(2, 4)

    assert _opencv_line_segments(nested) == _opencv_line_segments(flat) == [
        (10.0, 20.0, 30.0, 40.0),
        (50.0, 60.0, 70.0, 80.0),
    ]


def test_board_region_bridges_occluded_surface_edge_only_at_silhouette() -> None:
    mask = np.zeros((300, 500), dtype=np.uint8)
    mask[40:260, 100:500] = 255
    region = _BoardColorRegion(
        mask=mask,
        outline=[
            (100.0, 40.0),
            (499.0, 40.0),
            (499.0, 259.0),
            (100.0, 259.0),
        ],
        confidence=0.9,
        source={},
    )
    end_line = (100.0, 40.0, 100.0, 259.0)
    occluded_surface_edge = (138.0, 150.0, 499.0, 150.0)

    quality = _board_region_corner_bridge_quality(
        (100.0, 150.0),
        end_line=end_line,
        longitudinal_line=occluded_surface_edge,
        region=region,
        side="left",
        direct_join_tolerance=15.0,
    )
    internal_quality = _board_region_corner_bridge_quality(
        (260.0, 150.0),
        end_line=(260.0, 40.0, 260.0, 259.0),
        longitudinal_line=(298.0, 150.0, 499.0, 150.0),
        region=region,
        side="left",
        direct_join_tolerance=15.0,
    )

    assert quality >= 0.5
    assert internal_quality == 0.0


def test_robust_board_end_detection_has_no_one_degree_angle_cliff() -> None:
    image = np.full((420, 800, 3), 30, dtype=np.uint8)
    board = np.asarray(
        [
            (165, 85),
            (799, 255),
            (799, 405),
            (145, 265),
        ],
        dtype=np.int32,
    )
    cv2.fillConvexPoly(image, board, (205, 205, 205))
    frame = FramePayload.from_cv2_frame(image, mime_type="image/png")

    snapshots = []
    for angle, lower, upper, hough, minimum_length in (
        (13, 5, 35, 5, 5),
        (14, 240, 250, 250, 600),
    ):
        result = BoardEndCoordinateFrameHandler().execute(
            _context(),
            _node(
                "analysis.board_end_coordinate_frame",
                {
                    "board_end_side": "left",
                    "line_detector_mode": "robust_lsd",
                    "blur_kernel_size": 3,
                    "lower_threshold": lower,
                    "upper_threshold": upper,
                    "hough_threshold": hough,
                    "minimum_line_length_px": minimum_length,
                    "longitudinal_angle_tolerance_degrees": angle,
                    "minimum_board_end_span_percent": 25,
                    "minimum_longitudinal_span_percent": 30,
                    "maximum_reference_join_gap_px": 25,
                },
            ),
            {"frame": frame},
        )
        coordinate = result.outputs["coordinate_frame"].frame
        assert coordinate.source["found"] is True, coordinate.source
        snapshots.append(
            (
                coordinate.origin.x,
                coordinate.origin.y,
                tuple(coordinate.source["start_edge"]),
                tuple(coordinate.source["side_edge"]),
            )
        )

    first, second = snapshots
    assert first == second


def test_board_end_detection_rejects_tiny_coordinate_system_candidates() -> None:
    image = np.full((420, 800, 3), 30, dtype=np.uint8)
    cv2.rectangle(image, (330, 70), (799, 180), (215, 215, 215), thickness=-1)
    cv2.rectangle(image, (330, 180), (799, 350), (135, 135, 135), thickness=-1)
    cv2.line(image, (50, 35), (50, 100), (230, 230, 230), thickness=3)
    cv2.line(image, (50, 100), (160, 100), (230, 230, 230), thickness=3)
    frame = FramePayload.from_cv2_frame(image, mime_type="image/jpeg")

    result = BoardEndCoordinateFrameHandler().execute(
        _context(),
        _node(
            "analysis.board_end_coordinate_frame",
            {
                "board_end_side": "left",
                "blur_kernel_size": 3,
                "lower_threshold": 30,
                "upper_threshold": 100,
                "hough_threshold": 12,
                "minimum_line_length_px": 12,
                "max_line_gap_px": 20,
                "minimum_board_end_span_percent": 25,
                "minimum_longitudinal_span_percent": 30,
            },
        ),
        {"frame": frame},
    )

    coordinate = result.outputs["coordinate_frame"]
    assert coordinate.frame.source["found"] is True
    assert abs(coordinate.frame.origin.x - 330) <= 8
    assert coordinate.frame.source["color_region_rejected_end_segment_count"] > 0


def test_board_end_detection_does_not_join_unrelated_perpendicular_lines() -> None:
    image = np.full((300, 500, 3), 30, dtype=np.uint8)
    cv2.line(image, (100, 30), (100, 170), (230, 230, 230), thickness=3)
    cv2.line(image, (100, 225), (480, 225), (230, 230, 230), thickness=3)
    frame = FramePayload.from_cv2_frame(image, mime_type="image/jpeg")

    result = BoardEndCoordinateFrameHandler().execute(
        _context(),
        _node(
            "analysis.board_end_coordinate_frame",
            {
                "board_end_side": "left",
                "blur_kernel_size": 3,
                "lower_threshold": 30,
                "upper_threshold": 100,
                "hough_threshold": 10,
                "minimum_line_length_px": 10,
                "max_line_gap_px": 80,
                "maximum_reference_join_gap_px": 15,
                "minimum_board_end_span_percent": 25,
                "minimum_longitudinal_span_percent": 30,
            },
        ),
        {"frame": frame},
    )

    coordinate = result.outputs["coordinate_frame"]
    assert coordinate.frame.source["found"] is False, coordinate.frame.source
    assert coordinate.frame.source["reason_code"] == "reference_line_pair_missing"
    assert result.outputs["reference_edges"].edges == []


def test_board_end_prefers_surface_boundary_over_thin_internal_wood_line() -> None:
    image = np.full((420, 800, 3), 35, dtype=np.uint8)
    cv2.rectangle(image, (120, 70), (799, 260), (210, 210, 210), thickness=-1)
    cv2.rectangle(image, (120, 260), (799, 360), (125, 125, 125), thickness=-1)
    cv2.line(image, (145, 170), (760, 170), (45, 45, 45), thickness=3)
    frame = FramePayload.from_cv2_frame(image, mime_type="image/png")

    result = BoardEndCoordinateFrameHandler().execute(
        _context(),
        _node(
            "analysis.board_end_coordinate_frame",
            {
                "board_end_side": "left",
                "line_detector_mode": "robust_lsd",
                "blur_kernel_size": 3,
                "max_line_gap_px": 20,
                "maximum_reference_join_gap_px": 20,
                "minimum_board_end_span_percent": 25,
                "minimum_longitudinal_span_percent": 30,
            },
        ),
        {"frame": frame},
    )

    coordinate = result.outputs["coordinate_frame"].frame
    assert coordinate.source["found"] is True, coordinate.source
    surface_candidates = coordinate.source["surface_boundary_candidates"]

    def midpoint_y(candidate: dict) -> float:
        line = candidate["line"]
        return (float(line[1]) + float(line[3])) / 2.0

    seam_candidates = [
        candidate
        for candidate in surface_candidates
        if abs(midpoint_y(candidate) - 260.0) <= 12.0
    ]
    internal_candidates = [
        candidate
        for candidate in surface_candidates
        if abs(midpoint_y(candidate) - 170.0) <= 12.0
    ]
    assert seam_candidates, surface_candidates
    assert len(surface_candidates) <= 8
    assert all(
        min(candidate["line"][0], candidate["line"][2]) >= 100.0
        for candidate in seam_candidates
    )
    assert max(candidate["quality"] for candidate in seam_candidates) > max(
        (candidate["quality"] for candidate in internal_candidates),
        default=0.0,
    )
    selected_line = coordinate.source["side_edge"]
    selected_midpoint_y = (selected_line[1] + selected_line[3]) / 2.0
    assert abs(selected_midpoint_y - 170.0) > 20.0
    assert coordinate.source["surface_boundary_consensus_count"] >= 2
    board_edges = coordinate.source["board_edges"]
    assert board_edges["end"] == coordinate.source["start_edge"]
    assert board_edges["longitudinal_reference"] == selected_line
    assert len(board_edges["visible_longitudinal"]) >= 2


def test_board_color_region_rejects_table_shadow_on_bright_grey_setup() -> None:
    image = np.full((420, 800, 3), (185, 185, 185), dtype=np.uint8)
    cv2.rectangle(image, (145, 70), (799, 265), (180, 190, 198), thickness=-1)
    cv2.rectangle(image, (145, 265), (799, 350), (105, 145, 175), thickness=-1)
    cv2.line(image, (30, 390), (760, 375), (115, 115, 115), thickness=7)
    frame = FramePayload.from_cv2_frame(image, mime_type="image/png")

    result = BoardEndCoordinateFrameHandler().execute(
        _context(),
        _node(
            "analysis.board_end_coordinate_frame",
            {
                "board_end_side": "left",
                "line_detector_mode": "robust_lsd",
                "blur_kernel_size": 3,
                "maximum_reference_join_gap_px": 20,
                "minimum_board_end_span_percent": 25,
                "minimum_longitudinal_span_percent": 30,
            },
        ),
        {"frame": frame},
    )

    coordinate = result.outputs["coordinate_frame"].frame
    assert coordinate.source["board_color_region_found"] is True, coordinate.source
    assert coordinate.source["board_color_region"]["dark_background_mode"] is False
    assert (
        coordinate.source["board_color_region"]["color_expansion_threshold"]
        < coordinate.source["board_color_region"]["color_threshold"]
    )
    color_outline = coordinate.source["board_color_region_outline"]
    assert min(point[1] for point in color_outline) <= 85.0
    assert max(point[1] for point in color_outline) >= 335.0
    assert coordinate.source["found"] is True, coordinate.source
    assert abs(coordinate.origin.x - 145.0) <= 10.0
    assert coordinate.source["color_region_rejected_longitudinal_segment_count"] > 0
    assert all(
        max(candidate["line"][1], candidate["line"][3]) < 370.0
        for candidate in coordinate.source["surface_boundary_candidates"]
    )


def test_board_color_region_supports_black_production_background() -> None:
    image = np.full((420, 800, 3), (5, 5, 5), dtype=np.uint8)
    cv2.rectangle(image, (210, 80), (799, 250), (175, 195, 205), thickness=-1)
    cv2.rectangle(image, (210, 250), (799, 355), (100, 130, 150), thickness=-1)
    frame = FramePayload.from_cv2_frame(image, mime_type="image/png")

    result = BoardEndCoordinateFrameHandler().execute(
        _context(),
        _node(
            "analysis.board_end_coordinate_frame",
            {
                "board_end_side": "left",
                "line_detector_mode": "robust_lsd",
                "blur_kernel_size": 3,
                "maximum_reference_join_gap_px": 20,
                "minimum_board_end_span_percent": 25,
                "minimum_longitudinal_span_percent": 30,
            },
        ),
        {"frame": frame},
    )

    coordinate = result.outputs["coordinate_frame"].frame
    assert coordinate.source["board_color_region_found"] is True, coordinate.source
    assert coordinate.source["board_color_region"]["dark_background_mode"] is True
    assert coordinate.source["found"] is True, coordinate.source
    assert abs(coordinate.origin.x - 210.0) <= 10.0


def test_board_color_tolerance_expands_dark_side_seed_into_pale_top() -> None:
    image = np.full((420, 800, 3), (185, 185, 185), dtype=np.uint8)
    cv2.rectangle(image, (145, 70), (799, 265), (180, 190, 198), thickness=-1)
    cv2.rectangle(image, (145, 265), (799, 350), (105, 145, 175), thickness=-1)

    strict = _detect_board_color_region(
        image,
        board_end_side="left",
        color_tolerance_fraction=0.20,
    )
    tolerant = _detect_board_color_region(
        image,
        board_end_side="left",
        color_tolerance_fraction=0.75,
    )

    assert strict is not None
    assert tolerant is not None
    assert min(point[1] for point in strict.outline) >= 250.0
    assert min(point[1] for point in tolerant.outline) <= 85.0
    assert tolerant.source["area_fraction"] > strict.source["area_fraction"] * 2.5


def test_board_region_uses_wood_texture_when_top_colour_matches_background() -> None:
    image = np.full((420, 800, 3), (185, 185, 185), dtype=np.uint8)
    cv2.rectangle(image, (145, 70), (799, 265), (183, 185, 187), thickness=-1)
    for y in range(78, 260, 8):
        cv2.line(image, (150, y), (799, y + 3), (170, 174, 180), thickness=1)
    cv2.rectangle(image, (145, 265), (799, 350), (105, 145, 175), thickness=-1)

    region = _detect_board_color_region(
        image,
        board_end_side="left",
        color_tolerance_fraction=0.75,
    )

    assert region is not None
    assert region.source["texture_threshold"] is not None
    assert region.source["texture_envelope"] is not None
    assert min(point[1] for point in region.outline) <= 85.0
    assert max(point[1] for point in region.outline) >= 335.0


def test_edge_supported_refinement_extends_and_stabilizes_fragmented_line() -> None:
    y_values = np.arange(20.0, 221.0)
    x_values = 100.0 - y_values * 0.08
    edge_points = np.column_stack((x_values, y_values))
    direction = np.asarray((-0.08, 1.0), dtype=np.float64)
    direction /= np.linalg.norm(direction)
    normal = np.asarray((-direction[1], direction[0]), dtype=np.float64)
    edge_gradients = np.repeat(normal.reshape(1, 2), len(edge_points), axis=0)

    refined = _refine_lines_from_edge_support(
        [(94.4, 70.0, 87.2, 160.0)],
        edge_points=edge_points,
        edge_gradients=edge_gradients,
        band_tolerance=3.0,
        extension_tolerance=70.0,
        support_gap=4.0,
    )

    assert len(refined) == 1
    line, support = refined[0]
    assert min(line[1], line[3]) <= 22
    assert max(line[1], line[3]) >= 218
    assert support >= 0.8


def test_slanted_board_image_uses_calibration_edges_instead_of_internal_line() -> None:
    image = np.full((400, 600, 3), 30, dtype=np.uint8)
    cv2.fillPoly(
        image,
        [np.asarray([(120, 60), (599, 60), (599, 180), (100, 180)], dtype=np.int32)],
        (215, 215, 215),
    )
    cv2.fillPoly(
        image,
        [np.asarray([(100, 180), (599, 180), (599, 330), (75, 330)], dtype=np.int32)],
        (135, 135, 135),
    )
    cv2.line(image, (87, 260), (599, 260), (225, 225, 225), thickness=3)
    frame = FramePayload.from_cv2_frame(image, mime_type="image/jpeg")
    coordinate_result = BoardEndCoordinateFrameHandler().execute(
        _context(),
        _node(
            "analysis.board_end_coordinate_frame",
            {
                "board_end_side": "left",
                "blur_kernel_size": 3,
                "lower_threshold": 30,
                "upper_threshold": 100,
                "hough_threshold": 15,
                "minimum_line_length_px": 25,
                "max_line_gap_px": 40,
                "axis_angle_tolerance_degrees": 30,
            },
        ),
        {"frame": frame},
    )
    config = _longitudinal_config("upper")
    config["safe_visibility_margin_mm"] = 50
    config["calibration_pose_boundary_tolerance_px"] = 24
    config["surfaces"] = [
        {
            "surface_role": "top",
            "image_points": [
                {"x": 120 / 600, "y": 60 / 400},
                {"x": 1.0, "y": 60 / 400},
                {"x": 1.0, "y": 180 / 400},
                {"x": 100 / 600, "y": 180 / 400},
            ],
            "world_points_mm": [
                {"x": 0, "y": 0},
                {"x": 600, "y": 0},
                {"x": 600, "y": 100},
                {"x": 0, "y": 100},
            ],
        },
        {
            "surface_role": "side_a",
            "image_points": [
                {"x": 100 / 600, "y": 180 / 400},
                {"x": 1.0, "y": 180 / 400},
                {"x": 1.0, "y": 330 / 400},
                {"x": 75 / 600, "y": 330 / 400},
            ],
            "world_points_mm": [
                {"x": 0, "y": 0},
                {"x": 600, "y": 0},
                {"x": 600, "y": 100},
                {"x": 0, "y": 100},
            ],
        },
    ]
    result = BoardViewCutConstraintsHandler().execute(
        _context(),
        _node("analysis.board_view_cut_constraints", config),
        {
            "frame": frame,
            "coordinate_frame": coordinate_result.outputs["coordinate_frame"],
            "features": DetectionPayload(
                detections=[],
                image_width=600,
                image_height=400,
                image_context_id="ctx_board",
            ),
        },
    )

    payload = result.outputs["constraints"]
    assert payload.valid is True, (payload.status, payload.reason_code)
    assert payload.metadata["board_pose"]["method"] == "calibration_guided_hough_pose"
    assert payload.metadata["board_pose"]["matched_calibration_boundaries"] == 3
    selected_longitudinal = payload.metadata["board_pose"]["longitudinal_line"]
    selected_y = (selected_longitudinal[1] + selected_longitudinal[3]) / 2
    assert min(abs(selected_y - expected) for expected in (60, 180, 330)) < 8
    assert abs(selected_y - 260) > 20


def test_axis_segment_merge_preserves_transitive_connections() -> None:
    merged = _merge_axis_line_segments(
        [
            (10.0, 0.0, 10.0, 10.0),
            (11.0, 14.0, 11.0, 24.0),
            (12.0, 28.0, 12.0, 38.0),
        ],
        axis="vertical",
        position_tolerance=2.0,
        gap_tolerance=4.0,
    )

    assert len(merged) == 1
    assert min(merged[0][1], merged[0][3]) == 0.0
    assert max(merged[0][1], merged[0][3]) == 38.0


def test_free_oriented_segment_merge_keeps_a_slanted_board_end() -> None:
    merged = _merge_free_oriented_line_segments(
        [
            (30.0, 10.0, 38.0, 45.0),
            (39.0, 49.0, 47.0, 84.0),
        ],
        position_tolerance=3.0,
        gap_tolerance=6.0,
        angle_tolerance_degrees=5.0,
    )

    assert len(merged) == 1
    line = merged[0]
    assert line[2] - line[0] > 15.0
    assert line[3] - line[1] > 65.0


def _coordinates() -> CoordinateFramePayload:
    return CoordinateFramePayload(
        frame=InspectionCoordinateFrame(
            frame_id="board_frame",
            image_context_id="ctx_board",
            origin=InspectionPoint(x=0, y=25),
            longitudinal_axis=InspectionVector(x=1, y=0),
            transverse_axis=InspectionVector(x=0, y=1),
            confidence=0.99,
        )
    )


def _shifted_coordinates(
    *,
    board_end_x: float,
    board_end_y: float = 25.0,
    visible_end_start_y: float = 0.0,
    visible_end_end_y: float = 100.0,
) -> CoordinateFramePayload:
    return CoordinateFramePayload(
        frame=InspectionCoordinateFrame(
            frame_id="board_frame",
            image_context_id="ctx_board",
            origin=InspectionPoint(x=board_end_x, y=board_end_y),
            longitudinal_axis=InspectionVector(x=1, y=0),
            transverse_axis=InspectionVector(x=0, y=1),
            confidence=0.99,
            source={
                "found": True,
                "start_edge": [
                    board_end_x,
                    visible_end_start_y,
                    board_end_x,
                    visible_end_end_y,
                ],
            },
        )
    )


def _guided_coordinates(*candidate_lines: list[float]) -> CoordinateFramePayload:
    return CoordinateFramePayload(
        frame=InspectionCoordinateFrame(
            frame_id="board_frame",
            image_context_id="ctx_board",
            origin=InspectionPoint(x=20, y=65),
            longitudinal_axis=InspectionVector(x=1, y=0),
            transverse_axis=InspectionVector(x=0, y=1),
            confidence=0.9,
            source={
                "method": "canny_hough_merged_reference_pair",
                "found": True,
                "board_end_side": "left",
                "start_edge": [20, 20, 20, 85],
                "side_edge": [20, 65, 100, 65],
                "calibration_candidates": list(candidate_lines),
            },
        )
    )


def _detections(*boxes: DetectionBox) -> DetectionPayload:
    return DetectionPayload(
        detections=[DetectionPrediction(label="knot", score=0.9, box=box) for box in boxes],
        image_width=100,
        image_height=100,
        image_context_id="ctx_board",
    )


def _longitudinal_config(view_role: str) -> dict:
    roles = ("top", "side_a") if view_role == "upper" else ("bottom", "side_b")
    return {
        "view_role": view_role,
        "inspection_length_mm": 300.0,
        "safe_visibility_margin_mm": 100.0,
        "calibration_rms_mm": 2.0,
        "max_calibration_rms_mm": 2.0,
        "uncertainty_multiplier": 3.0,
        "minimum_board_end_confidence": 0.5,
        "surfaces": [
            _surface(roles[0], 0.0, 0.5),
            _surface(roles[1], 0.5, 1.0),
        ],
        "class_rules": [_rule()],
    }


def _offset_longitudinal_config(view_role: str = "upper") -> dict:
    config = _longitudinal_config(view_role)
    roles = ("top", "side_a") if view_role == "upper" else ("bottom", "side_b")
    config["surfaces"] = [
        {
            **_surface(roles[0], 0.0, 0.5),
            "image_points": [
                {"x": 0.4, "y": 0.0},
                {"x": 1.0, "y": 0.0},
                {"x": 1.0, "y": 0.5},
                {"x": 0.4, "y": 0.5},
            ],
        },
        {
            **_surface(roles[1], 0.5, 1.0),
            "image_points": [
                {"x": 0.4, "y": 0.5},
                {"x": 1.0, "y": 0.5},
                {"x": 1.0, "y": 1.0},
                {"x": 0.4, "y": 1.0},
            ],
        },
    ]
    return config


def _tilted_longitudinal_config() -> dict:
    config = _longitudinal_config("upper")
    config["safe_visibility_margin_mm"] = 50.0
    config["surfaces"] = [
        {
            "surface_role": "top",
            "image_points": [
                {"x": 0.20, "y": 0.10},
                {"x": 1.00, "y": 0.10},
                {"x": 1.00, "y": 0.45},
                {"x": 0.16, "y": 0.45},
            ],
            "world_points_mm": [
                {"x": 0.0, "y": 0.0},
                {"x": 500.0, "y": 0.0},
                {"x": 500.0, "y": 100.0},
                {"x": 0.0, "y": 100.0},
            ],
        },
        {
            "surface_role": "side_a",
            "image_points": [
                {"x": 0.16, "y": 0.45},
                {"x": 1.00, "y": 0.45},
                {"x": 1.00, "y": 0.80},
                {"x": 0.12, "y": 0.80},
            ],
            "world_points_mm": [
                {"x": 0.0, "y": 0.0},
                {"x": 500.0, "y": 0.0},
                {"x": 500.0, "y": 100.0},
                {"x": 0.0, "y": 100.0},
            ],
        },
    ]
    return config


def _constraints(role: str, *items: BoardFeatureConstraint) -> BoardCutConstraintsPayload:
    return BoardCutConstraintsPayload(
        workpiece_id="plc-cycle-123",
        view_role=role,
        inspection_length_mm=300,
        calibrated_limit_mm=400,
        constraints=list(items),
    )


def _constraint(role: str, required_cut_mm: float) -> BoardFeatureConstraint:
    surface = {"upper": "top", "lower": "bottom", "end_face": "end_face"}[role]
    return BoardFeatureConstraint(
        constraint_id=f"{role}:knot",
        view_role=role,
        surface_role=surface,
        feature_id=f"{role}-feature",
        class_id=0,
        label_snapshot="knot",
        confidence=0.9,
        measurement_basis="max_feret_diameter",
        feature_size_mm=10,
        clearance_mm=10,
        uncertainty_mm=1,
        required_cut_mm=required_cut_mm,
    )


def _longitudinal_constraint(
    role: str,
    *,
    feature_id: str,
    start_mm: float,
    end_mm: float,
    clearance_mm: float,
    uncertainty_mm: float = 0.0,
    clipped: bool = False,
) -> BoardFeatureConstraint:
    required_cut_mm = end_mm + clearance_mm + uncertainty_mm
    return BoardFeatureConstraint(
        constraint_id=f"{role}:{feature_id}",
        view_role=role,
        surface_role="top" if role == "upper" else "bottom",
        feature_id=feature_id,
        class_id=0,
        label_snapshot="knot",
        confidence=0.9,
        measurement_basis="max_feret_diameter",
        feature_start_mm=start_mm,
        feature_end_mm=end_mm,
        feature_size_mm=clearance_mm,
        clearance_mm=clearance_mm,
        uncertainty_mm=uncertainty_mm,
        required_cut_mm=required_cut_mm,
        clipped=clipped,
    )


def _frame_set(*, valid: bool = True, skew: float = 8.4) -> SynchronizedFrameSetPayload:
    return SynchronizedFrameSetPayload(
        workpiece_id="plc-cycle-123",
        trigger_id="trigger-1",
        frames=[
            SynchronizedCameraFrame(role=role, channel_id=f"channel-{role}", frame=_frame())
            for role in ("upper", "lower", "end_face")
        ],
        capture_skew_ms=skew,
        valid=valid,
        status="captured" if valid else "capture_not_synchronized",
    )


def _single_frame_set(role: str = "upper") -> SynchronizedFrameSetPayload:
    return SynchronizedFrameSetPayload(
        workpiece_id="plc-cycle-123",
        trigger_id="trigger-1",
        frames=[
            SynchronizedCameraFrame(
                role=role,
                channel_id=f"channel-{role}",
                frame=_frame(),
            )
        ],
        capture_skew_ms=0,
        max_capture_skew_ms=0,
        valid=True,
        status="captured",
        metadata={"single_camera_test": True},
    )


def test_longitudinal_constraint_uses_feature_end_diameter_and_uncertainty() -> None:
    result = BoardViewCutConstraintsHandler().execute(
        _context(),
        _node("analysis.board_view_cut_constraints", _longitudinal_config("upper")),
        {
            "frame": _frame(),
            "coordinate_frame": _coordinates(),
            "features": _detections(DetectionBox(x_min=0.10, y_min=0.10, x_max=0.15, y_max=0.20)),
        },
    )
    payload = result.outputs["constraints"]
    assert isinstance(payload, BoardCutConstraintsPayload)
    assert payload.valid is True, (payload.status, payload.reason_code)
    assert payload.uncertainty_mm == 6.0
    assert len(payload.constraints) == 1
    constraint = payload.constraints[0]
    assert constraint.feature_end_mm == 60.0
    assert round(constraint.feature_size_mm, 3) == 28.284
    assert round(constraint.required_cut_mm, 3) == 94.284
    assert round(constraint.metadata["required_free_before_feature_mm"], 3) == 34.284
    assert round(constraint.metadata["required_free_after_feature_mm"], 3) == 34.284


def test_longitudinal_surfaces_follow_a_board_shifted_from_the_calibration_position() -> None:
    config = _offset_longitudinal_config()
    result = BoardViewCutConstraintsHandler().execute(
        _context(),
        _node("analysis.board_view_cut_constraints", config),
        {
            "frame": _frame(),
            "coordinate_frame": _shifted_coordinates(
                board_end_x=10,
                board_end_y=20,
                visible_end_start_y=20,
            ),
            "features": _detections(
                DetectionBox(x_min=0.15, y_min=0.55, x_max=0.20, y_max=0.60)
            ),
        },
    )

    payload = result.outputs["constraints"]
    assert payload.valid is True
    assert len(payload.constraints) == 1
    assert payload.constraints[0].feature_end_mm > 60
    assert payload.constraints[0].surface_role == "top"
    assert payload.metadata["surface_tracking_mode"] == "follow_detected_board"
    overlay = result.outputs["overlay"]
    assert any(primitive.layer == "board_dynamic_surface" for primitive in overlay.primitives)


def test_shifted_board_fails_closed_when_safe_inspection_is_outside_the_image() -> None:
    result = BoardViewCutConstraintsHandler().execute(
        _context(),
        _node("analysis.board_view_cut_constraints", _longitudinal_config("upper")),
        {
            "frame": _frame(),
            "coordinate_frame": _shifted_coordinates(board_end_x=40),
            "features": _detections(),
        },
    )

    payload = result.outputs["constraints"]
    assert payload.valid is False
    assert payload.status == "safe_cut_outside_inspection_range"
    assert payload.reason_code == "tracked_board_does_not_cover_inspection_and_safety_margin"


def test_calibration_guided_pose_uses_4_to_1_and_ignores_far_2_to_3_edge() -> None:
    result = BoardViewCutConstraintsHandler().execute(
        _context(),
        _node("analysis.board_view_cut_constraints", _tilted_longitudinal_config()),
        {
            "frame": _frame(),
            "coordinate_frame": _guided_coordinates(
                [25, 15, 17, 85],
                [25, 15, 100, 15],
                [21, 50, 100, 50],
                [17, 85, 100, 85],
                [19, 65, 100, 65],
                [100, 15, 100, 85],
            ),
            "features": _detections(
                DetectionBox(x_min=0.30, y_min=0.25, x_max=0.35, y_max=0.32)
            ),
        },
    )

    payload = result.outputs["constraints"]
    assert payload.valid is True
    assert payload.metadata["board_pose"]["method"] == "calibration_guided_hough_pose"
    assert payload.metadata["board_pose"]["matched_calibration_boundaries"] == 3
    assert payload.metadata["board_pose"]["point_2_to_3_used_for_pose"] is False
    assert payload.metadata["board_pose"]["board_end_reference_surface"] == "top"
    assert payload.metadata["board_pose"]["board_end_reference_is_image_vertical"] is False
    selected_end = payload.metadata["board_pose"]["board_end_line"]
    assert max(selected_end[0], selected_end[2]) < 50
    top_mapping = next(
        mapping
        for mapping in payload.metadata["surface_mappings"]
        if mapping["surface_role"] == "top"
    )
    first_point = top_mapping["current_image_polygon"][0]
    assert abs(first_point["x"] - 0.25) < 0.02
    assert abs(first_point["y"] - 0.15) < 0.02
    overlay = result.outputs["overlay"]
    assert any(
        primitive.layer == "calibrated_board_end"
        for primitive in overlay.primitives
    )


def test_calibration_guided_pose_prefers_silhouette_over_internal_line() -> None:
    result = BoardViewCutConstraintsHandler().execute(
        _context(),
        _node("analysis.board_view_cut_constraints", _tilted_longitudinal_config()),
        {
            "frame": _frame(),
            "coordinate_frame": _guided_coordinates(
                [25, 15, 17, 85],
                [65, 15, 57, 85],
                [25, 15, 100, 15],
                [21, 50, 100, 50],
                [17, 85, 100, 85],
            ),
            "features": _detections(),
        },
    )

    payload = result.outputs["constraints"]
    assert payload.valid is True
    selected_end = payload.metadata["board_pose"]["board_end_line"]
    assert max(selected_end[0], selected_end[2]) < 40
    assert (
        payload.metadata["board_pose"]["leading_edge_selection"]
        == "leftmost_complete_calibration_match"
    )
    assert payload.metadata["board_pose"]["preliminary_gate_enabled"] is True
    assert payload.metadata["board_pose"]["preliminary_gate_rejection_count"] >= 1


def test_calibration_guided_pose_falls_back_to_preliminary_cross_when_hough_is_inside_board(
) -> None:
    result = BoardViewCutConstraintsHandler().execute(
        _context(),
        _node("analysis.board_view_cut_constraints", _tilted_longitudinal_config()),
        {
            "frame": _frame(),
            "coordinate_frame": _guided_coordinates(
                [65, 15, 57, 85],
                [65, 15, 100, 15],
                [61, 50, 100, 50],
                [57, 85, 100, 85],
            ),
            "features": _detections(),
        },
    )

    payload = result.outputs["constraints"]
    assert payload.valid is True, (payload.status, payload.reason_code)
    assert (
        payload.metadata["board_pose"]["method"]
        == "preliminary_anchored_calibration_pose"
    )
    assert (
        payload.metadata["board_pose"]["raw_hough_candidates_used_for_surface"]
        is False
    )
    assert payload.metadata["board_pose"]["preliminary_gate_rejection_count"] >= 1
    top_mapping = next(
        mapping
        for mapping in payload.metadata["surface_mappings"]
        if mapping["surface_role"] == "top"
    )
    assert abs(top_mapping["current_image_polygon"][0]["x"] - 0.20) < 0.02
    overlay = result.outputs["overlay"]
    assert any(
        primitive.layer == "calibrated_board_end"
        and primitive.label == "Preliminary-anchored board end"
        for primitive in overlay.primitives
    )
    assert any(
        primitive.layer == "board_dynamic_surface"
        for primitive in overlay.primitives
    )


def test_guided_surfaces_follow_each_perspective_boundary_instead_of_rigid_rotation() -> None:
    result = BoardViewCutConstraintsHandler().execute(
        _context(),
        _node("analysis.board_view_cut_constraints", _tilted_longitudinal_config()),
        {
            "frame": _frame(),
            "coordinate_frame": _guided_coordinates(
                [25, 15, 17, 85],
                [25, 15, 100, 22],
                [21, 50, 100, 50],
                [17, 85, 100, 78],
                [100, 15, 100, 85],
            ),
            "features": _detections(),
        },
    )

    payload = result.outputs["constraints"]
    assert payload.valid is True
    assert payload.metadata["board_pose"]["surface_projection"] == (
        "detected_edges_to_image_boundary"
    )
    mappings = {
        mapping["surface_role"]: mapping
        for mapping in payload.metadata["surface_mappings"]
    }
    top_polygon = mappings["top"]["current_image_polygon"]
    side_polygon = mappings["side_a"]["current_image_polygon"]
    assert abs(top_polygon[1]["y"] - 0.22) < 0.02
    assert abs(top_polygon[2]["y"] - 0.50) < 0.02
    assert abs(side_polygon[1]["y"] - 0.50) < 0.02
    assert abs(side_polygon[2]["y"] - 0.78) < 0.02
    assert abs(top_polygon[1]["y"] - top_polygon[2]["y"]) > 0.20


def test_side_surface_and_cut_line_use_their_own_perspective_board_end() -> None:
    config = _tilted_longitudinal_config()
    config["surfaces"][1]["image_points"][3]["x"] = 0.18
    constraint_result = BoardViewCutConstraintsHandler().execute(
        _context(),
        _node("analysis.board_view_cut_constraints", config),
        {
            "frame": _frame(),
            "coordinate_frame": _guided_coordinates(
                [25, 15, 21, 50],
                [21, 50, 23, 85],
                [25, 15, 100, 22],
                [21, 50, 100, 50],
                [23, 85, 100, 78],
                [100, 15, 100, 85],
            ),
            "features": _detections(
                DetectionBox(x_min=0.30, y_min=0.55, x_max=0.35, y_max=0.62)
            ),
        },
    )

    payload = constraint_result.outputs["constraints"]
    assert payload.valid is True
    surface_ends = payload.metadata["board_pose"]["surface_end_lines"]
    top_end = surface_ends["top"]["line"]
    side_end = surface_ends["side_a"]["line"]
    assert top_end[2:] == side_end[:2]
    assert surface_ends["side_a"]["source"] == "calibrated_surface_direction"
    top_extension_x_at_side_bottom = top_end[2] + (
        (side_end[3] - top_end[3])
        * (top_end[2] - top_end[0])
        / (top_end[3] - top_end[1])
    )
    assert abs(side_end[2] - top_extension_x_at_side_bottom) > 4.0

    mappings = {
        mapping["surface_role"]: mapping
        for mapping in payload.metadata["surface_mappings"]
    }
    for mapping in mappings.values():
        homography = np.asarray(mapping["homography_image_to_world"])
        current_polygon = [
            (point["x"], point["y"])
            for point in mapping["current_image_polygon"]
        ]
        expected_world = [(0, 0), (500, 0), (500, 100), (0, 100)]
        for point, expected in zip(current_polygon, expected_world, strict=True):
            vector = homography @ np.asarray([point[0], point[1], 1.0])
            projected = vector[:2] / vector[2]
            assert np.allclose(projected, expected, atol=1e-6)

    decision_result = SingleViewBoardCutDecisionHandler().execute(
        _context(),
        _node(
            "analysis.single_view_board_cut_decision",
            {"inspection_length_mm": 300, "show_cut_line_overlay": True},
        ),
        {
            "constraints": payload,
            "frame_set": _single_frame_set("upper"),
        },
    )
    cut_lines = {
        primitive.metadata["surface_role"]: primitive
        for primitive in decision_result.outputs["overlay"].primitives
        if primitive.layer == "board_cut_line"
    }
    assert set(cut_lines) == {"top", "side_a"}
    top_cut = cut_lines["top"].geometry
    side_cut = cut_lines["side_a"].geometry
    assert abs(top_cut["end"]["x"] - side_cut["start"]["x"]) < 1e-12
    assert abs(top_cut["end"]["y"] - side_cut["start"]["y"]) < 1e-12
    assert side_cut["end"]["x"] > side_cut["start"]["x"]


def test_realtime_board_pose_smooths_jitter_and_bridges_short_detection_gaps() -> None:
    context = _context()
    context.mode = RuntimeMode.REALTIME
    handler = BoardViewCutConstraintsHandler()
    node = _node(
        "analysis.board_view_cut_constraints",
        _tilted_longitudinal_config(),
    )

    def execute_with_shift(shift: float) -> BoardCutConstraintsPayload:
        result = handler.execute(
            context,
            node,
            {
                "frame": _frame(),
                "coordinate_frame": _guided_coordinates(
                    [25 + shift, 15, 17 + shift, 85],
                    [25 + shift, 15, 100 + shift, 15],
                    [21 + shift, 50, 100 + shift, 50],
                    [17 + shift, 85, 100 + shift, 85],
                ),
                "features": _detections(),
            },
        )
        return result.outputs["constraints"]

    first = execute_with_shift(0)
    second = execute_with_shift(10)
    first_top = next(
        mapping
        for mapping in first.metadata["surface_mappings"]
        if mapping["surface_role"] == "top"
    )
    second_top = next(
        mapping
        for mapping in second.metadata["surface_mappings"]
        if mapping["surface_role"] == "top"
    )
    first_x = first_top["current_image_polygon"][0]["x"]
    second_x = second_top["current_image_polygon"][0]["x"]
    assert first.valid is True
    assert second.valid is True
    assert first_x < second_x < first_x + 0.05
    assert (
        second.metadata["board_pose"]["temporal_stabilization"]["status"]
        == "smoothed"
    )

    missing_inputs = {
        "frame": _frame(),
        "coordinate_frame": _guided_coordinates(),
        "features": _detections(),
    }
    for missed_frame in range(1, 5):
        held = handler.execute(context, node, missing_inputs).outputs["constraints"]
        assert held.valid is True
        temporal = held.metadata["board_pose"]["temporal_stabilization"]
        assert temporal["status"] == "held_after_missing_detection"
        assert temporal["missed_frames"] == missed_frame
    expired = handler.execute(context, node, missing_inputs).outputs["constraints"]
    assert expired.valid is False
    assert expired.reason_code == "calibration_guided_pose_candidates_missing"


def test_calibration_guided_pose_fails_closed_for_two_equally_good_boards() -> None:
    result = BoardViewCutConstraintsHandler().execute(
        _context(),
        _node("analysis.board_view_cut_constraints", _tilted_longitudinal_config()),
        {
            "frame": _frame(),
            "coordinate_frame": _guided_coordinates(
                [25, 15, 17, 85],
                [25, 15, 100, 15],
                [21, 50, 100, 50],
                [17, 85, 100, 85],
                [45, 15, 37, 85],
                [45, 15, 100, 15],
                [41, 50, 100, 50],
                [37, 85, 100, 85],
            ),
            "features": _detections(),
        },
    )

    payload = result.outputs["constraints"]
    assert payload.valid is False
    assert payload.status == "board_end_not_found"
    assert payload.reason_code == "calibration_guided_pose_ambiguous"


def test_end_face_feature_uses_default_two_diameters() -> None:
    config = {
        "inspection_length_mm": 300,
        "calibration_rms_mm": 1,
        "max_calibration_rms_mm": 2,
        "surface": {
            "surface_role": "end_face",
            "image_points": [
                {"x": 0, "y": 0},
                {"x": 1, "y": 0},
                {"x": 1, "y": 1},
                {"x": 0, "y": 1},
            ],
            "world_points_mm": [
                {"x": 0, "y": 0},
                {"x": 100, "y": 0},
                {"x": 100, "y": 30},
                {"x": 0, "y": 30},
            ],
        },
        "class_rules": [_rule()],
    }
    result = EndFaceCutConstraintsHandler().execute(
        _context(),
        _node("analysis.end_face_cut_constraints", config),
        {"features": _detections(DetectionBox(x_min=0.2, y_min=0.2, x_max=0.4, y_max=0.4))},
    )
    payload = result.outputs["constraints"]
    assert isinstance(payload, BoardCutConstraintsPayload)
    diameter = payload.constraints[0].feature_size_mm
    assert round(payload.constraints[0].required_cut_mm, 6) == round(2 * diameter, 6)


def test_fusion_returns_valid_exact_zero_for_three_clear_views() -> None:
    result = MultiViewBoardCutFusionHandler().execute(
        _context(),
        _node("analysis.multi_view_board_cut_fusion", {"inspection_length_mm": 300}),
        {
            "upper": _constraints("upper"),
            "lower": _constraints("lower"),
            "end_face": _constraints("end_face"),
            "frame_set": _frame_set(),
        },
    )
    decision = result.outputs["decision"].decision
    assert decision.valid is True
    assert decision.status == "no_cut_required"
    assert decision.cut_position_mm == 0
    assert decision.longitudinal_shift_mm == 0
    assert decision.views_used == ["upper", "lower", "end_face"]


def test_fusion_uses_largest_constraint_without_cross_camera_matching() -> None:
    result = MultiViewBoardCutFusionHandler().execute(
        _context(),
        _node("analysis.multi_view_board_cut_fusion", {"inspection_length_mm": 300}),
        {
            "upper": _constraints("upper", _constraint("upper", 80)),
            "lower": _constraints("lower", _constraint("lower", 125)),
            "end_face": _constraints("end_face", _constraint("end_face", 42)),
            "frame_set": _frame_set(),
        },
    )
    decision = result.outputs["decision"].decision
    assert decision.valid is True
    assert decision.status == "cut_required"
    assert decision.longitudinal_shift_mm == 125
    assert decision.dominant_constraint is not None
    assert decision.dominant_constraint.view_role == "lower"


def test_fusion_rejects_different_constraint_inspection_lengths() -> None:
    upper = _constraints("upper")
    upper.inspection_length_mm = 250
    result = MultiViewBoardCutFusionHandler().execute(
        _context(),
        _node("analysis.multi_view_board_cut_fusion", {"inspection_length_mm": 300}),
        {
            "upper": upper,
            "lower": _constraints("lower"),
            "end_face": _constraints("end_face"),
            "frame_set": _frame_set(),
        },
    )

    decision = result.outputs["decision"].decision
    assert decision.valid is False
    assert decision.status == "calibration_invalid"
    assert decision.reason_code == "inspection_length_mismatch:upper"


def test_distant_feature_at_inspection_boundary_allows_a_valid_zero_cut() -> None:
    constraint_result = BoardViewCutConstraintsHandler().execute(
        _context(),
        _node("analysis.board_view_cut_constraints", _longitudinal_config("upper")),
        {
            "frame": _frame(),
            "coordinate_frame": _coordinates(),
            "features": _detections(DetectionBox(x_min=0.74, y_min=0.10, x_max=0.76, y_max=0.20)),
        },
    )
    payload = constraint_result.outputs["constraints"]
    assert payload.valid is True
    assert payload.constraints[0].clipped is True
    decision_result = SingleViewBoardCutDecisionHandler().execute(
        _context(),
        _node(
            "analysis.single_view_board_cut_decision",
            {"inspection_length_mm": 300},
        ),
        {
            "constraints": payload,
            "frame_set": _single_frame_set("upper"),
        },
    )
    decision = decision_result.outputs["decision"].decision
    assert decision.valid is True
    assert decision.status == "no_cut_required"
    assert decision.longitudinal_shift_mm == 0


def test_single_view_returns_zero_when_clearance_before_first_feature_is_free() -> None:
    distant = _longitudinal_constraint(
        "upper",
        feature_id="distant-knot",
        start_mm=80,
        end_mm=100,
        clearance_mm=20,
        uncertainty_mm=3,
    )
    result = SingleViewBoardCutDecisionHandler().execute(
        _context(),
        _node(
            "analysis.single_view_board_cut_decision",
            {"inspection_length_mm": 300},
        ),
        {
            "constraints": _constraints("upper", distant),
            "frame_set": _single_frame_set("upper"),
        },
    )

    decision = result.outputs["decision"].decision
    assert decision.valid is True
    assert decision.status == "no_cut_required"
    assert decision.longitudinal_shift_mm == 0
    assert decision.dominant_constraint is None


def test_single_view_cuts_a_feature_inside_its_required_front_clearance() -> None:
    close = _longitudinal_constraint(
        "upper",
        feature_id="close-knot",
        start_mm=10,
        end_mm=30,
        clearance_mm=20,
        uncertainty_mm=3,
    )
    result = SingleViewBoardCutDecisionHandler().execute(
        _context(),
        _node(
            "analysis.single_view_board_cut_decision",
            {"inspection_length_mm": 300},
        ),
        {
            "constraints": _constraints("upper", close),
            "frame_set": _single_frame_set("upper"),
        },
    )

    decision = result.outputs["decision"].decision
    assert decision.valid is True
    assert decision.status == "cut_required"
    assert decision.longitudinal_shift_mm == 53
    assert decision.dominant_constraint is not None
    assert decision.dominant_constraint.feature_id == "close-knot"


def test_fusion_expands_a_cut_chain_across_camera_views() -> None:
    first = _longitudinal_constraint(
        "upper",
        feature_id="upper-close",
        start_mm=5,
        end_mm=20,
        clearance_mm=10,
        uncertainty_mm=1,
    )
    second = _longitudinal_constraint(
        "lower",
        feature_id="lower-following",
        start_mm=35,
        end_mm=50,
        clearance_mm=10,
        uncertainty_mm=1,
    )
    distant = _longitudinal_constraint(
        "upper",
        feature_id="upper-distant",
        start_mm=100,
        end_mm=115,
        clearance_mm=10,
        uncertainty_mm=1,
    )
    result = MultiViewBoardCutFusionHandler().execute(
        _context(),
        _node(
            "analysis.multi_view_board_cut_fusion",
            {"inspection_length_mm": 300},
        ),
        {
            "upper": _constraints("upper", first, distant),
            "lower": _constraints("lower", second),
            "end_face": _constraints("end_face"),
            "frame_set": _frame_set(),
        },
    )

    decision = result.outputs["decision"].decision
    assert decision.valid is True
    assert decision.status == "cut_required"
    assert decision.longitudinal_shift_mm == 61
    assert decision.dominant_constraint is not None
    assert decision.dominant_constraint.feature_id == "lower-following"


def test_cut_chain_stops_when_gap_contains_clearance_after_and_before_features() -> None:
    first = _longitudinal_constraint(
        "upper",
        feature_id="first-knot",
        start_mm=5,
        end_mm=20,
        clearance_mm=10,
    )
    second = _longitudinal_constraint(
        "lower",
        feature_id="second-knot",
        start_mm=40,
        end_mm=55,
        clearance_mm=10,
    )
    result = MultiViewBoardCutFusionHandler().execute(
        _context(),
        _node(
            "analysis.multi_view_board_cut_fusion",
            {"inspection_length_mm": 300},
        ),
        {
            "upper": _constraints("upper", first),
            "lower": _constraints("lower", second),
            "end_face": _constraints("end_face"),
            "frame_set": _frame_set(),
        },
    )

    decision = result.outputs["decision"].decision
    assert decision.valid is True
    assert decision.status == "cut_required"
    assert decision.longitudinal_shift_mm == 30
    assert decision.dominant_constraint is not None
    assert decision.dominant_constraint.feature_id == "first-knot"


def test_active_clipped_feature_remains_fail_closed() -> None:
    clipped = _longitudinal_constraint(
        "upper",
        feature_id="clipped-close-knot",
        start_mm=5,
        end_mm=25,
        clearance_mm=20,
        uncertainty_mm=2,
        clipped=True,
    )
    result = SingleViewBoardCutDecisionHandler().execute(
        _context(),
        _node(
            "analysis.single_view_board_cut_decision",
            {"inspection_length_mm": 300},
        ),
        {
            "constraints": _constraints("upper", clipped),
            "frame_set": _single_frame_set("upper"),
        },
    )

    decision = result.outputs["decision"].decision
    assert decision.valid is False
    assert decision.status == "feature_clipped"
    assert decision.longitudinal_shift_mm is None
    assert decision.reason_code == "active_cut_chain_contains_clipped_feature"


def test_unsynchronized_capture_produces_invalid_decision_without_cut_value() -> None:
    frame_set = _frame_set(valid=False, skew=75)
    result = MultiViewBoardCutFusionHandler().execute(
        _context(),
        _node("analysis.multi_view_board_cut_fusion", {"inspection_length_mm": 300}),
        {
            "upper": _constraints("upper"),
            "lower": _constraints("lower"),
            "end_face": _constraints("end_face"),
            "frame_set": frame_set,
        },
    )
    decision = result.outputs["decision"].decision
    assert decision.valid is False
    assert decision.status == "capture_not_synchronized"
    assert decision.cut_position_mm is None
    assert decision.longitudinal_shift_mm is None


def test_synchronized_source_requires_all_three_fixed_roles() -> None:
    context = _context()
    context.input_frames = {"upper": _frame(), "lower": _frame()}
    result = SynchronizedCameraFramesHandler().execute(
        context,
        _node(
            "source.synchronized_camera_frames",
            {
                "upper_channel_id": "channel-upper",
                "lower_channel_id": "channel-lower",
                "end_face_channel_id": "channel-end",
                "max_capture_skew_ms": 50,
            },
        ),
        {},
    )
    frame_set = result.outputs["frame_set"]
    assert isinstance(frame_set, SynchronizedFrameSetPayload)
    assert frame_set.valid is False
    assert frame_set.status == "camera_missing"
    assert "end_face_frame" in result.outputs


def test_synchronized_source_rejects_missing_capture_timestamp() -> None:
    context = _context()
    context.input_frames = {
        "upper": _frame(),
        "lower": _frame(),
        "end_face": _frame().model_copy(update={"timestamp": None}),
    }
    result = SynchronizedCameraFramesHandler().execute(
        context,
        _node(
            "source.synchronized_camera_frames",
            {
                "upper_channel_id": "channel-upper",
                "lower_channel_id": "channel-lower",
                "end_face_channel_id": "channel-end",
                "max_capture_skew_ms": 50,
            },
        ),
        {},
    )
    frame_set = result.outputs["frame_set"]
    assert frame_set.valid is False
    assert frame_set.status == "capture_not_synchronized"
    assert frame_set.reason_code == "capture_timestamp_invalid"


def test_single_camera_test_source_accepts_one_configured_view() -> None:
    context = _context()
    context.input_frames = {"lower": _frame()}
    result = SingleBoardCameraTestSourceHandler().execute(
        context,
        _node(
            "source.single_board_camera_test",
            {"camera_channel_id": "channel-lower", "camera_role": "lower"},
        ),
        {},
    )
    frame_set = result.outputs["frame_set"]
    assert frame_set.valid is True
    assert [entry.role for entry in frame_set.frames] == ["lower"]
    assert result.outputs["frame"].width == 100


def test_single_view_decision_returns_exact_zero_for_clear_test_view() -> None:
    result = SingleViewBoardCutDecisionHandler().execute(
        _context(),
        _node("analysis.single_view_board_cut_decision", {"inspection_length_mm": 300}),
        {
            "constraints": _constraints("upper"),
            "frame_set": _single_frame_set("upper"),
        },
    )
    decision = result.outputs["decision"].decision
    assert decision.valid is True
    assert decision.status == "no_cut_required"
    assert decision.longitudinal_shift_mm == 0
    assert decision.views_used == ["upper"]


def test_single_view_decision_uses_the_selected_camera_constraint() -> None:
    result = SingleViewBoardCutDecisionHandler().execute(
        _context(),
        _node("analysis.single_view_board_cut_decision", {"inspection_length_mm": 300}),
        {
            "constraints": _constraints("end_face", _constraint("end_face", 42)),
            "frame_set": _single_frame_set("end_face"),
        },
    )
    decision = result.outputs["decision"].decision
    assert decision.valid is True
    assert decision.status == "cut_required"
    assert decision.longitudinal_shift_mm == 42
    assert decision.views_used == ["end_face"]


def test_single_view_decision_draws_metric_virtual_cut_line() -> None:
    constraint_result = BoardViewCutConstraintsHandler().execute(
        _context(),
        _node("analysis.board_view_cut_constraints", _offset_longitudinal_config()),
        {
            "frame": _frame(),
            "coordinate_frame": _shifted_coordinates(board_end_x=10),
            "features": _detections(
                DetectionBox(x_min=0.15, y_min=0.10, x_max=0.20, y_max=0.20)
            ),
        },
    )
    result = SingleViewBoardCutDecisionHandler().execute(
        _context(),
        _node(
            "analysis.single_view_board_cut_decision",
            {"inspection_length_mm": 300, "show_cut_line_overlay": True},
        ),
        {
            "constraints": constraint_result.outputs["constraints"],
            "frame_set": _single_frame_set("upper"),
        },
    )

    decision = result.outputs["decision"].decision
    overlay = result.outputs["overlay"]
    cut_lines = [
        primitive for primitive in overlay.primitives if primitive.layer == "board_cut_line"
    ]
    assert decision.valid is True
    assert decision.cut_position_mm is not None
    assert len(cut_lines) == 2
    expected_x = 0.1 + decision.cut_position_mm * 0.6 / 400.0
    for line in cut_lines:
        assert abs(line.geometry["start"]["x"] - expected_x) < 1e-6
        assert abs(line.geometry["end"]["x"] - expected_x) < 1e-6
        assert line.metadata["virtual_cut_line"] is True


def test_single_view_decision_rejects_different_inspection_lengths() -> None:
    constraints = _constraints("upper")
    constraints.inspection_length_mm = 250
    result = SingleViewBoardCutDecisionHandler().execute(
        _context(),
        _node("analysis.single_view_board_cut_decision", {"inspection_length_mm": 300}),
        {
            "constraints": constraints,
            "frame_set": _single_frame_set("upper"),
        },
    )

    decision = result.outputs["decision"].decision
    assert decision.valid is False
    assert decision.status == "calibration_invalid"
    assert decision.reason_code == "inspection_length_mismatch"


def test_single_view_decision_rejects_camera_role_mismatch() -> None:
    result = SingleViewBoardCutDecisionHandler().execute(
        _context(),
        _node("analysis.single_view_board_cut_decision", {"inspection_length_mm": 300}),
        {
            "constraints": _constraints("lower"),
            "frame_set": _single_frame_set("upper"),
        },
    )
    decision = result.outputs["decision"].decision
    assert decision.valid is False
    assert decision.status == "camera_missing"
    assert decision.longitudinal_shift_mm is None


def test_single_view_decision_supports_realtime_channel_frames() -> None:
    context = _context()
    context.mode = RuntimeMode.REALTIME
    result = SingleViewBoardCutDecisionHandler().execute(
        context,
        _node("analysis.single_view_board_cut_decision", {"inspection_length_mm": 300}),
        {"constraints": _constraints("upper", _constraint("upper", 64))},
    )
    decision = result.outputs["decision"].decision
    assert decision.valid is True
    assert decision.status == "cut_required"
    assert decision.longitudinal_shift_mm == 64
    assert decision.views_used == ["upper"]


def test_board_cut_registry_is_no_code_and_requires_all_fusion_inputs() -> None:
    channel_frame = BUILTIN_NODE_TYPE_BY_KEY["source.channel_frame"]
    assert (
        next(
            field.field_type
            for field in channel_frame.property_schema
            if field.key == "authoring_channel_id"
        )
        == "channel_ref"
    )
    source = BUILTIN_NODE_TYPE_BY_KEY["source.synchronized_camera_frames"]
    channel_fields = {
        field.key: field.field_type
        for field in source.property_schema
        if field.key.endswith("_channel_id")
    }
    assert channel_fields == {
        "upper_channel_id": "channel_ref",
        "lower_channel_id": "channel_ref",
        "end_face_channel_id": "channel_ref",
    }
    constraints = BUILTIN_NODE_TYPE_BY_KEY["analysis.board_view_cut_constraints"]
    simplifier = BUILTIN_NODE_TYPE_BY_KEY["analysis.segmentation_polygon_simplification"]
    assert {port.key for port in simplifier.input_ports} == {"segments"}
    assert {port.key for port in simplifier.output_ports} == {"segments", "overlay"}
    coordinates = BUILTIN_NODE_TYPE_BY_KEY["analysis.board_end_coordinate_frame"]
    assert {port.key for port in coordinates.input_ports} == {"frame", "segments"}
    assert (
        next(
            field.field_type
            for field in coordinates.property_schema
            if field.key == "board_surface_class_mapping"
        )
        == "board_surface_class_mapping"
    )
    assert (
        next(
            field.field_type for field in constraints.property_schema if field.key == "class_rules"
        )
        == "board_cut_class_rules"
    )
    assert (
        next(field.field_type for field in constraints.property_schema if field.key == "surfaces")
        == "board_surface_calibration"
    )
    assert constraints.default_config["follow_board_position"] is True
    assert constraints.default_config["use_calibration_pose_reference"] is True
    assert constraints.default_config["realtime_pose_stabilization"] is True
    assert constraints.default_config["realtime_stability_profile"] == "balanced"
    assert constraints.default_config["calibration_pose_angle_tolerance_degrees"] == 15.0
    assert (
        constraints.default_config[
            "calibration_preliminary_junction_fraction_tolerance"
        ]
        == 0.22
    )
    assert constraints.default_config["calibration_surface_end_angle_tolerance_degrees"] == 18.0
    assert constraints.default_config["calibration_surface_end_tolerance_px"] == 24.0
    assert constraints.default_config["show_dynamic_surface_overlay"] is True
    assert {port.key for port in constraints.output_ports} == {"constraints", "overlay"}
    end_face = BUILTIN_NODE_TYPE_BY_KEY["analysis.end_face_cut_constraints"]
    assert {port.key for port in end_face.input_ports} == {"features", "frame", "segments"}
    assert {port.key for port in end_face.output_ports} == {"constraints", "overlay"}
    calibration_angle_help = next(
        field.help_text or ""
        for field in constraints.property_schema
        if field.key == "calibration_pose_angle_tolerance_degrees"
    )
    assert "4-to-1" in calibration_angle_help
    assert "not from the vertical or horizontal image axes" in calibration_angle_help
    fusion = BUILTIN_NODE_TYPE_BY_KEY["analysis.multi_view_board_cut_fusion"]
    assert {port.key for port in fusion.input_ports if port.required} == {
        "upper",
        "lower",
        "end_face",
        "frame_set",
    }
    assert RuntimeMode.REALTIME not in fusion.supported_modes
    assert fusion.default_config["show_cut_line_overlay"] is True
    assert {port.key for port in fusion.output_ports} == {"decision", "result", "overlay"}
    for node_type in (
        "analysis.board_end_coordinate_frame",
        "analysis.board_view_cut_constraints",
        "analysis.end_face_cut_constraints",
    ):
        assert RuntimeMode.REALTIME in BUILTIN_NODE_TYPE_BY_KEY[node_type].supported_modes
    test_source = BUILTIN_NODE_TYPE_BY_KEY["source.single_board_camera_test"]
    assert {field.key: field.field_type for field in test_source.property_schema} == {
        "camera_channel_id": "channel_ref",
        "camera_role": "select",
    }
    single_view = BUILTIN_NODE_TYPE_BY_KEY["analysis.single_view_board_cut_decision"]
    assert {port.key for port in single_view.input_ports if port.required} == {"constraints"}
    assert RuntimeMode.REALTIME in single_view.supported_modes
    assert single_view.default_config["show_cut_line_overlay"] is True


def test_board_end_registry_explains_operator_adjustments_for_every_field() -> None:
    node = BUILTIN_NODE_TYPE_BY_KEY["analysis.board_end_coordinate_frame"]
    help_by_key = {field.key: field.help_text or "" for field in node.property_schema}

    assert set(help_by_key) == {
        "geometry_source",
        "view_role",
        "board_surface_class_mapping",
        "board_end_side",
        "line_detector_mode",
        "board_color_tolerance_percent",
        "blur_kernel_size",
        "lower_threshold",
        "upper_threshold",
        "hough_threshold",
        "minimum_line_length_px",
        "max_line_gap_px",
        "board_end_angle_tolerance_degrees",
        "longitudinal_angle_tolerance_degrees",
        "maximum_reference_join_gap_px",
        "minimum_edge_support_percent",
        "minimum_board_end_span_percent",
        "minimum_longitudinal_span_percent",
        "leading_edge_score_tolerance",
        "processing_max_dimension_px",
        "max_axis_segments",
        "debug_overlay",
    }
    assert "board continues" in help_by_key["board_end_side"]
    for key in (
        "blur_kernel_size",
        "board_color_tolerance_percent",
        "lower_threshold",
        "upper_threshold",
        "hough_threshold",
        "minimum_line_length_px",
        "max_line_gap_px",
        "board_end_angle_tolerance_degrees",
        "longitudinal_angle_tolerance_degrees",
        "maximum_reference_join_gap_px",
        "minimum_edge_support_percent",
        "minimum_board_end_span_percent",
        "minimum_longitudinal_span_percent",
        "leading_edge_score_tolerance",
        "processing_max_dimension_px",
        "max_axis_segments",
    ):
        help_text = help_by_key[key].lower()
        assert "increase" in help_text or "raise" in help_text
        assert "decrease" in help_text or "lower" in help_text
    assert all(
        field.field_type == "slider"
        for field in node.property_schema
        if field.key
        not in {
            "geometry_source",
            "view_role",
            "board_surface_class_mapping",
            "board_end_side",
            "line_detector_mode",
            "debug_overlay",
        }
    )
    assert "does not change the cut result" in help_by_key["debug_overlay"]


def test_trigger_contract_accepts_role_based_test_images() -> None:
    request = RuntimeTriggerExecutionRequest.model_validate(
        {
            "trigger_id": "cycle-123",
            "payload": {"workpiece_id": "board-123"},
            "board_format": {
                "format_id": "fmt-200x40",
                "format_version": "3",
                "width_mm": 200,
                "thickness_mm": 40,
            },
            "images_by_role": {
                role: {"image_base64": "A" * 16, "mime_type": "image/png"}
                for role in ("upper", "lower", "end_face")
            },
        }
    )
    assert set(request.images_by_role) == {"upper", "lower", "end_face"}
    assert request.board_format is not None
    assert request.board_format.width_mm == 200
