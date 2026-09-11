from __future__ import annotations

import base64
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from cv_contracts.runtime import (
    RuntimeCameraCalibrationBoardDefinition,
    RuntimeImageViewProfile,
    RuntimeImageViewProfileCalculateRequest,
    RuntimeLogicalModelRef,
    RuntimeNodeSpec,
    RuntimeNormalizedImagePoint,
    RuntimeOverlayPrimitiveState,
)
from cv_runtime import BUILTIN_NODE_TYPE_BY_KEY, create_identity_camera_calibration
from cv_runtime.context import RuntimeExecutionContext
from cv_runtime.image_view import (
    ImageViewValidationError,
    apply_image_view,
    calculate_image_view_profile,
    restore_view_mask,
    source_points_to_view,
    validate_image_view_frame,
    view_overlay_geometry_to_source,
    view_points_to_source,
)
from cv_runtime.nodes.builtin import (
    CropHandler,
    FullFrameCaptureHandler,
    _validate_inference_image_view,
)
from cv_runtime.nodes.stem_process_coordinates import StemProcessCoordinateRectifyHandler
from cv_runtime.payloads import (
    FramePayload,
    ResultPayload,
    RuntimeImageContext,
    RuntimeImageTransform,
)
from cv_status import RuntimeMode, TaskType
from realtime_executor.live import (
    _raw_overlay_from_undistorted,
    _source_overlay_from_crop,
    _source_overlay_from_image_view,
)


def _request(
    *,
    quadrilateral: list[tuple[float, float]] | None = None,
    output_width: int = 100,
    output_height: int = 60,
    transform_type: str = "PERSPECTIVE_RECTIFY",
) -> RuntimeImageViewProfileCalculateRequest:
    points = quadrilateral or [(0.1, 0.15), (0.9, 0.1), (0.85, 0.9), (0.15, 0.85)]
    return RuntimeImageViewProfileCalculateRequest(
        view_profile_id="stem-measurement-view-v1",
        source_image_width=160,
        source_image_height=120,
        intrinsic_calibration_hash="a" * 64,
        source_quadrilateral=[RuntimeNormalizedImagePoint(x=x, y=y) for x, y in points],
        output_width=output_width,
        output_height=output_height,
        transform_type=transform_type,
        interpolation="LINEAR",
        source_frame_timestamp="2026-08-05T10:00:00Z",
        notes="test corridor",
    )


def _profile() -> RuntimeImageViewProfile:
    result = calculate_image_view_profile(
        source_channel_id="chn_stem",
        request=_request(),
        generated_at="2026-08-05T10:01:00Z",
    )
    assert result.status == "valid"
    assert result.profile is not None
    return result.profile


def _source_image() -> np.ndarray:
    yy, xx = np.indices((120, 160), dtype=np.uint16)
    return np.stack(
        (
            (xx % 256).astype(np.uint8),
            (yy % 256).astype(np.uint8),
            ((xx * 3 + yy * 5) % 256).astype(np.uint8),
        ),
        axis=2,
    )


def _undistorted_frame() -> FramePayload:
    return FramePayload.from_cv2_frame(
        _source_image(),
        timestamp="2026-08-05T10:02:00Z",
        image_context=RuntimeImageContext(
            context_id="ctx_undistorted",
            kind="processed",
            width=160,
            height=120,
            source_image_context_id="ctx_raw",
            transform=RuntimeImageTransform(
                transform_type="undistort",
                coordinate_projection="not_projectable",
                parameters={
                    "calibration_hash": "a" * 64,
                    "perspective_rectified": False,
                },
            ),
        ),
        metadata={
            "calibration_hash": "a" * 64,
            "intrinsic_calibration_hash": "a" * 64,
            "undistort_preprocessing_version": "camera.undistort.v1",
            "color_space": "BGR",
        },
    )


