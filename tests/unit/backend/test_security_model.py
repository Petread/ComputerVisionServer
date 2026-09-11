from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from admin_api.main import app as admin_app
from control_worker.jobs.dataset import finalize_upload_session
from cv_db import (
    DatasetAsset,
    DeploymentRelease,
    DeploymentTarget,
    ModelFamily,
    ModelVersion,
    UploadSession,
    get_dataset_record,
    session_scope,
)
from cv_status import TargetPath, TaskType
from inference_gateway.main_realtime import app as realtime_app
from inference_gateway.runtime.service import RealtimeGatewayService

from tests.conftest import build_test_settings, configure_sqlite_factories
from tests.support.scenario_helpers import FakeQueue


def test_admin_session_requires_csrf_for_mutations(sqlite_engine, tmp_path: Path) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api")
    settings = build_test_settings("admin-api", tmp_path)
    admin_app.state.settings = settings

    with TestClient(admin_app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "adminadmin"},
        )
        assert login.status_code == 200
        csrf_token = login.json()["csrf_token"]

        without_csrf = client.post(
            "/api/v1/datasets",
            json={
                "name": "Security Dataset",
                "slug": "security-dataset",
                "task_type": "classification",
                "label_schema_version": "1.0.0",
            },
        )
        assert without_csrf.status_code == 401

        with_csrf = client.post(
            "/api/v1/datasets",
            json={
                "name": "Security Dataset",
                "slug": "security-dataset",
                "task_type": "classification",
                "label_schema_version": "1.0.0",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert with_csrf.status_code == 200


def test_realtime_gateway_requires_scoped_machine_identity(
    sqlite_engine,
    tmp_path: Path,
    sample_png_base64: str,
) -> None:
    configure_sqlite_factories(sqlite_engine, "inference-gateway-rt")
    settings = build_test_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[
            (
                "rt-client",
                "rt-secret",
                ["realtime.predict"],
                "inference_client",
                "realtime-smoke",
            )
        ],
    )
    realtime_app.state.settings = settings

    with Session(sqlite_engine, expire_on_commit=False) as session:
        _seed_realtime_release(session, tmp_path=tmp_path)
        session.commit()

    with TestClient(realtime_app) as client:
        realtime_app.state.realtime_gateway_service = RealtimeGatewayService(
            settings,
            service_name="inference-gateway-rt",
            target_path=TargetPath.REALTIME,
        )
        unauthorized = client.post(
            "/api/v1/realtime/classify",
            json={"deployment_target": "widget-inspection-rt", "image_base64": sample_png_base64},
        )
        assert unauthorized.status_code == 401

        authorized = client.post(
            "/api/v1/realtime/classify",
            json={"deployment_target": "widget-inspection-rt", "image_base64": sample_png_base64},
            headers={"Authorization": "Bearer rt-client.rt-secret"},
        )
        assert authorized.status_code == 200


def test_realtime_gateway_can_run_with_api_key_auth_disabled(
    sqlite_engine,
    tmp_path: Path,
    sample_png_base64: str,
) -> None:
    configure_sqlite_factories(sqlite_engine, "inference-gateway-rt")
    settings = build_test_settings("inference-gateway-rt", tmp_path)
    settings.api_keys.enabled = False
    realtime_app.state.settings = settings

    with Session(sqlite_engine, expire_on_commit=False) as session:
        _seed_realtime_release(session, tmp_path=tmp_path)
        session.commit()

    with TestClient(realtime_app) as client:
        realtime_app.state.realtime_gateway_service = RealtimeGatewayService(
            settings,
            service_name="inference-gateway-rt",
            target_path=TargetPath.REALTIME,
        )
        response = client.post(
            "/api/v1/realtime/classify",
            json={"deployment_target": "widget-inspection-rt", "image_base64": sample_png_base64},
        )

    assert response.status_code == 200


def test_upload_session_validates_images_and_finalizes_assets(
    sqlite_engine,
    tmp_path: Path,
    monkeypatch,
    sample_png_bytes: bytes,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "control-worker")
    admin_settings = build_test_settings("admin-api", tmp_path)
    worker_settings = build_test_settings("control-worker", tmp_path)
    admin_app.state.settings = admin_settings
    upload_queue = FakeQueue()

    monkeypatch.setattr("admin_api.api.routes.uploads._queue", lambda _settings: upload_queue)

    with TestClient(admin_app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "adminadmin"},
        )
        csrf_token = login.json()["csrf_token"]

        dataset = client.post(
            "/api/v1/datasets",
            json={
                "name": "Uploads",
                "slug": "uploads",
                "task_type": "classification",
                "label_schema_version": "1.0.0",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        dataset_id = dataset.json()["dataset_id"]

        session_response = client.post(
            f"/api/v1/datasets/{dataset_id}/upload-sessions",
            json={"expected_file_count": 1},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert session_response.status_code == 200
        upload_session_id = session_response.json()["upload_session_id"]

        bad_upload = client.put(
            f"/api/v1/upload-sessions/{upload_session_id}/parts/1",
            content=b"not-an-image",
            headers={
                "X-CSRF-Token": csrf_token,
                "X-Upload-Filename": "broken.txt",
                "Content-Type": "text/plain",
            },
        )
        assert bad_upload.status_code == 415

        good_upload = client.put(
            f"/api/v1/upload-sessions/{upload_session_id}/parts/1",
            content=sample_png_bytes,
            headers={
                "X-CSRF-Token": csrf_token,
                "X-Upload-Filename": "part-one.png",
                "Content-Type": "image/png",
            },
        )
        assert good_upload.status_code == 200

        completed = client.post(
            f"/api/v1/upload-sessions/{upload_session_id}:complete",
            json={},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert completed.status_code == 202

    monkeypatch.setattr("control_worker.jobs.dataset.load_platform_settings", lambda _service: worker_settings)
    finalize_upload_session(upload_session_id)

    with session_scope(worker_settings) as session:
        upload_session = session.get(UploadSession, upload_session_id)
        dataset = get_dataset_record(session, dataset_id)
        assets = session.query(DatasetAsset).filter(DatasetAsset.upload_session_id == upload_session_id).all()

    assert upload_session is not None
    assert upload_session.status == "completed"
    assert dataset is not None
    assert dataset.status == "ready"
    assert len(assets) == 1
    assert (worker_settings.storage.dataset_asset_root(dataset_id) / assets[0].relative_path).exists()


def _seed_realtime_release(session: Session, *, tmp_path: Path) -> None:
    family = ModelFamily(
        id="mfy_rt_security",
        slug="rt-security",
        task_type=TaskType.CLASSIFICATION.value,
        contract_major=1,
        input_schema_ref="contracts/classification/input.v1.json",
        output_schema_ref="contracts/classification/output.v1.json",
        active=True,
    )
    package_root = tmp_path / "package_rt_security"
    package_root.mkdir(parents=True, exist_ok=True)
    version = ModelVersion(
        id="mvr_rt_security",
        model_family_id=family.id,
        version_no=1,
        status="production",
        serving_manifest_uri=str(package_root / "manifest.json"),
        serving_manifest={
            "package_root": str(package_root),
            "class_count": 2,
            "confidence_threshold_default": 0.5,
            "compatible_targets": [TargetPath.REALTIME.value],
            "preprocessing": {"resize": {"width": 224, "height": 224}},
            "postprocessing": {"class_map": ["accepted", "rework"]},
            "io_schema": {
                "input_schema_ref": family.input_schema_ref,
                "output_schema_ref": family.output_schema_ref,
                "contract_major": 1,
                "schema_digest": "security-schema",
            },
            "hardware_compatibility": {"max_batch_size": {"realtime": 1}},
            "source_manifests": {},
        },
    )
    release = DeploymentRelease(
        id="rel_rt_security",
        target_path=TargetPath.REALTIME.value,
        model_family_id=family.id,
        model_version_id=version.id,
        triton_model_name="realtime_security",
        triton_model_version=1,
        status="activated",
    )
    target = DeploymentTarget(
        target_path=TargetPath.REALTIME.value,
        active_release_id=release.id,
        previous_release_id=None,
        updated_at=datetime.now(timezone.utc),
    )
    session.add_all([family, version, release, target])
