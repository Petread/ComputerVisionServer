from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from admin_api.main import app as admin_app
from cv_core import generate_prefixed_id
from cv_db import (
    PipelineDefinition,
    PipelineNode,
    PipelineVersion,
    ResultRecord,
    RuntimeChannel,
    StemOrientationReview,
    session_scope,
    stem_orientation_result_from_record,
)
from cv_status import RuntimeChannelDesiredState, RuntimeMode, RuntimeObservedState
from fastapi.testclient import TestClient
from result_service.main import app as result_app
from tests.conftest import build_test_settings, configure_sqlite_factories


def test_result_service_filters_history_and_returns_typed_payload_sections(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "result-service")
    admin_settings = build_test_settings("admin-api", tmp_path)
    result_settings = build_test_settings("result-service", tmp_path)
    admin_app.state.settings = admin_settings
    result_app.state.settings = result_settings
    now = datetime(2026, 5, 2, 9, 30, tzinfo=UTC)

    with session_scope(result_settings) as session:
        session.add(
            RuntimeChannel(
                id="chn_press_result",
                slug="press-result",
                name="Press inspection",
                technical_place="press-line-1",
                mode=RuntimeMode.REALTIME.value,
                desired_state=RuntimeChannelDesiredState.RUNNING.value,
                observed_state=RuntimeObservedState.RUNNING.value,
            )
        )
        session.add(
            RuntimeChannel(
                id="chn_trigger_result",
                slug="trigger-result",
                name="Packing camera",
                technical_place="packing-1",
                mode=RuntimeMode.REALTIME.value,
                desired_state=RuntimeChannelDesiredState.RUNNING.value,
                observed_state=RuntimeObservedState.RUNNING.value,
            )
        )
        session.add(
            _result_record(
                channel_id="chn_press_result",
                pipeline_version_id="plv_realtime",
                mode="realtime",
                status="completed",
                result_type="realtime_frame",
                completed_at=now,
                trigger_response={"count": 2},
            )
        )
        session.add(
            _result_record(
                channel_id="chn_trigger_result",
                pipeline_version_id="plv_trigger",
                mode="trigger",
                status="failed",
                result_type="failed_execution",
                completed_at=now - timedelta(minutes=10),
                trigger_response={"result": False},
            )
        )
        session.add(
            _result_record(
                channel_id="chn_press_result",
                pipeline_version_id="plv_realtime_old",
                mode="realtime",
                status="completed",
                result_type="realtime_frame",
                completed_at=now - timedelta(days=2),
                trigger_response={"count": 1},
            )
        )
        session.add(
            _result_record(
                channel_id="chn_press_result",
                pipeline_version_id="plv_trigger_timeout",
                mode="trigger",
                status="timeout",
                result_type="timeout",
                completed_at=now + timedelta(minutes=5),
                trigger_response={},
            )
        )

    with TestClient(admin_app) as admin_client, TestClient(result_app) as result_client:
        public_latest = result_client.get(
            "/api/v1/runtime/channels/chn_press_result/results/latest"
        )
        public_latest_basic_header = result_client.get(
            "/api/v1/runtime/channels/chn_press_result/results/latest",
            headers={"Authorization": "Basic ignored-for-public-results"},
        )
        _login(admin_client)
        _copy_auth_cookies(admin_client, result_client, admin_settings)
        filtered = result_client.get(
            "/api/v1/runtime/results",
            params={
                "channel_id": "chn_press_result",
                "pipeline_version_id": "plv_realtime",
                "mode": "realtime",
                "status": "completed",
                "result_type": "realtime_frame",
                "completed_from": "2026-05-02T09:00:00Z",
            },
        )
        search = result_client.get("/api/v1/runtime/results", params={"search": "Packing"})
        basic_header = result_client.get(
            "/api/v1/runtime/results",
            headers={"Authorization": "Basic ignored-for-public-results"},
        )
        detail = result_client.get("/api/v1/runtime/results/rrc_plv_realtime")

    assert filtered.status_code == 200
    assert public_latest.status_code == 200
    assert public_latest.json()["item"]["channel_id"] == "chn_press_result"
    assert public_latest.json()["item"]["mode"] == "realtime"
    assert public_latest.json()["item"]["pipeline_version_id"] == "plv_realtime"
    assert public_latest_basic_header.status_code == 200
    assert filtered.json()["total"] == 1
    item = filtered.json()["items"][0]
    assert item["channel_name"] == "Press inspection"
    assert item["pipeline_version_id"] == "plv_realtime"
    assert item["trigger_response"] == {"count": 2}
    assert item["derived_preview"]["count"] == 2
    assert search.status_code == 200
    assert search.json()["total"] == 1
    assert search.json()["items"][0]["channel_name"] == "Packing camera"
    assert basic_header.status_code == 200
    assert detail.status_code == 200
    detail_item = detail.json()["item"]
    assert detail_item["raw_inference_outputs"]
    assert detail_item["normalized_inference_outputs"]
    assert detail_item["derived_outputs"]
    assert detail_item["overlay_primitives"]
    assert detail_item["runtime_metadata"]["mode"] == "realtime"