def _node(node_type: str, config: dict[str, object]) -> RuntimeNodeSpec:
    definition = BUILTIN_NODE_TYPE_BY_KEY[node_type]
    return RuntimeNodeSpec(
        node_key=node_type.replace(".", "_"),
        node_type=definition.node_type,
        node_category=definition.node_category,
        node_contract_version=definition.contract_version,
        input_ports=definition.input_ports,
        output_ports=definition.output_ports,
        config={**definition.default_config, **config},
        logical_model_ref=(
            RuntimeLogicalModelRef(
                model_family_slug="stem-segmentation",
                required_task_type=TaskType.SEGMENTATION,
            )
            if node_type == "inference.segmentation"
            else None
        ),
    )


def _context() -> RuntimeExecutionContext:
    return RuntimeExecutionContext(
        correlation_id="corr_view",
        pipeline_version_id="piv_view",
        channel_id="chn_stem",
        mode=RuntimeMode.REALTIME,
    )


def test_profile_homographies_round_trip_known_points() -> None:
    profile = _profile()
    source = np.asarray([[16.0, 18.0], [80.0, 60.0], [136.0, 102.0]])

    view = source_points_to_view(source, profile)
    restored = view_points_to_source(view, profile)

    assert np.allclose(restored, source, atol=1e-5)
    assert profile.profile_checksum != "0" * 64
    assert profile.source_space == "UNDISTORTED_IMAGE"
    assert profile.provenance.source_frame_timestamp == "2026-08-05T10:00:00Z"


def test_new_calculation_requests_default_to_masked_crop_while_legacy_profiles_keep_rectify() -> (
    None
):
    request_value = _request().model_dump(mode="json", exclude={"transform_type"})
    request = RuntimeImageViewProfileCalculateRequest.model_validate(request_value)
    legacy_profile_value = _profile().model_dump(mode="json", exclude={"transform_type"})
    legacy_profile = RuntimeImageViewProfile.model_validate(legacy_profile_value)

    assert request.transform_type == "POLYGON_MASKED_CROP"
    assert legacy_profile.transform_type == "PERSPECTIVE_RECTIFY"


def test_transform_is_deterministic_and_mask_can_be_back_projected() -> None:
    profile = _profile()
    first = apply_image_view(_source_image(), profile)
    second = apply_image_view(_source_image(), profile)
    view_mask = np.zeros((profile.output_height, profile.output_width), dtype=np.uint8)
    view_mask[10:50, 15:85] = 255

    restored_mask = restore_view_mask(view_mask, profile)
    projected_again = apply_image_view(restored_mask, profile, mask=True)

    assert np.array_equal(first, second)
    assert first.shape == (60, 100, 3)
    assert restored_mask.shape == (120, 160)
    assert np.mean(projected_again[12:48, 17:83] == 255) > 0.98


def test_polygon_masked_crop_preserves_source_perspective_and_masks_outside_corridor() -> None:
    result = calculate_image_view_profile(
        source_channel_id="chn_stem",
        request=_request(transform_type="POLYGON_MASKED_CROP"),
        generated_at="2026-08-05T10:01:00Z",
    )
    assert result.status == "valid"
    assert result.profile is not None
    profile = result.profile
    matrix = np.asarray(profile.source_to_view_homography, dtype=np.float64).reshape(3, 3)

    source_points = np.asarray([[25.0, 30.0], [55.0, 30.0], [25.0, 50.0]])
    view_points = source_points_to_view(source_points, profile)
    source_horizontal = source_points[1] - source_points[0]
    source_vertical = source_points[2] - source_points[0]
    view_horizontal = view_points[1] - view_points[0]
    view_vertical = view_points[2] - view_points[0]

    assert profile.transform_type == "POLYGON_MASKED_CROP"
    assert np.allclose(matrix[2], [0.0, 0.0, 1.0], atol=1e-12)
    assert np.isclose(
        np.linalg.norm(view_horizontal) / np.linalg.norm(source_horizontal),
        np.linalg.norm(view_vertical) / np.linalg.norm(source_vertical),
        atol=1e-10,
    )
    assert np.isclose(
        np.dot(view_horizontal, view_vertical),
        np.dot(source_horizontal, source_vertical)
        * (np.linalg.norm(view_horizontal) / np.linalg.norm(source_horizontal)) ** 2,
        atol=1e-8,
    )

    image = np.full((120, 160, 3), 127, dtype=np.uint8)
    transformed = apply_image_view(image, profile)
    transformed_polygon = source_points_to_view(
        np.asarray([[point.x * 159, point.y * 119] for point in profile.source_quadrilateral]),
        profile,
    )
    assert transformed.shape == (60, 100, 3)
    assert np.all(transformed[0, 0] == 0)
    centroid = np.rint(np.mean(transformed_polygon, axis=0)).astype(int)
    assert np.all(transformed[centroid[1], centroid[0]] == 127)
    assert np.allclose(
        view_points_to_source(view_points, profile),
        source_points,
        atol=1e-6,
    )


