from __future__ import annotations

from pathlib import Path

from cv_contracts.runtime import (
    RuntimeAuthoringNodeSpec,
    RuntimeCameraIntrinsicCalibrationProfile,
    RuntimeGraphEdge,
    RuntimeImageViewProfileCalculateRequest,
    RuntimeLogicalModelRef,
    RuntimeNodeSpec,
    RuntimeNormalizedImagePoint,
    RuntimePipelineAuthoringGraph,
    RuntimePipelineDefinitionCreateRequest,
    RuntimePipelineGraph,
    RuntimePipelineValidationRequest,
    RuntimePipelineVersionCreateRequest,
    RuntimeStemCurvatureProcessCalibrationProfile,
)
from cv_db import (
    create_pipeline_definition,
    create_pipeline_version,
    session_scope,
    validate_pipeline_request,
)
from cv_db.pipeline_store import _validate_stem_minimum_principle_validation_graph
from cv_runtime import BUILTIN_NODE_TYPE_BY_KEY
from cv_runtime.image_view import calculate_image_view_profile
from cv_runtime.stem_minimum_principle_validation import (
    calculate_stem_minimum_principle_validation,
)
from cv_runtime.stem_process_coordinates import (
    calculate_stem_curvature_process_calibration,
    stem_curvature_process_calibration_hash,
)
from cv_runtime.stem_surface_phase import (
    calculate_stem_surface_phase_calibration,
    stem_surface_phase_calibration_hash,
)
from cv_status import RuntimeMode, TargetPath, TaskType
from sqlalchemy.engine import Engine
from tests.conftest import build_test_settings, configure_sqlite_factories
from tests.support.runtime_model_seed import seed_deployed_runtime_model
from tests.unit.backend.test_runtime_camera_undistort_validation import _profile
from tests.unit.backend.test_stem_minimum_principle_validation import (
    _request as minimum_principle_validation_request,
)
from tests.unit.backend.test_stem_process_coordinates import process_calibration_request
from tests.unit.backend.test_stem_surface_phase import _request as phase_calibration_request


def _process_profile() -> tuple[
    RuntimeCameraIntrinsicCalibrationProfile,
    RuntimeStemCurvatureProcessCalibrationProfile,
]:
    intrinsic = _profile()
    request = process_calibration_request().model_copy(
        update={"intrinsic_calibration_hash": intrinsic.calibration_hash}
    )
    result = calculate_stem_curvature_process_calibration(
        source_channel_id=intrinsic.source_channel_id,
        request=request,
        generated_at="2026-08-04T10:00:00Z",
    )
    assert result.profile is not None
    return intrinsic, result.profile


def _graph(
    *,
    process_calibration: object,
    view_profile: object | None = None,
    view_profile_id: str | None = None,
) -> RuntimePipelineAuthoringGraph:
    intrinsic, _process = _process_profile()
    return RuntimePipelineAuthoringGraph(
        nodes=[
            RuntimeAuthoringNodeSpec(
                node_key="source",
                node_type="source.file_test",
                config={"path": "synthetic-undistort-source.png"},
            ),
            RuntimeAuthoringNodeSpec(
                node_key="undistort",
                node_type="preprocessing.camera_undistort",
                config={"calibration": intrinsic.model_dump(mode="json")},
            ),
            RuntimeAuthoringNodeSpec(
                node_key="process_rectify",
                node_type="preprocessing.process_coordinate_rectify",
                config={
                    "calibration": process_calibration,
                    "view_profile": view_profile,
                    "view_profile_id": view_profile_id,
                },
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
                to_node_key="process_rectify",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="process_rectify",
                from_port="frame",
                to_node_key="display",
                to_port="frame",
            ),
        ],
    )


def test_pipeline_validation_requires_valid_bound_process_profile(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)
    _intrinsic, process = _process_profile()
    mismatched = process.model_copy(update={"intrinsic_calibration_hash": "b" * 64})
    mismatched = mismatched.model_copy(
        update={"calibration_hash": stem_curvature_process_calibration_hash(mismatched)}
    )

    with session_scope(settings) as session:
        missing = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                authoring_graph=_graph(process_calibration=None),
            ),
        )
        wrong_binding = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                authoring_graph=_graph(process_calibration=mismatched.model_dump(mode="json")),
            ),
        )
        valid = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                authoring_graph=_graph(process_calibration=process.model_dump(mode="json")),
            ),
        )

    assert any(issue.code == "image_view_profile_missing" for issue in missing.issues)
    assert any(
        issue.code == "stem_process_intrinsic_binding_mismatch" for issue in wrong_binding.issues
    )
    assert valid.status == "valid", [issue.model_dump(mode="json") for issue in valid.issues]


