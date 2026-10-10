from __future__ import annotations

from pathlib import Path

import pytest
from cv_contracts.edge import EdgeDeviceRegistrationRequest
from cv_contracts.runtime import (
    RuntimeAuthoringNodeSpec,
    RuntimeChannelCreateRequest,
    RuntimeGraphEdge,
    RuntimeLogicalModelRef,
    RuntimeNodeSpec,
    RuntimePipelineAuthoringGraph,
    RuntimePipelineDefinitionCreateRequest,
    RuntimePipelineGraph,
    RuntimePipelineTemplateInstantiateRequest,
    RuntimePipelineValidationRequest,
    RuntimePipelineVersionCreateRequest,
)
from cv_db import (
    PipelineDefinition,
    PipelineVersion,
    RuntimeChannel,
    RuntimeControlCommandRecord,
    TriggerEndpoint,
    archive_pipeline_definition,
    create_edge_device,
    create_pipeline_definition,
    create_pipeline_version,
    create_runtime_channel,
    get_pipeline_definition_detail,
    instantiate_pipeline_template,
    list_pipeline_definitions,
    list_runtime_node_types,
    publish_pipeline_version,
    request_runtime_channel_start,
    session_scope,
    validate_pipeline_request,
    validate_runtime_channel_start_requirements,
)
from cv_db.pipeline_store import _template_definitions, _validate_box_list, _validate_model_binding_groups
from cv_status import RuntimeMode, RuntimeNodeCategory, RuntimeSourceType, TargetPath, TaskType
from sqlalchemy import select
from sqlalchemy.engine import Engine
from tests.conftest import build_test_settings, configure_sqlite_factories
from tests.support.runtime_model_seed import seed_deployed_runtime_model


def test_segmented_board_cut_templates_use_existing_shared_segmentation_binding() -> None:
    templates = {item["slug"]: item for item in _template_definitions()}
    realtime = templates["template-realtime-single-longitudinal-segmented-board-cut-test"]
    multiview = templates["template-trigger-multiview-segmented-board-cut"]

    realtime_nodes = {node.node_key: node for node in realtime["graph"].nodes}
    assert realtime["mode"] == RuntimeMode.REALTIME
    assert realtime_nodes["board_surfaces"].node_type == "inference.segmentation"
    assert realtime_nodes["board_surfaces"].config["model_binding_group"] == "board_surface"
    assert realtime_nodes["coordinates"].config["geometry_source"] == "segmentation"
    assert not realtime_nodes["result_output"].config.get("allow_plc", False)

    segmentation_nodes = [
        node for node in multiview["graph"].nodes if node.node_type == "inference.segmentation"
    ]
    assert multiview["mode"] == RuntimeMode.TRIGGER
    assert len(segmentation_nodes) == 3
    assert {node.config.get("model_binding_group") for node in segmentation_nodes} == {
        "board_surface"
    }
    assert all(node.logical_model_ref is None for node in segmentation_nodes)
    assert all(node.config.get("template_model_unbound") is True for node in segmentation_nodes)
    assert realtime_nodes["board_surfaces"].logical_model_ref is None
    assert _validate_model_binding_groups(graph=multiview["graph"]) == []

    mismatched_nodes = [
        node.model_copy(
            update={
                "logical_model_ref": RuntimeLogicalModelRef(
                    model_family_slug="different-board-surfaces",
                    target_path=TargetPath.REALTIME,
                    required_task_type=TaskType.SEGMENTATION,
                    selection_policy="active_production",
                )
            }
        )
        if node.node_key == "lower_board_surfaces"
        else node
        for node in multiview["graph"].nodes
    ]
    mismatch = _validate_model_binding_groups(
        graph=multiview["graph"].model_copy(update={"nodes": mismatched_nodes})
    )
    assert {issue.code for issue in mismatch} == {"model_binding_group_mismatch"}


def test_validation_requires_confirmed_surface_roles_through_polygon_simplifier(
    sqlite_engine,
    tmp_path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)
    model_ref = RuntimeLogicalModelRef(
        model_family_slug="board-surfaces",
        target_path=TargetPath.REALTIME,
        required_task_type=TaskType.SEGMENTATION,
        selection_policy="explicit_release",
        deployment_release_id="rel_board_surfaces",
    )
    mapping = {
        "board_main_face": {
            "class_id": 0,
            "label_snapshot": "custom-main",
            "class_map_version": "surface-map-1",
            "deployment_release_id": "rel_board_surfaces",
            "confirmed": True,
        },
        "board_side_face": {
            "class_id": 1,
            "label_snapshot": "custom-side",
            "class_map_version": "surface-map-1",
            "deployment_release_id": "rel_board_surfaces",
            "confirmed": True,
        },
    }

    def request(surface_mapping: dict) -> RuntimePipelineValidationRequest:
        return RuntimePipelineValidationRequest(
            mode=RuntimeMode.REALTIME,
            authoring_graph=RuntimePipelineAuthoringGraph(
                nodes=[
                    RuntimeAuthoringNodeSpec(
                        node_key="source",
                        node_type="source.file_test",
                        config={"path": "development-test-frame.png"},
                    ),
                    RuntimeAuthoringNodeSpec(
                        node_key="segment",
                        node_type="inference.segmentation",
                        logical_model_ref=model_ref,
                    ),
                    RuntimeAuthoringNodeSpec(
                        node_key="simplify",
                        node_type="analysis.segmentation_polygon_simplification",
                    ),
                    RuntimeAuthoringNodeSpec(
                        node_key="coordinates",
                        node_type="analysis.board_end_coordinate_frame",
                        config={
                            "geometry_source": "segmentation",
                            "view_role": "upper",
                            "board_surface_class_mapping": surface_mapping,
                        },
                    ),
                ],
                edges=[
                    RuntimeGraphEdge(
                        from_node_key="source",
                        from_port="frame",
                        to_node_key="segment",
                        to_port="frame",
                    ),
                    RuntimeGraphEdge(
                        from_node_key="segment",
                        from_port="segments",
                        to_node_key="simplify",
                        to_port="segments",
                    ),
                    RuntimeGraphEdge(
                        from_node_key="simplify",
                        from_port="segments",
                        to_node_key="coordinates",
                        to_port="segments",
                    ),
                    RuntimeGraphEdge(
                        from_node_key="source",
                        from_port="frame",
                        to_node_key="coordinates",
                        to_port="frame",
                    ),
                ],
            ),
        )

    with session_scope(settings) as session:
        seed_deployed_runtime_model(
            session,
            release_id="rel_board_surfaces",
            model_family_id="mf_board_surfaces",
            model_family_slug="board-surfaces",
            model_family_display_name="Board surfaces",
            model_version_id="mvr_board_surfaces",
            task_type=TaskType.SEGMENTATION,
            serving_manifest={
                "class_labels": ["custom-main", "custom-side", "custom-end"],
                "class_map_version": "surface-map-1",
                "confidence_threshold_default": 0.35,
                "compatible_targets": ["realtime"],
            },
        )
        valid_report = validate_pipeline_request(session, request(mapping))
        invalid_report = validate_pipeline_request(session, request({}))

    assert "board_surface_class_mapping_confirmation_required" not in {
        issue.code for issue in valid_report.issues
    }
    assert "board_surface_class_mapping_confirmation_required" in {
        issue.code for issue in invalid_report.issues
    }


def test_tracker_roi_validation_accepts_normalized_convex_polygons() -> None:
    polygon = [
        {
            "label": "right-side entry",
            "points": [
                {"x": 0.65, "y": 0.15},
                {"x": 1.0, "y": 0.0},
                {"x": 1.0, "y": 1.0},
                {"x": 0.72, "y": 0.85},
            ],
        }
    ]

    assert _validate_box_list(
        polygon,
        field_name="Transfer ROIs",
        code="roi_config_invalid",
        message_suffix="Use normalized boxes or polygons.",
        node_key="active_stem",
        field_key="transfer_rois",
        allow_polygons=True,
    ) == []
    assert _validate_box_list(
        polygon,
        field_name="Transfer ROIs",
        code="roi_config_invalid",
        message_suffix="Use normalized boxes.",
        node_key="active_stem",
        field_key="transfer_rois",
    )