def test_result_service_can_require_result_scope_bearer_token(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "result-service")
    result_settings = build_test_settings(
        "result-service",
        tmp_path,
        api_keys=[
            (
                "result-client",
                "result-secret",
                ["runtime.results.read"],
                "application",
                "mes-result-reader",
            ),
            (
                "trigger-client",
                "trigger-secret",
                ["runtime.trigger.invoke"],
                "application",
                "plc-trigger",
            ),
        ],
    )
    result_settings.runtime.result_endpoint_auth_required = True
    result_app.state.settings = result_settings

    with session_scope(result_settings) as session:
        session.add(
            RuntimeChannel(
                id="chn_press_result",
                slug="press-result",
                name="Press inspection",
                technical_place="press-line-1",
                mode=RuntimeMode.REALTIME.value,
                desired_state=RuntimeChannelDesiredState.RUNNING.value,
                observed_state=RuntimeObservedState.RUNNING.value,
            )
        )
        session.add(
            _result_record(
                channel_id="chn_press_result",
                pipeline_version_id="plv_realtime",
                mode="realtime",
                status="completed",
                result_type="realtime_frame",
                completed_at=datetime(2026, 5, 2, 9, 30, tzinfo=UTC),
                trigger_response={"count": 2},
            )
        )

    with TestClient(result_app) as result_client:
        unauthenticated = result_client.get(
            "/api/v1/runtime/channels/chn_press_result/results/latest"
        )
        wrong_scope = result_client.get(
            "/api/v1/runtime/channels/chn_press_result/results/latest",
            headers={"Authorization": "Bearer trigger-client.trigger-secret"},
        )
        authorized = result_client.get(
            "/api/v1/runtime/channels/chn_press_result/results/latest",
            headers={"Authorization": "Bearer result-client.result-secret"},
        )

    assert unauthenticated.status_code == 401
    assert wrong_scope.status_code == 401
    assert authorized.status_code == 200
    assert authorized.json()["item"]["channel_id"] == "chn_press_result"


def test_result_history_keeps_legacy_v1_quality_results_readable_after_temporal_update(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "result-service")
    admin_settings = build_test_settings("admin-api", tmp_path)
    result_settings = build_test_settings("result-service", tmp_path)
    admin_app.state.settings = admin_settings
    result_app.state.settings = result_settings

    with session_scope(result_settings) as session:
        session.add(
            RuntimeChannel(
                id="chn_legacy_quality",
                slug="legacy-quality",
                name="Legacy quality channel",
                technical_place="legacy-line",
                mode=RuntimeMode.REALTIME.value,
                desired_state=RuntimeChannelDesiredState.STOPPED.value,
                observed_state=RuntimeObservedState.STOPPED.value,
            )
        )
        record = _result_record(
            channel_id="chn_legacy_quality",
            pipeline_version_id="plv_quality_v1",
            mode="realtime",
            status="completed",
            result_type="quality_result",
            completed_at=datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
            trigger_response={"result_label": "good"},
        )
        record.derived_outputs = [
            {
                "node_key": "quality-v1",
                "payload": {
                    "payload_type": "quality_decision",
                    "decision": {
                        "status": "completed",
                        "result_label": "good",
                        "confidence": 0.91,
                    },
                },
            }
        ]
        session.add(record)

    with TestClient(admin_app) as admin_client, TestClient(result_app) as result_client:
        _login(admin_client)
        _copy_auth_cookies(admin_client, result_client, admin_settings)
        response = result_client.get("/api/v1/runtime/results/rrc_plv_quality_v1")

    assert response.status_code == 200
    item = response.json()["item"]
    assert item["schema_version"] == "runtime.execution.result.v1"
    assert item["result_type"] == "quality_result"
    assert item["derived_preview"]["status"] == "completed"
    assert item["derived_preview"]["result_label"] == "good"
    assert item["trigger_response"] == {"result_label": "good"}