def test_pipeline_validation_reports_changed_canonical_corridor(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)
    intrinsic, process = _process_profile()
    view_result = calculate_image_view_profile(
        source_channel_id=intrinsic.source_channel_id,
        request=RuntimeImageViewProfileCalculateRequest(
            view_profile_id="stem-measurement-view-v1",
            source_image_width=intrinsic.image_width,
            source_image_height=intrinsic.image_height,
            intrinsic_calibration_hash=intrinsic.calibration_hash,
            source_quadrilateral=[
                RuntimeNormalizedImagePoint(x=0.05, y=0.05),
                RuntimeNormalizedImagePoint(x=0.95, y=0.05),
                RuntimeNormalizedImagePoint(x=0.95, y=0.95),
                RuntimeNormalizedImagePoint(x=0.05, y=0.95),
            ],
            output_width=640,
            output_height=480,
            transform_type="POLYGON_MASKED_CROP",
        ),
        generated_at="2026-08-06T14:00:00Z",
    )
    assert view_result.profile is not None
    view_profile = view_result.profile
    bound = process.model_copy(
        update={
            "measurement_geometry_mode": "CANONICAL_VIEW_CORRIDOR_V1",
            "canonical_view_profile_id": view_profile.view_profile_id,
            "canonical_view_profile_checksum": "b" * 64,
        }
    )
    bound = bound.model_copy(
        update={"calibration_hash": stem_curvature_process_calibration_hash(bound)}
    )

    with session_scope(settings) as session:
        report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                authoring_graph=_graph(
                    process_calibration=bound.model_dump(mode="json"),
                    view_profile=view_profile.model_dump(mode="json"),
                ),
            ),
        )

    assert any(
        issue.code == "stem_process_canonical_view_binding_mismatch"
        for issue in report.issues
    )

    with session_scope(settings) as session:
        id_mismatch = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                authoring_graph=_graph(
                    process_calibration=process.model_dump(mode="json"),
                    view_profile=view_profile.model_dump(mode="json"),
                    view_profile_id="stem-measurement-view-v2",
                ),
            ),
        )

    id_issue = next(
        issue
        for issue in id_mismatch.issues
        if issue.code == "image_view_intrinsic_binding_mismatch"
    )
    assert "configured profile ID 'stem-measurement-view-v2'" in id_issue.message
    assert "applied profile is 'stem-measurement-view-v1'" in id_issue.message