def test_builtin_node_registry_covers_required_categories(sqlite_engine, tmp_path) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        items = list_runtime_node_types(session)

    node_types = {item.node_type for item in items}
    categories = {item.node_category for item in items}
    assert {
        RuntimeNodeCategory.TRIGGER,
        RuntimeNodeCategory.SOURCE,
        RuntimeNodeCategory.PREPROCESSING,
        RuntimeNodeCategory.COMPUTER_VISION,
        RuntimeNodeCategory.INFERENCE,
        RuntimeNodeCategory.ANALYSIS,
        RuntimeNodeCategory.CAPTURE,
        RuntimeNodeCategory.OUTPUT,
    }.issubset(categories)
    assert {
        "trigger.http_put",
        "source.channel_frame",
        "source.test_photo",
        "source.rtsp",
        "source.trigger_snapshot",
        "source.file_test",
        "computer_vision.canny_edge_detection",
        "computer_vision.color_object_segmentation",
        "computer_vision.reference_edge_from_cv_edges",
        "inference.detection",
        "analysis.reference_edge_from_features",
        "analysis.coordinate_frame_from_edges",
        "analysis.feature_measurement",
        "analysis.clearance_zone_generator",
        "analysis.first_free_region_finder",
        "analysis.pixel_to_metric",
        "analysis.quality_rule_evaluation",
        "analysis.stem_mask_geometry",
        "analysis.stem_horizontal_curvature",
        "analysis.zone_detection",
        "analysis.close_detection",
        "analysis.object_spatial_signal",
        "analysis.object_orientation_signal",
        "analysis.rule_evaluation",
        "capture.multi_project",
        "output.display_output",
        "output.result",
        "output.http_trigger_response",
    }.issubset(node_types)
    for item in items:
        if item.compatibility_constraints.get("hidden_from_palette") is True:
            continue
        authoring = item.compatibility_constraints.get("authoring")
        assert isinstance(authoring, dict), item.node_type
        assert {
            "starter",
            "singleton",
            "blocks_with_node_types",
            "requires_upstream_data_types",
            "terminal",
            "palette_reason",
        }.issubset(authoring), item.node_type
        for field in item.property_schema:
            assert field.section, f"{item.node_type}.{field.key} is missing a section"
            assert field.help_text, f"{item.node_type}.{field.key} is missing help_text"
    assert all(item.property_schema or item.node_type == "source.channel_frame" for item in items)
    legacy_rtsp = next(item for item in items if item.node_type == "source.rtsp")
    assert legacy_rtsp.compatibility_constraints["hidden_from_palette"] is True
    display_output = next(item for item in items if item.node_type == "output.display_output")
    assert display_output.compatibility_constraints["live_view_display_selector"] is True
    assert any(port.key == "frame" for port in display_output.input_ports)
    result_output = next(item for item in items if item.node_type == "output.result")
    assert result_output.compatibility_constraints["primary_result_output"] is True
    legacy_persist = next(item for item in items if item.node_type == "output.persisted_result")
    assert legacy_persist.compatibility_constraints["hidden_from_palette"] is True
    inference_nodes = [
        item for item in items if item.node_category == RuntimeNodeCategory.INFERENCE
    ]
    for item in inference_nodes:
        assert item.default_config["timeout_ms"] == 1500
        timeout_property = next(field for field in item.property_schema if field.key == "timeout_ms")
        assert timeout_property.default_value == 1500


def test_builtin_stem_curvature_minimum_template_is_complete_and_fail_closed(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        summaries = list_pipeline_definitions(
            session,
            include_templates=True,
            scope="template",
        )
        template = next(
            item
            for item in summaries
            if item.slug == "template-realtime-stem-curvature-minimum"
        )
        detail = get_pipeline_definition_detail(session, template.pipeline_definition_id)
        assert detail is not None
        assert detail.latest_version is not None
        graph = detail.latest_version.graph
        validation_codes = {
            issue.code for issue in detail.latest_version.validation_report.issues
        }

        instance = instantiate_pipeline_template(
            session,
            template.pipeline_definition_id,
            RuntimePipelineTemplateInstantiateRequest(
                name="Stammausrichtung Linie 1",
                change_note="Bind site calibrations before publishing",
            ),
            created_by="tester",
        )
        legacy_authoring_graph = RuntimePipelineAuthoringGraph(
            nodes=[
                RuntimeAuthoringNodeSpec(
                    node_key=node.node_key,
                    node_type=node.node_type,
                    config=node.config,
                    logical_model_ref=node.logical_model_ref,
                    position=node.position,
                )
                for node in graph.nodes
            ],
            edges=[
                edge
                for edge in graph.edges
                if not (
                    edge.from_node_key == "measurement_eligibility"
                    and edge.to_node_key == "measure_curvature"
                    and edge.to_port == "eligibility"
                )
            ],
        )
        migrated_version = create_pipeline_version(
            session,
            instance.pipeline_definition_id,
            RuntimePipelineVersionCreateRequest(
                mode=RuntimeMode.REALTIME,
                authoring_graph=legacy_authoring_graph,
                change_note="Normalize early built-in template graph",
            ),
            created_by="tester",
        )

    nodes = {node.node_key: node for node in graph.nodes}
    assert [nodes[key].node_type for key in (
        "channel_input",
        "undistort",
        "canonical_view",
        "segment_stem",
        "active_stem",
        "measurement_eligibility",
        "stem_geometry",
        "measure_curvature",
        "track_surface_phase",
        "solve_orientation",
        "result_output",
        "diagnostic_overlay",
        "display_model_view",
    )] == [
        "source.channel_frame",
        "preprocessing.camera_undistort",
        "preprocessing.process_coordinate_rectify",
        "inference.segmentation",
        "analysis.active_stem_tracker",
        "analysis.stem_measurement_eligibility",
        "analysis.stem_mask_geometry",
        "analysis.stem_horizontal_curvature",
        "analysis.stem_surface_phase",
        "analysis.stem_curvature_phase_solver",
        "output.result",
        "output.overlay",
        "output.display_output",
    ]
    assert all(node.node_type != "output.siemens_sps_write" for node in graph.nodes)
    assert nodes["canonical_view"].config["view_profile_id"] == (
        "stem-measurement-view-v1"
    )
    assert nodes["measurement_eligibility"].config["view_profile_id"] == (
        "stem-measurement-view-v1"
    )
    assert nodes["segment_stem"].config["require_image_view_compatibility"] is True
    assert nodes["segment_stem"].logical_model_ref is not None
    assert nodes["segment_stem"].logical_model_ref.required_task_type == (
        TaskType.SEGMENTATION
    )
    assert nodes["track_surface_phase"].config["phase_estimator_version"] == (
        "CYLINDRICAL_SURFACE_V2"
    )
    assert nodes["track_surface_phase"].config["execution_mode"] == "HIGH_RATE_SEEDED"
    assert nodes["track_surface_phase"].config["target_phase_fps"] == 25.0
    assert nodes["measure_curvature"].config["minimum_observation_interval_ms"] == 100
    assert nodes["solve_orientation"].config["shadow_mode_enabled"] is True
    assert nodes["solve_orientation"].config["shadow_evaluation_approved"] is False
    projections = nodes["result_output"].config["projections"]
    assert {item["key"] for item in projections}.issuperset(
        {
            "algorithm",
            "decision",
            "signed_correction_deg",
            "curvature_span_mm",
            "target_phase_deg",
            "current_phase_deg",
            "overall_confidence",
            "reason_codes",
            "diagnostic_graph",
            "result_expiry",
        }
    )
    assert all("plc" not in item["destinations"] for item in projections)
    assert {
        (edge.from_node_key, edge.from_port, edge.to_node_key, edge.to_port)
        for edge in graph.edges
    }.issuperset(
        {
            ("undistort", "frame", "canonical_view", "frame"),
            ("canonical_view", "frame", "segment_stem", "frame"),
            ("active_stem", "segments", "measure_curvature", "segments"),
            (
                "measurement_eligibility",
                "result",
                "measure_curvature",
                "eligibility",
            ),
            ("stem_geometry", "stem_geometry", "track_surface_phase", "stem_geometry"),
            ("measure_curvature", "curvature", "solve_orientation", "curvature"),
            (
                "track_surface_phase",
                "surface_phase",
                "solve_orientation",
                "surface_phase",
            ),
            ("solve_orientation", "solution", "result_output", "result"),
        }
    )
    assert {
        "port_type_incompatible",
        "missing_required_input",
        "cycle_detected",
        "projection_source_field_not_produced",
        "node_output_unused",
    }.isdisjoint(validation_codes)
    assert instance.source_template_definition_id == template.pipeline_definition_id
    assert instance.latest_version is not None
    assert instance.latest_version.graph == graph
    assert (
        "measurement_eligibility",
        "result",
        "measure_curvature",
        "eligibility",
    ) in {
        (edge.from_node_key, edge.from_port, edge.to_node_key, edge.to_port)
        for edge in migrated_version.graph.edges
    }
    assert {
        "projection_source_field_not_produced",
        "node_output_unused",
    }.isdisjoint(
        {issue.code for issue in migrated_version.validation_report.issues}
    )


def test_stem_geometry_file_test_pipeline_is_validatable(sqlite_engine, tmp_path) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(
            session,
            release_id="rel_stem_segmentation",
            model_family_id="mf_stem_segmentation",
            model_family_slug="stem-segmentation",
            model_family_display_name="Stem Segmentation",
            model_version_id="mvr_stem_segmentation",
            task_type=TaskType.SEGMENTATION,
        )
        report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                authoring_graph=RuntimePipelineAuthoringGraph(
                    nodes=[
                        RuntimeAuthoringNodeSpec(
                            node_key="source",
                            node_type="source.file_test",
                            config={"path": "development-test-frame.png"},
                        ),
                        RuntimeAuthoringNodeSpec(
                            node_key="segment",
                            node_type="inference.segmentation",
                            logical_model_ref=RuntimeLogicalModelRef(
                                model_family_slug="stem-segmentation",
                                target_path=TargetPath.REALTIME,
                                required_task_type=TaskType.SEGMENTATION,
                                selection_policy="explicit_release",
                                deployment_release_id="rel_stem_segmentation",
                            ),
                        ),
                        RuntimeAuthoringNodeSpec(
                            node_key="geometry",
                            node_type="analysis.stem_mask_geometry",
                        ),
                        RuntimeAuthoringNodeSpec(
                            node_key="overlay",
                            node_type="output.overlay",
                            config={
                                "layers": ["lines", "polygons", "measurements"],
                            },
                        ),
                    ],
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
                ),
            ),
        )

    assert report.status == "valid", [issue.model_dump(mode="json") for issue in report.issues]
    assert report.error_count == 0
    assert "channel_source_recommended" in {issue.code for issue in report.issues}


