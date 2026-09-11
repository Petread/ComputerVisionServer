from __future__ import annotations

import base64
import importlib.util
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from admin_api.main import app as admin_app
from cv_contracts.runtime import (
    RuntimeCameraCalibrationObservationResult,
    RuntimeGraphEdge,
    RuntimeLiveFrame,
    RuntimeLogicalModelRef,
    RuntimeNodeSpec,
    RuntimePipelineDefinitionCreateRequest,
    RuntimePipelineGraph,
    RuntimePipelineVersionCreateRequest,
)
from cv_core import PlatformSettings
from cv_db import (
    PipelineDefinition,
    PipelineVersion,
    ResultRecord,
    RuntimeChannel,
    RuntimeControlCommandRecord,
    RuntimeExecution,
    RuntimeSource,
    TriggerEndpoint,
    create_pipeline_definition,
    create_pipeline_version,
    publish_pipeline_version,
    session_scope,
)
from cv_runtime import (
    CameraCalibrationComputation,
    LatestFrameRingWriter,
    StemCurvatureProcessCalibrationComputation,
)
from cv_status import RuntimeMode, RuntimeNodeCategory, RuntimeSourceType, TargetPath, TaskType
from fastapi.testclient import TestClient
from runtime_admin_api.api import routes as runtime_admin_routes
from runtime_admin_api.main import app as runtime_admin_app
from sqlalchemy import select
from sqlalchemy.engine import Engine
from tests.conftest import build_test_settings, configure_sqlite_factories
from tests.support.runtime_model_seed import seed_deployed_runtime_model
from tests.unit.backend.test_stem_minimum_principle_validation import (
    _request as minimum_principle_validation_request,
)
from tests.unit.backend.test_stem_process_coordinates import process_calibration_request


def test_runtime_channel_crud_filtering_commands_and_audit(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings
    _seed_published_pipeline(runtime_settings, name="Press defects", mode=RuntimeMode.REALTIME)

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)

        created = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Press line camera",
                "technical_place": "press-line-1",
                "description": "Main camera over the press line",
                "source_type": "rtsp",
                "source_config": {"url": "rtsp://camera.local/press"},
                "mode": "realtime",
                "assigned_pipeline": "Press defects",
                "output_summary": {
                    "summary": "Overlay",
                    "connector_count": 1,
                    "primary_outputs": ["overlay"],
                },
                "capture_summary": {
                    "summary": "Capture rejects",
                    "rule_count": 1,
                    "target_project_count": 1,
                    "modes": ["roi"],
                },
            },
            headers={"X-CSRF-Token": csrf_token},
        )

        assert created.status_code == 201
        channel = created.json()
        channel_id = channel["channel_id"]
        assert channel["source_type"] == "rtsp"
        assert channel["source_summary"] == "rtsp://camera.local/press"
        assert channel["assigned_pipeline"] == "Press defects"
        assert channel["status"] == "stopped"
        channel_status = runtime_client.get(f"/api/v1/runtime/channels/{channel_id}/status")
        assert channel_status.status_code == 200
        channel_status_body = channel_status.json()
        assert channel_status_body["channel_id"] == channel_id
        assert channel_status_body["status"] == "stopped"
        assert "source_config" not in channel_status_body
        assert "request_endpoints" not in channel_status_body

        streams = runtime_client.get(f"/api/v1/runtime/channels/{channel_id}/streams")
        assert streams.status_code == 200
        stream_body = streams.json()
        assert stream_body["schema_version"] == "runtime.channel-streams.list.v2"
        assert stream_body["desired_state"] == "stopped"
        assert stream_body["actual_state"] == "stopped"
        assert list(stream_body["components"]) == [
            "camera",
            "encoder",
            "publication",
            "browser_readers",
        ]
        assert len(stream_body["items"]) == 2
        assert stream_body["items"][0]["variant"] == "raw"
        assert len(stream_body["items"][0]["endpoints"]) == 1
        stream_endpoint = stream_body["items"][0]["endpoints"][0]
        assert stream_endpoint["protocol"] == "webrtc"
        assert stream_endpoint["url"] == (
            "/stream-webrtc/channels/press-line-camera/live/whep"
        )
        assert stream_endpoint["browser_compatible"] is True
        assert stream_endpoint["vlc_compatible"] is False
        canonical_stream = stream_body["items"][1]
        assert canonical_stream["variant"] == "canonical_model"
        assert canonical_stream["status"] == "unavailable"
        assert canonical_stream["ready"] is False
        assert canonical_stream["endpoints"][0]["url"] == (
            "/stream-webrtc/channels/press-line-camera/model/whep"
        )

        stream_start = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/stream/start",
            headers={"X-CSRF-Token": csrf_token},
        )
        assert stream_start.status_code == 202
        assert stream_start.json()["desired_state"] == "running"
        assert stream_start.json()["actual_state"] == "starting"

        stream_stop = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/stream/stop",
            headers={"X-CSRF-Token": csrf_token},
        )
        assert stream_stop.status_code == 202
        assert stream_stop.json()["desired_state"] == "stopped"
        assert stream_stop.json()["actual_state"] == "stopped"

        trigger_source = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Packing station camera",
                "technical_place": "packing-station",
                "source_type": "snapshot",
                "source_config": {"url": "https://camera.local/reject-snapshot.jpg"},
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert trigger_source.status_code == 201
        trigger_channel_id = trigger_source.json()["channel_id"]
        trigger_pipeline_version_id = _seed_published_pipeline(
            runtime_settings,
            name="Reject decision",
            mode=RuntimeMode.TRIGGER,
            channel_id=trigger_channel_id,
        )

        endpoint_list = runtime_client.get(
            "/api/v1/runtime/trigger-endpoints",
            params={"channel_id": trigger_channel_id},
        )
        assert endpoint_list.status_code == 200
        endpoint_body = endpoint_list.json()["items"][0]
        assert endpoint_body["name"] == "Reject decision HTTP trigger"
        assert endpoint_body["derived_from_pipeline"] is True
        assert endpoint_body["pipeline_version_id"] == trigger_pipeline_version_id
        assert endpoint_body["path"].startswith("/api/v1/runtime/triggers/")
        assert endpoint_body["readiness"]["ready"] is True

        trigger_channel_detail = runtime_client.get(
            f"/api/v1/runtime/channels/{trigger_channel_id}",
        )
        assert trigger_channel_detail.status_code == 200
        linked_pipelines = trigger_channel_detail.json()["triggerable_pipelines"]
        assert [item["name"] for item in linked_pipelines] == ["Reject decision HTTP trigger"]

        filtered = runtime_client.get(
            "/api/v1/runtime/channels",
            params={"search": "press", "source_type": "rtsp", "mode": "realtime"},
        )
        assert filtered.status_code == 200
        assert [item["channel_id"] for item in filtered.json()["items"]] == [channel_id]

        status = runtime_client.get("/api/v1/runtime/channels/status")
        assert status.status_code == 200
        assert status.json()["counts"]["total"] == 2
        assert status.json()["counts"]["stopped"] == 2
        assert "press-line-1" in status.json()["technical_places"]

        runtime_metrics = runtime_client.get("/api/v1/runtime/metrics")
        assert runtime_metrics.status_code == 200
        assert "cv_runtime_channels_total" in runtime_metrics.text

        system_status = admin_client.get("/api/v1/system/status")
        assert system_status.status_code == 200
        system_body = system_status.json()
        assert system_body["runtime"]["total_channels"] == 2
        assert any(card["key"] == "runtime_channels" for card in system_body["cards"])
        runtime_attention_card = next(
            card for card in system_body["cards"] if card["key"] == "runtime_attention"
        )
        assert runtime_attention_card["value"] == "0"

        start = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}:start",
            json={"reason": "operator test"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert start.status_code == 202
        assert start.json()["channel"]["desired_state"] == "start_requested"
        assert start.json()["accepted_job"]["queue"] == "runtime-control"

        stop = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}:stop",
            json={},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert stop.status_code == 202
        assert stop.json()["channel"]["desired_state"] == "stop_requested"

        audit_events = admin_client.get(
            "/api/v1/audit-events",
            params={"aggregate_type": "runtime_channel", "aggregate_id": channel_id, "limit": 20},
        )
        audit_types = {event["event_type"] for event in audit_events.json()["items"]}
        assert "runtime.channel.created.v1" in audit_types
        assert "runtime.channel.start.requested.v1" in audit_types
        assert "runtime.channel.stop.requested.v1" in audit_types

        legacy_trigger_channel = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Legacy trigger-mode channel",
                "technical_place": "packing-station",
                "source_type": "snapshot",
                "source_config": {"url": "https://camera.local/legacy.jpg"},
                "mode": "trigger",
                "pipeline_version_id": trigger_pipeline_version_id,
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert legacy_trigger_channel.status_code == 422
        assert "HTTP Trigger Input" in legacy_trigger_channel.json()["detail"]