def test_pipeline_validation_reports_model_image_view_contract_mismatch(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)
    intrinsic, process = _process_profile()
    view_result = calculate_image_view_profile(
        source_channel_id=intrinsic.source_channel_id,
        request=RuntimeImageViewProfileCalculateRequest(
            view_profile_id="stem-measurement-view-v2",
            source_image_width=intrinsic.image_width,
            source_image_height=intrinsic.image_height,
            intrinsic_calibration_hash=intrinsic.calibration_hash,
            source_quadrilateral=[
                RuntimeNormalizedImagePoint(x=0.05, y=0.05),
                RuntimeNormalizedImagePoint(x=0.95, y=0.05),
                RuntimeNormalizedImagePoint(x=0.95, y=0.95),
                RuntimeNormalizedImagePoint(x=0.05, y=0.95),
            ],
            output_width=640,
            output_height=480,
            transform_type="POLYGON_MASKED_CROP",
        ),
        generated_at="2026-08-17T12:00:00Z",
    )
    assert view_result.profile is not None
    view_profile = view_result.profile
    graph = RuntimePipelineAuthoringGraph(
        nodes=[
            RuntimeAuthoringNodeSpec(
                node_key="source",
                node_type="source.file_test",
                config={"path": "stem.png"},
            ),
            RuntimeAuthoringNodeSpec(
                node_key="undistort",
                node_type="preprocessing.camera_undistort",
                config={"calibration": intrinsic.model_dump(mode="json")},
            ),
            RuntimeAuthoringNodeSpec(
                node_key="canonical_view",
                node_type="preprocessing.process_coordinate_rectify",
                config={
                    "view_profile_id": view_profile.view_profile_id,
                    "view_profile": view_profile.model_dump(mode="json"),
                    "calibration": process.model_dump(mode="json"),
                },
            ),
            RuntimeAuthoringNodeSpec(
                node_key="segment_stem",
                node_type="inference.segmentation",
                config={"require_image_view_compatibility": True},
                logical_model_ref=RuntimeLogicalModelRef(
                    model_family_slug="stem-segmentation-view-contract",
                    target_path=TargetPath.REALTIME,
                    required_task_type=TaskType.SEGMENTATION,
                    selection_policy="explicit_release",
                    deployment_release_id="rel_stem_view_contract",
                ),
            ),
            RuntimeAuthoringNodeSpec(node_key="result", node_type="output.result"),
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
                to_node_key="canonical_view",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="canonical_view",
                from_port="frame",
                to_node_key="segment_stem",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="segment_stem",
                from_port="segments",
                to_node_key="result",
                to_port="result",
            ),
        ],
    )

    with session_scope(settings) as session:
        seed_deployed_runtime_model(
            session,
            release_id="rel_stem_view_contract",
            model_family_id="mf_stem_view_contract",
            model_family_slug="stem-segmentation-view-contract",
            model_family_display_name="Stem segmentation view contract",
            model_version_id="mvr_stem_view_contract",
            task_type=TaskType.SEGMENTATION,
            serving_manifest={
                "class_labels": ["Stamm"],
                "preprocessing": {
                    "image_view": {
                        "view_profile_id": "stem-measurement-view-v1",
                        "profile_checksum": "f" * 64,
                        "input_width": 640,
                        "input_height": 480,
                        "color_space": "BGR",
                        "undistort_preprocessing_version": "camera.undistort.v1",
                        "image_view_preprocessing_version": "image.view-runtime.v1",
                    }
                },
            },
        )
        report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                authoring_graph=graph,
            ),
        )

    issue = next(
        item for item in report.issues if item.code == "model_image_view_difference"
    )
    assert "model=stem-measurement-view-v1" in issue.message
    assert "view=stem-measurement-view-v2" in issue.message
    assert issue.node_key == "segment_stem"
    assert issue.severity == "warning"
    assert report.status == "valid", [item.model_dump(mode="json") for item in report.issues]


def test_process_profile_round_trips_in_pipeline_version_graph(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)
    _intrinsic, process = _process_profile()

    with session_scope(settings) as session:
        definition = create_pipeline_definition(
            session,
            RuntimePipelineDefinitionCreateRequest(name="Stem process calibration"),
            created_by="tester",
        )
        version = create_pipeline_version(
            session,
            definition.pipeline_definition_id,
            RuntimePipelineVersionCreateRequest(
                mode=RuntimeMode.REALTIME,
                authoring_graph=_graph(process_calibration=process.model_dump(mode="json")),
            ),
            created_by="tester",
        )

    stored = next(node for node in version.graph.nodes if node.node_key == "process_rectify")
    assert stored.config["calibration"]["schema_version"] == (
        "stem.curvature-process-calibration.v1"
    )
    assert stored.config["calibration"]["calibration_hash"] == process.calibration_hash
    assert stored.config["calibration"]["decision_gate"]["process_start"]["x_mm"] > 0