def test_vector_overlay_polygon_is_back_projected_to_undistorted_full_image() -> None:
    profile = _profile()
    geometry = view_overlay_geometry_to_source(
        {
            "points": [
                {"x": 0.1, "y": 0.2},
                {"x": 0.9, "y": 0.2},
                {"x": 0.9, "y": 0.8},
                {"x": 0.1, "y": 0.8},
            ],
            "coordinate_system": "normalized_xy",
        },
        profile,
    )
    source_pixels = np.asarray(
        [
            [
                point["x"] * (profile.source_image_width - 1),
                point["y"] * (profile.source_image_height - 1),
            ]
            for point in geometry["points"]
        ]
    )
    round_trip = source_points_to_view(source_pixels, profile)
    expected_view = np.asarray(
        [
            [0.1 * (profile.output_width - 1), 0.2 * (profile.output_height - 1)],
            [0.9 * (profile.output_width - 1), 0.2 * (profile.output_height - 1)],
            [0.9 * (profile.output_width - 1), 0.8 * (profile.output_height - 1)],
            [0.1 * (profile.output_width - 1), 0.8 * (profile.output_height - 1)],
        ]
    )

    assert geometry["coordinate_system"] == "normalized_xy"
    assert geometry["projected_from_view_profile_id"] == profile.view_profile_id
    assert np.allclose(round_trip, expected_view, atol=1e-4)


def test_live_preview_projects_canonical_box_as_source_polygon() -> None:
    profile = _profile()
    source_frame = _undistorted_frame()
    display_frame = (
        StemProcessCoordinateRectifyHandler()
        .execute(
            _context(),
            _node(
                "preprocessing.process_coordinate_rectify",
                {
                    "view_profile_id": profile.view_profile_id,
                    "view_profile": profile.model_dump(mode="json"),
                    "calibration": None,
                },
            ),
            {"frame": source_frame},
        )
        .outputs["frame"]
    )
    primitive = RuntimeOverlayPrimitiveState(
        primitive_type="box",
        layer="detections",
        geometry={"x_min": 0.2, "y_min": 0.25, "x_max": 0.6, "y_max": 0.55},
        coordinate_system="normalized_xyxy",
        image_context_id=display_frame.image_context.context_id,
    )

    projected = _source_overlay_from_image_view(
        [primitive],
        display_frame=display_frame,
        source_frame=source_frame,
    )

    assert len(projected) == 1
    assert projected[0].primitive_type == "polygon"
    assert len(projected[0].geometry["points"]) == 4
    assert projected[0].image_context_id == source_frame.image_context.context_id
    assert projected[0].projected_from_context_id == display_frame.image_context.context_id