def test_runtime_channel_validation_rejects_inline_secrets(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)

        response = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Secret stream",
                "technical_place": "press-line-2",
                "source_type": "rtsp",
                "source_config": {
                    "url": "rtsp://camera.local/stream",
                    "password": "plain-text",
                },
                "mode": "realtime",
                "assigned_pipeline": "Inspection",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        snapshot_response = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Unsafe snapshot",
                "technical_place": "press-line-3",
                "source_type": "snapshot",
                "source_config": {"path": "C:/unsafe/latest.jpg"},
                "mode": "realtime",
                "assigned_pipeline": "Inspection",
            },
            headers={"X-CSRF-Token": csrf_token},
        )

    assert response.status_code == 422
    assert snapshot_response.status_code == 422
    assert "server-local paths" in snapshot_response.json()["detail"]


def test_runtime_channel_test_file_upload_configures_managed_file_source(
    sqlite_engine: Engine,
    tmp_path: Path,
    sample_png_bytes: bytes,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)

        upload = runtime_client.post(
            "/api/v1/runtime/channel-test-files",
            files={"file": ("press-sample.png", sample_png_bytes, "image/png")},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert upload.status_code == 201
        upload_body = upload.json()
        source_config = upload_body["source_config"]
        assert Path(upload_body["path"]).is_file()
        assert Path(upload_body["path"]).is_relative_to(runtime_settings.storage.runtime_test_files_root())
        assert source_config["managed_test_file"] is True
        assert source_config["file_name"] == "press-sample.png"

        created = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Test file camera",
                "technical_place": "pipeline-lab",
                "source_type": "file_test",
                "source_config": source_config,
                "mode": "realtime",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert created.status_code == 201
        channel = created.json()
        assert channel["source_type"] == "file_test"
        assert channel["source_summary"] == "press-sample.png"

        _seed_published_pipeline(
            runtime_settings,
            name="Test file trigger",
            mode=RuntimeMode.TRIGGER,
            channel_id=channel["channel_id"],
        )
        endpoint_list = runtime_client.get(
            "/api/v1/runtime/trigger-endpoints",
            params={"channel_id": channel["channel_id"]},
        )
        assert endpoint_list.status_code == 200
        readiness = endpoint_list.json()["items"][0]["readiness"]
        assert readiness["ready"] is True
        assert "managed channel test file" in readiness["reason"]


def test_runtime_channel_accepts_flir_blackfly_source(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)

        response = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Blackfly inspection camera",
                "technical_place": "press-line-blackfly",
                "source_type": "flir_blackfly",
                "source_config": {
                    "serial_number": "22334455",
                    "pixel_format": "BayerRG8",
                    "max_fps": "25",
                    "sample_every_n": "2",
                    "pipeline_max_inflight": "1",
                },
                "mode": "realtime",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
    assert response.status_code == 201
    body = response.json()
    assert body["source_type"] == "flir_blackfly"
    assert body["source_summary"] == "FLIR Blackfly 22334455"
    assert body["source_config"]["serial_number"] == "22334455"
    assert body["source_config"]["max_fps"] == 25.0
    assert body["source_config"]["sample_every_n"] == 2
    assert body["source_config"]["pipeline_max_inflight"] == 1


def test_runtime_channel_accepts_usb_opencv_source(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)
        response = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "USB inspection camera",
                "technical_place": "press-line-usb",
                "source_type": "usb_opencv",
                "source_config": {
                    "device_index": "2",
                    "backend": "dshow",
                    "width": "1280",
                    "height": "720",
                    "fps": "30",
                    "fourcc": "mjpg",
                    "buffer_size": "2",
                    "max_fps": "25",
                },
                "mode": "realtime",
            },
            headers={"X-CSRF-Token": csrf_token},
        )

    assert response.status_code == 201
    body = response.json()
    assert body["source_type"] == "usb_opencv"
    assert body["source_summary"] == "USB camera 2"
    assert body["source_config"] == {
        "device_index": 2,
        "backend": "dshow",
        "width": 1280,
        "height": 720,
        "fps": 30.0,
        "fourcc": "MJPG",
        "buffer_size": 2,
        "max_fps": 25.0,
    }