def test_horizontal_curvature_pipeline_is_validatable_with_existing_runtime_nodes(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)
    intrinsic, process = _process_profile()
    tracker_config = {
        "transfer_rois": [{"x": 0.0, "y": 0.0, "width": 0.35, "height": 1.0}],
        "longitudinal_conveyor_rois": [{"x": 0.2, "y": 0.0, "width": 0.8, "height": 1.0}],
    }

    graph = RuntimePipelineAuthoringGraph(
        nodes=[
            RuntimeAuthoringNodeSpec(
                node_key="source",
                node_type="source.file_test",
                config={"path": "synthetic-stem-frame.png"},
            ),
            RuntimeAuthoringNodeSpec(
                node_key="undistort",
                node_type="preprocessing.camera_undistort",
                config={"calibration": intrinsic.model_dump(mode="json")},
            ),
            RuntimeAuthoringNodeSpec(
                node_key="process_rectify",
                node_type="preprocessing.process_coordinate_rectify",
                config={"calibration": process.model_dump(mode="json")},
            ),
            RuntimeAuthoringNodeSpec(
                node_key="segment",
                node_type="inference.segmentation",
                logical_model_ref=RuntimeLogicalModelRef(
                    model_family_slug="stem-segmentation",
                    target_path=TargetPath.REALTIME,
                    required_task_type=TaskType.SEGMENTATION,
                    selection_policy="explicit_release",
                    deployment_release_id="rel_stem_curvature_segmentation",
                ),
            ),
            RuntimeAuthoringNodeSpec(
                node_key="active_tracker",
                node_type="analysis.active_stem_tracker",
                config=tracker_config,
            ),
            RuntimeAuthoringNodeSpec(
                node_key="curvature",
                node_type="analysis.stem_horizontal_curvature",
            ),
            RuntimeAuthoringNodeSpec(
                node_key="result",
                node_type="output.result",
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
                to_node_key="process_rectify",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="process_rectify",
                from_port="frame",
                to_node_key="segment",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="segment",
                from_port="segments",
                to_node_key="active_tracker",
                to_port="segments",
            ),
            RuntimeGraphEdge(
                from_node_key="process_rectify",
                from_port="frame",
                to_node_key="active_tracker",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="active_tracker",
                from_port="segments",
                to_node_key="curvature",
                to_port="segments",
            ),
            RuntimeGraphEdge(
                from_node_key="process_rectify",
                from_port="frame",
                to_node_key="curvature",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="curvature",
                from_port="curvature",
                to_node_key="result",
                to_port="result",
            ),
        ],
    )

    with session_scope(settings) as session:
        seed_deployed_runtime_model(
            session,
            release_id="rel_stem_curvature_segmentation",
            model_family_id="mf_stem_curvature_segmentation",
            model_family_slug="stem-segmentation",
            model_family_display_name="Stem Segmentation",
            model_version_id="mvr_stem_curvature_segmentation",
            task_type=TaskType.SEGMENTATION,
        )
        report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                authoring_graph=graph,
            ),
        )

    assert report.status == "valid", [issue.model_dump(mode="json") for issue in report.issues]
    assert "port_type_incompatible" not in {issue.code for issue in report.issues}


