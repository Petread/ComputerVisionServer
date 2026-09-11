from __future__ import annotations

import cv2
import numpy as np
import pytest
from cv_contracts import SegmentationPoint, SegmentationPrediction
from cv_contracts.runtime import (
    RuntimeImageViewProfileCalculateRequest,
    RuntimeNodeSpec,
    RuntimeNormalizedImagePoint,
    RuntimeStemCurvatureProcessCalibrationProfile,
)
from cv_runtime import BUILTIN_NODE_TYPE_BY_KEY
from cv_runtime.context import RuntimeExecutionContext
from cv_runtime.image_view import apply_image_view, calculate_image_view_profile
from cv_runtime.loader import load_runtime_handler
from cv_runtime.nodes.stem_horizontal_curvature import (
    StemHorizontalCurvatureHandler,
    _normalized_process_point,
)
from cv_runtime.payloads import (
    FramePayload,
    OverlayPayload,
    ResultPayload,
    RuntimeImageContext,
    RuntimeImageTransform,
    RuntimeObjectReference,
    SegmentPayload,
    StemHorizontalCurvaturePayload,
)
from cv_runtime.stem_horizontal_curvature import measure_stem_horizontal_curvature
from cv_runtime.stem_process_coordinates import (
    calculate_stem_curvature_process_calibration,
    process_profile_for_aligned_measurement_grid,
    process_profile_for_rectified_view,
    warp_mask_to_rectified,
)
from cv_status import RuntimeMode
from tests.unit.backend.test_stem_horizontal_curvature import _stem_mask
from tests.unit.backend.test_stem_process_coordinates import process_calibration_request


def _profile() -> RuntimeStemCurvatureProcessCalibrationProfile:
    result = calculate_stem_curvature_process_calibration(
        source_channel_id="chn_stem",
        request=process_calibration_request(),
        generated_at="2026-08-04T10:00:00Z",
    )
    assert result.profile is not None
    return result.profile


def _canonical_corridor_profile() -> RuntimeStemCurvatureProcessCalibrationProfile:
    request = process_calibration_request().model_copy(
        update={
            "measurement_geometry_mode": "CANONICAL_VIEW_CORRIDOR_V1",
            "canonical_view_profile_id": "stem-measurement-view-v1",
            "canonical_view_profile_checksum": "c" * 64,
        }
    )
    result = calculate_stem_curvature_process_calibration(
        source_channel_id="chn_stem",
        request=request,
        generated_at="2026-08-04T10:00:00Z",
    )
    assert result.profile is not None
    return result.profile


def _node() -> RuntimeNodeSpec:
    definition = BUILTIN_NODE_TYPE_BY_KEY["analysis.stem_horizontal_curvature"]
    return RuntimeNodeSpec(
        node_key="horizontal_curvature",
        node_type=definition.node_type,
        node_category=definition.node_category,
        node_contract_version=definition.contract_version,
        input_ports=definition.input_ports,
        output_ports=definition.output_ports,
        config={**definition.default_config, "minimum_geometry_confidence": 0.2},
    )


def _context() -> RuntimeExecutionContext:
    return RuntimeExecutionContext(
        correlation_id="corr_horizontal_curvature",
        pipeline_version_id="piv_horizontal_curvature",
        channel_id="chn_stem",
        mode=RuntimeMode.REALTIME,
        metadata={"live_frame_sequence": 41},
    )


def _frame(profile: RuntimeStemCurvatureProcessCalibrationProfile) -> FramePayload:
    context_id = "ctx_process_rectified"
    return FramePayload(
        image_base64="AA==",
        width=profile.image_width,
        height=profile.image_height,
        timestamp="2026-08-04T10:02:03Z",
        image_context=RuntimeImageContext(
            context_id=context_id,
            kind="transformed",
            width=profile.image_width,
            height=profile.image_height,
            source_image_context_id="ctx_undistorted",
            parent_image_context_id="ctx_undistorted",
            transform=RuntimeImageTransform(
                transform_type="perspective_rectify",
                coordinate_projection="homography",
                parameters={"process_calibration_hash": profile.calibration_hash},
            ),
        ),
        metadata={
            "process_calibration_hash": profile.calibration_hash,
            "stem_curvature_process_calibration": profile.model_dump(mode="json"),
            "perspective_rectified": True,
        },
    )


