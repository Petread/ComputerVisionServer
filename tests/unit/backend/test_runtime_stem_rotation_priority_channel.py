from __future__ import annotations

from cv_contracts import RuntimeStemRotationPriorityControl
from cv_contracts.runtime import RuntimeChannelCreateRequest, RuntimeChannelUpdateRequest
from cv_db import (
    RuntimeControlCommandRecord,
    create_runtime_channel,
    get_pipeline_definition_detail,
    list_pipeline_definitions,
    update_runtime_channel,
)
from cv_runtime import BUILTIN_NODE_TYPES, ExecutablePipelineGraph
from cv_status import RuntimeSourceType
from realtime_executor.live import (
    _ordered_stem_stateful_node_keys,
    _stem_measurement_parallel_node_keys,
)
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


def test_priority_control_is_exported_with_fail_closed_frontend_defaults() -> None:
    control = RuntimeStemRotationPriorityControl()

    assert control.priority_source == "FRONTEND"
    assert control.priority_mode == "NONE"
    assert control.optimization_enable is True


def test_channel_priority_defaults_and_revisioned_update_do_not_request_restart(
    sqlite_engine: Engine,
) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        created = create_runtime_channel(
            session,
            RuntimeChannelCreateRequest(
                name="Stem rotation camera",
                technical_place="infeed-line-1",
                source_type=RuntimeSourceType.RTSP,
                source_config={"url": "rtsp://camera.local/stem"},
            ),
            created_by="commissioning",
        )
        assert created.stem_rotation_priority_control.priority_source == "FRONTEND"
        assert created.stem_rotation_priority_control.priority_mode == "NONE"
        assert created.stem_rotation_priority_control.optimization_enable is True
        assert created.stem_rotation_priority_control.revision == 0
        command_count_before = session.scalar(
            select(func.count()).select_from(RuntimeControlCommandRecord)
        )

        updated = update_runtime_channel(
            session,
            created.channel_id,
            RuntimeChannelUpdateRequest(
                stem_rotation_priority_mode="NACHSCHNITT_KRITISCH",
            ),
            updated_by="commissioning",
        )
        command_count_after = session.scalar(
            select(func.count()).select_from(RuntimeControlCommandRecord)
        )

        assert updated.stem_rotation_priority_control.priority_source == "FRONTEND"
        assert updated.stem_rotation_priority_control.priority_mode == "NACHSCHNITT_KRITISCH"
        assert updated.stem_rotation_priority_control.revision == 1
        assert updated.stem_rotation_priority_control.updated_by == "commissioning"
        assert command_count_after == command_count_before


def test_rotation_optimization_template_reuses_the_existing_measurement_path(
    sqlite_engine: Engine,
) -> None:
    with Session(sqlite_engine, expire_on_commit=False) as session:
        template = next(
            item
            for item in list_pipeline_definitions(
                session,
                include_templates=True,
                templates_only=True,
            )
            if item.slug == "template-realtime-stem-rotation-optimization"
        )
        detail = get_pipeline_definition_detail(session, template.pipeline_definition_id)

        assert detail is not None
        assert detail.latest_version is not None
        node_types = {item.node_type for item in detail.latest_version.graph.nodes}
        assert {
            "analysis.active_stem_tracker",
            "analysis.stem_horizontal_curvature",
            "analysis.stem_surface_phase",
            "analysis.stem_slice_model",
            "analysis.stem_rotation_priority_adapter",
            "analysis.stem_rotation_optimizer",
            "output.result",
        } <= node_types
        assert "output.siemens_sps_write" not in node_types
        nodes_by_key = {item.node_key: item for item in detail.latest_version.graph.nodes}
        assert nodes_by_key["measure_curvature"].config["minimum_observation_interval_ms"] == 50
        executable = ExecutablePipelineGraph.build(
            pipeline_version_id=detail.latest_version.pipeline_version_id,
            graph=detail.latest_version.graph,
            handler_refs={item.node_type: item.runtime_handler_ref for item in BUILTIN_NODE_TYPES},
        )
        assert _stem_measurement_parallel_node_keys(executable) == (
            "measurement_eligibility",
            "stem_geometry",
            "measure_curvature",
            "track_surface_phase",
        )
        assert _ordered_stem_stateful_node_keys(executable) == (
            "active_stem",
            "measure_curvature",
            "track_surface_phase",
            "build_slice_model",
        )
