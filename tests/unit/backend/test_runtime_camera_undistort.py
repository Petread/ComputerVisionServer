from __future__ import annotations

import cv2
import numpy as np
import pytest
from cv_contracts.runtime import (
    RuntimeCameraCalibrationBoardDefinition,
    RuntimeCameraCalibrationRoi,
    RuntimeCameraIntrinsicCalibrationProfile,
    RuntimeGraphEdge,
    RuntimeNodeSpec,
    RuntimePipelineGraph,
)
from cv_runtime import BUILTIN_NODE_TYPE_BY_KEY, ExecutablePipelineGraph
from cv_runtime.camera_calibration import (
    apply_camera_distortion,
    camera_calibration_profile_hash,
    create_identity_camera_calibration,
    undistorted_points_to_raw,
)
from cv_runtime.context import RuntimeExecutionContext
from cv_runtime.errors import RuntimeNodeExecutionError
from cv_runtime.loader import load_runtime_handler
from cv_runtime.nodes.camera_undistort import CameraUndistortHandler
from cv_runtime.payloads import FramePayload
from cv_status import RuntimeMode
from realtime_executor.live import _stateful_pipeline_nodes


def _profile(
    *,
    width: int = 320,
    height: int = 240,
    distortion: list[float] | None = None,
    source_channel_id: str = "chn_camera",
) -> RuntimeCameraIntrinsicCalibrationProfile:
    focal_length = 260.0
    camera_matrix = [
        focal_length,
        0.0,
        width / 2.0,
        0.0,
        focal_length,
        height / 2.0,
        0.0,
        0.0,
        1.0,
    ]
    profile = RuntimeCameraIntrinsicCalibrationProfile(
        status="valid",
        source_channel_id=source_channel_id,
        image_width=width,
        image_height=height,
        camera_matrix=camera_matrix,
        output_camera_matrix=camera_matrix,
        distortion_coefficients=distortion or [0.0, 0.0, 0.0, 0.0, 0.0],
        rms_reprojection_error_px=0.2,
        accepted_observation_count=8,
        board_definition=RuntimeCameraCalibrationBoardDefinition(
            board_type="checkerboard",
            columns=9,
            rows=6,
        ),
        valid_roi=RuntimeCameraCalibrationRoi(x=0, y=0, width=width, height=height),
        generated_at="2026-08-03T10:00:00Z",
        calibration_hash="0" * 64,
        perspective_rectified=False,
    )
    return profile.model_copy(
        update={"calibration_hash": camera_calibration_profile_hash(profile)}
    )


def _node(profile: RuntimeCameraIntrinsicCalibrationProfile) -> RuntimeNodeSpec:
    definition = BUILTIN_NODE_TYPE_BY_KEY["preprocessing.camera_undistort"]
    return RuntimeNodeSpec(
        node_key="undistort",
        node_type=definition.node_type,
        node_category=definition.node_category,
        node_contract_version=definition.contract_version,
        input_ports=definition.input_ports,
        output_ports=definition.output_ports,
        config={
            **definition.default_config,
            "calibration": profile.model_dump(mode="json"),
        },
    )


def _context(*, correlation_id: str = "corr_undistort") -> RuntimeExecutionContext:
    return RuntimeExecutionContext(
        correlation_id=correlation_id,
        pipeline_version_id="piv_undistort",
        mode=RuntimeMode.REALTIME,
        channel_id="chn_camera",
    )


def _barrel_distort(
    ideal: np.ndarray,
    profile: RuntimeCameraIntrinsicCalibrationProfile,
) -> np.ndarray:
    height, width = ideal.shape[:2]
    raw_y, raw_x = np.indices((height, width), dtype=np.float32)
    raw_points = np.stack((raw_x, raw_y), axis=-1).reshape(-1, 1, 2)
    camera_matrix = np.asarray(profile.camera_matrix, dtype=np.float64).reshape(3, 3)
    distortion = np.asarray(profile.distortion_coefficients, dtype=np.float64)
    ideal_points = cv2.undistortPoints(
        raw_points,
        camera_matrix,
        distortion,
        P=camera_matrix,
    ).reshape(height, width, 2)
    return cv2.remap(
        ideal,
        ideal_points[:, :, 0],
        ideal_points[:, :, 1],
        cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
    )


def _vertical_line_deviation(image: np.ndarray, *, x_min: int, x_max: int) -> float:
    grayscale = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    centers: list[float] = []
    for row in grayscale[20:-20]:
        positions = np.flatnonzero(row[x_min:x_max] > 100)
        if positions.size:
            centers.append(float(np.mean(positions + x_min)))
    return float(np.std(centers))