def test_live_preview_projects_undistorted_vectors_and_values_to_raw_camera_space() -> None:
    calibration_result = create_identity_camera_calibration(
        source_channel_id="chn_stem",
        image_width=160,
        image_height=120,
        board_definition=RuntimeCameraCalibrationBoardDefinition(),
        generated_at="2026-08-05T10:00:00Z",
    )
    assert calibration_result.profile is not None
    frame = _undistorted_frame()
    primitives = [
        RuntimeOverlayPrimitiveState(
            primitive_type="polygon",
            layer="segmentation",
            geometry={
                "points": [
                    {"x": 0.2, "y": 0.25},
                    {"x": 0.7, "y": 0.25},
                    {"x": 0.7, "y": 0.6},
                ],
                "coordinate_system": "normalized_xy",
            },
            coordinate_system="normalized_xy",
            image_context_id="ctx_undistorted",
        ),
        RuntimeOverlayPrimitiveState(
            primitive_type="measurement",
            layer="stem_width",
            label="width",
            geometry={},
            metadata={"value": 148.2, "unit": "mm"},
        ),
    ]

    projected = _raw_overlay_from_undistorted(
        primitives,
        calibration=calibration_result.profile,
        raw_frame=frame,
    )

    assert len(projected) == 2
    actual_points = np.asarray(
        [[point["x"], point["y"]] for point in projected[0].geometry["points"]]
    )
    expected_points = np.asarray(
        [[point["x"], point["y"]] for point in primitives[0].geometry["points"]]
    )
    assert np.allclose(actual_points, expected_points, atol=1e-12)
    assert projected[0].metadata["projected_from_intrinsic_calibration_hash"] == (
        calibration_result.profile.calibration_hash
    )
    assert projected[1].metadata["value"] == 148.2


def test_live_preview_projects_crop_overlays_and_values_to_full_camera_space() -> None:
    source_frame = FramePayload.from_cv2_frame(
        _source_image(),
        image_context=RuntimeImageContext(
            context_id="ctx_raw_crop",
            kind="original",
            width=160,
            height=120,
        ),
    )
    display_frame = (
        CropHandler("preprocessing.crop")
        .execute(
            _context(),
            _node(
                "preprocessing.crop",
                {"x": 20, "y": 10, "width": 100, "height": 60},
            ),
            {"frame": source_frame},
        )
        .outputs["frame"]
    )
    texture = np.zeros((60, 100), dtype=np.uint8)
    texture[10:30, 20:50] = 255
    encoded_ok, encoded = cv2.imencode(".png", texture)
    assert encoded_ok
    primitives = [
        RuntimeOverlayPrimitiveState(
            primitive_type="box",
            layer="detections",
            geometry={"x_min": 0.2, "y_min": 0.25, "x_max": 0.6, "y_max": 0.75},
            coordinate_system="normalized_xyxy",
            image_context_id=display_frame.image_context.context_id,
        ),
        RuntimeOverlayPrimitiveState(
            primitive_type="measurement",
            layer="classification",
            label="quality",
            geometry={},
            metadata={"value": "good"},
        ),
        RuntimeOverlayPrimitiveState(
            primitive_type="mask",
            layer="segmentation_mask",
            geometry={"data": base64.b64encode(encoded.tobytes()).decode("ascii")},
            coordinate_system="normalized_xy",
        ),
    ]

    projected = _source_overlay_from_crop(
        primitives,
        display_frame=display_frame,
        source_frame=source_frame,
    )

    assert projected is not None
    assert len(projected) == 3
    assert projected[0].geometry["x_min"] == pytest.approx(0.25)
    assert projected[0].geometry["y_min"] == pytest.approx(25 / 120)
    assert projected[0].geometry["x_max"] == pytest.approx(0.5)
    assert projected[0].geometry["y_max"] == pytest.approx(55 / 120)
    assert projected[0].image_context_id == source_frame.image_context.context_id
    assert projected[1].metadata["value"] == "good"
    projected_texture = cv2.imdecode(
        np.frombuffer(base64.b64decode(projected[2].geometry["data"]), dtype=np.uint8),
        cv2.IMREAD_UNCHANGED,
    )
    assert projected_texture.shape == (120, 160)
    assert np.count_nonzero(projected_texture[:10]) == 0
    assert np.count_nonzero(projected_texture[:, :20]) == 0
    assert np.count_nonzero(projected_texture[20:40, 40:70]) > 0