def test_surface_phase_profile_is_optional_for_raw_phase_and_bound_for_degrees(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)
    intrinsic, process = _process_profile()
    phase_result = calculate_stem_surface_phase_calibration(
        source_channel_id=intrinsic.source_channel_id,
        request=phase_calibration_request().model_copy(
            update={
                "intrinsic_calibration_hash": intrinsic.calibration_hash,
                "process_calibration_hash": process.calibration_hash,
            }
        ),
        generated_at="2026-08-04T12:00:00Z",
    )
    assert phase_result.profile is not None
    phase = phase_result.profile
    mismatched = phase.model_copy(update={"process_calibration_hash": "f" * 64})
    mismatched = mismatched.model_copy(
        update={"calibration_hash": stem_surface_phase_calibration_hash(mismatched)}
    )

    def graph(calibration: object) -> RuntimePipelineAuthoringGraph:
        return RuntimePipelineAuthoringGraph(
            nodes=[
                RuntimeAuthoringNodeSpec(
                    node_key="source", node_type="source.file_test", config={"path": "stem.png"}
                ),
                RuntimeAuthoringNodeSpec(
                    node_key="undistort",
                    node_type="preprocessing.camera_undistort",
                    config={"calibration": intrinsic.model_dump(mode="json")},
                ),
                RuntimeAuthoringNodeSpec(
                    node_key="process_rectify",
                    node_type="preprocessing.process_coordinate_rectify",
                    config={"calibration": process.model_dump(mode="json")},
                ),
                RuntimeAuthoringNodeSpec(
                    node_key="segment",
                    node_type="inference.segmentation",
                    logical_model_ref=RuntimeLogicalModelRef(
                        model_family_slug="stem-segmentation",
                        target_path=TargetPath.REALTIME,
                        required_task_type=TaskType.SEGMENTATION,
                        selection_policy="explicit_release",
                        deployment_release_id="rel_stem_phase_segmentation",
                    ),
                ),
                RuntimeAuthoringNodeSpec(
                    node_key="active_tracker",
                    node_type="analysis.active_stem_tracker",
                    config={
                        "transfer_rois": [{"x": 0, "y": 0, "width": 0.35, "height": 1}],
                        "longitudinal_conveyor_rois": [
                            {"x": 0.2, "y": 0, "width": 0.8, "height": 1}
                        ],
                    },
                ),
                RuntimeAuthoringNodeSpec(
                    node_key="geometry", node_type="analysis.stem_mask_geometry"
                ),
                RuntimeAuthoringNodeSpec(
                    node_key="phase",
                    node_type="analysis.stem_surface_phase",
                    config={"surface_phase_calibration": calibration},
                ),
                RuntimeAuthoringNodeSpec(node_key="result", node_type="output.result"),
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
                    to_node_key="process_rectify",
                    to_port="frame",
                ),
                RuntimeGraphEdge(
                    from_node_key="process_rectify",
                    from_port="frame",
                    to_node_key="segment",
                    to_port="frame",
                ),
                RuntimeGraphEdge(
                    from_node_key="segment",
                    from_port="segments",
                    to_node_key="active_tracker",
                    to_port="segments",
                ),
                RuntimeGraphEdge(
                    from_node_key="process_rectify",
                    from_port="frame",
                    to_node_key="active_tracker",
                    to_port="frame",
                ),
                RuntimeGraphEdge(
                    from_node_key="active_tracker",
                    from_port="segments",
                    to_node_key="geometry",
                    to_port="segments",
                ),
                RuntimeGraphEdge(
                    from_node_key="process_rectify",
                    from_port="frame",
                    to_node_key="geometry",
                    to_port="frame",
                ),
                RuntimeGraphEdge(
                    from_node_key="active_tracker",
                    from_port="segments",
                    to_node_key="phase",
                    to_port="segments",
                ),
                RuntimeGraphEdge(
                    from_node_key="process_rectify",
                    from_port="frame",
                    to_node_key="phase",
                    to_port="frame",
                ),
                RuntimeGraphEdge(
                    from_node_key="geometry",
                    from_port="stem_geometry",
                    to_node_key="phase",
                    to_port="stem_geometry",
                ),
                RuntimeGraphEdge(
                    from_node_key="phase",
                    from_port="surface_phase",
                    to_node_key="result",
                    to_port="result",
                ),
            ],
        )

    with session_scope(settings) as session:
        seed_deployed_runtime_model(
            session,
            release_id="rel_stem_phase_segmentation",
            model_family_id="mf_stem_phase_segmentation",
            model_family_slug="stem-segmentation",
            model_family_display_name="Stem Segmentation",
            model_version_id="mvr_stem_phase_segmentation",
            task_type=TaskType.SEGMENTATION,
        )
        raw_only = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME, authoring_graph=graph(None)
            ),
        )
        calibrated = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME, authoring_graph=graph(phase.model_dump(mode="json"))
            ),
        )
        wrong_binding = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME, authoring_graph=graph(mismatched.model_dump(mode="json"))
            ),
        )
        definition = create_pipeline_definition(
            session,
            RuntimePipelineDefinitionCreateRequest(name="Stem surface phase calibration"),
            created_by="tester",
        )
        version = create_pipeline_version(
            session,
            definition.pipeline_definition_id,
            RuntimePipelineVersionCreateRequest(
                mode=RuntimeMode.REALTIME,
                authoring_graph=graph(phase.model_dump(mode="json")),
            ),
            created_by="tester",
        )

    assert raw_only.status == "valid", [issue.model_dump(mode="json") for issue in raw_only.issues]
    assert calibrated.status == "valid", [
        issue.model_dump(mode="json") for issue in calibrated.issues
    ]
    assert any(
        issue.code == "stem_surface_phase_process_calibration_mismatch"
        for issue in wrong_binding.issues
    )
    binding_issue = next(
        issue
        for issue in wrong_binding.issues
        if issue.code == "stem_surface_phase_process_calibration_mismatch"
    )
    assert mismatched.process_calibration_hash[:12] in binding_issue.message
    assert process.calibration_hash[:12] in binding_issue.message
    stored_phase = next(node for node in version.graph.nodes if node.node_key == "phase")
    assert stored_phase.config["surface_phase_calibration"]["calibration_hash"] == (
        phase.calibration_hash
    )