def test_runtime_channel_rejects_unsafe_usb_device_path(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)
        response = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Unsafe USB camera",
                "technical_place": "press-line-usb",
                "source_type": "usb_opencv",
                "source_config": {"device_path": "/srv/video.mp4"},
            },
            headers={"X-CSRF-Token": csrf_token},
        )

    assert response.status_code == 422
    assert "device_path" in response.json()["detail"]


def test_runtime_channel_accepts_basler_pylon_source(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)

        response = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Basler inspection camera",
                "technical_place": "press-line-basler",
                "source_type": "basler_pylon",
                "source_config": {
                    "serial_number": "40123456",
                    "pixel_format": "BayerRG8",
                    "acquisition_mode": "Continuous",
                    "grab_strategy": "LatestImageOnly",
                    "fps": "51",
                    "exposure_time_us": "8000",
                    "gain_db": "1.5",
                    "packet_size": "1500",
                    "inter_packet_delay": "1000",
                    "buffer_count": "24",
                    "output_queue_size": "2",
                    "frame_retention_ms": "500",
                    "grab_failure_reconnect_threshold": "3",
                    "max_fps": "25",
                    "sample_every_n": "2",
                },
                "mode": "realtime",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        channel_id = response.json()["channel_id"]
        stream_start = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/stream/start",
            headers={"X-CSRF-Token": csrf_token},
        )

    assert response.status_code == 201
    assert stream_start.status_code == 202
    assert stream_start.json()["actual_state"] == "starting"
    body = response.json()
    assert body["source_type"] == "basler_pylon"
    assert body["source_summary"] == "Basler Pylon 40123456"
    config = body["source_config"]
    assert config["serial_number"] == "40123456"
    assert config["transport_mode"] == "auto"
    assert config["bridge_name"].startswith("basler-")
    assert config["rtsp_host"] == "127.0.0.1"
    assert config["rtsp_mount"].startswith("/")
    assert config["url"].startswith("rtsp://127.0.0.1:")
    assert config["pixel_format"] == "BayerRG8"
    assert config["acquisition_mode"] == "Continuous"
    assert config["grab_strategy"] == "LatestImageOnly"
    assert config["trigger_mode"] == "Off"
    assert config["fps"] == 51.0
    assert config["acquisition_frame_rate_enable"] is True
    assert config["exposure_time_us"] == 8000.0
    assert config["gain_db"] == 1.5
    assert config["packet_size"] == 1500
    assert config["inter_packet_delay"] == 1000
    assert config["buffer_count"] == 24
    assert config["output_queue_size"] == 2
    assert config["frame_retention_ms"] == 500
    assert config["grab_failure_reconnect_threshold"] == 3
    assert config["max_fps"] == 25.0
    assert config["sample_every_n"] == 2


def test_stopped_basler_channel_can_capture_one_calibration_frame_and_releases_camera(
    sqlite_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings

    image_bytes = b"\xff\xd8\xff\xd9"

    class FakePacket:
        image_bytes = image_bytes
        mime_type = "image/jpeg"
        width = 640
        height = 480
        source_timestamp = "2026-07-21T08:00:00Z"
        source_uri = "basler://40123456"
        sequence = 1
        metadata = {"camera": "test"}

        def ensure_encoded(self) -> FakePacket:
            return self

    class FakeSource:
        def __init__(self) -> None:
            self.closed = False
            self._read = False

        def read(self, _stop_event: object) -> FakePacket | None:
            if self._read:
                return None
            self._read = True
            return FakePacket()

        def close(self) -> None:
            self.closed = True

    fake_source = FakeSource()
    monkeypatch.setattr(
        runtime_admin_routes,
        "build_frame_source",
        lambda **_kwargs: fake_source,
    )

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)
        created = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Basler calibration camera",
                "technical_place": "board-calibration",
                "source_type": "basler_pylon",
                "source_config": {"serial_number": "40123456"},
                "mode": "realtime",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert created.status_code == 201
        channel_id = created.json()["channel_id"]

        captured = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/calibration-frame",
            headers={"X-CSRF-Token": csrf_token},
        )
        with session_scope(runtime_settings) as session:
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            channel.desired_state = "running"
            channel.observed_state = "running"
        rejected_while_running = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/calibration-frame",
            headers={"X-CSRF-Token": csrf_token},
        )

    assert captured.status_code == 200
    state = captured.json()["state"]
    assert state["available"] is True
    assert state["actual_state"] == "stopped"
    assert state["frame"]["image_base64"] == base64.b64encode(image_bytes).decode("ascii")
    assert state["frame"]["metadata"]["camera_released_after_capture"] is True
    assert fake_source.closed is True
    assert rejected_while_running.status_code == 409
    assert rejected_while_running.json()["code"] == "runtime_calibration_camera_in_use"


def test_running_camera_stream_supplies_calibration_frames_without_a_valid_runtime_pipeline(
    sqlite_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings
    stream_frame = RuntimeLiveFrame(
        image_base64=base64.b64encode(b"stream-frame").decode("ascii"),
        mime_type="image/jpeg",
        width=1920,
        height=1080,
        source_timestamp="2026-08-05T12:00:00Z",
        captured_at="2026-08-05T12:00:00Z",
        sequence=27,
        image_context={"kind": "original", "width": 1920, "height": 1080},
        metadata={
            "calibration_capture_mode": "camera_media_stream",
            "stream_remained_active": True,
        },
    )
    monkeypatch.setattr(
        runtime_admin_routes,
        "_capture_calibration_frame_from_stream_ring",
        lambda **_kwargs: stream_frame,
    )
    monkeypatch.setattr(
        runtime_admin_routes,
        "build_frame_source",
        lambda **_kwargs: pytest.fail("the camera must not be opened beside camera-media"),
    )

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)
        created = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Streaming calibration camera",
                "technical_place": "stem-orientation",
                "source_type": "basler_pylon",
                "source_config": {"serial_number": "40123456"},
                "mode": "realtime",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        channel_id = created.json()["channel_id"]
        with session_scope(runtime_settings) as session:
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            channel.stream_desired_state = "running"
            channel.stream_actual_state = "ready"

        preview = runtime_client.get(
            f"/api/v1/runtime/channels/{channel_id}/live-view/latest",
            params={
                "include_frame": "true",
                "prefer_camera_stream_frame": "true",
            },
        )
        captured = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/calibration-frame",
            headers={"X-CSRF-Token": csrf_token},
        )

    assert created.status_code == 201
    assert preview.status_code == 200
    assert captured.status_code == 200
    for response in (preview, captured):
        state = response.json()["state"]
        assert state["available"] is True
        assert state["frame"]["width"] == 1920
        assert state["frame"]["height"] == 1080
        assert state["frame"]["sequence"] == 27
        assert state["display_context"]["purpose"] == "runtime_calibration"
        assert state["display_context"]["capture_mode"] == "camera_media_stream"
        assert state["source_health"]["connected"] is True