def test_live_preview_projects_canonical_mask_texture_to_full_image() -> None:
    profile = _profile()
    source_frame = _undistorted_frame()
    texture = np.zeros((profile.output_height, profile.output_width), dtype=np.uint8)
    texture[12:42, 24:76] = 255
    encoded_ok, encoded = cv2.imencode(".png", texture)
    assert encoded_ok
    primitive = RuntimeOverlayPrimitiveState(
        primitive_type="mask",
        layer="segmentation_mask",
        geometry={"data": base64.b64encode(encoded.tobytes()).decode("ascii")},
        coordinate_system="normalized_xy",
    )
    display_frame = FramePayload.from_cv2_frame(
        np.zeros((profile.output_height, profile.output_width, 3), dtype=np.uint8),
        image_context=RuntimeImageContext(
            context_id="ctx_canonical_mask",
            kind="processed",
            width=profile.output_width,
            height=profile.output_height,
            transform=RuntimeImageTransform(
                transform_type="perspective_rectify",
                coordinate_projection="homography",
                parameters={"view_profile": profile.model_dump(mode="json")},
            ),
        ),
        metadata={"image_view_profile": profile.model_dump(mode="json")},
    )

    projected = _source_overlay_from_image_view(
        [primitive],
        display_frame=display_frame,
        source_frame=source_frame,
    )

    assert len(projected) == 1
    projected_texture = cv2.imdecode(
        np.frombuffer(base64.b64decode(projected[0].geometry["data"]), dtype=np.uint8),
        cv2.IMREAD_UNCHANGED,
    )
    assert projected_texture.shape == (profile.source_image_height, profile.source_image_width)
    assert int(np.count_nonzero(projected_texture)) > 0


def test_invalid_geometry_and_source_resolution_fail_closed() -> None:
    bow_tie = calculate_image_view_profile(
        source_channel_id="chn_stem",
        request=_request(quadrilateral=[(0.1, 0.1), (0.9, 0.9), (0.9, 0.1), (0.1, 0.9)]),
    )
    tiny = calculate_image_view_profile(
        source_channel_id="chn_stem",
        request=_request(quadrilateral=[(0.1, 0.1), (0.11, 0.1), (0.11, 0.11), (0.1, 0.11)]),
    )

    assert bow_tie.status == "invalid"
    assert bow_tie.failure_code == "image_view_quadrilateral_self_intersecting"
    assert tiny.status == "invalid"
    assert tiny.failure_code == "image_view_quadrilateral_area_too_small"
    with pytest.raises(ImageViewValidationError, match="resolution"):
        validate_image_view_frame(
            _profile(),
            image_width=159,
            image_height=120,
            intrinsic_calibration_hash="a" * 64,
            source_channel_id="chn_stem",
        )


def test_runtime_rectify_and_capture_use_the_exact_pipeline_image_output() -> None:
    profile = _profile()
    rectify_node = _node(
        "preprocessing.process_coordinate_rectify",
        {
            "view_profile_id": profile.view_profile_id,
            "view_profile": profile.model_dump(mode="json"),
            "calibration": None,
        },
    )
    runtime_output = (
        StemProcessCoordinateRectifyHandler()
        .execute(_context(), rectify_node, {"frame": _undistorted_frame()})
        .outputs["frame"]
    )
    expected = apply_image_view(_source_image(), profile)

    assert isinstance(runtime_output, FramePayload)
    assert np.array_equal(runtime_output.cached_cv2_frame(), expected)
    assert runtime_output.metadata["canonical_model_view"] is True
    assert runtime_output.metadata["view_profile_checksum"] == profile.profile_checksum

    eligibility = ResultPayload(
        decision=True,
        result={"eligible": True, "task_role": "POSITIVE", "reason_codes": []},
    )
    capture_node = _node(
        "capture.full_frame_labelstudio",
        {
            "target_project_ids": ["prj_stem_corridor"],
            "require_eligibility": True,
        },
    )
    captured = (
        FullFrameCaptureHandler("capture.full_frame_labelstudio")
        .execute(
            _context(),
            capture_node,
            {"frame": runtime_output, "eligibility": eligibility},
        )
        .outputs["capture"]
    )

    assert len(captured.intents) == 1
    captured_frame = FramePayload.model_validate(captured.intents[0].asset)
    assert captured.intents[0].metadata["capture_source"] == "PIPELINE_IMAGE_OUTPUT"
    assert captured.intents[0].metadata["view_profile_id"] == profile.view_profile_id
    assert captured_frame.image_bytes() == runtime_output.image_bytes()


