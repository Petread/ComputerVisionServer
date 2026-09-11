from __future__ import annotations

import cv_runtime.nodes.stem_process_coordinates as process_coordinate_node
import numpy as np
import pytest
from cv_contracts.runtime import (
    RuntimeImageViewProfileCalculateRequest,
    RuntimeNodeSpec,
    RuntimeNormalizedImagePoint,
    RuntimeStemCurvatureProcessCalibrationProfile,
)
from cv_runtime import BUILTIN_NODE_TYPE_BY_KEY
from cv_runtime.context import RuntimeExecutionContext
from cv_runtime.errors import RuntimeNodeExecutionError
from cv_runtime.image_view import calculate_image_view_profile
from cv_runtime.loader import load_runtime_handler
from cv_runtime.nodes.stem_process_coordinates import StemProcessCoordinateRectifyHandler
from cv_runtime.payloads import FramePayload, RuntimeImageContext, RuntimeImageTransform
from cv_runtime.stem_process_coordinates import (
    calculate_stem_curvature_process_calibration,
    stem_curvature_process_calibration_hash,
)
from cv_status import RuntimeMode
from tests.unit.backend.test_stem_process_coordinates import process_calibration_request


def _profile() -> RuntimeStemCurvatureProcessCalibrationProfile:
    result = calculate_stem_curvature_process_calibration(
        source_channel_id="chn_stem",
        request=process_calibration_request(),
        generated_at="2026-08-04T10:00:00Z",
    )
    assert result.profile is not None
    return result.profile


def _node(profile: object, *, view_profile: object | None = None) -> RuntimeNodeSpec:
    definition = BUILTIN_NODE_TYPE_BY_KEY["preprocessing.process_coordinate_rectify"]
    return RuntimeNodeSpec(
        node_key="process_rectify",
        node_type=definition.node_type,
        node_category=definition.node_category,
        node_contract_version=definition.contract_version,
        input_ports=definition.input_ports,
        output_ports=definition.output_ports,
        config={"calibration": profile, "view_profile": view_profile},
    )


def _context() -> RuntimeExecutionContext:
    return RuntimeExecutionContext(
        correlation_id="corr_process_rectify",
        pipeline_version_id="piv_process_rectify",
        channel_id="chn_stem",
        mode=RuntimeMode.REALTIME,
    )


def _undistorted_frame(
    *,
    intrinsic_hash: str = "a" * 64,
    width: int = 640,
    height: int = 480,
) -> FramePayload:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :, 1] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    return FramePayload.from_cv2_frame(
        image,
        image_context=RuntimeImageContext(
            context_id="ctx_undistorted",
            kind="processed",
            width=width,
            height=height,
            source_image_context_id="ctx_raw",
            transform=RuntimeImageTransform(
                transform_type="undistort",
                coordinate_projection="not_projectable",
                parameters={
                    "calibration_hash": intrinsic_hash,
                    "perspective_rectified": False,
                },
            ),
            metadata={"calibration_hash": intrinsic_hash},
        ),
        metadata={
            "operation": "camera_undistort",
            "calibration_hash": intrinsic_hash,
            "perspective_rectified": False,
        },
    )


def test_registry_loader_and_property_editor_contract_expose_process_rectify() -> None:
    definition = BUILTIN_NODE_TYPE_BY_KEY["preprocessing.process_coordinate_rectify"]

    assert [(port.key, port.data_type) for port in definition.input_ports] == [("frame", "frame")]
    assert [(port.key, port.data_type) for port in definition.output_ports] == [("frame", "frame")]
    assert any(
        field.key == "calibration" and field.field_type == "stem_curvature_process_calibration"
        for field in definition.property_schema
    )
    handler = load_runtime_handler(
        definition.runtime_handler_ref,
        expected_node_type=definition.node_type,
    )
    assert handler.node_type == "preprocessing.process_coordinate_rectify"
    assert definition.compatibility_constraints["stateful"] is False


def test_handler_rectifies_frame_and_carries_versioned_process_geometry() -> None:
    profile = _profile()
    handler = StemProcessCoordinateRectifyHandler()

    result = handler.execute(
        _context(),
        _node(profile.model_dump(mode="json")),
        {"frame": _undistorted_frame()},
    )
    output = result.outputs["frame"]

    assert isinstance(output, FramePayload)
    assert output.width == profile.image_width
    assert output.height == profile.image_height
    assert output.image_context is not None
    assert output.image_context.transform.transform_type == "perspective_rectify"
    assert output.image_context.transform.coordinate_projection == "homography"
    assert output.image_context.transform.parameters["process_calibration_hash"] == (
        profile.calibration_hash
    )
    assert output.metadata["perspective_rectified"] is True
    assert (
        output.metadata["stem_curvature_process_calibration"]["decision_gate"]["process_start"][
            "x_mm"
        ]
        > 0
    )


