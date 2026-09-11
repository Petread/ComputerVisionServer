from __future__ import annotations

import os
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from admin_api.main import app as admin_app
from capture_service.executor import (
    StoredCaptureAsset,
    _assert_capture_storage_available,
    _latest_live_frame,
    _remove_redundant_capture_asset,
    drain_capture_work,
)
from cv_contracts import DetectionBox
from cv_contracts.runtime import RuntimeChannelCreateRequest
from cv_core import PlatformSettings
from cv_db import (
    AuditEvent,
    Dataset,
    LabelProject,
    RuntimeCaptureRequest,
    RuntimeManualCaptureRequestRecord,
    create_runtime_channel,
    record_runtime_execution_result,
    session_scope,
)
from cv_runtime.payloads import FramePayload, RoiPayload, RuntimeExecutionResult
from cv_status import DatasetStatus, LabelProjectStatus, RuntimeMode, RuntimeSourceType, TaskType
from cv_utils import PlatformError
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from runtime_admin_api.main import app as runtime_admin_app
from sqlalchemy.engine import Engine
from tests.conftest import build_test_settings, configure_sqlite_factories


class _FakeLabelStudioClient:
    calls: list[dict[str, object]] = []
    prediction_calls: list[dict[str, object]] = []

    def __init__(self, **_kwargs: object) -> None:
        pass

    def predict_with_ml_backend(self, backend_url: str, tasks: list[dict[str, object]]) -> dict[str, object]:
        self.__class__.prediction_calls.append({"backend_url": backend_url, "tasks": tasks})
        return {
            "model_version": "detector:v1:mvr_1",
            "predictions": [
                {
                    "result": [
                        {
                            "from_name": "label",
                            "to_name": "image",
                            "type": "rectanglelabels",
                            "value": {
                                "x": 10,
                                "y": 20,
                                "width": 30,
                                "height": 40,
                                "rectanglelabels": ["defect"],
                            },
                        }
                    ],
                    "score": 0.9,
                    "model_version": "detector:v1:mvr_1",
                }
            ],
        }

    def import_tasks(self, project_id: int, tasks: list[dict[str, object]]) -> dict[str, object]:
        self.__class__.calls.append({"project_id": project_id, "tasks": tasks})
        return {"ids": [7001]}


class _ExplodingLabelStudioClient:
    def __init__(self, **_kwargs: object) -> None:
        pass

    def predict_with_ml_backend(self, _backend_url: str, _tasks: list[dict[str, object]]) -> dict[str, object]:
        return {"predictions": []}

    def import_tasks(self, project_id: int, tasks: list[dict[str, object]]) -> dict[str, object]:
        raise RuntimeError("Label Studio connection dropped")