def _segments(
    profile: RuntimeStemCurvatureProcessCalibrationProfile,
    mask: np.ndarray,
    *,
    confidence: float = 0.91,
) -> SegmentPayload:
    contours, _hierarchy = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    assert len(contours) == 1
    contour = max(contours, key=cv2.contourArea).reshape(-1, 2)
    polygon = [
        SegmentationPoint(
            x=float(x) / max(profile.image_width - 1, 1),
            y=float(y) / max(profile.image_height - 1, 1),
        )
        for x, y in contour
    ]
    segment = SegmentationPrediction(label="stem", score=0.94, polygon=polygon)
    tracking = {
        "schema_version": "stem.active-tracker.v1",
        "status": "active",
        "track_id": "trk_stem_test_0001",
        "selection_confidence": confidence,
    }
    tracked = RuntimeObjectReference(
        object_id="obj_active_stem",
        object_type="tracked_object",
        source_image_context_id="ctx_process_rectified",
        coordinate_system="normalized_polygon",
        geometry={"points": [point.model_dump(mode="json") for point in polygon]},
        label="stem",
        score=0.94,
        track_id="trk_stem_test_0001",
        producer_node_key="active_tracker",
        metadata={"active_stem_tracking": tracking},
    )
    return SegmentPayload(
        segments=[segment],
        image_width=profile.image_width,
        image_height=profile.image_height,
        image_context_id="ctx_process_rectified",
        objects=[tracked],
        metadata={"active_stem_tracking": tracking},
    )


def test_registry_loader_and_designer_contract_expose_metric_curvature_node() -> None:
    definition = BUILTIN_NODE_TYPE_BY_KEY["analysis.stem_horizontal_curvature"]

    assert [(port.key, port.data_type) for port in definition.input_ports] == [
        ("segments", "segments"),
        ("frame", "frame"),
        ("eligibility", "result"),
    ]
    assert [(port.key, port.data_type) for port in definition.output_ports] == [
        ("curvature", "stem_horizontal_curvature"),
        ("overlay", "overlay"),
    ]
    assert definition.compatibility_constraints["fragment_reconstruction"] is False
    # Optional observation-rate limiting stores a track-local timestamp in
    # RuntimeExecutionContext.state, so the realtime executor must serialize it.
    assert definition.compatibility_constraints["stateful"] is True
    assert any(
        field.key == "inner_longitudinal_fraction"
        and field.section == "Comparable segment"
        for field in definition.property_schema
    )
    handler = load_runtime_handler(
        definition.runtime_handler_ref,
        expected_node_type=definition.node_type,
    )
    assert handler.node_type == "analysis.stem_horizontal_curvature"


def test_debug_overlay_projection_cannot_fail_on_out_of_frame_process_point() -> None:
    profile = _profile().model_copy(
        update={
            "homography_process_to_image": [
                1.0,
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
                0.0,
                0.0,
                1.0,
            ]
        }
    )

    assert _normalized_process_point(-10.0, -18.4, profile) == {"x": 0.0, "y": 0.0}
    assert _normalized_process_point(
        profile.image_width + 10.0,
        profile.image_height + 10.0,
        profile,
    ) == {"x": 1.0, "y": 1.0}