def test_handler_caches_static_profile_validation_but_keeps_frame_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    node = _node(profile.model_dump(mode="json"))
    handler = StemProcessCoordinateRectifyHandler()
    calls = {"profile": 0, "frame": 0}
    original_profile_validation = (
        process_coordinate_node.validate_stem_curvature_process_calibration
    )
    original_frame_validation = process_coordinate_node.validate_stem_curvature_process_frame

    def validate_profile(value: object) -> RuntimeStemCurvatureProcessCalibrationProfile:
        calls["profile"] += 1
        return original_profile_validation(value)

    def validate_frame(*args: object, **kwargs: object) -> None:
        calls["frame"] += 1
        return original_frame_validation(*args, **kwargs)

    process_coordinate_node._cached_process_profile.cache_clear()
    process_coordinate_node._cached_process_transform_metadata.cache_clear()
    monkeypatch.setattr(
        process_coordinate_node,
        "validate_stem_curvature_process_calibration",
        validate_profile,
    )
    monkeypatch.setattr(
        process_coordinate_node,
        "validate_stem_curvature_process_frame",
        validate_frame,
    )

    first = handler.execute(_context(), node, {"frame": _undistorted_frame()})
    handler.execute(_context(), node, {"frame": _undistorted_frame()})
    first_profile = first.outputs["frame"].metadata["stem_curvature_process_calibration"]
    first_profile["decision_gate"]["process_start"]["x_mm"] = -999.0
    third = handler.execute(_context(), node, {"frame": _undistorted_frame()})

    assert calls == {"profile": 1, "frame": 3}
    assert (
        third.outputs["frame"].metadata["stem_curvature_process_calibration"]["decision_gate"][
            "process_start"
        ]["x_mm"]
        > 0
    )


def test_handler_fails_closed_for_raw_resolution_or_camera_binding_changes() -> None:
    profile = _profile()
    handler = StemProcessCoordinateRectifyHandler()
    raw = FramePayload.from_cv2_frame(np.zeros((480, 640, 3), dtype=np.uint8))

    with pytest.raises(RuntimeNodeExecutionError) as raw_error:
        handler.execute(
            _context(),
            _node(profile.model_dump(mode="json")),
            {"frame": raw},
        )
    with pytest.raises(RuntimeNodeExecutionError) as resolution_error:
        handler.execute(
            _context(),
            _node(profile.model_dump(mode="json")),
            {"frame": _undistorted_frame(width=600, height=400)},
        )
    with pytest.raises(RuntimeNodeExecutionError) as binding_error:
        handler.execute(
            _context(),
            _node(profile.model_dump(mode="json")),
            {"frame": _undistorted_frame(intrinsic_hash="b" * 64)},
        )

    assert raw_error.value.error.code == "stem_process_intrinsic_undistort_required"
    assert resolution_error.value.error.code == "stem_process_frame_calibration_mismatch"
    assert binding_error.value.error.code == "stem_process_camera_pose_binding_mismatch"


def test_canonical_corridor_profile_is_bound_to_the_exact_image_view() -> None:
    view_result = calculate_image_view_profile(
        source_channel_id="chn_stem",
        request=RuntimeImageViewProfileCalculateRequest(
            view_profile_id="stem-measurement-view-v1",
            source_image_width=640,
            source_image_height=480,
            intrinsic_calibration_hash="a" * 64,
            source_quadrilateral=[
                RuntimeNormalizedImagePoint(x=0.05, y=0.05),
                RuntimeNormalizedImagePoint(x=0.95, y=0.05),
                RuntimeNormalizedImagePoint(x=0.95, y=0.95),
                RuntimeNormalizedImagePoint(x=0.05, y=0.95),
            ],
            output_width=320,
            output_height=240,
            transform_type="POLYGON_MASKED_CROP",
            interpolation="LINEAR",
        ),
        generated_at="2026-08-06T14:00:00Z",
    )
    assert view_result.profile is not None
    view_profile = view_result.profile
    bound_profile = _profile().model_copy(
        update={
            "measurement_geometry_mode": "CANONICAL_VIEW_CORRIDOR_V1",
            "canonical_view_profile_id": view_profile.view_profile_id,
            "canonical_view_profile_checksum": view_profile.profile_checksum,
        }
    )
    bound_profile = bound_profile.model_copy(
        update={"calibration_hash": stem_curvature_process_calibration_hash(bound_profile)}
    )
    handler = StemProcessCoordinateRectifyHandler()

    output = handler.execute(
        _context(),
        _node(
            bound_profile.model_dump(mode="json"),
            view_profile=view_profile.model_dump(mode="json"),
        ),
        {"frame": _undistorted_frame()},
    ).outputs["frame"]
    mismatched = bound_profile.model_copy(update={"canonical_view_profile_checksum": "b" * 64})
    mismatched = mismatched.model_copy(
        update={"calibration_hash": stem_curvature_process_calibration_hash(mismatched)}
    )
    with pytest.raises(RuntimeNodeExecutionError) as error:
        handler.execute(
            _context(),
            _node(
                mismatched.model_dump(mode="json"),
                view_profile=view_profile.model_dump(mode="json"),
            ),
            {"frame": _undistorted_frame()},
        )

    assert isinstance(output, FramePayload)
    assert output.width == 320
    assert output.height == 240
    assert error.value.error.code == "stem_process_canonical_view_binding_mismatch"
