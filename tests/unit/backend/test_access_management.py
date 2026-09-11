from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from admin_api.main import app as admin_app
import cv_db.auth_runtime as auth_runtime
from cv_db import ApiKey, DeploymentRelease, DeploymentTarget, ModelFamily, ModelVersion
from cv_db.models import PipelineDefinition, PipelineVersion
from cv_status import TargetPath, TaskType
from inference_gateway.main_realtime import app as realtime_app

from tests.conftest import build_test_settings, configure_sqlite_factories


def test_machine_usage_telemetry_does_not_block_authentication(
    sqlite_engine,
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = build_test_settings("inference-gateway-rt", tmp_path)
    started = Event()
    release = Event()
    finished = Event()

    def blocking_write(
        *,
        settings,
        key_id: str,
        usage_key: tuple[str, str],
    ) -> None:
        del settings, key_id
        started.set()
        release.wait(timeout=5)
        with auth_runtime._MACHINE_LAST_USED_LOCK:
            auth_runtime._MACHINE_LAST_USED_PENDING.discard(usage_key)
        finished.set()

    monkeypatch.setattr(auth_runtime, "_record_machine_identity_use", blocking_write)

    started_at = time.perf_counter()
    auth_runtime._touch_machine_identity(settings, key_id="internal-realtime")
    elapsed = time.perf_counter() - started_at

    try:
        assert started.wait(timeout=1)
        assert elapsed < 0.5
        usage_key = (auth_runtime._bootstrap_cache_key(settings), "internal-realtime")
        assert usage_key in auth_runtime._MACHINE_LAST_USED_PENDING

        auth_runtime._touch_machine_identity(settings, key_id="internal-realtime")
        assert usage_key in auth_runtime._MACHINE_LAST_USED_PENDING
    finally:
        release.set()
        assert finished.wait(timeout=1)


def test_user_management_password_rotation_and_deactivation(sqlite_engine, tmp_path: Path) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api")
    settings = build_test_settings("admin-api", tmp_path)
    admin_app.state.settings = settings

    with TestClient(admin_app) as admin_client:
        admin_csrf = _login(admin_client, username="admin", password="adminadmin")

        created = admin_client.post(
            "/api/v1/users",
            json={
                "username": "alice",
                "display_name": "Alice Example",
                "description": "Packaging line operator",
                "password": "initial-pass-123",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert created.status_code == 201
        user_id = created.json()["user_id"]

        audit_events = admin_client.get(
            "/api/v1/audit-events",
            params={"aggregate_type": "user", "aggregate_id": user_id, "limit": 20},
        )
        assert audit_events.status_code == 200
        assert any(event["event_type"] == "user.created.v1" for event in audit_events.json()["items"])

        with TestClient(admin_app) as alice_client:
            alice_csrf = _login(alice_client, username="alice", password="initial-pass-123")
            assert alice_csrf

            changed = admin_client.post(
                f"/api/v1/users/{user_id}:change-password",
                json={"new_password": "admin-reset-456"},
                headers={"X-CSRF-Token": admin_csrf},
            )
            assert changed.status_code == 200

            stale_session = alice_client.get("/api/v1/auth/me")
            assert stale_session.status_code == 401

        with TestClient(admin_app) as old_password_client:
            old_password_login = old_password_client.post(
                "/api/v1/auth/login",
                json={"username": "alice", "password": "initial-pass-123"},
            )
            assert old_password_login.status_code == 401

        with TestClient(admin_app) as fresh_alice_client:
            rotated_csrf = _login(fresh_alice_client, username="alice", password="admin-reset-456")
            rotated = fresh_alice_client.post(
                "/api/v1/users/me/change-password",
                json={
                    "current_password": "admin-reset-456",
                    "new_password": "alice-self-789",
                },
                headers={"X-CSRF-Token": rotated_csrf},
            )
            assert rotated.status_code == 200
            assert rotated.json()["authenticated"] is True
            fresh_session = fresh_alice_client.get("/api/v1/auth/me")
            assert fresh_session.status_code == 200

        deactivated = admin_client.post(
            f"/api/v1/users/{user_id}:deactivate",
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert deactivated.status_code == 200
        assert deactivated.json()["is_active"] is False

        with TestClient(admin_app) as inactive_client:
            inactive_login = inactive_client.post(
                "/api/v1/auth/login",
                json={"username": "alice", "password": "alice-self-789"},
            )
            assert inactive_login.status_code == 401

        final_audit = admin_client.get(
            "/api/v1/audit-events",
            params={"aggregate_type": "user", "aggregate_id": user_id, "limit": 20},
        )
        event_types = {event["event_type"] for event in final_audit.json()["items"]}
        assert "user.password.changed.v1" in event_types
        assert "user.deactivated.v1" in event_types


def test_managed_api_keys_authenticate_revoke_and_coexist_with_bootstrap(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "inference-gateway-rt")
    admin_settings = build_test_settings(
        "admin-api",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "bootstrap-rt")],
    )
    gateway_settings = build_test_settings(
        "inference-gateway-rt",
        tmp_path,
        api_keys=[("rt-client", "rt-secret", ["realtime.predict"], "inference_client", "bootstrap-rt")],
    )
    admin_app.state.settings = admin_settings
    realtime_app.state.settings = gateway_settings

    with Session(sqlite_engine, expire_on_commit=False) as session:
        _seed_public_realtime_release(session, tmp_path=tmp_path)
        session.commit()

    with TestClient(admin_app) as admin_client, TestClient(realtime_app) as realtime_client:
        admin_csrf = _login(admin_client, username="admin", password="adminadmin")

        list_before = admin_client.get("/api/v1/api-keys")
        assert list_before.status_code == 200
        assert any(item["source_type"] == "bootstrap" for item in list_before.json()["items"])

        scope_catalog = admin_client.get("/api/v1/api-key-scopes")
        assert scope_catalog.status_code == 200
        result_scope = next(
            item
            for item in scope_catalog.json()["items"]
            if item["scope"] == "runtime.results.read"
        )
        assert result_scope["assignable"] is True

        created = admin_client.post(
            "/api/v1/api-keys",
            json={
                "label": "Realtime PLC",
                "description": "Realtime key for the PLC integration",
                "intended_usage": "Synchronous reject/accept decisions on the packaging line",
                "subject_name": "packaging-plc",
                "subject_type": "application",
                "scopes": ["realtime.predict"],
                "endpoint_mode_hint": "realtime",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert created.status_code == 201
        payload = created.json()
        managed_api_key_id = payload["api_key"]["api_key_id"]
        managed_token = payload["token"]
        assert managed_token.startswith("key_")

        detail = admin_client.get(f"/api/v1/api-keys/{managed_api_key_id}")
        assert detail.status_code == 200
        assert "token" not in detail.json()
        assert detail.json()["masked_key"].endswith("...")

        with Session(sqlite_engine, expire_on_commit=False) as session:
            row = session.query(ApiKey).filter(ApiKey.record_id == managed_api_key_id).one()
            assert row.hashed_secret not in managed_token
            assert row.source_type == "managed"
            assert row.label == "Realtime PLC"

        managed_models = realtime_client.get(
            "/api/v1/models",
            headers={"Authorization": f"Bearer {managed_token}"},
        )
        assert managed_models.status_code == 200

        bootstrap_models = realtime_client.get(
            "/api/v1/models",
            headers={"Authorization": "Bearer rt-client.rt-secret"},
        )
        assert bootstrap_models.status_code == 200

        wrong_scope = admin_client.post(
            "/api/v1/api-keys",
            json={
                "label": "Batch only",
                "description": "Wrong scope test",
                "intended_usage": "Should fail on realtime",
                "subject_name": "batch-only-client",
                "subject_type": "application",
                "scopes": ["batch.submit"],
                "endpoint_mode_hint": "batch",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        wrong_scope_token = wrong_scope.json()["token"]
        wrong_scope_result = realtime_client.get(
            "/api/v1/models",
            headers={"Authorization": f"Bearer {wrong_scope_token}"},
        )
        assert wrong_scope_result.status_code == 401

        revoked = admin_client.post(
            f"/api/v1/api-keys/{managed_api_key_id}:revoke",
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert revoked.status_code == 200
        assert revoked.json()["api_key"]["is_revoked"] is True

        time.sleep(2.2)
        revoked_models = realtime_client.get(
            "/api/v1/models",
            headers={"Authorization": f"Bearer {managed_token}"},
        )
        assert revoked_models.status_code == 401

        api_key_audit = admin_client.get(
            "/api/v1/audit-events",
            params={"aggregate_type": "api_key", "aggregate_id": managed_api_key_id, "limit": 20},
        )
        assert api_key_audit.status_code == 200
        audit_types = {event["event_type"] for event in api_key_audit.json()["items"]}
        assert "api_key.created.v1" in audit_types
        assert "api_key.revoked.v1" in audit_types


def test_plc_write_access_status_discovers_sps_nodes_and_updates_runtime_env(
    sqlite_engine,
    tmp_path: Path,
    monkeypatch,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api")
    env_path = tmp_path / "runtime.env"
    env_path.write_text("CV_PLATFORM__RUNTIME__PLC_WRITES_ALLOWED=false\n", encoding="utf-8")
    monkeypatch.setenv("YBB_RUNTIME_ENV_FILE", str(env_path))
    settings = build_test_settings("admin-api", tmp_path)
    admin_app.state.settings = settings

    with Session(sqlite_engine, expire_on_commit=False) as session:
        definition = PipelineDefinition(
            id="pln_access_sps",
            slug="access-sps",
            name="Access SPS",
            description="PLC write access test",
            status="published",
            latest_version_id="plv_access_sps",
            published_version_id="plv_access_sps",
            template=False,
            created_by="admin",
        )
        version = PipelineVersion(
            id="plv_access_sps",
            pipeline_definition_id=definition.id,
            version_no=1,
            status="published",
            mode="trigger",
            graph={
                "nodes": [
                    {
                        "node_key": "sps_write",
                        "node_type": "output.siemens_sps_write",
                        "node_category": "output",
                        "node_contract_version": 1,
                        "input_ports": [],
                        "output_ports": [],
                        "config": {
                            "enabled": True,
                            "dry_run": False,
                            "connection": {
                                "name": "Line PLC",
                                "host": "192.168.0.10",
                                "port": 102,
                                "rack": 0,
                                "slot": 1,
                            },
                            "mappings": [
                                {
                                    "key": "classification_top1_label",
                                    "enabled": True,
                                    "target": {
                                        "db_number": 95,
                                        "byte_offset": 0,
                                        "data_type": "INT",
                                    },
                                }
                            ],
                        },
                        "position": {},
                    }
                ],
                "edges": [],
            },
            validation_report={
                "status": "valid",
                "checked_at": "2026-06-09T00:00:00Z",
                "error_count": 0,
                "warning_count": 0,
                "issues": [],
            },
            model_capability_constraints={},
            node_contract_versions={"sps_write": 1},
            config_digest="digest-sps",
            created_by="admin",
        )
        session.add_all([definition, version])
        session.commit()

    with TestClient(admin_app) as admin_client:
        admin_csrf = _login(admin_client, username="admin", password="adminadmin")

        status = admin_client.get("/api/v1/plc-write-access")
        assert status.status_code == 200
        payload = status.json()
        assert payload["allow_writes"] is False
        assert payload["runtime_env_allow_writes"] is False
        assert payload["connections"][0]["display_name"] == "Line PLC"
        assert payload["connections"][0]["status"] == "environment_blocked"
        assert payload["connections"][0]["reuse_policy"] == "per_execution"
        assert payload["connections"][0]["reconnect_policy"] == "on_failure"
        assert payload["connections"][0]["connection_safety_status"] == "ephemeral"
        assert payload["connections"][0]["persistent_connection_limit"] == 0
        assert payload["connections"][0]["sample_targets"] == ["DB95.DBW0 / INT"]

        updated = admin_client.put(
            "/api/v1/plc-write-access",
            json={"allow_writes": True},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert updated.status_code == 200
        assert updated.json()["desired_allow_writes"] is True
        assert updated.json()["restart_required"] is True
        assert "CV_PLATFORM__RUNTIME__PLC_WRITES_ALLOWED=true" in env_path.read_text(encoding="utf-8")

        created = admin_client.post(
            "/api/v1/users",
            json={
                "username": "plc-operator",
                "display_name": "PLC Operator",
                "description": None,
                "password": "operator-pass-123",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert created.status_code == 201

    with TestClient(admin_app) as operator_client:
        operator_csrf = _login(operator_client, username="plc-operator", password="operator-pass-123")
        rejected = operator_client.put(
            "/api/v1/plc-write-access",
            json={"allow_writes": False},
            headers={"X-CSRF-Token": operator_csrf},
        )
        assert rejected.status_code == 403


def test_public_realtime_routes_require_machine_auth_but_internal_routes_can_use_browser_session(
    sqlite_engine,
    tmp_path: Path,
) -> None:
    configure_sqlite_factories(sqlite_engine, "admin-api", "inference-gateway-rt")
    admin_settings = build_test_settings("admin-api", tmp_path)
    gateway_settings = build_test_settings("inference-gateway-rt", tmp_path)
    admin_app.state.settings = admin_settings
    realtime_app.state.settings = gateway_settings

    with Session(sqlite_engine, expire_on_commit=False) as session:
        _seed_public_realtime_release(session, tmp_path=tmp_path)
        session.commit()

    with TestClient(admin_app) as admin_client, TestClient(realtime_app) as realtime_client:
        _login(admin_client, username="admin", password="adminadmin")
        realtime_client.cookies.set(
            gateway_settings.session.cookie_name,
            admin_client.cookies.get(admin_settings.session.cookie_name),
        )

        public_models = realtime_client.get("/api/v1/models")
        assert public_models.status_code == 401

        internal_status = realtime_client.get("/api/v1/realtime/models/status")
        assert internal_status.status_code == 200


def _login(client: TestClient, *, username: str, password: str) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def _seed_public_realtime_release(session: Session, *, tmp_path: Path) -> None:
    family = ModelFamily(
        id="mfy_access_rt",
        slug="packaging-quality",
        task_type=TaskType.CLASSIFICATION.value,
        contract_major=1,
        input_schema_ref="contracts/classification/input.v1.json",
        output_schema_ref="contracts/classification/output.v1.json",
        active=True,
    )
    package_root = tmp_path / "package_access_rt"
    package_root.mkdir(parents=True, exist_ok=True)
    version = ModelVersion(
        id="mvr_access_rt",
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
                "schema_digest": "access-schema",
            },
            "hardware_compatibility": {"max_batch_size": {"realtime": 1}},
            "source_manifests": {},
            "public_contract": {
                "model_id": family.slug,
                "task_type": family.task_type,
                "availability": {"realtime": True},
            },
        },
    )
    release = DeploymentRelease(
        id="rel_access_rt",
        target_path=TargetPath.REALTIME.value,
        model_family_id=family.id,
        model_version_id=version.id,
        triton_model_name="realtime_access",
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