def test_runtime_masked_crop_marks_the_view_as_perspective_preserving() -> None:
    computation = calculate_image_view_profile(
        source_channel_id="chn_stem",
        request=_request(transform_type="POLYGON_MASKED_CROP"),
        generated_at="2026-08-05T10:01:00Z",
    )
    assert computation.profile is not None
    profile = computation.profile
    output = (
        StemProcessCoordinateRectifyHandler()
        .execute(
            _context(),
            _node(
                "preprocessing.process_coordinate_rectify",
                {
                    "view_profile_id": profile.view_profile_id,
                    "view_profile": profile.model_dump(mode="json"),
                    "calibration": None,
                },
            ),
            {"frame": _undistorted_frame()},
        )
        .outputs["frame"]
    )

    assert output.image_context.transform.transform_type == "masked_crop"
    assert output.image_context.transform.parameters["preserves_source_perspective"] is True
    assert output.image_context.transform.parameters["perspective_rectified"] is False
    assert output.metadata["image_view_transform_type"] == "POLYGON_MASKED_CROP"


def test_model_view_contract_accepts_exact_profile_and_keeps_legacy_models_compatible() -> None:
    profile = _profile()
    frame = (
        StemProcessCoordinateRectifyHandler()
        .execute(
            _context(),
            _node(
                "preprocessing.process_coordinate_rectify",
                {
                    "view_profile_id": profile.view_profile_id,
                    "view_profile": profile.model_dump(mode="json"),
                    "calibration": None,
                },
            ),
            {"frame": _undistorted_frame()},
        )
        .outputs["frame"]
    )
    inference_node = _node(
        "inference.segmentation",
        {"require_image_view_compatibility": True},
    )
    requirement = {
        "view_profile_id": profile.view_profile_id,
        "profile_checksum": profile.profile_checksum,
        "input_width": profile.output_width,
        "input_height": profile.output_height,
        "color_space": "BGR",
        "undistort_preprocessing_version": "camera.undistort.v1",
        "image_view_preprocessing_version": "image.view-runtime.v1",
    }

    exact = _validate_inference_image_view(
        frame,
        SimpleNamespace(capability={"preprocessing": {"image_view": requirement}}),
        node=inference_node,
    )
    assert exact["status"] == "MATCH"
    assert exact["blocking"] is False

    wrong = dict(requirement, view_profile_id="another-view")
    mismatch = _validate_inference_image_view(
        frame,
        SimpleNamespace(capability={"preprocessing": {"image_view": wrong}}),
        node=inference_node,
    )
    assert mismatch["status"] == "DIFFERENT_RUNTIME_VIEW"
    assert mismatch["blocking"] is False
    assert mismatch["mismatches"]["view_profile_id"] == {
        "expected": "another-view",
        "actual": profile.view_profile_id,
    }

    legacy_node = _node(
        "inference.segmentation",
        {"require_image_view_compatibility": False},
    )
    legacy = _validate_inference_image_view(
        FramePayload.from_cv2_frame(_source_image()),
        SimpleNamespace(capability={"preprocessing": {}}),
        node=legacy_node,
    )
    assert legacy["status"] == "TRAINING_VIEW_NOT_DECLARED"
    assert legacy["blocking"] is False
