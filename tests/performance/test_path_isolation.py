from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cv_status import TargetPath, TaskType
from inference_gateway.main_batch import app as batch_app
from inference_gateway.main_realtime import app as realtime_app
from inference_gateway.runtime.service import BatchGatewayService, RealtimeGatewayService
from tests.conftest import build_test_settings, configure_sqlite_factories
from tests.support.scenario_helpers import seed_release


def test_realtime_and_batch_paths_use_distinct_active_releases(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "inference-gateway-rt", "inference-gateway-batch")
    realtime_settings = build_test_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "rt-smoke")],
    )
    batch_settings = build_test_settings(
        "inference-gateway-batch",
        tmp_path,
        api_keys=[("batch-client", "batch-secret", ["batch.submit"], "inference_client", "batch-smoke")],
    )

    with Session(sqlite_engine, expire_on_commit=False) as session:
        realtime_release_id = seed_release(
            session,
            tmp_path=tmp_path,
            target_path=TargetPath.REALTIME,
            task_type=TaskType.CLASSIFICATION,
            release_suffix="perf_rt",
            active=True,
        )
        batch_release_id = seed_release(
            session,
            tmp_path=tmp_path,
            target_path=TargetPath.BATCH,
            task_type=TaskType.CLASSIFICATION,
            release_suffix="perf_batch",
            active=True,
        )
        session.commit()

    with TestClient(realtime_app) as realtime_client, TestClient(batch_app) as batch_client:
        realtime_app.state.settings = realtime_settings
        batch_app.state.settings = batch_settings
        realtime_app.state.realtime_gateway_service = RealtimeGatewayService(
            realtime_settings,
            service_name="inference-gateway-rt",
            target_path=TargetPath.REALTIME,
        )
        batch_app.state.batch_gateway_service = BatchGatewayService(
            batch_settings,
            service_name="inference-gateway-batch",
            target_path=TargetPath.BATCH,
        )

        realtime_status = realtime_client.get(
            "/api/v1/realtime/models/status",
            headers={"Authorization": "Bearer rt-client.rt-secret"},
        )
        batch_status = batch_client.get(
            "/api/v1/batch/models/status",
            headers={"Authorization": "Bearer batch-client.batch-secret"},
        )

    assert realtime_status.status_code == 200
    assert batch_status.status_code == 200
    assert realtime_status.json()["items"][0]["route"]["target_path"] == "realtime"
    assert batch_status.json()["items"][0]["route"]["target_path"] == "batch"
    assert realtime_status.json()["items"][0]["route"]["deployment_release_id"] == realtime_release_id
    assert batch_status.json()["items"][0]["route"]["deployment_release_id"] == batch_release_id


def test_batch_pressure_does_not_block_realtime_inference_path(
    sqlite_engine,
    tmp_path: Path,
    sample_png_base64: str,
) -> None:
    configure_sqlite_factories(sqlite_engine, "inference-gateway-rt", "inference-gateway-batch")
    realtime_settings = build_test_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "rt-smoke")],
    )
    batch_settings = build_test_settings(
        "inference-gateway-batch",
        tmp_path,
        api_keys=[("batch-client", "batch-secret", ["batch.submit"], "inference_client", "batch-smoke")],
    )

    with Session(sqlite_engine, expire_on_commit=False) as session:
        seed_release(
            session,
            tmp_path=tmp_path,
            target_path=TargetPath.REALTIME,
            task_type=TaskType.CLASSIFICATION,
            release_suffix="perf_rt_busy",
            active=True,
        )
        seed_release(
            session,
            tmp_path=tmp_path,
            target_path=TargetPath.BATCH,
            task_type=TaskType.CLASSIFICATION,
            release_suffix="perf_batch_busy",
            active=True,
        )
        session.commit()

    realtime_service = RealtimeGatewayService(
        realtime_settings,
        service_name="inference-gateway-rt",
        target_path=TargetPath.REALTIME,
    )
    batch_service = BatchGatewayService(
        batch_settings,
        service_name="inference-gateway-batch",
        target_path=TargetPath.BATCH,
    )
    batch_service.gate.current_inflight = batch_service.gate.limit

    with TestClient(realtime_app) as realtime_client:
        realtime_app.state.settings = realtime_settings
        realtime_app.state.realtime_gateway_service = realtime_service
        realtime_response = realtime_client.post(
            "/api/v1/realtime/classify",
            json={
                "deployment_target": "widget-inspection-rt",
                "image_base64": sample_png_base64,
            },
            headers={"Authorization": "Bearer rt-client.rt-secret"},
        )

    assert realtime_response.status_code == 200
    assert batch_service.gate.current_inflight == batch_service.gate.limit
