import cv_db.models  # noqa: F401
import pytest
from cv_contracts.runtime import (
    RuntimeChannelCreateRequest,
    RuntimeGraphEdge,
    RuntimeLogicalModelRef,
    RuntimeNodeSpec,
    RuntimePipelineGraph,
)
from cv_db.base import Base
from cv_runtime import (
    can_transition_channel_desired,
    can_transition_execution,
    runtime_service_names,
)
from cv_status import (
    RuntimeChannelDesiredState,
    RuntimeExecutionStatus,
    RuntimeMode,
    RuntimeNodeCategory,
    RuntimeSourceType,
    TargetPath,
    TaskType,
)
from pydantic import ValidationError


def test_runtime_service_set_is_complete() -> None:
    assert set(runtime_service_names()) == {
        "runtime-admin-api",
        "channel-service",
        "pipeline-definition-service",
        "runtime-orchestrator",
        "realtime-executor",
        "trigger-executor",
        "overlay-service",
        "capture-service",
        "result-service",
        "connector-service",
    }


def test_channel_create_request_does_not_require_internal_ids() -> None:
    request = RuntimeChannelCreateRequest(
        name="Press line camera",
        technical_place="press-line-1",
        mode=RuntimeMode.REALTIME,
    )

    assert request.source_id is None
    assert request.pipeline_version_id is None
    assert request.result_history_policy.enabled is False
    assert request.result_history_policy.retention_hours == 24
    assert request.result_history_policy.sample_interval_seconds == 5
    assert request.result_history_policy.max_records == 10_000
    assert "id" not in RuntimeChannelCreateRequest.model_fields


def test_inference_nodes_require_logical_model_refs() -> None:
    with pytest.raises(ValidationError):
        RuntimeNodeSpec(
            node_key="detect_defects",
            node_type="inference.detection",
            node_category=RuntimeNodeCategory.INFERENCE,
        )

    node = RuntimeNodeSpec(
        node_key="detect_defects",
        node_type="inference.detection",
        node_category=RuntimeNodeCategory.INFERENCE,
        logical_model_ref=RuntimeLogicalModelRef(
            model_family_slug="defect_detection",
            target_path=TargetPath.REALTIME,
            required_task_type=TaskType.DETECTION,
        ),
    )

    assert node.logical_model_ref is not None
    assert node.logical_model_ref.model_family_slug == "defect_detection"


def test_pipeline_graph_rejects_unknown_edges() -> None:
    source = RuntimeNodeSpec(
        node_key="rtsp_input",
        node_type=RuntimeSourceType.RTSP.value,
        node_category=RuntimeNodeCategory.SOURCE,
    )

    with pytest.raises(ValidationError):
        RuntimePipelineGraph(
            nodes=[source],
            edges=[
                RuntimeGraphEdge(
                    from_node_key="rtsp_input",
                    from_port="frame",
                    to_node_key="missing_node",
                    to_port="frame",
                )
            ],
        )


def test_runtime_state_transitions_are_constrained() -> None:
    assert can_transition_channel_desired(
        RuntimeChannelDesiredState.STOPPED,
        RuntimeChannelDesiredState.START_REQUESTED,
    )
    assert not can_transition_channel_desired(
        RuntimeChannelDesiredState.RUNNING,
        RuntimeChannelDesiredState.DRAFT,
    )
    assert can_transition_execution(
        RuntimeExecutionStatus.RUNNING,
        RuntimeExecutionStatus.DRAINING,
    )
    assert not can_transition_execution(
        RuntimeExecutionStatus.STOPPED,
        RuntimeExecutionStatus.RUNNING,
    )


def test_runtime_tables_are_registered_in_metadata() -> None:
    assert {
        "runtime_channels",
        "runtime_sources",
        "pipeline_definitions",
        "pipeline_versions",
        "pipeline_nodes",
        "runtime_executions",
        "trigger_endpoints",
        "capture_rules",
        "output_connectors",
        "result_records",
    }.issubset(Base.metadata.tables)