def test_registry_and_loader_expose_camera_undistort() -> None:
    definition = BUILTIN_NODE_TYPE_BY_KEY["preprocessing.camera_undistort"]

    assert [(port.key, port.data_type) for port in definition.input_ports] == [
        ("frame", "frame")
    ]
    assert [(port.key, port.data_type) for port in definition.output_ports] == [
        ("frame", "frame")
    ]
    assert any(
        field.key == "calibration" and field.field_type == "camera_intrinsic_calibration"
        for field in definition.property_schema
    )
    handler = load_runtime_handler(
        definition.runtime_handler_ref,
        expected_node_type=definition.node_type,
    )
    assert handler.node_type == "preprocessing.camera_undistort"


def test_realtime_executor_shares_undistort_runtime_state_between_frames() -> None:
    assert _stateful_pipeline_nodes(
        [
            {
                "node_key": "undistort",
                "node_type": "preprocessing.camera_undistort",
            }
        ]
    ) == ["undistort"]


def test_profile_round_trip_preserves_hash_and_intrinsic_only_marker() -> None:
    profile = _profile(distortion=[-0.24, 0.06, 0.001, -0.001, 0.0])

    restored = RuntimeCameraIntrinsicCalibrationProfile.model_validate_json(
        profile.model_dump_json()
    )

    assert restored == profile
    assert camera_calibration_profile_hash(restored) == restored.calibration_hash
    assert restored.perspective_rectified is False


def test_undistort_makes_synthetic_reference_lines_straighter() -> None:
    profile = _profile(distortion=[-0.32, 0.09, 0.001, -0.001, 0.0])
    ideal = np.zeros((profile.image_height, profile.image_width, 3), dtype=np.uint8)
    for x in range(36, profile.image_width - 20, 44):
        cv2.line(ideal, (x, 12), (x, profile.image_height - 13), (255, 255, 255), 2)
    distorted = _barrel_distort(ideal, profile)
    handler = CameraUndistortHandler()

    result = handler.execute(
        _context(),
        _node(profile),
        {"frame": FramePayload.from_cv2_frame(distorted)},
    )
    corrected = result.outputs["frame"].cached_cv2_frame()
    assert corrected is not None
    inner = np.s_[18:-18, 18:-18]
    distorted_error = float(np.mean(np.abs(distorted[inner].astype(float) - ideal[inner])))
    corrected_error = float(np.mean(np.abs(corrected[inner].astype(float) - ideal[inner])))
    distorted_deviation = _vertical_line_deviation(distorted, x_min=8, x_max=65)
    corrected_deviation = _vertical_line_deviation(corrected, x_min=8, x_max=65)

    assert corrected_error < distorted_error * 0.65
    assert corrected_deviation < distorted_deviation * 0.4
    assert corrected_deviation < 0.5


def test_inverse_projection_maps_undistorted_points_and_textures_back_to_raw() -> None:
    profile = _profile(distortion=[-0.24, 0.06, 0.001, -0.001, 0.0])
    raw_points = np.asarray([[32.0, 28.0], [160.0, 120.0], [285.0, 205.0]])
    camera_matrix = np.asarray(profile.camera_matrix, dtype=np.float64).reshape(3, 3)
    undistorted = cv2.undistortPoints(
        raw_points.reshape(-1, 1, 2),
        camera_matrix,
        np.asarray(profile.distortion_coefficients, dtype=np.float64),
        P=np.asarray(profile.output_camera_matrix, dtype=np.float64).reshape(3, 3),
    ).reshape(-1, 2)
    restored = undistorted_points_to_raw(undistorted, profile)

    ideal = np.zeros((profile.image_height, profile.image_width), dtype=np.uint8)
    cv2.circle(ideal, (70, 60), 18, 255, -1)
    expected_texture = _barrel_distort(ideal, profile)
    actual_texture = apply_camera_distortion(ideal, profile, interpolation="nearest")

    assert np.allclose(restored, raw_points, atol=0.003)
    assert np.mean(actual_texture == expected_texture) > 0.995


def test_undistort_keeps_oblique_pose_and_documents_non_projectable_transform() -> None:
    profile = _profile()
    image = np.zeros((profile.image_height, profile.image_width, 3), dtype=np.uint8)
    cv2.line(image, (30, 205), (285, 55), (255, 255, 255), 4)
    handler = CameraUndistortHandler()

    result = handler.execute(
        _context(),
        _node(profile),
        {"frame": FramePayload.from_cv2_frame(image)},
    )
    output = result.outputs["frame"]
    corrected = output.cached_cv2_frame()

    assert corrected is not None
    assert float(np.mean(np.abs(corrected.astype(float) - image.astype(float)))) < 0.1
    assert output.image_context is not None
    assert output.image_context.transform.transform_type == "undistort"
    assert output.image_context.transform.coordinate_projection == "not_projectable"
    assert output.image_context.transform.parameters["perspective_rectified"] is False
    assert output.image_context.transform.parameters["homography_applied"] is False