def test_camera_stream_calibration_frame_reads_the_full_resolution_shared_ring(
    tmp_path: Path,
) -> None:
    settings = build_test_settings("runtime-admin-api", tmp_path)
    settings.runtime.camera_media_runtime_dir = str(tmp_path / "camera-media")
    source_frame = np.full((48, 96, 3), 73, dtype=np.uint8)
    writer = LatestFrameRingWriter(
        runtime_dir=settings.runtime.camera_media_runtime_dir,
        channel_id="chn_stream_calibration",
        frame=source_frame,
        source_uri="basler://40123456",
        source_metadata={"source_type": "basler_pylon"},
    )
    try:
        writer.write(source_frame, sequence=41)
        frame = runtime_admin_routes._capture_calibration_frame_from_stream_ring(
            channel_id="chn_stream_calibration",
            settings=settings,
            timeout_ms=500,
            max_bytes=2 * 1024 * 1024,
        )
    finally:
        writer.close()

    assert frame.width == 96
    assert frame.height == 48
    assert frame.sequence == 41
    assert frame.image_base64
    assert frame.metadata["source"] == "camera_media_shared_memory"
    assert frame.metadata["stream_remained_active"] is True
    assert frame.metadata["camera_released_after_capture"] is False


def test_camera_calibration_calculation_uses_existing_channel_route(
    sqlite_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings
    monkeypatch.setattr(
        runtime_admin_routes,
        "calculate_camera_calibration",
        lambda **_kwargs: CameraCalibrationComputation(
            status="invalid",
            profile=None,
            observations=[
                RuntimeCameraCalibrationObservationResult(
                    observation_id=f"observation-{index}",
                    accepted=False,
                    reason_code="calibration_board_not_detected",
                )
                for index in range(1, 4)
            ],
            failure_code="calibration_observations_insufficient",
            failure_message="At least three detected observations are required.",
        ),
    )

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)
        created = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Intrinsic calibration camera",
                "technical_place": "stem-orientation",
                "source_type": "file_test",
                "source_config": {"path": "calibration.png"},
                "mode": "realtime",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        channel_id = created.json()["channel_id"]
        calculated = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/camera-calibration/calculate",
            json={
                "board_definition": {
                    "board_type": "checkerboard",
                    "columns": 9,
                    "rows": 6,
                    "square_length": 1,
                    "dictionary_name": "DICT_4X4_50",
                },
                "observations": [
                    {
                        "observation_id": f"observation-{index}",
                        "frame": {
                            "image_base64": "AA==",
                            "mime_type": "image/png",
                            "width": 320,
                            "height": 240,
                            "sequence": index,
                            "metadata": {},
                        },
                    }
                    for index in range(1, 4)
                ],
                "minimum_observation_count": 3,
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        passthrough = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/camera-calibration/calculate",
            json={
                "board_definition": {
                    "board_type": "checkerboard",
                    "columns": 9,
                    "rows": 6,
                    "square_length": 1,
                    "dictionary_name": "DICT_4X4_50",
                },
                "calibration_method": "IDENTITY_PASSTHROUGH",
                "observations": [],
                "image_width": 1920,
                "image_height": 1080,
            },
            headers={"X-CSRF-Token": csrf_token},
        )

    assert created.status_code == 201
    assert calculated.status_code == 200
    body = calculated.json()
    assert body["schema_version"] == "runtime.camera-calibration.calculate.v1"
    assert body["status"] == "invalid"
    assert body["failure_code"] == "calibration_observations_insufficient"
    assert len(body["observations"]) == 3
    assert passthrough.status_code == 200
    passthrough_body = passthrough.json()
    assert passthrough_body["status"] == "valid"
    assert passthrough_body["rms_reprojection_error_px"] is None
    assert passthrough_body["profile"]["calibration_method"] == "IDENTITY_PASSTHROUGH"
    assert passthrough_body["profile"]["image_width"] == 1920
    assert passthrough_body["profile"]["image_height"] == 1080


def test_stem_process_calibration_uses_existing_channel_and_audited_runtime_route(
    sqlite_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings
    monkeypatch.setattr(
        runtime_admin_routes,
        "calculate_stem_curvature_process_calibration",
        lambda **_kwargs: StemCurvatureProcessCalibrationComputation(
            status="invalid",
            profile=None,
            failure_code="stem_process_gate_order_invalid",
            failure_message="Gate order is invalid.",
        ),
    )

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)
        created = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Stem process calibration camera",
                "technical_place": "stem-orientation",
                "source_type": "file_test",
                "source_config": {"path": "calibration.png"},
                "mode": "realtime",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        channel_id = created.json()["channel_id"]
        calculated = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/stem-process-calibration/calculate",
            json=process_calibration_request().model_dump(mode="json"),
            headers={"X-CSRF-Token": csrf_token},
        )

    assert created.status_code == 201
    assert calculated.status_code == 200
    body = calculated.json()
    assert body["schema_version"] == "runtime.stem-process-calibration.calculate.v1"
    assert body["status"] == "invalid"
    assert body["failure_code"] == "stem_process_gate_order_invalid"


def test_minimum_principle_validation_uses_existing_channel_and_returns_reports(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)
        created = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Stem objective validation camera",
                "technical_place": "stem-orientation",
                "source_type": "file_test",
                "source_config": {"path": "reference-sequence.mp4"},
                "mode": "realtime",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        channel_id = created.json()["channel_id"]
        calculated = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/stem-minimum-principle-validation/calculate",
            json=minimum_principle_validation_request().model_dump(mode="json"),
            headers={"X-CSRF-Token": csrf_token},
        )

    assert created.status_code == 201
    assert calculated.status_code == 200
    body = calculated.json()
    assert body["schema_version"] == (
        "runtime.stem-minimum-principle-validation.calculate.v1"
    )
    assert body["status"] == "VALIDATION_PASSED"
    assert body["artifact"]["source_channel_id"] == channel_id
    assert body["artifact"]["artifact_hash"] == body["report_json"]["artifact_hash"]
    assert body["report_markdown"].startswith("# Objective curvature-minimum validation")
    assert body["report_html"].startswith("<!doctype html>")