def test_stem_orientation_review_is_append_only_and_preserves_original_result(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "result-service")
    admin_settings = build_test_settings("admin-api", tmp_path)
    result_settings = build_test_settings("result-service", tmp_path)
    admin_app.state.settings = admin_settings
    result_app.state.settings = result_settings
    orientation = {
        "schema_version": "stem.orientation.v2",
        "session_id": "stem-session-7",
        "track_id": "stem-track-4",
        "decision_ready": True,
        "decision": {"ready": True, "frame_sequence": 152},
        "recommendation": {
            "signed_rotation_deg": -43.2,
            "direction": "counter_clockwise_viewed_from_configured_log_end",
            "rotation_viewpoint_convention": "viewed_from_configured_log_end_a",
        },
        "post_rotation_verification": None,
    }
    with session_scope(result_settings) as session:
        session.add(
            RuntimeChannel(
                id="chn_stem_result",
                slug="stem-result",
                name="Stem orientation",
                technical_place="longitudinal-conveyor-1",
                mode=RuntimeMode.REALTIME.value,
                desired_state=RuntimeChannelDesiredState.RUNNING.value,
                observed_state=RuntimeObservedState.RUNNING.value,
            )
        )
        session.add(
            ResultRecord(
                id="rrc_stem_orientation",
                channel_id="chn_stem_result",
                pipeline_version_id="plv_stem_v2",
                runtime_execution_id="rex_stem",
                correlation_id="corr_stem",
                completed_at=datetime(2026, 8, 3, 10, 30, tzinfo=UTC),
                mode="realtime",
                status="completed",
                result_type="stem_orientation",
                schema_version="runtime.execution.result.v1",
                result={"persisted_results": []},
                derived_outputs=[
                    {
                        "node_key": "orientation_result",
                        "payload": {"payload_type": "result", "result": orientation},
                    }
                ],
                trigger_response=orientation,
                history_retained=True,
            )
        )

    with TestClient(admin_app) as admin_client, TestClient(result_app) as result_client:
        _login(admin_client)
        _copy_auth_cookies(admin_client, result_client, admin_settings)
        csrf_token = admin_client.cookies.get(admin_settings.session.csrf_cookie_name)
        first = result_client.post(
            "/api/v1/runtime/results/rrc_stem_orientation/stem-orientation-reviews",
            headers={"X-CSRF-Token": str(csrf_token)},
            json={
                "proposal_plausibility": "yes",
                "direction_assessment": "correct",
                "angle_assessment": "appropriate",
                "bow_relevance": "relevant",
                "final_position": "good",
                "target_frame_start": 106,
                "comment": "Plausible pre-entry recommendation",
                "special_case": "",
            },
        )
        second = result_client.post(
            "/api/v1/runtime/results/rrc_stem_orientation/stem-orientation-reviews",
            headers={"X-CSRF-Token": str(csrf_token)},
            json={
                "proposal_plausibility": "unclear",
                "direction_assessment": "unclear",
                "angle_assessment": "not_assessable",
                "bow_relevance": "unclear",
                "final_position": "acceptable",
                "target_frame_start": 104,
                "target_frame_end": 108,
                "comment": "Second look",
                "special_case": "partial_occlusion",
            },
        )
        history = result_client.get(
            "/api/v1/runtime/results/rrc_stem_orientation/stem-orientation-reviews"
        )
        filtered = result_client.get(
            "/api/v1/runtime/results",
            params={"result_type": "stem_orientation", "search": "stem-track-4"},
        )

    assert first.status_code == 201
    assert first.json()["item"]["schema_version"] == "stem.orientation.review.v1"
    assert first.json()["item"]["revision"] == 1
    assert first.json()["item"]["target_frame_end"] == 106
    assert second.status_code == 201
    assert second.json()["item"]["revision"] == 2
    assert second.json()["item"]["supersedes_review_id"] == first.json()["item"]["review_id"]
    assert history.status_code == 200
    assert [item["revision"] for item in history.json()["items"]] == [2, 1]
    assert history.json()["latest"]["revision"] == 2
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1

    with session_scope(result_settings) as session:
        stored_result = session.get(ResultRecord, "rrc_stem_orientation")
        reviews = list(session.query(StemOrientationReview).all())
        assert stored_result is not None
        assert stored_result.trigger_response["recommendation"]["signed_rotation_deg"] == -43.2
        assert stored_result.trigger_response["post_rotation_verification"] is None
        assert len(reviews) == 2