def test_objective_validation_artifact_is_bound_to_upstream_calibration_versions() -> None:
    intrinsic, process = _process_profile()
    phase_result = calculate_stem_surface_phase_calibration(
        source_channel_id=intrinsic.source_channel_id,
        request=phase_calibration_request().model_copy(
            update={
                "intrinsic_calibration_hash": intrinsic.calibration_hash,
                "process_calibration_hash": process.calibration_hash,
            }
        ),
        generated_at="2026-08-05T10:00:00Z",
    )
    assert phase_result.profile is not None
    phase = phase_result.profile
    validation_request = minimum_principle_validation_request().model_copy(
        update={
            "image_width": process.image_width,
            "image_height": process.image_height,
            "process_calibration_id": process.calibration_id,
            "process_calibration_hash": process.calibration_hash,
            "phase_calibration_id": phase.calibration_id,
            "phase_calibration_version": phase.calibration_version,
            "phase_calibration_hash": phase.calibration_hash,
        }
    )
    artifact = calculate_stem_minimum_principle_validation(
        source_channel_id=process.source_channel_id,
        request=validation_request,
        generated_at="2026-08-05T12:00:00Z",
    ).artifact

    def node(node_key: str, node_type: str, config: dict[str, object]) -> RuntimeNodeSpec:
        definition = BUILTIN_NODE_TYPE_BY_KEY[node_type]
        return RuntimeNodeSpec(
            node_key=node_key,
            node_type=node_type,
            node_category=definition.node_category,
            node_contract_version=definition.contract_version,
            input_ports=definition.input_ports,
            output_ports=definition.output_ports,
            config={**definition.default_config, **config},
        )

    process_node = node(
        "process_rectify",
        "preprocessing.process_coordinate_rectify",
        {"calibration": process.model_dump(mode="json")},
    )
    phase_node = node(
        "surface_phase",
        "analysis.stem_surface_phase",
        {"surface_phase_calibration": phase.model_dump(mode="json")},
    )
    curvature_node = node("curvature", "analysis.stem_horizontal_curvature", {})
    solver_node = node(
        "solver",
        "analysis.stem_curvature_phase_solver",
        {"minimum_principle_validation": artifact.model_dump(mode="json")},
    )
    graph = RuntimePipelineGraph(
        nodes=[process_node, phase_node, curvature_node, solver_node],
        edges=[
            RuntimeGraphEdge(
                from_node_key="process_rectify",
                from_port="frame",
                to_node_key="surface_phase",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="process_rectify",
                from_port="frame",
                to_node_key="curvature",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="surface_phase",
                from_port="surface_phase",
                to_node_key="solver",
                to_port="surface_phase",
            ),
            RuntimeGraphEdge(
                from_node_key="curvature",
                from_port="curvature",
                to_node_key="solver",
                to_port="curvature",
            ),
        ],
    )

    assert _validate_stem_minimum_principle_validation_graph(
        graph=graph,
        nodes_by_key={item.node_key: item for item in graph.nodes},
    ) == []

    mismatched_request = validation_request.model_copy(
        update={"process_calibration_hash": "f" * 64}
    )
    mismatched_artifact = calculate_stem_minimum_principle_validation(
        source_channel_id=process.source_channel_id,
        request=mismatched_request,
        generated_at="2026-08-05T12:00:00Z",
    ).artifact
    mismatched_solver = solver_node.model_copy(
        update={
            "config": {
                **solver_node.config,
                "minimum_principle_validation": mismatched_artifact.model_dump(mode="json"),
            }
        }
    )
    mismatched_graph = graph.model_copy(
        update={
            "nodes": [process_node, phase_node, curvature_node, mismatched_solver],
        }
    )
    issues = _validate_stem_minimum_principle_validation_graph(
        graph=mismatched_graph,
        nodes_by_key={item.node_key: item for item in mismatched_graph.nodes},
    )

    assert [item.code for item in issues] == [
        "stem_minimum_principle_validation_binding_invalid"
    ]
