from pathlib import Path

import pytest
from pydantic import ValidationError

from cv_core import load_platform_settings
from cv_core.settings import (
    DatabaseConfig,
    DvcConfig,
    EnvironmentName,
    PlatformSettings,
    ServiceRuntimeConfig,
    SessionConfig,
    StorageRoots,
    TlsConfig,
    TrainingRuntimeConfig,
    prepare_runtime_paths,
)


def test_settings_loader_reads_base_and_env_files(monkeypatch) -> None:
    monkeypatch.setenv("ENV", "dev")
    settings = load_platform_settings("admin-api", root_dir=Path.cwd())
    assert settings.service.name == "admin-api"
    assert settings.database.database == "cv_platform"
    assert settings.redis.host == "redis"
    assert settings.api_keys.enabled is False


def test_prepare_runtime_paths_creates_quarantine_backup_and_dvc_roots(tmp_path: Path) -> None:
    settings = PlatformSettings(
        service=ServiceRuntimeConfig(name="admin-api"),
        storage=StorageRoots(
            state_root=str(tmp_path / "state"),
            training_root=str(tmp_path / "state" / "training"),
            serving_root=str(tmp_path / "state" / "serving"),
            audit_root=str(tmp_path / "state" / "audit"),
            logs_root=str(tmp_path / "state" / "logs"),
        ),
        dvc=DvcConfig(repo_root=str(tmp_path / "dvc" / "repo")),
    )

    prepare_runtime_paths(settings)

    assert settings.storage.backups_root().exists()
    assert settings.storage.quarantine_root().exists()
    assert settings.storage.runtime_test_files_root().exists()
    assert Path(settings.dvc.repo_root).exists()


def test_settings_validation_rejects_invalid_training_heartbeat_window() -> None:
    with pytest.raises(ValidationError):
        PlatformSettings(
            service=ServiceRuntimeConfig(name="training-worker"),
            training=TrainingRuntimeConfig(
                heartbeat_interval_seconds=30,
                stale_after_seconds=30,
            ),
        )


def test_prod_settings_require_tls_and_secure_cookies(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir(parents=True, exist_ok=True)
    database_password = secret_root / "postgres.txt"
    session_secret = secret_root / "session.txt"
    bootstrap_hash = secret_root / "bootstrap.hash"
    for path, value in (
        (database_password, "db-password"),
        (session_secret, "session-secret"),
        (bootstrap_hash, "argon-hash"),
    ):
        path.write_text(value, encoding="utf-8")

    with pytest.raises(ValidationError):
        PlatformSettings(
            env=EnvironmentName.PROD,
            service=ServiceRuntimeConfig(
                name="admin-api", public_base_url="http://127.0.0.1"
            ),
            database=DatabaseConfig(password_file=str(database_password)),
            storage=StorageRoots(
                state_root="/srv/cv-platform",
                training_root="/srv/cv-platform/training",
                serving_root="/srv/cv-platform/serving",
                audit_root="/srv/cv-platform/audit",
                logs_root="/srv/cv-platform/logs",
            ),
            session=SessionConfig(
                secure_cookie=False,
                secret_file=str(session_secret),
                bootstrap_admin_password_hash_file=str(bootstrap_hash),
            ),
            tls=TlsConfig(enabled=False),
        )


def test_prod_settings_reject_onnx_cpu_fallback(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir(parents=True, exist_ok=True)
    database_password = secret_root / "postgres.txt"
    session_secret = secret_root / "session.txt"
    bootstrap_hash = secret_root / "bootstrap.hash"
    for path, value in (
        (database_password, "db-password"),
        (session_secret, "session-secret"),
        (bootstrap_hash, "argon-hash"),
    ):
        path.write_text(value, encoding="utf-8")

    with pytest.raises(ValidationError) as exc_info:
        PlatformSettings(
            env=EnvironmentName.PROD,
            service=ServiceRuntimeConfig(
                name="admin-api", public_base_url="https://127.0.0.1"
            ),
            database=DatabaseConfig(password_file=str(database_password)),
            storage=StorageRoots(
                state_root="/srv/cv-platform",
                training_root="/srv/cv-platform/training",
                serving_root="/srv/cv-platform/serving",
                audit_root="/srv/cv-platform/audit",
                logs_root="/srv/cv-platform/logs",
            ),
            session=SessionConfig(
                secure_cookie=True,
                secret_file=str(session_secret),
                bootstrap_admin_password_hash_file=str(bootstrap_hash),
            ),
            tls=TlsConfig(enabled=True),
            training=TrainingRuntimeConfig(allow_onnx_deployment_fallback=True),
        )

    assert "allow_onnx_deployment_fallback must be false in production" in str(
        exc_info.value
    )