def test_curvature_minimum_v3_result_remains_reviewable_without_a_migration() -> None:
    orientation = {
        "schema_version": "stem.orientation.v3",
        "algorithm": "CURVATURE_PHASE_MINIMUM",
        "session_id": "stem-session-v3",
        "track_id": "stem-track-v3",
        "decision_ready": True,
        "decision": "ROTATE",
        "recommended_rotation_deg_signed": -34.5,
        "direction": "COUNTER_CLOCKWISE",
        "decision_frame_sequence": 211,
    }
    row = _result_record(
        channel_id="chn_stem_result",
        pipeline_version_id="plv_stem_v3",
        mode="realtime",
        status="completed",
        result_type="stem_orientation",
        completed_at=datetime(2026, 8, 4, 10, 0, tzinfo=UTC),
        trigger_response=orientation,
    )

    assert stem_orientation_result_from_record(row) == orientation


def test_shadow_evaluation_endpoint_and_v3_technical_review_contract(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "result-service")
    admin_settings = build_test_settings("admin-api", tmp_path)
    result_settings = build_test_settings("result-service", tmp_path)
    admin_app.state.settings = admin_settings
    result_app.state.settings = result_settings
    started = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
    orientation = {
        "schema_version": "stem.orientation.v3",
        "algorithm": "CURVATURE_PHASE_MINIMUM",
        "session_id": "shadow-session-1",
        "track_id": "shadow-track-1",
        "shadow_mode": True,
        "decision_ready": True,
        "decision": "UNCERTAIN",
        "recommended_rotation_deg_signed": None,
        "overall_confidence": 0.41,
        "session_started_timestamp": started.isoformat(),
        "decision_timestamp": (started + timedelta(milliseconds=620)).isoformat(),
        "deadline_met": True,
        "process_calibration_hash": "process-shadow-hash",
        "rotation_calibration_hash": "rotation-shadow-hash",
        "reason_codes": ["PHASE_TRACK_LOST"],
    }
    with session_scope(result_settings) as session:
        session.add(
            RuntimeChannel(
                id="chn_shadow_result",
                slug="shadow-result",
                name="Shadow result",
                technical_place="longitudinal-conveyor-1",
                mode=RuntimeMode.REALTIME.value,
                desired_state=RuntimeChannelDesiredState.RUNNING.value,
                observed_state=RuntimeObservedState.RUNNING.value,
            )
        )
        session.add(
            PipelineDefinition(
                id="pdef_shadow_result",
                slug="shadow-result-pipeline",
                name="Shadow result pipeline",
            )
        )
        session.add(
            PipelineVersion(
                id="plv_shadow_result",
                pipeline_definition_id="pdef_shadow_result",
                version_no=1,
                mode="realtime",
                graph={},
                config_digest="shadow-result-digest",
            )
        )
        session.add(
            PipelineNode(
                id="pnode_shadow_result",
                pipeline_version_id="plv_shadow_result",
                node_key="stem_solver",
                node_type="analysis.stem_curvature_phase_solver",
                node_category="analysis",
                config={"shadow_mode_enabled": True},
            )
        )
        session.add(
            ResultRecord(
                id="rrc_shadow_result",
                channel_id="chn_shadow_result",
                pipeline_version_id="plv_shadow_result",
                runtime_execution_id="rex_shadow_result",
                correlation_id="corr_shadow_result",
                completed_at=started + timedelta(milliseconds=620),
                mode="realtime",
                status="completed",
                result_type="stem_orientation",
                schema_version="runtime.execution.result.v1",
                result={"final_response": orientation},
                trigger_response=orientation,
                history_retained=True,
            )
        )

    with TestClient(admin_app) as admin_client, TestClient(result_app) as result_client:
        _login(admin_client)
        _copy_auth_cookies(admin_client, result_client, admin_settings)
        csrf_token = admin_client.cookies.get(admin_settings.session.csrf_cookie_name)
        evaluation = result_client.get(
            "/api/v1/runtime/pipeline-versions/plv_shadow_result/stem-shadow-evaluation"
        )
        accepted_review = result_client.post(
            "/api/v1/runtime/results/rrc_shadow_result/stem-orientation-reviews",
            headers={"X-CSRF-Token": str(csrf_token)},
            json={
                "proposal_plausibility": "unclear",
                "direction_assessment": "unclear",
                "angle_assessment": "not_assessable",
                "bow_relevance": "unclear",
                "final_position": "not_assessed",
                "comment": "",
                "special_case": "surface_feature_loss",
            },
        )
        rejected_angle_review = result_client.post(
            "/api/v1/runtime/results/rrc_shadow_result/stem-orientation-reviews",
            headers={"X-CSRF-Token": str(csrf_token)},
            json={
                "proposal_plausibility": "yes",
                "direction_assessment": "correct",
                "angle_assessment": "appropriate",
                "bow_relevance": "unclear",
                "final_position": "not_assessed",
                "comment": "",
                "special_case": "",
            },
        )

    assert evaluation.status_code == 200
    assert evaluation.json()["evaluation_status"] == "ACTIVE"
    assert evaluation.json()["sessions_analyzed"] == 1
    assert evaluation.json()["uncertain_share"] == 1
    assert accepted_review.status_code == 201
    assert accepted_review.json()["item"]["special_case"] == "surface_feature_loss"
    assert rejected_angle_review.status_code == 409