def test_manual_live_single_capture_imports_to_selected_label_studio_project(
    sqlite_engine: Engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
    monkeypatch: MonkeyPatch,
) -> None:
    settings = _configure_apps(sqlite_engine, tmp_path)
    image_path = tmp_path / "live-frame.png"
    image_path.write_bytes(sample_png_bytes)
    channel_id, target_id = _seed_channel_and_project(settings, image_path=image_path)
    _FakeLabelStudioClient.calls = []
    _FakeLabelStudioClient.prediction_calls = []
    monkeypatch.setattr(
        "capture_service.executor.LabelStudioClient",
        _FakeLabelStudioClient,
    )

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, settings)

        targets = runtime_client.get(
            f"/api/v1/runtime/channels/{channel_id}/manual-capture/targets"
        )
        assert targets.status_code == 200
        assert targets.json()["items"][0]["display_name"] == "Bearing defects"
        assert "lpr_" not in targets.json()["items"][0]["display_name"]

        response = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/manual-capture/single",
            json={"capture_target_id": target_id},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 200
        assert response.json()["result"]["status"] == "queued"
        stats = drain_capture_work(settings=settings, client_factory=_FakeLabelStudioClient)
        assert stats.delivered == 1
        history = runtime_client.get(
            f"/api/v1/runtime/channels/{channel_id}/manual-capture/history"
        )

    payload = history.json()["items"][0]
    assert payload["status"] == "success"
    assert payload["mode"] == "single"
    assert payload["target_display_name"] == "Bearing defects"
    assert payload["label_studio_task_ids"] == [7001]
    assert _FakeLabelStudioClient.calls[0]["project_id"] == 42

    task = _FakeLabelStudioClient.calls[0]["tasks"][0]
    assert task["data"]["image"].startswith("/data/local-files/?d=")
    assert _FakeLabelStudioClient.prediction_calls[0]["backend_url"].endswith("/predict")
    prediction_task = _FakeLabelStudioClient.prediction_calls[0]["tasks"][0]
    assert prediction_task["data"]["image"].startswith("data:image/png;base64,")
    assert task["predictions"][0]["model_version"] == "detector:v1:mvr_1"
    assert task["meta"]["source"] == "runtime_capture"
    assert task["meta"]["capture_source_type"] == "manual"
    assert task["meta"]["channel_id"] == channel_id

    with session_scope(settings) as session:
        assert session.query(RuntimeManualCaptureRequestRecord).count() == 1
        assert session.query(RuntimeCaptureRequest).count() == 1
        request = session.query(RuntimeCaptureRequest).first()
        assert request.status == "predicted"
        assert request.metadata_json["prediction_status"] == "predicted"
        if os.name != "nt":
            assert Path(request.asset_uri).stat().st_mode & stat.S_IROTH
        assert (
            session.query(AuditEvent)
            .filter(AuditEvent.event_type == "runtime.capture.delivered.v1")
            .count()
            == 0
        )
        assert (
            session.query(AuditEvent)
            .filter(AuditEvent.event_type == "runtime.capture.predicted.v1")
            .count()
            == 1
        )


def test_manual_live_single_capture_can_skip_predictions(
    sqlite_engine: Engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
    monkeypatch: MonkeyPatch,
) -> None:
    settings = _configure_apps(sqlite_engine, tmp_path)
    image_path = tmp_path / "live-frame.png"
    image_path.write_bytes(sample_png_bytes)
    channel_id, target_id = _seed_channel_and_project(settings, image_path=image_path)
    _FakeLabelStudioClient.calls = []
    _FakeLabelStudioClient.prediction_calls = []
    monkeypatch.setattr(
        "capture_service.executor.LabelStudioClient",
        _FakeLabelStudioClient,
    )

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, settings)
        response = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/manual-capture/single",
            json={"capture_target_id": target_id, "request_predictions": False},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 200

    stats = drain_capture_work(settings=settings, client_factory=_FakeLabelStudioClient)

    assert stats.delivered == 1
    assert _FakeLabelStudioClient.prediction_calls == []
    task = _FakeLabelStudioClient.calls[0]["tasks"][0]
    assert "predictions" not in task
    with session_scope(settings) as session:
        request = session.query(RuntimeCaptureRequest).one()
        assert request.status == "delivered"
        assert request.metadata_json["prediction_status"] == "disabled"


