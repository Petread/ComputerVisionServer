from __future__ import annotations

import base64
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cv_core import hash_password
from cv_core.settings import (
    ApiKeyBootstrapConfig,
    ApiKeyConfig,
    InferenceLimitsConfig,
    LabelStudioConfig,
    LabelStudioWebhookAuthConfig,
    PlatformSettings,
    RequestLimitsConfig,
    ServiceRuntimeConfig,
    SessionConfig,
    StorageRoots,
    UploadSecurityConfig,
)
from cv_db import Base
import cv_db.auth_runtime as auth_runtime_module
from cv_db.session import _session_factories, _session_factory_pids

SAMPLE_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4////fwAJ+wP9KobjigAAAABJRU5ErkJggg=="
)


@pytest.fixture()
def sample_png_bytes() -> bytes:
    return base64.b64decode(SAMPLE_PNG_BASE64)


@pytest.fixture()
def sample_png_base64() -> str:
    return SAMPLE_PNG_BASE64


@pytest.fixture()
def sqlite_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    _session_factories.clear()
    _session_factory_pids.clear()
    auth_runtime_module._BOOTSTRAP_CACHE.clear()
    auth_runtime_module._MACHINE_AUTH_CACHE.clear()
    auth_runtime_module._MACHINE_LAST_USED_CACHE.clear()
    auth_runtime_module._MACHINE_LAST_USED_PENDING.clear()
    yield engine
    _session_factories.clear()
    _session_factory_pids.clear()
    auth_runtime_module._BOOTSTRAP_CACHE.clear()
    auth_runtime_module._MACHINE_AUTH_CACHE.clear()
    auth_runtime_module._MACHINE_LAST_USED_CACHE.clear()
    auth_runtime_module._MACHINE_LAST_USED_PENDING.clear()


def configure_sqlite_factories(engine, *service_names: str) -> None:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    for service_name in service_names:
        _session_factories[service_name] = factory
        _session_factory_pids[service_name] = None  # explicit test override, not bound to a forked worker PID


def build_test_settings(
    service_name: str,
    tmp_path: Path,
    *,
    bootstrap_password: str = "adminadmin",
    label_studio_token: str = "labelstudio-test-token",
    webhook_secret: str = "labelstudio-webhook-secret",
    api_keys: list[tuple[str, str, list[str], str, str]] | None = None,
) -> PlatformSettings:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir(parents=True, exist_ok=True)
    session_secret_path = secret_root / "session_secret"
    password_hash_path = secret_root / "bootstrap_admin_password_hash"
    label_studio_token_path = secret_root / "labelstudio_api_token"
    webhook_secret_path = secret_root / "labelstudio_webhook_secret"
    session_secret_path.write_text("test-session-secret", encoding="utf-8")
    password_hash_path.write_text(hash_password(bootstrap_password), encoding="utf-8")
    label_studio_token_path.write_text(label_studio_token, encoding="utf-8")
    webhook_secret_path.write_text(webhook_secret, encoding="utf-8")

    bootstrap_keys: list[ApiKeyBootstrapConfig] = []
    for index, (key_id, secret_value, scopes, subject_type, subject_id) in enumerate(api_keys or []):
        secret_path = secret_root / f"api_key_{index}_{key_id}"
        secret_path.write_text(secret_value, encoding="utf-8")
        bootstrap_keys.append(
            ApiKeyBootstrapConfig(
                key_id=key_id,
                subject_type=subject_type,
                subject_id=subject_id,
                scopes=scopes,
                secret_file=str(secret_path),
            )
        )

    return PlatformSettings(
        service=ServiceRuntimeConfig(name=service_name),
        storage=StorageRoots(
            state_root=str(tmp_path / "state"),
            training_root=str(tmp_path / "state" / "training"),
            serving_root=str(tmp_path / "state" / "serving"),
            audit_root=str(tmp_path / "state" / "audit"),
            logs_root=str(tmp_path / "state" / "logs"),
        ),
        session=SessionConfig(
            secret_file=str(session_secret_path),
            bootstrap_admin_password_hash_file=str(password_hash_path),
        ),
        api_keys=ApiKeyConfig(bootstrap_keys=bootstrap_keys),
        label_studio=LabelStudioConfig(
            token_file=str(label_studio_token_path),
            local_files_root=str(tmp_path / "state"),
            webhook_auth=LabelStudioWebhookAuthConfig(
                secret_file=str(webhook_secret_path),
            ),
        ),
        request_limits=RequestLimitsConfig(
            default_timeout_seconds=30,
            upload_timeout_seconds=120,
            default_body_limit_bytes=1_048_576,
            inference_body_limit_bytes=3_145_728,
            upload_body_limit_bytes=3_145_728,
            batch_manifest_max_bytes=1_048_576,
            max_image_bytes=1_048_576,
        ),
        uploads=UploadSecurityConfig(
            max_files_per_session=10,
            max_total_bytes=10_485_760,
        ),
        inference_limits=InferenceLimitsConfig(
            realtime_timeout_ms=750,
            batch_timeout_seconds=30,
            batch_chunk_size=4,
            realtime_max_concurrency=1,
            batch_max_concurrency=1,
            realtime_requests_per_minute=120,
            batch_requests_per_minute=60,
            metrics_window_size=50,
            routing_cache_ttl_seconds=1,
            warm_ttl_seconds=60,
        ),
    )
@pytest.fixture
def edge_system_package_cache(tmp_path, monkeypatch):
    """Exercise real offline staging with pinned fixture bytes; never use the network."""
    import hashlib
    import json
    from scripts.release import edge_system_packages

    cache = tmp_path / "ubuntu-cache"
    cache.mkdir()
    content = b"unit-test-deb-payload"
    name = "ufw_0.36.2-6_all.deb"
    (cache / name).write_bytes(content)
    lock = tmp_path / "ubuntu-packages.lock.json"
    lock.write_text(json.dumps({
        "ubuntu_codename": "noble", "architecture": "arm64", "install": ["ufw"],
        "packages": [{
            "name": "ufw", "version": "0.36.2-6", "architecture": "all", "control": {},
            "url": "https://ports.ubuntu.com/ubuntu-ports/pool/main/u/ufw/" + name,
            "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
        }],
    }))
    monkeypatch.setattr(edge_system_packages, "LOCK_FILE", lock)
    monkeypatch.setattr(edge_system_packages, "CACHE_ROOT", cache)

    def unexpected_network(*args, **kwargs):
        raise AssertionError("A cached prerequisite must not access the network.")

    monkeypatch.setattr(edge_system_packages, "urlopen", unexpected_network)
    return edge_system_packages