def test_handler_emits_metric_profiles_for_active_track_before_freeze_gate() -> None:
    profile = _profile()
    frame = _frame(profile)
    segments = _segments(
        profile,
        _stem_mask(profile, bow_mm=40.0, x_start_mm=520.0, x_end_mm=1600.0),
    )

    result = StemHorizontalCurvatureHandler().execute(
        _context(),
        _node(),
        {
            "frame": frame,
            "segments": segments,
            "eligibility": ResultPayload(
                decision=True,
                result={"eligible": True, "task_role": "POSITIVE", "reason_codes": []},
            ),
        },
    )
    payload = result.outputs["curvature"]
    overlay = result.outputs["overlay"]

    assert isinstance(payload, StemHorizontalCurvaturePayload)
    assert isinstance(overlay, OverlayPayload)
    assert payload.valid, (payload.reason_codes, payload.metadata)
    assert payload.track_id == "trk_stem_test_0001"
    assert payload.frame_sequence == 41
    assert payload.calibration_id == profile.calibration_id
    assert payload.signed_bow_mm is not None and payload.signed_bow_mm > 10
    assert payload.curvature_rms_mm is not None
    assert payload.centerline_profile
    assert payload.width_profile
    assert payload.common_segment_descriptor.comparable
    assert payload.metadata["no_fragment_reconstruction"] is True
    assert {primitive.layer for primitive in overlay.primitives} >= {
        "stem_process_axes",
        "stem_curvature_centerline",
        "stem_curvature_reference",
        "stem_curvature_residual",
    }

    result_definition = BUILTIN_NODE_TYPE_BY_KEY["output.result"]
    result_node = RuntimeNodeSpec(
        node_key="result",
        node_type=result_definition.node_type,
        node_category=result_definition.node_category,
        node_contract_version=result_definition.contract_version,
        input_ports=result_definition.input_ports,
        output_ports=result_definition.output_ports,
        config={"include_runtime_metadata": False},
    )
    result_handler = load_runtime_handler(
        result_definition.runtime_handler_ref,
        expected_node_type=result_definition.node_type,
    )
    projected = result_handler.execute(
        _context(),
        result_node,
        {"result": payload},
    ).outputs["result"]
    assert projected.result["signed_bow_mm"] == payload.signed_bow_mm
    assert projected.result["stem_horizontal_curvature"]["track_id"] == payload.track_id
    assert projected.decision is True


def test_metric_curvature_is_stable_when_model_view_preserves_camera_perspective() -> None:
    profile = _profile()
    source_mask = _stem_mask(
        profile,
        bow_mm=40.0,
        x_start_mm=520.0,
        x_end_mm=1600.0,
    )
    view_computation = calculate_image_view_profile(
        source_channel_id="chn_stem",
        request=RuntimeImageViewProfileCalculateRequest(
            view_profile_id="stem-perspective-preserved-v1",
            source_image_width=profile.image_width,
            source_image_height=profile.image_height,
            intrinsic_calibration_hash=profile.intrinsic_calibration_hash,
            source_quadrilateral=[
                RuntimeNormalizedImagePoint(x=0.08, y=0.08),
                RuntimeNormalizedImagePoint(x=0.92, y=0.05),
                RuntimeNormalizedImagePoint(x=0.88, y=0.94),
                RuntimeNormalizedImagePoint(x=0.10, y=0.90),
            ],
            output_width=640,
            output_height=480,
            transform_type="POLYGON_MASKED_CROP",
        ),
    )
    assert view_computation.profile is not None
    view_profile = view_computation.profile
    view_mask = apply_image_view(source_mask, view_profile, mask=True)
    source_to_process = np.asarray(
        profile.homography_image_to_process, dtype=np.float64
    ).reshape(3, 3)
    view_to_source = np.asarray(
        view_profile.view_to_source_homography, dtype=np.float64
    ).reshape(3, 3)
    view_to_process = source_to_process @ view_to_source
    view_to_process /= view_to_process[2, 2]
    process_to_view = np.linalg.inv(view_to_process)
    process_to_view /= process_to_view[2, 2]
    view_process_profile = process_profile_for_rectified_view(
        profile,
        image_width=view_profile.output_width,
        image_height=view_profile.output_height,
        transform_parameters={
            "homography_view_to_process": view_to_process.reshape(-1).tolist(),
            "homography_process_to_view": process_to_view.reshape(-1).tolist(),
        },
    )
    aligned_mask = warp_mask_to_rectified(view_mask, view_process_profile)
    aligned_profile = process_profile_for_aligned_measurement_grid(view_process_profile)

    direct = measure_stem_horizontal_curvature(source_mask, profile)
    through_model_view = measure_stem_horizontal_curvature(
        aligned_mask,
        aligned_profile,
        internal_aligned_process_grid=True,
    )

    assert direct.valid, direct.reason_codes
    assert through_model_view.valid, through_model_view.reason_codes
    assert through_model_view.signed_bow_mm == pytest.approx(direct.signed_bow_mm, abs=2.5)
    assert through_model_view.curvature_rms_mm == pytest.approx(
        direct.curvature_rms_mm, abs=1.5
    )