def test_manual_live_capture_preserves_crop_offsets_from_pipeline_live_view(
    sample_png_base64: str,
    monkeypatch: MonkeyPatch,
) -> None:
    image_context = {
        "context_id": "ctx_crop_1",
        "kind": "crop",
        "width": 320,
        "height": 200,
        "source_image_context_id": "ctx_original_1",
        "parent_image_context_id": "ctx_original_1",
        "transform": {
            "transform_type": "crop",
            "coordinate_projection": "crop_offset",
            "parameters": {
                "x": 137,
                "y": 83,
                "width": 320,
                "height": 200,
                "source_width": 1280,
                "source_height": 720,
            },
        },
    }
    live_state = SimpleNamespace(
        available=True,
        stale=False,
        runtime_instance_id="rte_crop_1",
        frame=SimpleNamespace(
            image_base64=sample_png_base64,
            mime_type="image/png",
            width=320,
            height=200,
            metadata={"source_uri": "camera://press"},
            source_timestamp="2026-08-04T08:00:00Z",
            captured_at="2026-08-04T08:00:00Z",
            sequence=19,
            image_context=image_context,
        ),
    )
    monkeypatch.setattr(
        "capture_service.executor.get_runtime_live_view_state",
        lambda *_args, **_kwargs: live_state,
    )

    resolved = _latest_live_frame(
        SimpleNamespace(),
        SimpleNamespace(),
        "chn_crop_1",
    )

    assert resolved is not None
    frame, source_metadata, runtime_execution_id = resolved
    assert frame.image_context is not None
    assert frame.image_context.transform.parameters["x"] == 137
    assert frame.image_context.transform.parameters["y"] == 83
    assert source_metadata["crop"] == {
        "x": 137,
        "y": 83,
        "width": 320,
        "height": 200,
        "source_width": 1280,
        "source_height": 720,
    }
    assert source_metadata["image_context"]["context_id"] == "ctx_crop_1"
    assert runtime_execution_id == "rte_crop_1"


def test_manual_capture_pipeline_image_output_requires_profile_and_eligibility(
    sample_png_base64: str,
    monkeypatch: MonkeyPatch,
) -> None:
    live_state = SimpleNamespace(
        available=True,
        stale=False,
        runtime_instance_id="rte_canonical_1",
        frame=SimpleNamespace(
            image_base64=sample_png_base64,
            mime_type="image/png",
            width=1600,
            height=640,
            metadata={
                "canonical_model_view": True,
                "view_profile_id": "stem-measurement-view-v1",
                "view_profile_checksum": "a" * 64,
            },
            source_timestamp="2026-08-04T08:00:00Z",
            captured_at="2026-08-04T08:00:00Z",
            sequence=20,
            image_context={},
        ),
        overlay=[
            SimpleNamespace(
                layer="stem_capture_eligibility",
                metadata={"status": "ACCEPTED", "reason_codes": []},
            )
        ],
    )
    monkeypatch.setattr(
        "capture_service.executor.get_runtime_live_view_state",
        lambda *_args, **_kwargs: live_state,
    )

    resolved = _latest_live_frame(
        SimpleNamespace(),
        SimpleNamespace(),
        "chn_canonical_1",
        require_pipeline_image_output=True,
        expected_view_profile_id="stem-measurement-view-v1",
        require_eligibility=True,
    )

    assert resolved is not None
    frame, metadata, runtime_execution_id = resolved
    assert frame.width == 1600
    assert frame.height == 640
    assert metadata["source"] == "pipeline_image_output"
    assert metadata["view_profile_id"] == "stem-measurement-view-v1"
    assert metadata["eligibility_status"] == "ACCEPTED"
    assert metadata["eligibility_verified"] is True
    assert runtime_execution_id == "rte_canonical_1"


def test_manual_canonical_bootstrap_capture_records_unavailable_eligibility(
    sample_png_base64: str,
    monkeypatch: MonkeyPatch,
) -> None:
    live_state = SimpleNamespace(
        available=True,
        stale=False,
        runtime_instance_id="rte_canonical_bootstrap",
        frame=SimpleNamespace(
            image_base64=sample_png_base64,
            mime_type="image/png",
            width=1600,
            height=640,
            metadata={
                "canonical_model_view": True,
                "view_profile_id": "stem-measurement-view-v1",
                "view_profile_checksum": "a" * 64,
            },
            source_timestamp="2026-08-06T08:00:00Z",
            captured_at="2026-08-06T08:00:00Z",
            sequence=22,
            image_context={},
        ),
        overlay=[],
    )
    monkeypatch.setattr(
        "capture_service.executor.get_runtime_live_view_state",
        lambda *_args, **_kwargs: live_state,
    )

    resolved = _latest_live_frame(
        SimpleNamespace(),
        SimpleNamespace(),
        "chn_canonical_bootstrap",
        require_pipeline_image_output=True,
        expected_view_profile_id="stem-measurement-view-v1",
        require_eligibility=False,
    )

    assert resolved is not None
    _, metadata, _ = resolved
    assert metadata["source"] == "pipeline_image_output"
    assert metadata["eligibility_status"] == "UNAVAILABLE"
    assert metadata["eligibility_verified"] is False
    assert metadata["eligibility_reason_codes"] == []


