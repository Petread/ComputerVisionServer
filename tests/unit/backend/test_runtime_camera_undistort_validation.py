from __future__ import annotations

from pathlib import Path

import pytest
from cv_contracts.runtime import (
    RuntimeAuthoringNodeSpec,
    RuntimeCameraCalibrationBoardDefinition,
    RuntimeCameraCalibrationRoi,
    RuntimeCameraIntrinsicCalibrationProfile,
    RuntimeGraphEdge,
    RuntimePipelineAuthoringGraph,
    RuntimePipelineDefinitionCreateRequest,
    RuntimePipelineValidationRequest,
    RuntimePipelineVersionCreateRequest,
)
from cv_db import (
    create_pipeline_definition,
    create_pipeline_version,
    list_runtime_node_types,
    publish_pipeline_version,
    session_scope,
    validate_pipeline_request,
)
from cv_runtime.camera_calibration import (
    camera_calibration_profile_hash,
    create_identity_camera_calibration,
)
from cv_status import RuntimeMode
from sqlalchemy.engine import Engine
from tests.conftest import build_test_settings, configure_sqlite_factories


def _profile() -> RuntimeCameraIntrinsicCalibrationProfile:
    profile = RuntimeCameraIntrinsicCalibrationProfile(
        status="valid",
        source_channel_id="chn_camera",
        image_width=640,
        image_height=480,
        camera_matrix=[500.0, 0.0, 320.0, 0.0, 500.0, 240.0, 0.0, 0.0, 1.0],
        output_camera_matrix=[
            500.0,
            0.0,
            320.0,
            0.0,
            500.0,
            240.0,
            0.0,
            0.0,
            1.0,
        ],
        distortion_coefficients=[-0.2, 0.03, 0.0, 0.0, 0.0],
        rms_reprojection_error_px=0.4,
        accepted_observation_count=7,
        board_definition=RuntimeCameraCalibrationBoardDefinition(
            board_type="checkerboard",
            columns=9,
            rows=6,
        ),
        valid_roi=RuntimeCameraCalibrationRoi(x=0, y=0, width=640, height=480),
        generated_at="2026-08-03T10:00:00Z",
        calibration_hash="0" * 64,
    )
    return profile.model_copy(
        update={"calibration_hash": camera_calibration_profile_hash(profile)}
    )


def _graph(calibration: object) -> RuntimePipelineAuthoringGraph:
    return RuntimePipelineAuthoringGraph(
        nodes=[
            RuntimeAuthoringNodeSpec(
                node_key="source",
                node_type="source.file_test",
                config={"path": "synthetic-camera-frame.png"},
            ),
            RuntimeAuthoringNodeSpec(
                node_key="undistort",
                node_type="preprocessing.camera_undistort",
                config={"calibration": calibration},
            ),
            RuntimeAuthoringNodeSpec(
                node_key="display",
                node_type="output.display_output",
                config={},
            ),
        ],
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


def test_catalog_exposes_undistort_and_validation_requires_valid_profile(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        node_types = {item.node_type for item in list_runtime_node_types(session)}
        invalid = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                authoring_graph=_graph(None),
            ),
        )
        valid = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                authoring_graph=_graph(_profile().model_dump(mode="json")),
            ),
        )
        passthrough_profile = create_identity_camera_calibration(
            source_channel_id="chn_camera",
            image_width=640,
            image_height=480,
            board_definition=RuntimeCameraCalibrationBoardDefinition(),
            generated_at="2026-08-06T08:00:00Z",
        ).profile
        assert passthrough_profile is not None
        passthrough = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                authoring_graph=_graph(passthrough_profile.model_dump(mode="json")),
            ),
        )

    assert "preprocessing.camera_undistort" in node_types
    assert any(issue.code == "camera_intrinsic_calibration_invalid" for issue in invalid.issues)
    assert valid.status == "valid"
    assert passthrough.status == "valid"
    assert not any(
        issue.code == "camera_intrinsic_calibration_invalid" for issue in valid.issues
    )


def test_publish_is_blocked_until_a_valid_profile_is_versioned(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        definition = create_pipeline_definition(
            session,
            RuntimePipelineDefinitionCreateRequest(name="Camera calibration gate"),
            created_by="tester",
        )
        invalid_version = create_pipeline_version(
            session,
            definition.pipeline_definition_id,
            RuntimePipelineVersionCreateRequest(
                mode=RuntimeMode.REALTIME,
                authoring_graph=_graph(None),
            ),
            created_by="tester",
        )
        with pytest.raises(ValueError, match="validation passes"):
            publish_pipeline_version(
                session,
                invalid_version.pipeline_version_id,
                published_by="tester",
            )

        valid_version = create_pipeline_version(
            session,
            definition.pipeline_definition_id,
            RuntimePipelineVersionCreateRequest(
                mode=RuntimeMode.REALTIME,
                authoring_graph=_graph(_profile().model_dump(mode="json")),
            ),
            created_by="tester",
        )
        published = publish_pipeline_version(
            session,
            valid_version.pipeline_version_id,
            published_by="tester",
        )

    assert invalid_version.status == "draft"
    assert published.status == "published"