def test_measurement_eligibility_rejection_blocks_geometry_observation() -> None:
    profile = _profile()
    frame = _frame(profile)
    segments = _segments(
        profile,
        _stem_mask(profile, bow_mm=40.0, x_start_mm=520.0, x_end_mm=1600.0),
    )

    result = StemHorizontalCurvatureHandler().execute(
        _context(),
        _node(),
        {
            "frame": frame,
            "segments": segments,
            "eligibility": ResultPayload(
                decision=False,
                result={
                    "eligible": False,
                    "task_role": "POSITIVE",
                    "reason_codes": ["STEM_CAPTURE_CROWD_BOUNDARY_TOUCHED"],
                },
            ),
        },
    )
    payload = result.outputs["curvature"]
    overlay = result.outputs["overlay"]

    assert isinstance(payload, StemHorizontalCurvaturePayload)
    assert not payload.valid
    assert payload.signed_bow_mm is None
    assert payload.reason_codes == ["measurement_eligibility_rejected"]
    assert payload.metadata["geometry_observation_performed"] is False
    assert "STEM_CAPTURE_CROWD_BOUNDARY_TOUCHED" in payload.metadata["detail"]
    assert isinstance(overlay, OverlayPayload)
    assert {primitive.label for primitive in overlay.primitives} >= {"X [mm]", "Y [mm]"}


def test_freeze_gate_prevents_a_new_geometry_observation_and_all_metrics() -> None:
    profile = _canonical_corridor_profile()
    frame = _frame(profile)
    segments = _segments(
        profile,
        _stem_mask(profile, bow_mm=40.0, x_start_mm=520.0, x_end_mm=1750.0),
    )

    result = StemHorizontalCurvatureHandler().execute(
        _context(),
        _node(),
        {"frame": frame, "segments": segments},
    )
    payload = result.outputs["curvature"]

    assert isinstance(payload, StemHorizontalCurvaturePayload)
    assert not payload.valid
    assert payload.observation_state == "curvature_frozen"
    assert payload.signed_bow_mm is None
    assert payload.centerline_profile == []
    assert payload.metadata["geometry_observation_performed"] is False
    assert payload.metadata["measurement_geometry_mode"] == "CANONICAL_VIEW_CORRIDOR_V1"
    assert payload.metadata["canonical_view_profile_id"] == "stem-measurement-view-v1"
    assert "curvature_freeze_gate_reached" in payload.reason_codes


def test_handler_fails_closed_for_tracking_or_process_context_mismatch() -> None:
    profile = _profile()
    frame = _frame(profile)
    mask = _stem_mask(profile, bow_mm=30.0, x_start_mm=520.0, x_end_mm=1600.0)
    low_confidence = _segments(profile, mask, confidence=0.1)
    wrong_context = _segments(profile, mask)
    wrong_context.image_context_id = "ctx_other"

    low_result = StemHorizontalCurvatureHandler().execute(
        _context(),
        _node(),
        {"frame": frame, "segments": low_confidence},
    ).outputs["curvature"]
    mismatch_result = StemHorizontalCurvatureHandler().execute(
        _context(),
        _node(),
        {"frame": frame, "segments": wrong_context},
    ).outputs["curvature"]

    assert isinstance(low_result, StemHorizontalCurvaturePayload)
    assert not low_result.valid and low_result.signed_bow_mm is None
    assert low_result.reason_codes == ["active_stem_tracking_confidence_low"]
    assert isinstance(mismatch_result, StemHorizontalCurvaturePayload)
    assert not mismatch_result.valid and mismatch_result.signed_bow_mm is None
    assert mismatch_result.reason_codes == ["active_stem_image_context_mismatch"]