def test_manual_capture_pipeline_image_output_never_falls_back_on_mismatch(
    sample_png_base64: str,
    monkeypatch: MonkeyPatch,
) -> None:
    live_state = SimpleNamespace(
        available=True,
        stale=False,
        runtime_instance_id="rte_canonical_rejected",
        frame=SimpleNamespace(
            image_base64=sample_png_base64,
            mime_type="image/png",
            width=1600,
            height=640,
            metadata={
                "canonical_model_view": True,
                "view_profile_id": "different-view-v1",
            },
            source_timestamp="2026-08-04T08:00:00Z",
            captured_at="2026-08-04T08:00:00Z",
            sequence=21,
            image_context={},
        ),
        overlay=[
            SimpleNamespace(
                layer="stem_capture_eligibility",
                metadata={"status": "REJECTED"},
            )
        ],
    )
    monkeypatch.setattr(
        "capture_service.executor.get_runtime_live_view_state",
        lambda *_args, **_kwargs: live_state,
    )

    assert (
        _latest_live_frame(
            SimpleNamespace(),
            SimpleNamespace(),
            "chn_canonical_rejected",
            require_pipeline_image_output=True,
            expected_view_profile_id="stem-measurement-view-v1",
            require_eligibility=True,
        )
        is None
    )


def test_manual_live_capture_records_unexpected_delivery_failure_instead_of_sticking_processing(
    sqlite_engine: Engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    settings = _configure_apps(sqlite_engine, tmp_path)
    image_path = tmp_path / "live-frame.png"
    image_path.write_bytes(sample_png_bytes)
    channel_id, target_id = _seed_channel_and_project(settings, image_path=image_path)

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, settings)
        response = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/manual-capture/single",
            json={"capture_target_id": target_id},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 200

    stats = drain_capture_work(settings=settings, client_factory=_ExplodingLabelStudioClient)

    assert stats.failed_retryable == 1
    with session_scope(settings) as session:
        row = session.query(RuntimeCaptureRequest).one()
        manual = session.query(RuntimeManualCaptureRequestRecord).one()
        assert row.status == "failed_retryable"
        assert row.next_attempt_at is not None
        assert "Unexpected capture delivery failure" in str(row.last_error)
        assert manual.status == "failed"
        assert manual.error_summary == row.last_error


def test_stale_processing_manual_live_capture_is_reclaimed_and_delivered(
    sqlite_engine: Engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    settings = _configure_apps(sqlite_engine, tmp_path)
    image_path = tmp_path / "live-frame.png"
    image_path.write_bytes(sample_png_bytes)
    channel_id, target_id = _seed_channel_and_project(settings, image_path=image_path)
    _FakeLabelStudioClient.calls = []
    _FakeLabelStudioClient.prediction_calls = []

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, settings)
        response = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/manual-capture/single",
            json={"capture_target_id": target_id},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 200

    with session_scope(settings) as session:
        row = session.query(RuntimeCaptureRequest).one()
        row.status = "processing"
        row.claimed_at = datetime.now(tz=UTC) - timedelta(minutes=5)

    stats = drain_capture_work(settings=settings, client_factory=_FakeLabelStudioClient)

    assert stats.delivered == 1
    assert _FakeLabelStudioClient.calls[0]["project_id"] == 42
    with session_scope(settings) as session:
        row = session.query(RuntimeCaptureRequest).one()
        manual = session.query(RuntimeManualCaptureRequestRecord).one()
        assert row.status == "predicted"
        assert row.metadata_json["prediction_status"] == "predicted"
        assert row.retry_count == 1
        assert manual.status == "success"
        assert manual.label_studio_task_ids == [7001]