def test_runtime_channel_rejects_basler_payload_above_configured_link_budget(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)

        response = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Overcommitted Basler camera",
                "technical_place": "press-line-basler-overcommit",
                "source_type": "basler_pylon",
                "source_config": {
                    "serial_number": "40999999",
                    "pixel_format": "RGB8",
                    "width": 1900,
                    "height": 1200,
                    "fps": 30,
                    "link_speed_mbps": 1000,
                },
                "mode": "realtime",
            },
            headers={"X-CSRF-Token": csrf_token},
        )

    assert response.status_code == 422
    assert "exceeds 80%" in response.text


def test_runtime_channel_accepts_aravis_gige_source(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)

        response = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "FLIR bridge camera",
                "technical_place": "press-line-aravis",
                "source_type": "aravis_gige",
                "source_config": {
                    "camera_name": "Point Grey Research-22334455",
                    "pixel_format": "BayerRG8",
                    "max_fps": "20",
                    "sample_every_n": "3",
                },
                "mode": "realtime",
            },
            headers={"X-CSRF-Token": csrf_token},
        )

    assert response.status_code == 201
    body = response.json()
    assert body["source_type"] == "aravis_gige"
    assert "Aravis GigE" in body["source_summary"]
    assert "Point Grey Research-22334455" in body["source_summary"]
    config = body["source_config"]
    assert config["camera_name"] == "Point Grey Research-22334455"
    assert config["bridge_name"].startswith("src-")
    assert config["rtsp_host"] == "127.0.0.1"
    assert config["rtsp_mount"].startswith("/")
    assert config["url"].startswith("rtsp://127.0.0.1:")
    assert config["pixel_format"] == "BayerRG8"
    assert config["conversion_mode"] == "bayer"
    assert config["max_fps"] == 20.0
    assert config["sample_every_n"] == 3


def test_legacy_trigger_channel_migration_creates_pipeline_trigger_endpoint_metadata(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "runtime-admin-api")
    settings = build_test_settings("runtime-admin-api", tmp_path)

    with session_scope(settings) as session:
        source = RuntimeSource(
            id="src_legacy_trigger",
            slug="legacy-trigger-source",
            name="Legacy trigger source",
            source_type=RuntimeSourceType.SNAPSHOT.value,
            config={"url": "https://camera.local/legacy-snapshot.jpg"},
            secret_refs={},
        )
        channel = RuntimeChannel(
            id="chn_legacy_trigger",
            slug="legacy-trigger-channel",
            name="Legacy trigger channel",
            technical_place="packing-station",
            source_id=source.id,
            mode=RuntimeMode.TRIGGER.value,
            assigned_pipeline="Reject decision",
            pipeline_version_id="plv_legacy_trigger",
            active_runtime_execution_id="rex_legacy_trigger",
            desired_state="running",
            observed_state="running",
            health_state="ok",
            output_summary={},
            capture_summary={},
            created_by="operator",
        )
        session.add_all([source, channel])

    migration = _load_pipeline_trigger_migration()
    with sqlite_engine.begin() as connection:
        migration._migrate_legacy_trigger_channels(connection)

    with session_scope(settings) as session:
        channel = session.get(RuntimeChannel, "chn_legacy_trigger")
        endpoint = session.query(TriggerEndpoint).filter_by(channel_id="chn_legacy_trigger").one()

    assert channel is not None
    assert channel.mode == RuntimeMode.REALTIME.value
    assert channel.assigned_pipeline is None
    assert channel.pipeline_version_id is None
    assert channel.active_runtime_execution_id is None
    assert channel.desired_state == "stopped"
    assert channel.observed_state == "stopped"
    assert channel.health_state == "down"
    assert endpoint.pipeline_version_id == "plv_legacy_trigger"
    assert endpoint.display_name == "Legacy trigger channel pipeline trigger"
    assert endpoint.trigger_type == "http_put"
    assert endpoint.method == "PUT"
    assert endpoint.auth_required is True
    assert endpoint.auth_policy["source"] == "legacy_channel_migration"
    assert endpoint.status == "degraded"


def test_rtsp_channel_credentials_are_stored_as_secret_refs(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings
    pipeline_version_id = _seed_published_pipeline(
        runtime_settings,
        name="Credentialed stream",
        mode=RuntimeMode.REALTIME,
    )

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)

        response = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Credentialed camera",
                "technical_place": "press-line-4",
                "source_type": "rtsp",
                "source_config": {
                    "url": "rtsp://operator:secret@camera.local:554/secure-stream",
                    "max_fps": "9",
                    "sample_every_n": "2",
                },
                "mode": "realtime",
                "pipeline_version_id": pipeline_version_id,
            },
            headers={"X-CSRF-Token": csrf_token},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["source_summary"] == "rtsp://camera.local:554/secure-stream"
        assert "operator" not in str(body["source_config"])
        assert "secret" not in str(body["source_config"])

        source_id = body["source_id"]
        with session_scope(runtime_settings) as session:
            from cv_db.models import RuntimeSource

            source = session.get(RuntimeSource, source_id)
            assert source is not None
            assert source.config == {
                "scheme": "rtsp",
                "host": "camera.local",
                "port": 554,
                "path": "secure-stream",
                "max_fps": 9.0,
                "sample_every_n": 2,
            }
            username_path = Path(source.secret_refs["username_file"])
            password_path = Path(source.secret_refs["password_file"])
            assert username_path.read_text(encoding="utf-8") == "operator"
            assert password_path.read_text(encoding="utf-8") == "secret"


def test_runtime_channel_validation_rejects_invalid_source_tuning(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)

        bad_fps = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Bad FPS camera",
                "technical_place": "press-line-bad-fps",
                "source_type": "rtsp",
                "source_config": {"url": "rtsp://camera.local/stream", "max_fps": 0},
                "mode": "realtime",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        bad_sample = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Bad sample camera",
                "technical_place": "press-line-bad-sample",
                "source_type": "rtsp",
                "source_config": {"url": "rtsp://camera.local/stream", "sample_every_n": 0},
                "mode": "realtime",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        bad_inflight = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Bad inflight camera",
                "technical_place": "press-line-bad-inflight",
                "source_type": "rtsp",
                "source_config": {
                    "url": "rtsp://camera.local/stream",
                    "pipeline_max_inflight": 17,
                },
                "mode": "realtime",
            },
            headers={"X-CSRF-Token": csrf_token},
        )

    assert bad_fps.status_code == 422
    assert "max_fps" in bad_fps.json()["detail"]
    assert bad_sample.status_code == 422
    assert "sample_every_n" in bad_sample.json()["detail"]
    assert bad_inflight.status_code == 422
    assert "pipeline_max_inflight" in bad_inflight.json()["detail"]