def test_create_pipeline_version_accepts_minimal_authoring_graph(
    sqlite_engine,
    tmp_path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        definition = create_pipeline_definition(
            session,
            RuntimePipelineDefinitionCreateRequest(name="Minimal authoring pipeline"),
            created_by="tester",
        )
        version = create_pipeline_version(
            session,
            definition.pipeline_definition_id,
            RuntimePipelineVersionCreateRequest(
                mode=RuntimeMode.REALTIME,
                authoring_graph=RuntimePipelineAuthoringGraph(
                    nodes=[
                        RuntimeAuthoringNodeSpec(
                            node_key="channel_input",
                            node_type="source.channel_frame",
                        ),
                        RuntimeAuthoringNodeSpec(
                            node_key="result_output",
                            node_type="output.result",
                        ),
                    ],
                    edges=[
                        RuntimeGraphEdge(
                            from_node_key="channel_input",
                            from_port="frame",
                            to_node_key="result_output",
                            to_port="result",
                        )
                    ],
                ),
            ),
            created_by="tester",
        )

    graph = version.graph
    nodes = {node.node_key: node for node in graph.nodes}
    assert nodes["channel_input"].node_category == RuntimeNodeCategory.SOURCE
    assert nodes["channel_input"].output_ports[0].key == "frame"
    assert nodes["result_output"].node_category == RuntimeNodeCategory.OUTPUT
    assert nodes["result_output"].input_ports[0].key == "result"
    assert nodes["result_output"].config["projections"] == []


def test_validation_detects_missing_model_refs_and_bad_task_output_combo(
    sqlite_engine,
    tmp_path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                graph=_classification_to_detection_size_graph(),
            ),
        )

    codes = {issue.code for issue in report.issues}
    assert report.status == "invalid"
    assert "model_reference_missing" in codes
    assert "port_type_incompatible" in codes


def test_validation_rejects_invalid_spatial_rule_table(
    sqlite_engine,
    tmp_path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(session)
        report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                graph=RuntimePipelineGraph(
                    nodes=[
                        _runtime_node("channel_input", "source.channel_frame"),
                        _runtime_node("detect_objects", "inference.detection"),
                        _runtime_node(
                            "spatial_signal",
                            "analysis.object_spatial_signal",
                            config={
                                "rules": [
                                    {
                                        "relation": "distance",
                                        "max_distance_percent": -1,
                                    }
                                ]
                            },
                        ),
                        _runtime_node("persist", "output.persisted_result"),
                    ],
                    edges=[
                        RuntimeGraphEdge(
                            from_node_key="channel_input",
                            from_port="frame",
                            to_node_key="detect_objects",
                            to_port="frame",
                        ),
                        RuntimeGraphEdge(
                            from_node_key="detect_objects",
                            from_port="detections",
                            to_node_key="spatial_signal",
                            to_port="primary_objects",
                        ),
                        RuntimeGraphEdge(
                            from_node_key="spatial_signal",
                            from_port="signal",
                            to_node_key="persist",
                            to_port="result",
                        ),
                    ],
                ),
            ),
        )

    assert "spatial_rule_table_invalid" in {issue.code for issue in report.issues}


def test_validation_rejects_invalid_color_orientation_configuration(
    sqlite_engine,
    tmp_path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                graph=RuntimePipelineGraph(
                    nodes=[
                        _runtime_node("channel_input", "source.channel_frame"),
                        _runtime_node(
                            "color_objects",
                            "computer_vision.color_object_segmentation",
                            config={
                                "profiles": [
                                    {
                                        "label": "board",
                                        "enabled": True,
                                        "color_space": "hsv",
                                        "hue_min": -1,
                                        "hue_max": 179,
                                        "saturation_min": 0,
                                        "saturation_max": 255,
                                        "value_min": 0,
                                        "value_max": 255,
                                    }
                                ],
                                "zones": [
                                    {
                                        "label": "lane",
                                        "x": 0.1,
                                        "y": 0.1,
                                        "width": 0.4,
                                        "height": 0.3,
                                        "angle_degrees": "not-a-number",
                                    }
                                ],
                            },
                        ),
                        _runtime_node(
                            "orientation_signal",
                            "analysis.object_orientation_signal",
                            config={
                                "reference_direction": {
                                    "start": {"x": 0.5, "y": 0.5},
                                    "end": {"x": 0.5, "y": 0.5},
                                }
                            },
                        ),
                        _runtime_node("persist", "output.persisted_result"),
                    ],
                    edges=[
                        RuntimeGraphEdge(
                            from_node_key="channel_input",
                            from_port="frame",
                            to_node_key="color_objects",
                            to_port="frame",
                        ),
                        RuntimeGraphEdge(
                            from_node_key="color_objects",
                            from_port="objects",
                            to_node_key="orientation_signal",
                            to_port="objects",
                        ),
                        RuntimeGraphEdge(
                            from_node_key="orientation_signal",
                            from_port="signal",
                            to_node_key="persist",
                            to_port="result",
                        ),
                    ],
                ),
            ),
        )

    codes = {issue.code for issue in report.issues}
    assert "color_profile_table_invalid" in codes
    assert "zone_config_invalid" in codes
    assert "direction_reference_invalid" in codes


def test_validation_accepts_color_region_set_as_result_output(
    sqlite_engine,
    tmp_path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                graph=RuntimePipelineGraph(
                    nodes=[
                        _runtime_node("channel_input", "source.channel_frame"),
                        _runtime_node(
                            "color_objects",
                            "computer_vision.color_object_segmentation",
                            config={
                                "profiles": [
                                    {
                                        "label": "red_part",
                                        "enabled": True,
                                        "color_space": "hsv",
                                        "hue_min": 170,
                                        "hue_max": 10,
                                        "saturation_min": 140,
                                        "saturation_max": 255,
                                        "value_min": 120,
                                        "value_max": 255,
                                        "min_area_percent": 0.1,
                                        "min_aspect_ratio": 1,
                                    }
                                ]
                            },
                        ),
                        _runtime_node(
                            "result_output",
                            "output.result",
                            config={
                                "result_fields": [
                                    {
                                        "source_field": "count_total",
                                        "response_key": "count_total",
                                        "required": False,
                                    }
                                ]
                            },
                        ),
                    ],
                    edges=[
                        RuntimeGraphEdge(
                            from_node_key="channel_input",
                            from_port="frame",
                            to_node_key="color_objects",
                            to_port="frame",
                        ),
                        RuntimeGraphEdge(
                            from_node_key="color_objects",
                            from_port="objects",
                            to_node_key="result_output",
                            to_port="result",
                        ),
                    ],
                ),
            ),
        )

    codes = {issue.code for issue in report.issues}
    assert "port_type_incompatible" not in codes
    assert report.status == "valid", [issue.model_dump(mode="json") for issue in report.issues]


def test_validation_requires_display_output_for_ambiguous_visual_branches(
    sqlite_engine,
    tmp_path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                graph=RuntimePipelineGraph(
                    nodes=[
                        RuntimeNodeSpec(
                            node_key="channel_input",
                            node_type="source.channel_frame",
                            node_category=RuntimeNodeCategory.SOURCE,
                        ),
                        RuntimeNodeSpec(
                            node_key="crop_a",
                            node_type="preprocessing.crop",
                            node_category=RuntimeNodeCategory.PREPROCESSING,
                        ),
                        RuntimeNodeSpec(
                            node_key="resize_b",
                            node_type="preprocessing.resize",
                            node_category=RuntimeNodeCategory.PREPROCESSING,
                        ),
                        RuntimeNodeSpec(
                            node_key="persist_a",
                            node_type="output.persisted_result",
                            node_category=RuntimeNodeCategory.OUTPUT,
                        ),
                        RuntimeNodeSpec(
                            node_key="event_b",
                            node_type="output.internal_event",
                            node_category=RuntimeNodeCategory.OUTPUT,
                        ),
                    ],
                    edges=[
                        RuntimeGraphEdge(
                            from_node_key="channel_input",
                            from_port="frame",
                            to_node_key="crop_a",
                            to_port="frame",
                        ),
                        RuntimeGraphEdge(
                            from_node_key="crop_a",
                            from_port="frame",
                            to_node_key="persist_a",
                            to_port="result",
                        ),
                        RuntimeGraphEdge(
                            from_node_key="channel_input",
                            from_port="frame",
                            to_node_key="resize_b",
                            to_port="frame",
                        ),
                        RuntimeGraphEdge(
                            from_node_key="resize_b",
                            from_port="frame",
                            to_node_key="event_b",
                            to_port="result",
                        ),
                    ],
                ),
            ),
        )

    assert report.status == "invalid"
    assert "display_output_ambiguous" in {issue.code for issue in report.issues}


