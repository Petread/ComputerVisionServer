from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from capture_service.executor import _resolve_frame
from cv_contracts.runtime import (
    RuntimeManualCaptureRequest,
    RuntimeManualIntervalCaptureStartRequest,
    RuntimePipelineCaptureImage,
)
from cv_db.pipeline_image_capture import selected_pipeline_capture_frame
from cv_runtime.payloads import FramePayload, RuntimeImageContext
from PIL import Image
from pytest import MonkeyPatch
from sqlalchemy import Engine


def snapshot() -> RuntimePipelineCaptureImage:
    frame = FramePayload.from_image(
        Image.new("RGB", (12, 8), "red"), image_format="PNG", mime_type="image/png"
    )
    frame.image_context = RuntimeImageContext(
        context_id="ctx_rectified", width=12, height=8, kind="transformed"
    )
    return RuntimePipelineCaptureImage(
        node_key="left_metric",
        output_port="frame",
        pipeline_version_id="pv_training",
        frame={
            "image_base64": frame.image_base64_value(),
            "mime_type": frame.mime_type,
            "width": frame.width,
            "height": frame.height,
            "image_context": frame.image_context.model_dump(mode="json"),
            "metadata": {"registration_hash": "registration-left"},
        },
        eligibility={"eligible": True, "image_context_id": frame.image_context.context_id},
    )


def test_snapshot_keeps_exact_pixels_context_and_stage_without_reading_live_frame(
    monkeypatch: MonkeyPatch,
) -> None:
    image = snapshot()
    frame = selected_pipeline_capture_frame(image, require_eligibility=True, max_image_bytes=10000)
    row = SimpleNamespace(
        source_type="manual",
        metadata_json={"pipeline_snapshot": True},
        asset_payload=frame.model_dump(mode="json"),
        roi={},
        source_frame_metadata={},
    )

    def unexpected_live_frame(*args: object, **kwargs: object) -> None:
        pytest.fail("A selected snapshot must never resolve a later live frame")

    monkeypatch.setattr("capture_service.executor._latest_live_frame", unexpected_live_frame)
    selected, metadata, _ = _resolve_frame(None, None, row)
    assert selected.image_base64 == image.frame.image_base64
    assert selected.image_context.model_dump(mode="json") == image.frame.image_context
    assert selected.metadata["capture_source_node_key"] == "left_metric"
    assert selected.metadata["capture_pipeline_version_id"] == "pv_training"
    assert selected.metadata["registration_hash"] == "registration-left"
    assert metadata["image_context"]["context_id"] == image.frame.image_context["context_id"]


@pytest.mark.parametrize(
    "eligibility",
    [None, {"eligible": False}, {"eligible": True, "image_context_id": "other-context"}],
)
def test_snapshot_rejects_missing_failed_or_foreign_eligibility(
    eligibility: dict[str, object] | None,
) -> None:
    image = snapshot().model_copy(update={"eligibility": eligibility})
    with pytest.raises(ValueError, match="eligib"):
        selected_pipeline_capture_frame(image, require_eligibility=True, max_image_bytes=10000)


@pytest.mark.parametrize(
    "field,value", [("width", 99), ("mime_type", "image/jpeg"), ("image_base64", "invalid!")]
)
def test_snapshot_rejects_inconsistent_pixels(field: str, value: str | int) -> None:
    image = snapshot()
    image.frame = image.frame.model_copy(update={field: value})
    with pytest.raises(ValueError):
        selected_pipeline_capture_frame(image, require_eligibility=True, max_image_bytes=10000)


def test_snapshot_enforces_size_and_capture_mode() -> None:
    image = snapshot()
    with pytest.raises(ValueError, match="size limit"):
        selected_pipeline_capture_frame(
            image,
            require_eligibility=True,
            max_image_bytes=len(base64.b64decode(image.frame.image_base64)) - 1,
        )
    with pytest.raises(ValueError, match="PIPELINE_IMAGE_OUTPUT"):
        RuntimeManualCaptureRequest(capture_target_id="target", pipeline_image=image)
    with pytest.raises(ValueError, match="single capture"):
        RuntimeManualIntervalCaptureStartRequest(
            capture_target_id="target", capture_source="PIPELINE_IMAGE_OUTPUT", pipeline_image=image
        )
    assert RuntimeManualCaptureRequest(capture_target_id="target").pipeline_image is None


@pytest.mark.parametrize(
    "eligibility,expected",
    [
        ({"eligible": True, "image_context_id": "ctx_rectified"}, 1),
        ({"eligible": True, "image_context_id": "ctx_other_stage"}, 0),
        ({"eligible": False, "image_context_id": "ctx_rectified"}, 0),
        (None, 0),
    ],
)
def test_automatic_capture_requires_matching_stage_eligibility(
    eligibility: dict[str, object] | None, expected: int
) -> None:
    from cv_db.pipeline_store import _template_node
    from cv_runtime.nodes.builtin import FullFrameCaptureHandler
    from cv_runtime.payloads import ResultPayload
    from tests.unit.backend.test_bark_measurement import context

    node = _template_node("capture_metric", "capture.full_frame_labelstudio", 0, 0)
    node.config.update({"target_project_ids": ["lpr_training"], "require_eligibility": True})
    frame = selected_pipeline_capture_frame(
        snapshot(), require_eligibility=True, max_image_bytes=10000
    )
    inputs = {"frame": frame}
    if eligibility is not None:
        inputs["eligibility"] = ResultPayload(result=eligibility)
    result = FullFrameCaptureHandler("capture.full_frame_labelstudio").execute(
        context(), node, inputs
    )
    assert len(result.outputs["capture"].intents) == expected