def test_running_channel_pipeline_update_queues_runtime_restart(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings
    old_pipeline_version_id = _seed_published_pipeline(
        runtime_settings,
        name="Runtime old",
        mode=RuntimeMode.REALTIME,
    )
    new_pipeline_version_id = _seed_published_pipeline(
        runtime_settings,
        name="Runtime new",
        mode=RuntimeMode.REALTIME,
    )

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)
        created = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Restart camera",
                "technical_place": "press-line-restart",
                "source_type": "rtsp",
                "source_config": {"url": "rtsp://camera.local/restart"},
                "mode": "realtime",
                "pipeline_version_id": old_pipeline_version_id,
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert created.status_code == 201
        channel_id = created.json()["channel_id"]

        with session_scope(runtime_settings) as session:
            from cv_db.models import RuntimeChannel

            execution = RuntimeExecution(
                id="rex_running_before_pipeline_change",
                channel_id=channel_id,
                pipeline_version_id=old_pipeline_version_id,
                mode="realtime",
                executor_service="realtime-executor",
                status="running",
                actual_state="running",
                desired_state="running",
                heartbeat_at=datetime.now(tz=UTC),
            )
            session.add(execution)
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            channel.active_runtime_execution_id = execution.id
            channel.desired_state = "running"
            channel.observed_state = "running"
            channel.health_state = "ok"

        updated = runtime_client.put(
            f"/api/v1/runtime/channels/{channel_id}",
            json={"pipeline_version_id": new_pipeline_version_id},
            headers={"X-CSRF-Token": csrf_token},
        )

        assert updated.status_code == 200
        body = updated.json()
        assert body["pipeline_version_id"] == new_pipeline_version_id
        assert body["desired_state"] == "start_requested"
        assert body["status"] == "starting"
        with session_scope(runtime_settings) as session:
            from cv_db.models import RuntimeControlCommandRecord

            command = session.scalar(
                select(RuntimeControlCommandRecord).where(
                    RuntimeControlCommandRecord.channel_id == channel_id,
                    RuntimeControlCommandRecord.command == "restart",
                )
            )
            assert command is not None
            assert command.status == "queued"


def test_running_channel_trigger_pipeline_update_does_not_queue_live_restart(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings
    live_pipeline_version_id = _seed_published_pipeline(
        runtime_settings,
        name="Runtime live",
        mode=RuntimeMode.REALTIME,
    )
    trigger_pipeline_version_id = _seed_published_pipeline(
        runtime_settings,
        name="Runtime trigger",
        mode=RuntimeMode.TRIGGER,
    )

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)
        created = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Trigger save camera",
                "technical_place": "press-line-trigger-save",
                "source_type": "rtsp",
                "source_config": {"url": "rtsp://camera.local/trigger-save"},
                "mode": "realtime",
                "pipeline_version_id": live_pipeline_version_id,
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert created.status_code == 201
        channel_id = created.json()["channel_id"]

        with session_scope(runtime_settings) as session:
            execution = RuntimeExecution(
                id="rex_running_before_trigger_pipeline_change",
                channel_id=channel_id,
                pipeline_version_id=live_pipeline_version_id,
                mode="realtime",
                executor_service="realtime-executor",
                status="running",
                actual_state="running",
                desired_state="running",
                heartbeat_at=datetime.now(tz=UTC),
            )
            session.add(execution)
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            channel.active_runtime_execution_id = execution.id
            channel.desired_state = "running"
            channel.observed_state = "running"
            channel.health_state = "ok"

        updated = runtime_client.put(
            f"/api/v1/runtime/channels/{channel_id}",
            json={"pipeline_version_id": trigger_pipeline_version_id},
            headers={"X-CSRF-Token": csrf_token},
        )

        assert updated.status_code == 200
        body = updated.json()
        assert body["pipeline_version_id"] != trigger_pipeline_version_id
        assert body["desired_state"] == "running"
        assert [endpoint["kind"] for endpoint in body["request_endpoints"]] == ["trigger_execution"]
        assert [
            endpoint["pipeline_version_id"] for endpoint in body["triggerable_pipelines"]
        ] == [body["pipeline_version_id"]]
        with session_scope(runtime_settings) as session:
            from cv_db.models import RuntimeControlCommandRecord

            restart = session.scalar(
                select(RuntimeControlCommandRecord).where(
                    RuntimeControlCommandRecord.channel_id == channel_id,
                    RuntimeControlCommandRecord.command == "restart",
                )
            )
            endpoint = session.scalar(
                select(TriggerEndpoint).where(
                    TriggerEndpoint.pipeline_version_id == body["pipeline_version_id"],
                )
            )
            assert restart is None
            assert endpoint is not None
            assert endpoint.derived_from_pipeline is True