def test_manual_live_interval_capture_start_stop_is_backend_session_controlled(
    sqlite_engine: Engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
    monkeypatch: MonkeyPatch,
) -> None:
    settings = _configure_apps(sqlite_engine, tmp_path)
    image_path = tmp_path / "live-frame.png"
    image_path.write_bytes(sample_png_bytes)
    channel_id, target_id = _seed_channel_and_project(settings, image_path=image_path)
    _FakeLabelStudioClient.calls = []
    _FakeLabelStudioClient.prediction_calls = []
    monkeypatch.setattr(
        "capture_service.executor.LabelStudioClient",
        _FakeLabelStudioClient,
    )

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, settings)

        start = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/manual-capture/interval-session",
            json={"capture_target_id": target_id, "interval_seconds": 1},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert start.status_code == 202
        assert start.json()["session"]["state"] == "running"

        time.sleep(1.1)
        stats = drain_capture_work(settings=settings, client_factory=_FakeLabelStudioClient)
        assert stats.enqueued_interval_captures == 1
        assert stats.delivered == 1
        history = _wait_for_manual_capture_history(runtime_client, channel_id)
        assert history["items"][0]["mode"] == "interval"
        assert history["items"][0]["status"] == "success"

        stop = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/manual-capture/interval-session:stop",
            json={"reason": "test stop"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert stop.status_code == 200
        assert stop.json()["session"]["state"] == "stopped"
        assert stop.json()["session"]["capture_count"] >= 1


def test_manual_live_capture_rejects_invalid_interval_values(
    sqlite_engine: Engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    settings = _configure_apps(sqlite_engine, tmp_path)
    image_path = tmp_path / "live-frame.png"
    image_path.write_bytes(sample_png_bytes)
    channel_id, target_id = _seed_channel_and_project(settings, image_path=image_path)

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, settings)

        response = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/manual-capture/interval-session",
            json={"capture_target_id": target_id, "interval_seconds": 0},
            headers={"X-CSRF-Token": csrf_token},
        )

    assert response.status_code == 422


def test_manual_live_capture_rejects_unavailable_label_studio_project(
    sqlite_engine: Engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    settings = _configure_apps(sqlite_engine, tmp_path)
    image_path = tmp_path / "live-frame.png"
    image_path.write_bytes(sample_png_bytes)
    channel_id, target_id = _seed_channel_and_project(
        settings,
        image_path=image_path,
        project_status=LabelProjectStatus.NEEDS_ATTENTION,
    )

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, settings)

        response = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/manual-capture/single",
            json={"capture_target_id": target_id},
            headers={"X-CSRF-Token": csrf_token},
        )

    assert response.status_code == 422
    assert "not available" in response.json()["detail"]