def _result_record(
    *,
    channel_id: str,
    pipeline_version_id: str,
    mode: str,
    status: str,
    result_type: str,
    completed_at: datetime,
    trigger_response: dict[str, object],
) -> ResultRecord:
    record_id = "rrc_" + pipeline_version_id
    return ResultRecord(
        id=record_id,
        channel_id=channel_id,
        pipeline_version_id=pipeline_version_id,
        runtime_execution_id=generate_prefixed_id("rex"),
        correlation_id=f"corr_{pipeline_version_id}",
        completed_at=completed_at,
        mode=mode,
        status=status,
        result_type=result_type,
        schema_version="runtime.execution.result.v1",
        result={"pipeline_version_id": pipeline_version_id, "final_response": trigger_response},
        raw_inference_outputs=[
            {
                "node_key": "detect",
                "gateway_response": {"route": {"deployment_release_id": "rel_1"}},
            }
        ],
        normalized_inference_outputs=[
            {"node_key": "detect", "payload": {"payload_type": "detections", "detections": []}}
        ],
        derived_outputs=[
            {
                "node_key": "count",
                "payload": {
                    "payload_type": "measurements",
                    "measurements": [{"key": "count", "value": trigger_response.get("count", 0)}],
                },
            }
        ],
        overlay_primitives=[{"primitive_type": "measurement", "label": "count", "geometry": {}}],
        trigger_response=trigger_response,
        runtime_metadata={"mode": mode},
        timing={"total_ms": 12.5},
        model_routes=[{"deployment_release_id": "rel_1"}],
        history_retained=True,
    )


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "adminadmin"},
    )
    assert response.status_code == 200


def _copy_auth_cookies(
    source_client: TestClient,
    target_client: TestClient,
    settings,
) -> None:
    target_client.cookies.set(
        settings.session.cookie_name,
        source_client.cookies.get(settings.session.cookie_name),
    )
    target_client.cookies.set(
        settings.session.csrf_cookie_name,
        source_client.cookies.get(settings.session.csrf_cookie_name),
    )