def test_realtime_channel_switches_between_trigger_and_live_channel_instances(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings
    trigger_pipeline_version_id = _seed_published_pipeline(
        runtime_settings,
        name="Saw infeed trigger",
        mode=RuntimeMode.TRIGGER,
    )
    crop_pipeline_version_id = _seed_published_pipeline(
        runtime_settings,
        name="Crop",
        mode=RuntimeMode.REALTIME,
    )

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)
        created = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Saegezubringung",
                "technical_place": "saw-infeed",
                "source_type": "snapshot",
                "source_config": {"url": "https://camera.local/saw-infeed.jpg"},
                "mode": "realtime",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert created.status_code == 201
        channel_id = created.json()["channel_id"]

        trigger_update = runtime_client.put(
            f"/api/v1/runtime/channels/{channel_id}",
            json={"pipeline_version_id": trigger_pipeline_version_id},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert trigger_update.status_code == 200
        trigger_body = trigger_update.json()
        assert trigger_body["pipeline_version_id"] != trigger_pipeline_version_id
        assert trigger_body["assigned_pipeline"] == "Saegezubringung Pipeline"
        assert [
            endpoint["kind"] for endpoint in trigger_body["request_endpoints"]
        ] == ["trigger_execution"]
        assert [
            endpoint["pipeline_version_id"]
            for endpoint in trigger_body["triggerable_pipelines"]
        ] == [trigger_body["pipeline_version_id"]]
        with session_scope(runtime_settings) as session:
            trigger_instance_version = session.get(PipelineVersion, trigger_body["pipeline_version_id"])
            assert trigger_instance_version is not None
            trigger_instance_definition = session.get(
                PipelineDefinition,
                trigger_instance_version.pipeline_definition_id,
            )
            assert trigger_instance_definition is not None
            assert trigger_instance_definition.scope == "channel_instance"
            assert trigger_instance_definition.owner_channel_id == channel_id
            assert trigger_instance_version.mode == "trigger"

        updated = runtime_client.put(
            f"/api/v1/runtime/channels/{channel_id}",
            json={"pipeline_version_id": crop_pipeline_version_id},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert updated.status_code == 200
        body = updated.json()
        assert body["pipeline_version_id"] != crop_pipeline_version_id
        assert body["pipeline_version_id"] != trigger_body["pipeline_version_id"]
        assert body["assigned_pipeline"] == "Saegezubringung Pipeline"
        assert [
            endpoint["kind"] for endpoint in body["request_endpoints"]
        ] == ["latest_result"]
        assert body["request_endpoints"][0]["available"] is False
        assert "Ergebnis-Historie" in body["request_endpoints"][0]["unavailable_reason"]
        assert body["triggerable_pipelines"] == []
        with session_scope(runtime_settings) as session:
            live_instance_version = session.get(PipelineVersion, body["pipeline_version_id"])
            assert live_instance_version is not None
            live_instance_definition = session.get(
                PipelineDefinition,
                live_instance_version.pipeline_definition_id,
            )
            assert live_instance_definition is not None
            assert live_instance_definition.scope == "channel_instance"
            assert live_instance_definition.owner_channel_id == channel_id
            assert live_instance_version.mode == "realtime"
            old_trigger_endpoint = session.scalar(
                select(TriggerEndpoint).where(
                    TriggerEndpoint.pipeline_version_id == trigger_body["pipeline_version_id"]
                )
            )
            assert old_trigger_endpoint is not None
            assert old_trigger_endpoint.enabled is False
            assert old_trigger_endpoint.status == "disabled"

        start = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}:start",
            json={"reason": "operator selected live crop pipeline"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert start.status_code == 202
        assert start.json()["channel"]["desired_state"] == "start_requested"


def test_runtime_channel_read_models_use_latest_runtime_records(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings
    pipeline_version_id = _seed_published_pipeline(
        runtime_settings,
        name="Read model pipeline",
        mode=RuntimeMode.REALTIME,
    )
    now = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)
        created = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Read model camera",
                "technical_place": "press-line-read-model",
                "source_type": "rtsp",
                "source_config": {"url": "rtsp://camera.local/read-model"},
                "mode": "realtime",
                "pipeline_version_id": pipeline_version_id,
                "result_history_policy": {
                    "enabled": True,
                    "retention_hours": 24,
                    "sample_interval_seconds": 5,
                    "max_records": 100,
                },
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert created.status_code == 201
        channel_id = created.json()["channel_id"]

        with session_scope(runtime_settings) as session:
            session.add_all(
                [
                    RuntimeExecution(
                        id="rex_channel_read_old",
                        channel_id=channel_id,
                        pipeline_version_id=pipeline_version_id,
                        mode="realtime",
                        executor_service="realtime-executor",
                        status="stopped",
                        actual_state="stopped",
                        desired_state="stopped",
                        frame_count=4,
                        created_at=now - timedelta(minutes=5),
                    ),
                    RuntimeExecution(
                        id="rex_channel_read_latest",
                        channel_id=channel_id,
                        pipeline_version_id=pipeline_version_id,
                        mode="realtime",
                        executor_service="realtime-executor",
                        status="running",
                        actual_state="running",
                        desired_state="running",
                        frame_count=88,
                        drop_count=2,
                        trigger_count=3,
                        created_at=now,
                    ),
                ]
            )
            for index in range(25):
                session.add(
                    ResultRecord(
                        id=f"rrc_channel_read_{index}",
                        channel_id=channel_id,
                        pipeline_version_id=pipeline_version_id,
                        runtime_execution_id="rex_channel_read_latest",
                        correlation_id=f"corr_channel_read_{index}",
                        completed_at=now + timedelta(seconds=index),
                        mode="realtime",
                        status="completed",
                        result_type="realtime_frame",
                        schema_version="runtime.execution.result.v1",
                        result={},
                        timing={"total_ms": index},
                        model_routes=[],
                        history_retained=True,
                    )
                )

        listed = runtime_client.get("/api/v1/runtime/channels")
        detail = runtime_client.get(f"/api/v1/runtime/channels/{channel_id}")
        assert detail.status_code == 200
        current_endpoint = detail.json()["request_endpoints"][0]
        assert current_endpoint["kind"] == "latest_result"
        assert current_endpoint["status"] == "active"

        stale_pipeline_version_id = _seed_published_pipeline(
            runtime_settings,
            name="Read model pipeline replacement",
            mode=RuntimeMode.REALTIME,
            channel_id=channel_id,
        )
        assert stale_pipeline_version_id != pipeline_version_id
        stale_detail = runtime_client.get(f"/api/v1/runtime/channels/{channel_id}")
        status = runtime_client.get("/api/v1/runtime/channels/status")

    assert listed.status_code == 200
    item = listed.json()["items"][0]
    assert item["runtime_state"]["runtime_instance_id"] == "rex_channel_read_latest"
    assert item["performance"]["frame_count"] == 88
    assert item["performance"]["latency_ms"] == 24.0
    assert detail.json()["performance"]["trigger_count"] == 3
    assert stale_detail.status_code == 200
    stale_endpoint = stale_detail.json()["request_endpoints"][0]
    assert stale_endpoint["kind"] == "latest_result"
    assert stale_endpoint["pipeline_version_id"] == stale_pipeline_version_id
    assert stale_endpoint["status"] == "degraded"
    assert status.status_code == 200
    assert status.json()["counts"]["total"] == 1


def test_runtime_channel_status_marks_stale_running_heartbeat_degraded(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings("runtime-admin-api", tmp_path)
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings
    pipeline_version_id = _seed_published_pipeline(
        runtime_settings,
        name="Stale heartbeat pipeline",
        mode=RuntimeMode.REALTIME,
    )

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)
        created = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Stale heartbeat camera",
                "technical_place": "press-line-stale-heartbeat",
                "source_type": "rtsp",
                "source_config": {"url": "rtsp://camera.local/stale-heartbeat"},
                "mode": "realtime",
                "pipeline_version_id": pipeline_version_id,
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert created.status_code == 201
        channel_id = created.json()["channel_id"]

        with session_scope(runtime_settings) as session:
            execution = RuntimeExecution(
                id="rex_stale_running_heartbeat",
                channel_id=channel_id,
                pipeline_version_id=pipeline_version_id,
                mode="realtime",
                executor_service="realtime-executor",
                status="running",
                actual_state="running",
                desired_state="running",
                heartbeat_at=datetime.now(tz=UTC) - timedelta(minutes=3),
            )
            session.add(execution)
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            channel.active_runtime_execution_id = execution.id
            channel.desired_state = "running"
            channel.observed_state = "running"
            channel.health_state = "ok"
            channel.health_summary = {
                "state": "ok",
                "status_text": "Runtime instance is running",
            }

        status = runtime_client.get(f"/api/v1/runtime/channels/{channel_id}/status")

    assert status.status_code == 200
    body = status.json()
    assert body["health"]["state"] == "degraded"
    assert body["health_state"] == "degraded"
    assert body["health"]["status_text"] == "Runtime heartbeat is stale"
    assert body["runtime_state"]["heartbeat_age_seconds"] >= 60
    assert body["desired_state"] == "start_requested"
    with session_scope(runtime_settings) as session:
        command = session.scalar(
            select(RuntimeControlCommandRecord).where(
                RuntimeControlCommandRecord.channel_id == channel_id,
                RuntimeControlCommandRecord.command == "restart",
            )
        )
        assert command is not None
        assert command.status == "queued"
        assert command.idempotency_key == (
            "runtime-stale-heartbeat-recovery:"
            f"{channel_id}:rex_stale_running_heartbeat"
        )


def test_runtime_channel_status_accepts_result_scope_machine_key(
    sqlite_engine: Engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_settings = build_test_settings("admin-api", tmp_path)
    runtime_settings = build_test_settings(
        "runtime-admin-api",
        tmp_path,
        api_keys=[
            (
                "result-client",
                "result-secret",
                ["runtime.results.read"],
                "application",
                "diagnostic-client",
            ),
            (
                "runtime-control",
                "control-secret",
                ["runtime.control"],
                "service",
                "runtime-admin-repair",
            ),
        ],
    )
    admin_app.state.settings = admin_settings
    runtime_admin_app.state.settings = runtime_settings
    pipeline_version_id = _seed_published_pipeline(
        runtime_settings,
        name="Machine read pipeline",
        mode=RuntimeMode.REALTIME,
    )

    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf_token = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, admin_settings)
        created = runtime_client.post(
            "/api/v1/runtime/channels",
            json={
                "name": "Machine readable camera",
                "technical_place": "press-line-machine-readable",
                "source_type": "rtsp",
                "source_config": {"url": "rtsp://camera.local/machine-readable"},
                "mode": "realtime",
                "pipeline_version_id": pipeline_version_id,
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert created.status_code == 201
        channel_id = created.json()["channel_id"]

        machine_status = runtime_client.get(
            f"/api/v1/runtime/channels/{channel_id}/status",
            headers={"X-API-Key": "result-client.result-secret"},
        )
        machine_start = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}:start",
            json={},
            headers={"X-API-Key": "result-client.result-secret"},
        )
        machine_restart = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}:restart",
            json={"reason": "machine requested stale runtime repair"},
            headers={"X-API-Key": "runtime-control.control-secret"},
        )

    assert machine_status.status_code == 200
    assert machine_status.json()["channel_id"] == channel_id
    assert machine_start.status_code == 401
    assert machine_restart.status_code == 202
    assert machine_restart.json()["accepted_job"]["command"] == "restart"


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