def test_identity_passthrough_keeps_pixels_and_skips_remap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    computation = create_identity_camera_calibration(
        source_channel_id="chn_camera",
        image_width=320,
        image_height=240,
        board_definition=RuntimeCameraCalibrationBoardDefinition(),
        generated_at="2026-08-06T08:00:00Z",
    )
    assert computation.profile is not None
    image = np.random.default_rng(7).integers(0, 256, (240, 320, 3), dtype=np.uint8)
    handler = CameraUndistortHandler()

    def unexpected_remap(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("identity passthrough must not construct a remap")

    monkeypatch.setattr(cv2, "initUndistortRectifyMap", unexpected_remap)
    result = handler.execute(
        _context(),
        _node(computation.profile),
        {"frame": FramePayload.from_cv2_frame(image)},
    )
    output = result.outputs["frame"]
    actual = output.cached_cv2_frame()

    assert actual is not None
    assert np.array_equal(actual, image)
    assert handler.cache_size() == 0
    assert output.metadata["calibration_method"] == "IDENTITY_PASSTHROUGH"
    assert output.metadata["intrinsic_calibrated"] is False
    assert output.metadata["reason_codes"] == [
        "CAMERA_INTRINSIC_CALIBRATION_BYPASSED"
    ]


def test_wrong_frame_resolution_fails_closed() -> None:
    profile = _profile(width=320, height=240)
    handler = CameraUndistortHandler()

    with pytest.raises(RuntimeNodeExecutionError) as error:
        handler.execute(
            _context(),
            _node(profile),
            {"frame": FramePayload.from_cv2_frame(np.zeros((200, 300, 3), dtype=np.uint8))},
        )

    assert error.value.error.code == "camera_calibration_resolution_mismatch"
    assert error.value.error.metadata["resolution_policy"] == "exact"


def test_remap_is_cached_in_runtime_state_and_shared_handler_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile(distortion=[-0.11, 0.02, 0.0, 0.0, 0.0])
    handler = CameraUndistortHandler()
    context = _context()
    frame = FramePayload.from_cv2_frame(
        np.zeros((profile.image_height, profile.image_width, 3), dtype=np.uint8)
    )
    original = cv2.initUndistortRectifyMap
    calls = 0

    def counted(
        *args: object,
        **kwargs: object,
    ) -> tuple[np.ndarray, np.ndarray]:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(cv2, "initUndistortRectifyMap", counted)
    handler.execute(context, _node(profile), {"frame": frame})
    handler.execute(context, _node(profile), {"frame": frame})
    handler.execute(_context(correlation_id="corr_second"), _node(profile), {"frame": frame})

    assert calls == 1
    assert handler.cache_size() == 1
    assert context.state["undistort"]["camera_undistort_remap"]["cache_key"].startswith(
        profile.calibration_hash
    )
    assert context.state["undistort"]["cache_hits"] == 1


def test_pipeline_engine_runs_source_undistort_and_display() -> None:
    profile = _profile()
    source_definition = BUILTIN_NODE_TYPE_BY_KEY["source.channel_frame"]
    display_definition = BUILTIN_NODE_TYPE_BY_KEY["output.display_output"]
    nodes = [
        RuntimeNodeSpec(
            node_key="source",
            node_type=source_definition.node_type,
            node_category=source_definition.node_category,
            input_ports=source_definition.input_ports,
            output_ports=source_definition.output_ports,
            config=source_definition.default_config,
        ),
        _node(profile),
        RuntimeNodeSpec(
            node_key="display",
            node_type=display_definition.node_type,
            node_category=display_definition.node_category,
            input_ports=display_definition.input_ports,
            output_ports=display_definition.output_ports,
            config=display_definition.default_config,
        ),
    ]
    graph = RuntimePipelineGraph(
        nodes=nodes,
        edges=[
            RuntimeGraphEdge(
                from_node_key="source",
                from_port="frame",
                to_node_key="undistort",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="undistort",
                from_port="frame",
                to_node_key="display",
                to_port="frame",
            ),
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="piv_undistort",
        graph=graph,
        handler_refs={
            node.node_type: BUILTIN_NODE_TYPE_BY_KEY[node.node_type].runtime_handler_ref
            for node in nodes
        },
    )
    context = _context()
    context.input_frame = FramePayload.from_cv2_frame(
        np.zeros((profile.image_height, profile.image_width, 3), dtype=np.uint8)
    )

    execution = executable.execute(context)

    assert execution.status == "completed"
    assert execution.errors == []
    assert execution.node_outputs["undistort"]["frame"]["image_context"]["transform"][
        "transform_type"
    ] == "undistort"
    assert execution.node_outputs["display"]["display"]["display_context"]["transform"][
        "parameters"
    ]["perspective_rectified"] is False
