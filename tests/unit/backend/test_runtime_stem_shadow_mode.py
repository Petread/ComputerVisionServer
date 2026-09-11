from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cv_contracts import ResponseMeta
from cv_contracts.runtime import RuntimeNodeSpec, RuntimePipelineGraph
from cv_db.models import (
    PipelineDefinition,
    PipelineNode,
    PipelineVersion,
    ResultRecord,
    StemOrientationReview,
)
from cv_db.pipeline_store import _validate_stem_shadow_mode_graph
from cv_db.stem_shadow_store import (
    RuntimeStemShadowEvaluationQuery,
    stem_shadow_evaluation_response,
)
from cv_status import RuntimeNodeCategory
from sqlalchemy.orm import Session


def test_shadow_evaluation_aggregates_five_hundred_bounded_sessions(
    sqlite_engine,
) -> None:
    started = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)
    with Session(sqlite_engine) as session:
        session.add(
            PipelineDefinition(
                id="pdef_shadow",
                slug="stem-shadow",
                name="Stem shadow",
            )
        )
        session.add(
            PipelineVersion(
                id="plv_shadow",
                pipeline_definition_id="pdef_shadow",
                version_no=1,
                mode="realtime",
                graph={},
                config_digest="shadow-digest",
            )
        )
        session.add(
            PipelineNode(
                id="pnode_shadow_solver",
                pipeline_version_id="plv_shadow",
                node_key="stem_solver",
                node_type="analysis.stem_curvature_phase_solver",
                node_category="analysis",
                config={
                    "shadow_mode_enabled": True,
                    "shadow_drift_window_sessions": 50,
                    "shadow_confidence_drop_threshold": 0.15,
                    "shadow_uncertain_rate_increase_threshold": 0.15,
                },
            )
        )
        for index in range(500):
            decision = (
                "UNCERTAIN"
                if index % 10 == 0
                else "NO_ROTATION"
                if index % 5 == 0
                else "ROTATE"
            )
            confidence = 0.9 if index < 250 else 0.6
            session_started = started + timedelta(seconds=index * 2)
            decision_timestamp = session_started + timedelta(milliseconds=500 + index % 5)
            orientation = {
                "schema_version": "stem.orientation.v3",
                "algorithm": "CURVATURE_PHASE_MINIMUM",
                "session_id": f"session-{index}",
                "track_id": f"track-{index}",
                "shadow_mode": True,
                "decision": decision,
                "recommended_rotation_deg_signed": (
                    float((index % 61) - 30) if decision == "ROTATE" else None
                ),
                "overall_confidence": confidence,
                "session_started_timestamp": session_started.isoformat(),
                "decision_timestamp": decision_timestamp.isoformat(),
                "deadline_met": index % 100 != 0,
                "process_calibration_hash": (
                    "process-hash-a" if index < 250 else "process-hash-b"
                ),
                "rotation_calibration_hash": "rotation-hash-a",
                "reason_codes": (
                    ["PHASE_TRACK_LOST"] if decision == "UNCERTAIN" else ["RESULT_READY"]
                ),
            }
            session.add(
                ResultRecord(
                    id=f"rrc_shadow_{index}",
                    channel_id="chn_shadow",
                    pipeline_version_id="plv_shadow",
                    runtime_execution_id="rex_shadow",
                    correlation_id=f"corr_shadow_{index}",
                    completed_at=decision_timestamp,
                    mode="realtime",
                    status="completed",
                    result_type="stem_orientation",
                    schema_version="runtime.execution.result.v1",
                    result={"final_response": orientation},
                    trigger_response=orientation,
                    history_retained=True,
                )
            )
        session.add_all(
            [
                _review(
                    review_id="review-shadow-0-v1",
                    result_id="rrc_shadow_0",
                    revision=1,
                    plausibility="yes",
                    technical_reason="",
                ),
                _review(
                    review_id="review-shadow-0-v2",
                    result_id="rrc_shadow_0",
                    revision=2,
                    plausibility="no",
                    technical_reason="surface_feature_loss",
                ),
                _review(
                    review_id="review-shadow-1-v1",
                    result_id="rrc_shadow_1",
                    revision=1,
                    plausibility="unclear",
                    technical_reason="impact_relock_ambiguous",
                ),
            ]
        )
        session.commit()

        response = stem_shadow_evaluation_response(
            session,
            pipeline_version_id="plv_shadow",
            query=RuntimeStemShadowEvaluationQuery(maximum_sessions=1_000),
            meta=ResponseMeta(generated_at="2026-08-05T08:00:00Z"),
        )

    decisions = {item.key: item.count for item in response.decision_distribution}
    ratings = {item.key: item.count for item in response.feedback.ratings}
    assert response.evaluation_status == "ACTIVE"
    assert response.sessions_available == 500
    assert response.sessions_analyzed == 500
    assert response.limit_reached is False
    assert decisions == {"ROTATE": 400, "NO_ROTATION": 50, "UNCERTAIN": 50}
    assert response.uncertain_share == 0.1
    assert response.deadline_miss_share == 0.01
    assert response.decision_latency.measured_count == 500
    assert response.decision_latency.p95_ms is not None
    assert response.drift.status == "DRIFT_SIGNAL"
    assert "PROCESS_CALIBRATION_IDENTITY_CHANGED" in response.drift.signals
    assert "RECENT_CONFIDENCE_DROPPED" in response.drift.signals
    assert response.feedback.reviewed_session_count == 2
    assert ratings["implausible"] == 1
    assert ratings["uncertain"] == 1
    assert "# Stem Shadow Mode evaluation" in response.report_markdown
    assert response.report_json["sessions"]["analyzed"] == 500


def test_shadow_mode_rejects_active_plc_path_but_allows_dry_run() -> None:
    solver = _node(
        "solver",
        "analysis.stem_curvature_phase_solver",
        RuntimeNodeCategory.ANALYSIS,
        {"shadow_mode_enabled": True},
    )
    active_plc = _node(
        "plc",
        "output.siemens_sps_write",
        RuntimeNodeCategory.OUTPUT,
        {"enabled": True, "dry_run": False},
    )
    dry_run_plc = active_plc.model_copy(update={"config": {"enabled": True, "dry_run": True}})

    blocked = _validate_stem_shadow_mode_graph(
        graph=RuntimePipelineGraph(nodes=[solver, active_plc], edges=[])
    )
    allowed = _validate_stem_shadow_mode_graph(
        graph=RuntimePipelineGraph(nodes=[solver, dry_run_plc], edges=[])
    )

    assert [issue.code for issue in blocked] == ["stem_shadow_mode_plc_output_forbidden"]
    assert allowed == []


def _review(
    *,
    review_id: str,
    result_id: str,
    revision: int,
    plausibility: str,
    technical_reason: str,
) -> StemOrientationReview:
    return StemOrientationReview(
        id=review_id,
        result_record_id=result_id,
        revision=revision,
        review_schema_version="stem.orientation.review.v1",
        proposal_plausibility=plausibility,
        direction_assessment="unclear",
        angle_assessment="not_assessable",
        bow_relevance="unclear",
        final_position="unclear",
        comment="",
        special_case=technical_reason,
        reviewer_user_id="operator-1",
        reviewer_username="operator",
    )


def _node(
    node_key: str,
    node_type: str,
    category: RuntimeNodeCategory,
    config: dict[str, object],
) -> RuntimeNodeSpec:
    return RuntimeNodeSpec(
        node_key=node_key,
        node_type=node_type,
        node_category=category,
        input_ports=[],
        output_ports=[],
        config=config,
        position={},
    )