def _load_pipeline_trigger_migration():
    migration_path = (
        Path(__file__).resolve().parents[3]
        / "backend"
        / "migrations"
        / "versions"
        / "20260502_0021_pipeline_trigger_node_refactor.py"
    )
    spec = importlib.util.spec_from_file_location(
        "pipeline_trigger_node_refactor_migration",
        migration_path,
    )
    assert spec is not None
    assert spec.loader is not None
    sys.modules.setdefault("alembic", types.SimpleNamespace(op=types.SimpleNamespace()))
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _seed_published_pipeline(
    settings: PlatformSettings,
    *,
    name: str,
    mode: RuntimeMode,
    channel_id: str | None = None,
) -> str:
    with session_scope(settings) as session:
        seed_deployed_runtime_model(session)
        definition = create_pipeline_definition(
            session,
            RuntimePipelineDefinitionCreateRequest(name=name),
            created_by="tester",
        )
        version = create_pipeline_version(
            session,
            definition.pipeline_definition_id,
            RuntimePipelineVersionCreateRequest(
                mode=mode,
                graph=_valid_runtime_graph(mode, channel_id=channel_id),
            ),
            created_by="tester",
        )
        if channel_id is not None:
            channel = session.get(RuntimeChannel, channel_id)
            assert channel is not None
            channel.pipeline_version_id = version.pipeline_version_id
            channel.assigned_pipeline = name
        published = publish_pipeline_version(
            session,
            version.pipeline_version_id,
            published_by="tester",
        )
        return published.pipeline_version_id


def _valid_runtime_graph(mode: RuntimeMode, *, channel_id: str | None = None) -> RuntimePipelineGraph:
    trigger_node = RuntimeNodeSpec(
        node_key="http_trigger",
        node_type="trigger.http_put",
        node_category=RuntimeNodeCategory.TRIGGER,
        config={
            "method": "PUT",
            "timeout_ms": 1200,
            "auth_required": True,
            "endpoint_enabled": True,
        },
    )
    source_node = RuntimeNodeSpec(
        node_key="channel_input",
        node_type="source.channel_frame",
        node_category=RuntimeNodeCategory.SOURCE,
        config={},
    )
    output_node = (
        RuntimeNodeSpec(
            node_key="http_response",
            node_type="output.http_trigger_response",
            node_category=RuntimeNodeCategory.OUTPUT,
        )
        if mode == RuntimeMode.TRIGGER
        else RuntimeNodeSpec(
            node_key="persisted_result",
            node_type="output.persisted_result",
            node_category=RuntimeNodeCategory.OUTPUT,
        )
    )
    return RuntimePipelineGraph(
        nodes=[
            *([trigger_node] if mode == RuntimeMode.TRIGGER else []),
            source_node,
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
            output_node,
        ],
        edges=[
            *(
                [
                    RuntimeGraphEdge(
                        from_node_key=trigger_node.node_key,
                        from_port="trigger",
                        to_node_key=source_node.node_key,
                        to_port="trigger",
                    )
                ]
                if mode == RuntimeMode.TRIGGER
                else []
            ),
            RuntimeGraphEdge(
                from_node_key=source_node.node_key,
                from_port="frame",
                to_node_key="detect",
                to_port="frame",
            ),
            RuntimeGraphEdge(
                from_node_key="detect",
                from_port="detections",
                to_node_key=output_node.node_key,
                to_port="result",
            ),
        ],
    )