def test_validation_accepts_explicit_display_output_for_visual_branch_selection(
    sqlite_engine,
    tmp_path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                graph=_branched_display_graph(),
            ),
        )

    assert report.status == "valid"
    assert "display_output_ambiguous" not in {issue.code for issue in report.issues}


def test_validation_accepts_http_trigger_channel_frame_response_display_overlay(
    sqlite_engine,
    tmp_path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(
            session,
            release_id="rel_classifier",
            model_family_id="mf_quality_classifier",
            model_family_slug="quality-classifier",
            model_family_display_name="Quality Classifier",
            model_version_id="mvr_classifier",
            task_type=TaskType.CLASSIFICATION,
        )
        report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                graph=_trigger_classification_response_display_graph(),
            ),
        )

    assert report.status == "valid"
    assert "channel_binding_missing" not in {issue.code for issue in report.issues}
    assert "node_mode_incompatible" not in {issue.code for issue in report.issues}


def test_validation_accepts_result_output_as_trigger_response(
    sqlite_engine,
    tmp_path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(
            session,
            release_id="rel_classifier",
            model_family_id="mf_quality_classifier",
            model_family_slug="quality-classifier",
            model_family_display_name="Quality Classifier",
            model_version_id="mvr_classifier",
            task_type=TaskType.CLASSIFICATION,
        )
        report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.TRIGGER,
                graph=_trigger_classification_result_output_graph(),
            ),
        )

    assert report.status == "valid"
    codes = {issue.code for issue in report.issues}
    assert "trigger_response_missing" not in codes
    assert "response_field_not_produced" not in codes


@pytest.mark.parametrize(
    ("mode", "metadata", "require_key", "expected_issue"),
    [
        ("result_only", [], False, None),
        ("result_with_metadata", ["correlation_id", "duration_ms"], True, None),
        ("full", [], False, None),
        ("unknown", [], False, "edge_http_response_invalid"),
        ("result_with_metadata", ["secret"], False, "property_option_invalid"),
        ("result_with_metadata", ["status", "status"], False, "property_option_invalid"),
        ("result_with_metadata", {"status": True}, False, "property_option_invalid"),
        ("result_with_metadata", [{"status": True}], False, "property_option_invalid"),
        ("result_only", [], "false", "property_boolean_invalid"),
    ],
)
def test_edge_trigger_node_settings_validate_through_existing_pipeline_path(
    sqlite_engine, tmp_path, mode, metadata, require_key, expected_issue
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)
    graph = _trigger_classification_result_output_graph()
    for node in graph.nodes:
        if node.node_type == "output.result":
            node.config.update(edge_response_mode=mode, edge_response_metadata_fields=metadata)
        if node.node_type == "trigger.http_put":
            node.config["require_idempotency_key"] = require_key
    with session_scope(settings) as session:
        seed_deployed_runtime_model(
            session,
            release_id="rel_classifier",
            model_family_id="mf_quality_classifier",
            model_family_slug="quality-classifier",
            model_family_display_name="Quality Classifier",
            model_version_id="mvr_classifier",
            task_type=TaskType.CLASSIFICATION,
        )
        report = validate_pipeline_request(
            session, RuntimePipelineValidationRequest(mode=RuntimeMode.TRIGGER, graph=graph)
        )
    if expected_issue is None:
        assert report.status == "valid", report.issues
    else:
        assert report.status == "invalid"
        assert expected_issue in {issue.code for issue in report.issues}


def test_validation_rejects_active_production_model_ref_when_only_staged(
    sqlite_engine,
    tmp_path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(session, active_release=False, staged=True)
        report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                graph=_valid_detection_graph("result"),
            ),
        )

    codes = {issue.code for issue in report.issues}
    assert report.status == "invalid"
    assert "model_selection_policy_unavailable" in codes
    assert any("active production" in issue.message for issue in report.issues)


def test_validation_accepts_board_measurement_workflow(sqlite_engine, tmp_path) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(
            session,
            release_id="rel_detection",
            model_family_id="mf_defect_detector",
            model_family_slug="defect-detector",
            model_family_display_name="Defect Detector",
            model_version_id="mvr_detection",
            task_type=TaskType.DETECTION,
        )
        report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.TRIGGER,
                graph=_board_measurement_workflow_graph(),
            ),
        )

    assert report.status == "valid"


def test_validation_rejects_board_measurement_publish_blockers(sqlite_engine, tmp_path) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)
    graph = _board_measurement_workflow_graph(
        measurement_bases=["bbox_height"],
        clearance_basis="bbox_width",
        mm_per_pixel=0,
    )

    with session_scope(settings) as session:
        seed_deployed_runtime_model(
            session,
            release_id="rel_detection",
            model_family_id="mf_defect_detector",
            model_family_slug="defect-detector",
            model_family_display_name="Defect Detector",
            model_version_id="mvr_detection",
            task_type=TaskType.DETECTION,
        )
        report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(mode=RuntimeMode.TRIGGER, graph=graph),
        )

    codes = {issue.code for issue in report.issues}
    assert report.status == "invalid"
    assert "clearance_measurement_basis_unavailable" in codes
    assert "metric_calibration_invalid" in codes


def test_validation_accepts_multi_branch_cv_and_feature_detection_workflow(
    sqlite_engine, tmp_path
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(
            session,
            release_id="rel_detection",
            model_family_id="mf_defect_detector",
            model_family_slug="defect-detector",
            model_family_display_name="Defect Detector",
            model_version_id="mvr_detection",
            task_type=TaskType.DETECTION,
        )
        report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                graph=_cv_feature_multi_branch_workflow_graph(),
            ),
        )

    assert report.status == "valid"
    codes = {issue.code for issue in report.issues}
    assert "missing_required_input" not in codes
    assert "input_port_multiple_sources" not in codes


def test_validation_rejects_missing_fan_in_input_for_cv_board_workflow(
    sqlite_engine, tmp_path
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)
    graph = _cv_feature_multi_branch_workflow_graph(include_coordinate_to_clearance=False)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(
            session,
            release_id="rel_detection",
            model_family_id="mf_defect_detector",
            model_family_slug="defect-detector",
            model_family_display_name="Defect Detector",
            model_version_id="mvr_detection",
            task_type=TaskType.DETECTION,
        )
        report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(mode=RuntimeMode.REALTIME, graph=graph),
        )

    codes = {issue.code for issue in report.issues}
    assert report.status == "invalid"
    assert "missing_required_input" in codes