def test_selected_stage_is_queued_and_delivered_through_existing_manual_capture_api(
    sqlite_engine: Engine,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from cv_db import RuntimeCaptureRequest, session_scope
    from fastapi.testclient import TestClient
    from tests.unit.backend.test_runtime_manual_capture import (
        _configure_apps,
        _copy_auth_cookies,
        _FakeLabelStudioClient,
        _login,
        _seed_channel_and_project,
        admin_app,
        drain_capture_work,
        runtime_admin_app,
    )

    settings = _configure_apps(sqlite_engine, tmp_path)
    channel_id, target_id = _seed_channel_and_project(
        settings, image_path=tmp_path / "unavailable-live-frame.png"
    )
    image = snapshot()
    _FakeLabelStudioClient.calls = []
    monkeypatch.setattr("capture_service.executor.LabelStudioClient", _FakeLabelStudioClient)
    monkeypatch.setattr(
        "capture_service.executor.shutil.disk_usage",
        lambda _: SimpleNamespace(total=100_000_000_000, used=10_000_000_000, free=90_000_000_000),
    )
    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, settings)
        response = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/manual-capture/single",
            headers={"X-CSRF-Token": csrf},
            json={
                "capture_target_id": target_id,
                "capture_source": "PIPELINE_IMAGE_OUTPUT",
                "require_eligibility": True,
                "request_predictions": False,
                "pipeline_image": image.model_dump(mode="json"),
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["status"] == "queued"
        with session_scope(settings) as session:
            row = session.query(RuntimeCaptureRequest).one()
            assert row.asset_payload["image_base64"] == image.frame.image_base64
            assert row.source_frame_metadata["capture_source_node_key"] == "left_metric"
        stats = drain_capture_work(settings=settings, client_factory=_FakeLabelStudioClient)
        with session_scope(settings) as session:
            row = session.query(RuntimeCaptureRequest).one()
            assert stats.delivered == 1, row.last_error
        history = runtime_client.get(
            f"/api/v1/runtime/channels/{channel_id}/manual-capture/history"
        ).json()
        assert history["items"][0]["status"] == "success"
    task = _FakeLabelStudioClient.calls[0]["tasks"][0]
    assert _FakeLabelStudioClient.calls[0]["project_id"] == 42
    assert task["meta"]["source_frame"]["capture_source_node_key"] == "left_metric"
    assert (
        task["meta"]["source_frame"]["image_context"]["context_id"]
        == image.frame.image_context["context_id"]
    )
    assert task["meta"]["source_frame"]["registration_hash"] == "registration-left"


def test_preview_api_returns_training_images_before_unbound_models(
    sqlite_engine: Engine, tmp_path: Path
) -> None:
    from cv_db.pipeline_store import _bark_measurement_template_definition
    from fastapi.testclient import TestClient
    from tests.unit.backend.test_bark_measurement import csv_text
    from tests.unit.backend.test_runtime_manual_capture import (
        _configure_apps,
        _copy_auth_cookies,
        _login,
        _seed_channel_and_project,
        admin_app,
        runtime_admin_app,
    )

    settings = _configure_apps(sqlite_engine, tmp_path)
    channel_id, _ = _seed_channel_and_project(settings, image_path=tmp_path / "unused.png")
    graph = _bark_measurement_template_definition()["graph"]
    photo = FramePayload.from_image(
        Image.new("RGB", (200, 80)), image_format="PNG", mime_type="image/png"
    )
    with TestClient(admin_app) as admin_client, TestClient(runtime_admin_app) as runtime_client:
        csrf = _login(admin_client)
        _copy_auth_cookies(admin_client, runtime_client, settings)
        response = runtime_client.post(
            f"/api/v1/runtime/channels/{channel_id}/pipeline-preview",
            headers={"X-CSRF-Token": csrf},
            json={
                "mode": "trigger",
                "graph": graph.model_dump(mode="json"),
                "preview_frame": {
                    "image_base64": photo.image_base64_value(),
                    "mime_type": "image/png",
                    "width": 200,
                    "height": 80,
                },
                "bark_job": {"csv_text": csv_text(), "side_mapping": "left_first"},
                "selected_node_key": "split_faces",
                "include_execution_result": True,
            },
        )
        assert response.status_code == 200, response.text
        execution = response.json()["execution_result"]
        assert execution["status"] == "completed", execution["errors"]
        assert execution["node_outputs"]["split_faces"]["left_frame"]["image_base64"]
        assert execution["node_outputs"]["split_faces"]["right_frame"]["image_base64"]
        assert not execution["capture_intents"]