def test_pipeline_capture_intents_deliver_to_multiple_label_studio_projects(
    sqlite_engine: Engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    settings = _configure_apps(sqlite_engine, tmp_path)
    image_path = tmp_path / "live-frame.png"
    image_path.write_bytes(sample_png_bytes)
    channel_id, first_target_id = _seed_channel_and_project(settings, image_path=image_path)
    second_target_id = _seed_additional_project(settings, project_id=43, title="Bearing classifier")
    frame = FramePayload.from_bytes(sample_png_bytes, mime_type="image/png", width=1, height=1)
    _FakeLabelStudioClient.calls = []
    _FakeLabelStudioClient.prediction_calls = []

    with session_scope(settings) as session:
        result = RuntimeExecutionResult(
            status="completed",
            channel_id=channel_id,
            pipeline_version_id="plv_capture",
            runtime_execution_id="rte_capture",
            correlation_id="corr_capture",
            started_at="2026-05-01T10:00:00Z",
            completed_at="2026-05-01T10:00:01Z",
            capture_intents=[
                {
                    "node_key": "full_capture",
                    "node_type": "capture.full_frame_labelstudio",
                    "node_category": "capture",
                    "output_port": "capture",
                    "payload_type": "capture_intents",
                    "payload": {},
                    "intent": {
                        "capture_mode": "full_frame",
                        "target_project_ids": [first_target_id, second_target_id],
                        "asset": frame.model_dump(mode="json"),
                        "condition": None,
                        "metadata": {"node_key": "full_capture"},
                    },
                }
            ],
        )
        record_runtime_execution_result(session, result, channel_id=channel_id)

    stats = drain_capture_work(settings=settings, client_factory=_FakeLabelStudioClient)

    assert stats.delivered == 2
    assert sorted(call["project_id"] for call in _FakeLabelStudioClient.calls) == [42, 43]
    with session_scope(settings) as session:
        rows = (
            session.query(RuntimeCaptureRequest)
            .order_by(RuntimeCaptureRequest.label_studio_project_id)
            .all()
        )
        assert [row.status for row in rows] == ["predicted", "delivered"]
        assert rows[0].metadata_json["prediction_status"] == "predicted"
        assert rows[1].metadata_json["prediction_status"] == "not_configured"
        assert {row.source_type for row in rows} == {"pipeline"}


def test_pipeline_roi_capture_materializes_roi_metadata(
    sqlite_engine: Engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    settings = _configure_apps(sqlite_engine, tmp_path)
    image_path = tmp_path / "live-frame.png"
    image_path.write_bytes(sample_png_bytes)
    channel_id, target_id = _seed_channel_and_project(settings, image_path=image_path)
    frame = FramePayload.from_bytes(sample_png_bytes, mime_type="image/png", width=1, height=1)
    roi = RoiPayload(
        roi_key="defect_1",
        frame=frame,
        box=DetectionBox(x_min=0, y_min=0, x_max=1, y_max=1),
        label="defect",
        source_region_key="detection_1",
        metadata={"score": 0.92},
    )
    _FakeLabelStudioClient.calls = []
    _FakeLabelStudioClient.prediction_calls = []

    with session_scope(settings) as session:
        result = RuntimeExecutionResult(
            status="completed",
            channel_id=channel_id,
            pipeline_version_id="plv_roi_capture",
            runtime_execution_id="rte_roi_capture",
            correlation_id="corr_roi_capture",
            started_at="2026-05-01T10:00:00Z",
            completed_at="2026-05-01T10:00:01Z",
            capture_intents=[
                {
                    "node_key": "roi_capture",
                    "node_type": "capture.roi_labelstudio",
                    "node_category": "capture",
                    "output_port": "capture",
                    "payload_type": "capture_intents",
                    "payload": {},
                    "intent": {
                        "capture_mode": "roi",
                        "target_project_ids": [target_id],
                        "asset": roi.model_dump(mode="json"),
                        "condition": None,
                        "metadata": {"node_key": "roi_capture"},
                    },
                }
            ],
        )
        record_runtime_execution_result(session, result, channel_id=channel_id)

    stats = drain_capture_work(settings=settings, client_factory=_FakeLabelStudioClient)

    assert stats.delivered == 1
    task = _FakeLabelStudioClient.calls[0]["tasks"][0]
    assert task["meta"]["capture_mode"] == "roi"
    assert task["meta"]["roi"]["roi_key"] == "defect_1"
    assert task["meta"]["source_frame"]["source"] == "pipeline_roi"


def test_pipeline_capture_unavailable_project_fails_without_delivery(
    sqlite_engine: Engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    settings = _configure_apps(sqlite_engine, tmp_path)
    image_path = tmp_path / "live-frame.png"
    image_path.write_bytes(sample_png_bytes)
    channel_id, target_id = _seed_channel_and_project(
        settings,
        image_path=image_path,
        project_status=LabelProjectStatus.NEEDS_ATTENTION,
    )
    frame = FramePayload.from_bytes(sample_png_bytes, mime_type="image/png", width=1, height=1)
    _FakeLabelStudioClient.calls = []
    _FakeLabelStudioClient.prediction_calls = []

    with session_scope(settings) as session:
        result = RuntimeExecutionResult(
            status="completed",
            channel_id=channel_id,
            pipeline_version_id="plv_capture_unavailable",
            correlation_id="corr_capture_unavailable",
            started_at="2026-05-01T10:00:00Z",
            completed_at="2026-05-01T10:00:01Z",
            capture_intents=[
                {
                    "node_key": "full_capture",
                    "node_type": "capture.full_frame_labelstudio",
                    "node_category": "capture",
                    "output_port": "capture",
                    "payload_type": "capture_intents",
                    "payload": {},
                    "intent": {
                        "capture_mode": "full_frame",
                        "target_project_ids": [target_id],
                        "asset": frame.model_dump(mode="json"),
                        "condition": None,
                        "metadata": {"node_key": "full_capture"},
                    },
                }
            ],
        )
        record_runtime_execution_result(session, result, channel_id=channel_id)

    stats = drain_capture_work(settings=settings, client_factory=_FakeLabelStudioClient)

    assert stats.processed == 0
    assert _FakeLabelStudioClient.calls == []
    with session_scope(settings) as session:
        row = session.query(RuntimeCaptureRequest).one()
        assert row.status == "failed_terminal"
        assert "not available" in row.last_error


def test_pipeline_capture_rejects_raw_label_studio_project_ids(
    sqlite_engine: Engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    settings = _configure_apps(sqlite_engine, tmp_path)
    image_path = tmp_path / "live-frame.png"
    image_path.write_bytes(sample_png_bytes)
    channel_id, _target_id = _seed_channel_and_project(settings, image_path=image_path)
    frame = FramePayload.from_bytes(sample_png_bytes, mime_type="image/png", width=1, height=1)
    _FakeLabelStudioClient.calls = []
    _FakeLabelStudioClient.prediction_calls = []

    with session_scope(settings) as session:
        result = RuntimeExecutionResult(
            status="completed",
            channel_id=channel_id,
            pipeline_version_id="plv_capture_raw_project_id",
            correlation_id="corr_capture_raw_project_id",
            started_at="2026-05-01T10:00:00Z",
            completed_at="2026-05-01T10:00:01Z",
            capture_intents=[
                {
                    "node_key": "full_capture",
                    "node_type": "capture.full_frame_labelstudio",
                    "node_category": "capture",
                    "output_port": "capture",
                    "payload_type": "capture_intents",
                    "payload": {},
                    "intent": {
                        "capture_mode": "full_frame",
                        "target_project_ids": ["42"],
                        "asset": frame.model_dump(mode="json"),
                        "condition": None,
                        "metadata": {"node_key": "full_capture"},
                    },
                }
            ],
        )
        record_runtime_execution_result(session, result, channel_id=channel_id)

    stats = drain_capture_work(settings=settings, client_factory=_FakeLabelStudioClient)

    assert stats.processed == 0
    assert _FakeLabelStudioClient.calls == []
    with session_scope(settings) as session:
        row = session.query(RuntimeCaptureRequest).one()
        assert row.status == "failed_terminal"
        assert "Select a synced platform project" in row.last_error


def test_capture_storage_quota_stops_new_files(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    settings = build_test_settings("capture-service", tmp_path)
    capture_root = settings.storage.runtime_capture_root()
    capture_root.mkdir(parents=True)
    settings.runtime.capture_storage_max_bytes = 1_073_741_824
    settings.runtime.capture_min_free_bytes = 1_073_741_824
    monkeypatch.setattr(
        "capture_service.executor.shutil.disk_usage",
        lambda _path: SimpleNamespace(
            total=10_737_418_240,
            used=1_073_741_824,
            free=9_663_676_416,
        ),
    )
    monkeypatch.setattr(
        "capture_service.executor._capture_storage_used_bytes",
        lambda _root, *, cache_seconds: 1_073_741_824,
    )

    with pytest.raises(PlatformError) as exc_info:
        _assert_capture_storage_available(
            settings=settings,
            capture_root=capture_root,
            incoming_bytes=1,
        )

    assert exc_info.value.code == "runtime_capture_storage_quota_reached"
    assert exc_info.value.status_code == 507
    assert exc_info.value.retryable is False


def test_redundant_capture_file_is_removed_within_capture_root(tmp_path: Path) -> None:
    capture_root = tmp_path / "runtime" / "captures"
    asset_path = capture_root / "channel" / "duplicate.png"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"duplicate")
    asset = StoredCaptureAsset(
        path=asset_path,
        uri=str(asset_path),
        digest="sha256",
        mime_type="image/png",
        width=1,
        height=1,
        source_frame_metadata={},
    )

    _remove_redundant_capture_asset(asset=asset, capture_root=capture_root)

    assert not asset_path.exists()


def _configure_apps(sqlite_engine: Engine, tmp_path: Path) -> PlatformSettings:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api", "capture-service")
    settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_settings = build_test_settings("admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = settings
    return settings


def _seed_channel_and_project(
    settings: PlatformSettings,
    *,
    image_path: Path,
    project_status: LabelProjectStatus = LabelProjectStatus.ACTIVE,
) -> tuple[str, str]:
    with session_scope(settings) as session:
        dataset = Dataset(
            id="dst_manual_capture",
            slug="manual-capture",
            name="Manual Capture",
            task_type=TaskType.DETECTION.value,
            label_schema_version="ls-test",
            status=DatasetStatus.READY.value,
        )
        project = LabelProject(
            id="lpr_manual_capture",
            dataset_id=dataset.id,
            label_studio_project_id=42,
            title="Bearing defects",
            task_type=TaskType.DETECTION.value,
            status=project_status.value,
            label_config={},
            prediction_backend_enabled=True,
            prediction_backend_url="http://label-studio-ml-adapter/api/v1/projects/lpr_manual_capture",
        )
        channel = create_runtime_channel(
            session,
            RuntimeChannelCreateRequest(
                name="Press live camera",
                technical_place="press-line-1",
                source_type=RuntimeSourceType.FILE_TEST,
                source_config={"path": str(image_path)},
                mode=RuntimeMode.REALTIME,
            ),
            created_by="tester",
        )
        session.add_all([dataset, project])
        session.flush()
        return channel.channel_id, project.id


def _seed_additional_project(
    settings: PlatformSettings,
    *,
    project_id: int,
    title: str,
) -> str:
    with session_scope(settings) as session:
        project = LabelProject(
            id=f"lpr_manual_capture_{project_id}",
            dataset_id="dst_manual_capture",
            label_studio_project_id=project_id,
            title=title,
            task_type=TaskType.DETECTION.value,
            status=LabelProjectStatus.ACTIVE.value,
            label_config={},
        )
        session.add(project)
        session.flush()
        return project.id


def _wait_for_manual_capture_history(
    runtime_client: TestClient,
    channel_id: str,
) -> dict[str, object]:
    response = None
    for _ in range(30):
        response = runtime_client.get(
            f"/api/v1/runtime/channels/{channel_id}/manual-capture/history"
        )
        payload = response.json()
        if payload["items"]:
            return payload
        time.sleep(0.1)
    assert response is not None
    return response.json()


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "adminadmin"},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def _copy_auth_cookies(
    source_client: TestClient,
    target_client: TestClient,
    settings: PlatformSettings,
) -> None:
    target_client.cookies.set(
        settings.session.cookie_name,
        source_client.cookies.get(settings.session.cookie_name),
    )
    target_client.cookies.set(
        settings.session.csrf_cookie_name,
        source_client.cookies.get(settings.session.csrf_cookie_name),
    )