def test_validation_accepts_interval_trigger_channel_frame_pipeline(
    sqlite_engine,
    tmp_path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        report = validate_pipeline_request(
            session,
            RuntimePipelineValidationRequest(
                mode=RuntimeMode.REALTIME,
                graph=RuntimePipelineGraph(
                    nodes=[
                        RuntimeNodeSpec(
                            node_key="interval",
                            node_type="trigger.interval",
                            node_category=RuntimeNodeCategory.TRIGGER,
                            config={"interval_ms": 1000, "enabled": True},
                        ),
                        RuntimeNodeSpec(
                            node_key="channel_input",
                            node_type="source.channel_frame",
                            node_category=RuntimeNodeCategory.SOURCE,
                        ),
                        RuntimeNodeSpec(
                            node_key="persist",
                            node_type="output.persisted_result",
                            node_category=RuntimeNodeCategory.OUTPUT,
                        ),
                    ],
                    edges=[
                        RuntimeGraphEdge(
                            from_node_key="interval",
                            from_port="trigger",
                            to_node_key="channel_input",
                            to_port="trigger",
                        ),
                        RuntimeGraphEdge(
                            from_node_key="channel_input",
                            from_port="frame",
                            to_node_key="persist",
                            to_port="result",
                        ),
                    ],
                ),
            ),
        )

    assert report.status == "valid"
    assert "interval_trigger_not_connected" not in {issue.code for issue in report.issues}


def test_node_model_docs_track_display_and_context_contracts() -> None:
    text = Path("docs/runtime/pipeline-node-plugin-model.md").read_text(encoding="utf-8")

    assert "output.display_output" in text
    assert "Image Context" in text
    assert "Object Reference" in text
    assert "ROI Fan-Out" in text


def test_pipeline_versions_publish_and_supersede_previous_version(sqlite_engine, tmp_path) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(session)
        definition = create_pipeline_definition(
            session,
            RuntimePipelineDefinitionCreateRequest(
                name="Press defect detection",
                description="Realtime detector pipeline",
            ),
            created_by="tester",
        )
        first_version = create_pipeline_version(
            session,
            definition.pipeline_definition_id,
            RuntimePipelineVersionCreateRequest(
                mode=RuntimeMode.REALTIME,
                graph=_valid_detection_graph("persisted_result"),
                change_note="Initial version",
            ),
            created_by="tester",
        )
        published_first = publish_pipeline_version(
            session,
            first_version.pipeline_version_id,
            published_by="tester",
        )

        second_version = create_pipeline_version(
            session,
            definition.pipeline_definition_id,
            RuntimePipelineVersionCreateRequest(
                mode=RuntimeMode.REALTIME,
                graph=_valid_detection_graph("internal_event"),
                change_note="Switch output connector",
            ),
            created_by="tester",
        )
        published_second = publish_pipeline_version(
            session,
            second_version.pipeline_version_id,
            published_by="tester",
        )

    assert published_first.status == "published"
    assert published_second.status == "published"
    with session_scope(settings) as session:
        first_row = session.get(PipelineVersion, published_first.pipeline_version_id)
        assert first_row is not None
        assert first_row.status == "superseded"


def test_template_publish_does_not_rebind_channel_but_instance_publish_restarts_owner(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(session)
        definition = create_pipeline_definition(
            session,
            RuntimePipelineDefinitionCreateRequest(
                name="Channel-bound detector",
                description="Realtime channel pipeline",
            ),
            created_by="tester",
        )
        first_version = create_pipeline_version(
            session,
            definition.pipeline_definition_id,
            RuntimePipelineVersionCreateRequest(
                mode=RuntimeMode.REALTIME,
                graph=_valid_detection_graph("persisted_result"),
            ),
            created_by="tester",
        )
        published_first = publish_pipeline_version(
            session,
            first_version.pipeline_version_id,
            published_by="tester",
        )
        channel = create_runtime_channel(
            session,
            RuntimeChannelCreateRequest(
                name="Publish camera",
                technical_place="press-line-1",
                source_config={"url": "rtsp://camera.local/live"},
                pipeline_version_id=published_first.pipeline_version_id,
            ),
            created_by="tester",
        )
        channel_row = session.get(RuntimeChannel, channel.channel_id)
        assert channel_row is not None
        channel_instance_version_id = channel_row.pipeline_version_id
        assert channel_instance_version_id is not None
        assert channel_instance_version_id != published_first.pipeline_version_id
        channel_instance_version = session.get(PipelineVersion, channel_instance_version_id)
        assert channel_instance_version is not None
        channel_instance_definition_id = channel_instance_version.pipeline_definition_id
        channel_row.desired_state = "running"
        channel_row.observed_state = "running"
        channel_row.health_state = "ok"

        second_version = create_pipeline_version(
            session,
            definition.pipeline_definition_id,
            RuntimePipelineVersionCreateRequest(
                mode=RuntimeMode.REALTIME,
                graph=_valid_detection_graph("internal_event"),
            ),
            created_by="tester",
        )
        published_second = publish_pipeline_version(
            session,
            second_version.pipeline_version_id,
            published_by="tester",
        )

        unchanged_channel = session.get(RuntimeChannel, channel.channel_id)
        assert unchanged_channel is not None
        assert unchanged_channel.pipeline_version_id == channel_instance_version_id
        assert unchanged_channel.assigned_pipeline == "Publish camera Pipeline"
        assert published_second.pipeline_version_id != unchanged_channel.pipeline_version_id
        assert session.scalar(
            select(RuntimeControlCommandRecord).where(
                RuntimeControlCommandRecord.channel_id == channel.channel_id,
                RuntimeControlCommandRecord.command == "restart",
            )
        ) is None

        instance_second_version = create_pipeline_version(
            session,
            channel_instance_definition_id,
            RuntimePipelineVersionCreateRequest(
                mode=RuntimeMode.REALTIME,
                graph=_valid_detection_graph("internal_event"),
            ),
            created_by="tester",
        )
        published_instance_second = publish_pipeline_version(
            session,
            instance_second_version.pipeline_version_id,
            published_by="tester",
        )

        rebound_channel = session.get(RuntimeChannel, channel.channel_id)
        assert rebound_channel is not None
        assert rebound_channel.pipeline_version_id == published_instance_second.pipeline_version_id
        assert rebound_channel.assigned_pipeline == "Publish camera Pipeline"
        assert rebound_channel.desired_state == "start_requested"
        assert rebound_channel.observed_state == "starting"
        command = session.scalar(
            select(RuntimeControlCommandRecord).where(
                RuntimeControlCommandRecord.channel_id == channel.channel_id,
                RuntimeControlCommandRecord.command == "restart",
            )
        )
        assert command is not None
        assert command.status == "queued"


def test_pipeline_definition_list_filters_channel_instances_by_owner(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(session)
        template = create_pipeline_definition(
            session,
            RuntimePipelineDefinitionCreateRequest(
                name="Reusable channel basis",
                description="Template visible outside channels",
            ),
            created_by="tester",
        )
        version = create_pipeline_version(
            session,
            template.pipeline_definition_id,
            RuntimePipelineVersionCreateRequest(
                mode=RuntimeMode.REALTIME,
                graph=_valid_detection_graph("persisted_result"),
            ),
            created_by="tester",
        )
        published = publish_pipeline_version(
            session,
            version.pipeline_version_id,
            published_by="tester",
        )
        first_channel = create_runtime_channel(
            session,
            RuntimeChannelCreateRequest(
                name="Owner one camera",
                technical_place="owner-one",
                source_config={"url": "rtsp://camera.local/one"},
                pipeline_version_id=published.pipeline_version_id,
            ),
            created_by="tester",
        )
        second_channel = create_runtime_channel(
            session,
            RuntimeChannelCreateRequest(
                name="Owner two camera",
                technical_place="owner-two",
                source_config={"url": "rtsp://camera.local/two"},
                pipeline_version_id=published.pipeline_version_id,
            ),
            created_by="tester",
        )

        first_instances = list_pipeline_definitions(
            session,
            include_templates=False,
            scope="channel_instance",
            owner_channel_id=first_channel.channel_id,
        )
        second_instances = list_pipeline_definitions(
            session,
            include_templates=False,
            scope="channel_instance",
            owner_channel_id=second_channel.channel_id,
        )
        template_definitions = list_pipeline_definitions(
            session,
            include_templates=True,
            scope="template",
        )

    assert [item.owner_channel_id for item in first_instances] == [first_channel.channel_id]
    assert [item.owner_channel_id for item in second_instances] == [second_channel.channel_id]
    assert first_instances[0].pipeline_definition_id != second_instances[0].pipeline_definition_id
    assert template.pipeline_definition_id in {
        item.pipeline_definition_id for item in template_definitions
    }
    assert first_instances[0].pipeline_definition_id not in {
        item.pipeline_definition_id for item in template_definitions
    }


def test_channel_start_requires_published_channel_instance(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(session)
        definition = create_pipeline_definition(
            session,
            RuntimePipelineDefinitionCreateRequest(
                name="Draft channel detector",
                description="Not published yet",
            ),
            created_by="tester",
        )
        draft_version = create_pipeline_version(
            session,
            definition.pipeline_definition_id,
            RuntimePipelineVersionCreateRequest(
                mode=RuntimeMode.REALTIME,
                graph=_valid_detection_graph("persisted_result"),
            ),
            created_by="tester",
        )
        channel = create_runtime_channel(
            session,
            RuntimeChannelCreateRequest(
                name="Draft camera",
                technical_place="press-line-2",
                source_config={"url": "rtsp://camera.local/live"},
                pipeline_version_id=draft_version.pipeline_version_id,
            ),
            created_by="tester",
        )

        with pytest.raises(ValueError, match="must be published"):
            request_runtime_channel_start(session, channel.channel_id, requested_by="tester")


@pytest.mark.parametrize("execution_target", ["server", "edge_device"])
@pytest.mark.parametrize("running", [False, True])
def test_publishing_http_trigger_in_assigned_pipeline_rebinds_channel(
    sqlite_engine: Engine,
    tmp_path: Path,
    execution_target: str,
    running: bool,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(session)
        placement = {"target": execution_target}
        if execution_target == "edge_device":
            device = create_edge_device(
                session,
                EdgeDeviceRegistrationRequest(
                    name="Trigger switch Jetson",
                    address={"management_ip": "192.168.50.21"},
                    allowed_cidr="192.168.50.0/24",
                    server_ip="192.168.50.10",
                ),
                created_by="tester",
            )
            placement["edge_device_id"] = device.edge_device_id
        template = create_pipeline_definition(
            session,
            RuntimePipelineDefinitionCreateRequest(name="Trigger switch basis"),
            created_by="tester",
        )
        original_graph = _valid_detection_graph("persisted_result")
        template_version = create_pipeline_version(
            session,
            template.pipeline_definition_id,
            RuntimePipelineVersionCreateRequest(mode=RuntimeMode.REALTIME, graph=original_graph),
            created_by="tester",
        )
        publish_pipeline_version(session, template_version.pipeline_version_id, published_by="tester")
        channel = create_runtime_channel(
            session,
            RuntimeChannelCreateRequest(
                name="Trigger switch camera",
                technical_place="trigger-switch",
                source_config={"url": "rtsp://camera.local/live"},
                pipeline_version_id=template_version.pipeline_version_id,
                placement=placement,
            ),
            created_by="tester",
        )
        channel_row = session.get(RuntimeChannel, channel.channel_id)
        assert channel_row is not None
        original_version = session.get(PipelineVersion, channel_row.pipeline_version_id)
        assert original_version is not None
        instance_id = original_version.pipeline_definition_id
        if running:
            channel_row.desired_state = "running"
            channel_row.observed_state = "running"

        trigger_version = create_pipeline_version(
            session,
            instance_id,
            RuntimePipelineVersionCreateRequest(
                mode=RuntimeMode.REALTIME,
                graph=_trigger_detection_projection_graph(),
            ),
            created_by="tester",
        )
        assert trigger_version.mode == RuntimeMode.TRIGGER
        assert channel_row.pipeline_version_id == original_version.id
        assert original_version.status == "published"

        published = publish_pipeline_version(
            session, trigger_version.pipeline_version_id, published_by="tester"
        )
        assert published.status == "published"
        assert original_version.status == "superseded"
        assert channel_row.pipeline_version_id == published.pipeline_version_id
        assert channel_row.mode == "realtime"
        assert channel_row.execution_target == execution_target
        endpoint = session.scalar(
            select(TriggerEndpoint).where(TriggerEndpoint.channel_id == channel.channel_id)
        )
        assert endpoint is not None
        assert endpoint.pipeline_version_id == published.pipeline_version_id
        assert endpoint.enabled
        assert session.query(RuntimeControlCommandRecord).filter_by(
            channel_id=channel.channel_id, command="restart"
        ).count() == int(running)
        loaded = validate_runtime_channel_start_requirements(session, channel_id=channel.channel_id)
        assert loaded.version.id == published.pipeline_version_id
        assert loaded.version.mode == "trigger"

        if not running:
            # Reproduce a persisted assignment left behind by the old publisher.
            channel_row.pipeline_version_id = original_version.id
            session.flush()
            publish_pipeline_version(session, published.pipeline_version_id, published_by="tester")
            assert channel_row.pipeline_version_id == published.pipeline_version_id
            _, command = request_runtime_channel_start(
                session, channel.channel_id, requested_by="tester"
            )
            assert command.status == "queued"

        restored = create_pipeline_version(
            session,
            instance_id,
            RuntimePipelineVersionCreateRequest(mode=RuntimeMode.REALTIME, graph=original_graph),
            created_by="tester",
        )
        publish_pipeline_version(session, restored.pipeline_version_id, published_by="tester")
        assert channel_row.pipeline_version_id == restored.pipeline_version_id
        assert not endpoint.enabled
        loaded = validate_runtime_channel_start_requirements(session, channel_id=channel.channel_id)
        assert loaded.version.id == restored.pipeline_version_id
        assert loaded.version.mode == "realtime"


def test_publishing_trigger_channel_instance_does_not_restart_live_channel(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(session)
        live_definition = create_pipeline_definition(
            session,
            RuntimePipelineDefinitionCreateRequest(name="Live detector"),
            created_by="tester",
        )
        live_version = create_pipeline_version(
            session,
            live_definition.pipeline_definition_id,
            RuntimePipelineVersionCreateRequest(
                mode=RuntimeMode.REALTIME,
                graph=_valid_detection_graph("persisted_result"),
            ),
            created_by="tester",
        )
        published_live = publish_pipeline_version(
            session,
            live_version.pipeline_version_id,
            published_by="tester",
        )
        channel = create_runtime_channel(
            session,
            RuntimeChannelCreateRequest(
                name="Basler trigger switch camera",
                technical_place="press-line-basler",
                source_type=RuntimeSourceType.BASLER_PYLON,
                source_config={"serial_number": "41949247"},
                pipeline_version_id=published_live.pipeline_version_id,
            ),
            created_by="tester",
        )
        channel_row = session.get(RuntimeChannel, channel.channel_id)
        assert channel_row is not None
        live_channel_pipeline_id = channel_row.pipeline_version_id
        channel_row.desired_state = "running"
        channel_row.observed_state = "running"
        channel_row.health_state = "ok"

        trigger_definition = create_pipeline_definition(
            session,
            RuntimePipelineDefinitionCreateRequest(name="Basler request trigger"),
            created_by="tester",
        )
        trigger_definition_row = session.get(
            PipelineDefinition,
            trigger_definition.pipeline_definition_id,
        )
        assert trigger_definition_row is not None
        trigger_definition_row.scope = "channel_instance"
        trigger_definition_row.owner_channel_id = channel.channel_id
        trigger_version = create_pipeline_version(
            session,
            trigger_definition.pipeline_definition_id,
            RuntimePipelineVersionCreateRequest(
                mode=RuntimeMode.TRIGGER,
                graph=_trigger_detection_response_graph(),
            ),
            created_by="tester",
        )

        published_trigger = publish_pipeline_version(
            session,
            trigger_version.pipeline_version_id,
            published_by="tester",
        )

        channel_row = session.get(RuntimeChannel, channel.channel_id)
        assert channel_row is not None
        assert channel_row.pipeline_version_id == live_channel_pipeline_id
        assert (
            session.query(RuntimeControlCommandRecord)
            .filter_by(channel_id=channel.channel_id, command="restart")
            .count()
            == 0
        )
        endpoint = session.scalar(
            select(TriggerEndpoint).where(TriggerEndpoint.channel_id == channel.channel_id)
        )
        assert endpoint is not None
        assert endpoint.pipeline_version_id == published_trigger.pipeline_version_id


def test_published_trigger_endpoint_uses_result_projection_response_policy(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        seed_deployed_runtime_model(session)
        channel = create_runtime_channel(
            session,
            RuntimeChannelCreateRequest(
                name="Projection trigger camera",
                technical_place="projection-trigger",
                source_config={"url": "rtsp://camera.local/projection"},
            ),
            created_by="tester",
        )
        definition = create_pipeline_definition(
            session,
            RuntimePipelineDefinitionCreateRequest(name="Projection request trigger"),
            created_by="tester",
        )
        definition_row = session.get(PipelineDefinition, definition.pipeline_definition_id)
        assert definition_row is not None
        definition_row.scope = "channel_instance"
        definition_row.owner_channel_id = channel.channel_id
        version = create_pipeline_version(
            session,
            definition.pipeline_definition_id,
            RuntimePipelineVersionCreateRequest(
                mode=RuntimeMode.TRIGGER,
                graph=_trigger_detection_projection_graph(),
            ),
            created_by="tester",
        )

        published = publish_pipeline_version(
            session,
            version.pipeline_version_id,
            published_by="tester",
        )

        endpoint = session.scalar(
            select(TriggerEndpoint).where(TriggerEndpoint.channel_id == channel.channel_id)
        )

    assert endpoint is not None
    assert endpoint.pipeline_version_id == published.pipeline_version_id
    assert endpoint.response_policy["mode"] == "configured_fields"
    assert endpoint.response_policy["fields"] == [
        {
            "source_field": "piece_count",
            "response_key": "piece_count",
            "required": False,
            "default_value": None,
        }
    ]


def test_archiving_unused_pipeline_definition_hides_it_from_lists(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        definition = create_pipeline_definition(
            session,
            RuntimePipelineDefinitionCreateRequest(name="Temporary runtime pipeline"),
            created_by="tester",
        )
        archived = archive_pipeline_definition(
            session,
            definition.pipeline_definition_id,
            archived_by="tester",
            reason="No longer needed",
        )
        listed_ids = {
            item.pipeline_definition_id
            for item in list_pipeline_definitions(session, include_templates=False)
        }

    assert archived.status == "archived"
    assert definition.pipeline_definition_id not in listed_ids


def _classification_to_detection_size_graph() -> RuntimePipelineGraph:
    return RuntimePipelineGraph(
        nodes=[
            RuntimeNodeSpec(
                node_key="file_source",
                node_type="source.file_test",
                node_category=RuntimeNodeCategory.SOURCE,
                config={"path": "/tmp/image.jpg"},
            ),
            RuntimeNodeSpec(
                node_key="classify",
                node_type="inference.classification",
                node_category=RuntimeNodeCategory.INFERENCE,
                logical_model_ref=RuntimeLogicalModelRef(
                    model_family_slug="missing-classifier",
                    target_path=TargetPath.REALTIME,
                    required_task_type=TaskType.CLASSIFICATION,
                ),
            ),
            RuntimeNodeSpec(
                node_key="measure_detection",
                node_type="analysis.detection_size",
                node_category=RuntimeNodeCategory.ANALYSIS,
            ),
            RuntimeNodeSpec(
                node_key="persist",
                node_type="output.persisted_result",
                node_category=RuntimeNodeCategory.OUTPUT,
            ),
        ],
        edges=[
            RuntimeGraphEdge(
                from_node_key="file_source",
                from_port="frame",
                to_node_key="classify",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="classify",
                from_port="classifications",
                to_node_key="measure_detection",
                to_port="detections",
            ),
            RuntimeGraphEdge(
                from_node_key="measure_detection",
                from_port="sizes",
                to_node_key="persist",
                to_port="result",
            ),
        ],
    )


def _valid_detection_graph(output_suffix: str) -> RuntimePipelineGraph:
    output_type = f"output.{output_suffix}"
    output_port = "result"
    return RuntimePipelineGraph(
        nodes=[
            RuntimeNodeSpec(
                node_key="channel_input",
                node_type="source.channel_frame",
                node_category=RuntimeNodeCategory.SOURCE,
            ),
            RuntimeNodeSpec(
                node_key="detect",
                node_type="inference.detection",
                node_category=RuntimeNodeCategory.INFERENCE,
                logical_model_ref=RuntimeLogicalModelRef(
                    model_family_slug="defect-detector",
                    target_path=TargetPath.REALTIME,
                    required_task_type=TaskType.DETECTION,
                ),
            ),
            RuntimeNodeSpec(
                node_key=output_suffix,
                node_type=output_type,
                node_category=RuntimeNodeCategory.OUTPUT,
            ),
        ],
        edges=[
            RuntimeGraphEdge(
                from_node_key="channel_input",
                from_port="frame",
                to_node_key="detect",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="detect",
                from_port="detections",
                to_node_key=output_suffix,
                to_port=output_port,
            ),
        ],
    )


def _trigger_detection_response_graph() -> RuntimePipelineGraph:
    return RuntimePipelineGraph(
        nodes=[
            RuntimeNodeSpec(
                node_key="http_trigger",
                node_type="trigger.http_put",
                node_category=RuntimeNodeCategory.TRIGGER,
            ),
            RuntimeNodeSpec(
                node_key="channel_input",
                node_type="source.channel_frame",
                node_category=RuntimeNodeCategory.SOURCE,
            ),
            RuntimeNodeSpec(
                node_key="detect",
                node_type="inference.detection",
                node_category=RuntimeNodeCategory.INFERENCE,
                logical_model_ref=RuntimeLogicalModelRef(
                    model_family_slug="defect-detector",
                    target_path=TargetPath.REALTIME,
                    required_task_type=TaskType.DETECTION,
                ),
            ),
            RuntimeNodeSpec(
                node_key="http_response",
                node_type="output.http_trigger_response",
                node_category=RuntimeNodeCategory.OUTPUT,
            ),
        ],
        edges=[
            RuntimeGraphEdge(
                from_node_key="http_trigger",
                from_port="trigger",
                to_node_key="channel_input",
                to_port="trigger",
            ),
            RuntimeGraphEdge(
                from_node_key="channel_input",
                from_port="frame",
                to_node_key="detect",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="detect",
                from_port="detections",
                to_node_key="http_response",
                to_port="result",
            ),
        ],
    )


def _trigger_detection_projection_graph() -> RuntimePipelineGraph:
    return RuntimePipelineGraph(
        nodes=[
            RuntimeNodeSpec(
                node_key="http_trigger",
                node_type="trigger.http_put",
                node_category=RuntimeNodeCategory.TRIGGER,
            ),
            RuntimeNodeSpec(
                node_key="channel_input",
                node_type="source.channel_frame",
                node_category=RuntimeNodeCategory.SOURCE,
            ),
            RuntimeNodeSpec(
                node_key="detect",
                node_type="inference.detection",
                node_category=RuntimeNodeCategory.INFERENCE,
                logical_model_ref=RuntimeLogicalModelRef(
                    model_family_slug="defect-detector",
                    target_path=TargetPath.REALTIME,
                    required_task_type=TaskType.DETECTION,
                ),
            ),
            RuntimeNodeSpec(
                node_key="result_output",
                node_type="output.result",
                node_category=RuntimeNodeCategory.OUTPUT,
                config={
                    "projections": [
                        {
                            "key": "piece_count",
                            "source": {"node_key": "detect", "path": "count"},
                            "aggregate": {"op": "none"},
                            "destinations": ["response"],
                        }
                    ],
                    "include_runtime_metadata": False,
                },
            ),
        ],
        edges=[
            RuntimeGraphEdge(
                from_node_key="http_trigger",
                from_port="trigger",
                to_node_key="channel_input",
                to_port="trigger",
            ),
            RuntimeGraphEdge(
                from_node_key="channel_input",
                from_port="frame",
                to_node_key="detect",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="detect",
                from_port="detections",
                to_node_key="result_output",
                to_port="result",
            ),
        ],
    )


def _branched_display_graph() -> RuntimePipelineGraph:
    return RuntimePipelineGraph(
        nodes=[
            RuntimeNodeSpec(
                node_key="channel_input",
                node_type="source.channel_frame",
                node_category=RuntimeNodeCategory.SOURCE,
            ),
            RuntimeNodeSpec(
                node_key="crop_a",
                node_type="preprocessing.crop",
                node_category=RuntimeNodeCategory.PREPROCESSING,
            ),
            RuntimeNodeSpec(
                node_key="resize_b",
                node_type="preprocessing.resize",
                node_category=RuntimeNodeCategory.PREPROCESSING,
            ),
            RuntimeNodeSpec(
                node_key="persist_a",
                node_type="output.persisted_result",
                node_category=RuntimeNodeCategory.OUTPUT,
            ),
            RuntimeNodeSpec(
                node_key="event_b",
                node_type="output.internal_event",
                node_category=RuntimeNodeCategory.OUTPUT,
            ),
            RuntimeNodeSpec(
                node_key="display_crop",
                node_type="output.display_output",
                node_category=RuntimeNodeCategory.OUTPUT,
            ),
        ],
        edges=[
            RuntimeGraphEdge(
                from_node_key="channel_input",
                from_port="frame",
                to_node_key="crop_a",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="crop_a",
                from_port="frame",
                to_node_key="persist_a",
                to_port="result",
            ),
            RuntimeGraphEdge(
                from_node_key="channel_input",
                from_port="frame",
                to_node_key="resize_b",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="resize_b",
                from_port="frame",
                to_node_key="event_b",
                to_port="result",
            ),
            RuntimeGraphEdge(
                from_node_key="crop_a",
                from_port="frame",
                to_node_key="display_crop",
                to_port="frame",
            ),
        ],
    )


def _trigger_classification_response_display_graph() -> RuntimePipelineGraph:
    return RuntimePipelineGraph(
        nodes=[
            RuntimeNodeSpec(
                node_key="http_trigger",
                node_type="trigger.http_put",
                node_category=RuntimeNodeCategory.TRIGGER,
            ),
            RuntimeNodeSpec(
                node_key="channel_input",
                node_type="source.channel_frame",
                node_category=RuntimeNodeCategory.SOURCE,
            ),
            RuntimeNodeSpec(
                node_key="classify",
                node_type="inference.classification",
                node_category=RuntimeNodeCategory.INFERENCE,
                logical_model_ref=RuntimeLogicalModelRef(
                    model_family_slug="quality-classifier",
                    target_path=TargetPath.REALTIME,
                    required_task_type=TaskType.CLASSIFICATION,
                ),
            ),
            RuntimeNodeSpec(
                node_key="http_response",
                node_type="output.http_trigger_response",
                node_category=RuntimeNodeCategory.OUTPUT,
            ),
            RuntimeNodeSpec(
                node_key="overlay",
                node_type="output.overlay",
                node_category=RuntimeNodeCategory.OUTPUT,
            ),
            RuntimeNodeSpec(
                node_key="display",
                node_type="output.display_output",
                node_category=RuntimeNodeCategory.OUTPUT,
            ),
        ],
        edges=[
            RuntimeGraphEdge(
                from_node_key="http_trigger",
                from_port="trigger",
                to_node_key="channel_input",
                to_port="trigger",
            ),
            RuntimeGraphEdge(
                from_node_key="channel_input",
                from_port="frame",
                to_node_key="classify",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="classify",
                from_port="classifications",
                to_node_key="http_response",
                to_port="result",
            ),
            RuntimeGraphEdge(
                from_node_key="classify",
                from_port="classifications",
                to_node_key="overlay",
                to_port="result",
            ),
            RuntimeGraphEdge(
                from_node_key="channel_input",
                from_port="frame",
                to_node_key="display",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="classify",
                from_port="classifications",
                to_node_key="display",
                to_port="result",
            ),
        ],
    )


def _trigger_classification_result_output_graph() -> RuntimePipelineGraph:
    graph = _trigger_classification_response_display_graph()
    nodes = []
    for node in graph.nodes:
        if node.node_key != "http_response":
            nodes.append(node)
            continue
        nodes.append(
            RuntimeNodeSpec(
                node_key="result_output",
                node_type="output.result",
                node_category=RuntimeNodeCategory.OUTPUT,
                config={
                    "result_fields": [
                        {
                            "source_field": "top_label.label",
                            "response_key": "class",
                            "required": True,
                        }
                    ]
                },
            )
        )
    edges = [
        edge.model_copy(update={"to_node_key": "result_output"})
        if edge.to_node_key == "http_response"
        else edge
        for edge in graph.edges
    ]
    return RuntimePipelineGraph(nodes=nodes, edges=edges)


def _board_measurement_workflow_graph(
    *,
    measurement_bases: list[str] | None = None,
    clearance_basis: str = "bbox_width",
    mm_per_pixel: float = 2.0,
) -> RuntimePipelineGraph:
    bases = measurement_bases or [
        "bbox_width",
        "bbox_height",
        "approximate_diameter",
        "axis_projected_width",
        "axis_projected_length",
    ]
    return RuntimePipelineGraph(
        nodes=[
            _runtime_node("http_trigger", "trigger.http_put"),
            _runtime_node("channel_input", "source.channel_frame"),
            _runtime_node("detect_features", "inference.detection"),
            _runtime_node(
                "reference_edges",
                "analysis.reference_edge_from_features",
                config={
                    "class_role_mappings": [
                        {
                            "feature_class": "board_start_edge",
                            "edge_role": "start_edge",
                            "bbox_side": "left",
                        },
                        {
                            "feature_class": "board_side_edge",
                            "edge_role": "side_edge",
                            "bbox_side": "top",
                        },
                    ]
                },
            ),
            _runtime_node("coordinate_frame", "analysis.coordinate_frame_from_edges"),
            _runtime_node(
                "measure_features",
                "analysis.feature_measurement",
                config={"measurement_rules": [{"feature_type": "knot", "bases": bases}]},
            ),
            _runtime_node(
                "clearance_zones",
                "analysis.clearance_zone_generator",
                config={
                    "clearance_rules": [
                        {
                            "feature_type": "knot",
                            "enabled": True,
                            "measurement_basis": clearance_basis,
                            "multiplier": 1.0,
                            "minimum_clearance_px": 0,
                            "apply_before": True,
                            "apply_after": True,
                            "merge_overlapping_zones": True,
                        }
                    ]
                },
            ),
            _runtime_node("free_region", "analysis.first_free_region_finder"),
            _runtime_node(
                "metric", "analysis.pixel_to_metric", config={"mm_per_pixel": mm_per_pixel}
            ),
            _runtime_node("overlay", "output.overlay"),
            _runtime_node(
                "http_response",
                "output.http_trigger_response",
                config={
                    "response_fields": [
                        {
                            "source_field": "distance_mm",
                            "response_key": "distance_mm",
                            "required": True,
                        }
                    ]
                },
            ),
        ],
        edges=[
            RuntimeGraphEdge(
                from_node_key="http_trigger",
                from_port="trigger",
                to_node_key="channel_input",
                to_port="trigger",
            ),
            RuntimeGraphEdge(
                from_node_key="channel_input",
                from_port="frame",
                to_node_key="detect_features",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="detect_features",
                from_port="detections",
                to_node_key="reference_edges",
                to_port="features",
            ),
            RuntimeGraphEdge(
                from_node_key="reference_edges",
                from_port="reference_edges",
                to_node_key="coordinate_frame",
                to_port="reference_edges",
            ),
            RuntimeGraphEdge(
                from_node_key="detect_features",
                from_port="detections",
                to_node_key="measure_features",
                to_port="features",
            ),
            RuntimeGraphEdge(
                from_node_key="coordinate_frame",
                from_port="coordinate_frame",
                to_node_key="measure_features",
                to_port="coordinate_frame",
            ),
            RuntimeGraphEdge(
                from_node_key="measure_features",
                from_port="feature_measurements",
                to_node_key="clearance_zones",
                to_port="measurements",
            ),
            RuntimeGraphEdge(
                from_node_key="coordinate_frame",
                from_port="coordinate_frame",
                to_node_key="clearance_zones",
                to_port="coordinate_frame",
            ),
            RuntimeGraphEdge(
                from_node_key="coordinate_frame",
                from_port="coordinate_frame",
                to_node_key="free_region",
                to_port="coordinate_frame",
            ),
            RuntimeGraphEdge(
                from_node_key="clearance_zones",
                from_port="clearance_zones",
                to_node_key="free_region",
                to_port="clearance_zones",
            ),
            RuntimeGraphEdge(
                from_node_key="reference_edges",
                from_port="reference_edges",
                to_node_key="metric",
                to_port="reference_edges",
            ),
            RuntimeGraphEdge(
                from_node_key="coordinate_frame",
                from_port="coordinate_frame",
                to_node_key="metric",
                to_port="coordinate_frame",
            ),
            RuntimeGraphEdge(
                from_node_key="measure_features",
                from_port="feature_measurements",
                to_node_key="metric",
                to_port="measurements",
            ),
            RuntimeGraphEdge(
                from_node_key="clearance_zones",
                from_port="clearance_zones",
                to_node_key="metric",
                to_port="clearance_zones",
            ),
            RuntimeGraphEdge(
                from_node_key="free_region",
                from_port="free_region",
                to_node_key="metric",
                to_port="free_region",
            ),
            RuntimeGraphEdge(
                from_node_key="metric",
                from_port="result",
                to_node_key="overlay",
                to_port="result",
            ),
            RuntimeGraphEdge(
                from_node_key="metric",
                from_port="result",
                to_node_key="http_response",
                to_port="result",
            ),
        ],
    )


def _cv_feature_multi_branch_workflow_graph(
    *,
    include_coordinate_to_clearance: bool = True,
) -> RuntimePipelineGraph:
    edges = [
        RuntimeGraphEdge(
            from_node_key="channel_input",
            from_port="frame",
            to_node_key="canny_edges",
            to_port="frame",
        ),
        RuntimeGraphEdge(
            from_node_key="canny_edges",
            from_port="edges",
            to_node_key="cv_reference_edges",
            to_port="edges",
        ),
        RuntimeGraphEdge(
            from_node_key="cv_reference_edges",
            from_port="reference_edges",
            to_node_key="coordinate_frame",
            to_port="reference_edges",
        ),
        RuntimeGraphEdge(
            from_node_key="channel_input",
            from_port="frame",
            to_node_key="detect_features",
            to_port="frame",
        ),
        RuntimeGraphEdge(
            from_node_key="detect_features",
            from_port="detections",
            to_node_key="measure_features",
            to_port="features",
        ),
        RuntimeGraphEdge(
            from_node_key="coordinate_frame",
            from_port="coordinate_frame",
            to_node_key="measure_features",
            to_port="coordinate_frame",
        ),
        RuntimeGraphEdge(
            from_node_key="measure_features",
            from_port="feature_measurements",
            to_node_key="clearance_zones",
            to_port="measurements",
        ),
        RuntimeGraphEdge(
            from_node_key="coordinate_frame",
            from_port="coordinate_frame",
            to_node_key="free_region",
            to_port="coordinate_frame",
        ),
        RuntimeGraphEdge(
            from_node_key="clearance_zones",
            from_port="clearance_zones",
            to_node_key="free_region",
            to_port="clearance_zones",
        ),
        RuntimeGraphEdge(
            from_node_key="free_region",
            from_port="free_region",
            to_node_key="persist",
            to_port="result",
        ),
    ]
    if include_coordinate_to_clearance:
        edges.insert(
            7,
            RuntimeGraphEdge(
                from_node_key="coordinate_frame",
                from_port="coordinate_frame",
                to_node_key="clearance_zones",
                to_port="coordinate_frame",
            ),
        )
    return RuntimePipelineGraph(
        nodes=[
            _runtime_node("channel_input", "source.channel_frame"),
            _runtime_node("canny_edges", "computer_vision.canny_edge_detection"),
            _runtime_node("cv_reference_edges", "computer_vision.reference_edge_from_cv_edges"),
            _runtime_node("coordinate_frame", "analysis.coordinate_frame_from_edges"),
            _runtime_node("detect_features", "inference.detection"),
            _runtime_node(
                "measure_features",
                "analysis.feature_measurement",
                config={
                    "measurement_rules": [
                        {"feature_type": "knot", "bases": ["bbox_width", "axis_projected_length"]}
                    ]
                },
            ),
            _runtime_node(
                "clearance_zones",
                "analysis.clearance_zone_generator",
                config={
                    "clearance_rules": [
                        {
                            "feature_type": "knot",
                            "enabled": True,
                            "measurement_basis": "bbox_width",
                            "multiplier": 1.0,
                            "minimum_clearance_px": 0,
                            "apply_before": True,
                            "apply_after": True,
                            "merge_overlapping_zones": True,
                        }
                    ]
                },
            ),
            _runtime_node("free_region", "analysis.first_free_region_finder"),
            _runtime_node("persist", "output.persisted_result"),
        ],
        edges=edges,
    )


def _runtime_node(
    node_key: str,
    node_type: str,
    *,
    config: dict[str, object] | None = None,
) -> RuntimeNodeSpec:
    prefix = node_type.split(".", maxsplit=1)[0]
    category = RuntimeNodeCategory(prefix)
    return RuntimeNodeSpec(
        node_key=node_key,
        node_type=node_type,
        node_category=category,
        config=config or {},
        logical_model_ref=(
            RuntimeLogicalModelRef(
                model_family_slug="defect-detector",
                target_path=TargetPath.REALTIME,
                required_task_type=TaskType.DETECTION,
                deployment_release_id="rel_detection",
            )
            if category == RuntimeNodeCategory.INFERENCE
            else None
        ),
    )
