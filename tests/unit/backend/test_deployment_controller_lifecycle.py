from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from cv_core.settings import PlatformSettings, ServiceRuntimeConfig, StorageRoots
from cv_db import Base
from cv_db.models import DeploymentRelease, DeploymentTarget
from cv_status import DeploymentReleaseStatus, TargetPath
from deployment_controller.lifecycle import reload_active_releases, reload_active_releases_until_ready


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def settings(tmp_path) -> PlatformSettings:
    return PlatformSettings(
        service=ServiceRuntimeConfig(name="deployment-controller"),
        storage=StorageRoots(
            state_root=str(tmp_path / "state"),
            training_root=str(tmp_path / "state" / "training"),
            serving_root=str(tmp_path / "state" / "serving"),
            audit_root=str(tmp_path / "state" / "audit"),
            logs_root=str(tmp_path / "state" / "logs"),
        ),
    )


def test_startup_reloads_latest_release_per_model_and_target_bound_previous(
    engine,
    settings: PlatformSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Session(engine, expire_on_commit=False) as session:
        _seed_release(
            session,
            release_id="rel_active",
            version_id="mvr_active",
            model_name="rt__active",
            triton_model_version=3,
        )
        _seed_release(
            session,
            release_id="rel_previous",
            version_id="mvr_previous",
            model_name="rt__rollback",
        )
        _seed_release(
            session,
            release_id="rel_old",
            version_id="mvr_old",
            model_name="rt__active",
            triton_model_version=1,
        )
        _seed_release(
            session,
            release_id="rel_compare",
            version_id="mvr_compare",
            model_name="rt__compare",
            triton_model_version=2,
        )
        _seed_release(
            session,
            release_id="rel_failed",
            version_id="mvr_failed",
            model_name="rt__failed",
            status=DeploymentReleaseStatus.ACTIVATION_FAILED,
        )
        _seed_release(
            session,
            release_id="rel_retired",
            version_id="mvr_retired",
            model_name="rt__retired",
            retired=True,
        )
        session.add(
            DeploymentTarget(
                target_path=TargetPath.REALTIME.value,
                active_release_id="rel_active",
                previous_release_id="rel_previous",
                updated_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

    staged: list[tuple[str, str, int, bool]] = []
    loaded: list[tuple[str, str, int]] = []

    @contextmanager
    def _session_scope(_settings) -> Generator[Session, None, None]:
        session = Session(engine, expire_on_commit=False)
        try:
            yield session
            session.commit()
        finally:
            session.close()

    class FakeTritonClient:
        def __init__(self, *, endpoint_url: str, timeout_seconds: int) -> None:
            self.endpoint_url = endpoint_url
            self.timeout_seconds = timeout_seconds

    class FakeRepositoryManager:
        def __init__(self, _settings: PlatformSettings) -> None:
            pass

        def stage_release(
            self,
            *,
            target_path: TargetPath,
            stable_model_name: str,
            triton_model_version: int,
            package_manifest: dict[str, str],
            allow_repository_config_replacement: bool = False,
        ) -> None:
            _ = package_manifest
            staged.append(
                (
                    target_path.value,
                    stable_model_name,
                    triton_model_version,
                    allow_repository_config_replacement,
                )
            )

        def load_release(
            self,
            *,
            target_path: TargetPath,
            stable_model_name: str,
            triton_model_version: int,
            triton_client: FakeTritonClient,
        ) -> None:
            _ = triton_client
            loaded.append((target_path.value, stable_model_name, triton_model_version))

    monkeypatch.setattr("deployment_controller.lifecycle.session_scope", _session_scope)
    monkeypatch.setattr(
        "deployment_controller.lifecycle.get_deployment_package_detail",
        lambda _session, *, model_version_id: SimpleNamespace(
            serving_manifest={"model_version_id": model_version_id}
        ),
    )
    monkeypatch.setattr("deployment_controller.lifecycle.TritonRepositoryManager", FakeRepositoryManager)
    monkeypatch.setattr("deployment_controller.lifecycle.TritonHttpControlClient", FakeTritonClient)

    app = FastAPI()
    app.state.settings = settings

    asyncio.run(reload_active_releases(app))

    assert set(staged) == {
        ("realtime", "rt__active", 3, True),
        ("realtime", "rt__rollback", 1, True),
        ("realtime", "rt__compare", 2, True),
    }
    assert set(loaded) == {
        ("realtime", "rt__active", 3),
        ("realtime", "rt__rollback", 1),
        ("realtime", "rt__compare", 2),
    }


def test_reload_active_releases_retries_until_target_active_release_is_ready(
    settings: PlatformSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    missing_checks = iter([True, False])

    async def _reload(_app: FastAPI) -> None:
        calls.append("reload")

    monkeypatch.setattr("deployment_controller.lifecycle.reload_active_releases", _reload)
    monkeypatch.setattr(
        "deployment_controller.lifecycle._model_serving_state_drift",
        lambda _settings: next(missing_checks),
    )

    app = FastAPI()
    app.state.settings = settings

    missing_after_reload = asyncio.run(
        reload_active_releases_until_ready(
            app,
            attempts=3,
            retry_delay_seconds=0,
        )
    )

    assert missing_after_reload is False
    assert calls == ["reload", "reload"]


def test_startup_unloads_model_after_failed_reload_without_undeploying_release(
    engine,
    settings: PlatformSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Session(engine, expire_on_commit=False) as session:
        _seed_release(session, release_id="rel_good", version_id="mvr_good", model_name="rt__good")
        _seed_release(session, release_id="rel_bad", version_id="mvr_bad", model_name="rt__bad")
        session.add(
            DeploymentTarget(
                target_path=TargetPath.REALTIME.value,
                active_release_id="rel_good",
                previous_release_id="rel_bad",
                updated_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

    unloaded: list[str] = []

    @contextmanager
    def _session_scope(_settings) -> Generator[Session, None, None]:
        session = Session(engine, expire_on_commit=False)
        try:
            yield session
            session.commit()
        finally:
            session.close()

    class FakeTritonClient:
        def __init__(self, *, endpoint_url: str, timeout_seconds: int) -> None:
            self.endpoint_url = endpoint_url
            self.timeout_seconds = timeout_seconds

        def unload_model(self, model_name: str) -> None:
            unloaded.append(model_name)

    class FakeRepositoryManager:
        def __init__(self, _settings: PlatformSettings) -> None:
            pass

        def stage_release(self, **_kwargs: object) -> None:
            return None

        def load_release(self, **kwargs: object) -> None:
            if kwargs.get("stable_model_name") == "rt__bad":
                raise RuntimeError("load failed")

    monkeypatch.setattr("deployment_controller.lifecycle.session_scope", _session_scope)
    monkeypatch.setattr(
        "deployment_controller.lifecycle.get_deployment_package_detail",
        lambda _session, *, model_version_id: SimpleNamespace(
            serving_manifest={"model_version_id": model_version_id}
        ),
    )
    monkeypatch.setattr("deployment_controller.lifecycle.TritonRepositoryManager", FakeRepositoryManager)
    monkeypatch.setattr("deployment_controller.lifecycle.TritonHttpControlClient", FakeTritonClient)

    app = FastAPI()
    app.state.settings = settings

    asyncio.run(reload_active_releases(app))

    assert unloaded == ["rt__bad"]
    with Session(engine, expire_on_commit=False) as session:
        release = session.get(DeploymentRelease, "rel_bad")
    assert release is not None
    assert release.status == DeploymentReleaseStatus.ACTIVATED.value


def test_startup_recovers_passive_reload_failed_release(
    engine,
    settings: PlatformSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activated_at = datetime.now(timezone.utc)
    with Session(engine, expire_on_commit=False) as session:
        _seed_release(
            session,
            release_id="rel_recoverable",
            version_id="mvr_recoverable",
            model_name="rt__recoverable",
            status=DeploymentReleaseStatus.ACTIVATION_FAILED,
            activated_at=activated_at,
            smoke_report={"passed": True},
            compatibility_report={"compatible": True},
        )
        _seed_release(
            session,
            release_id="rel_explicit_failed",
            version_id="mvr_explicit_failed",
            model_name="rt__explicit_failed",
            status=DeploymentReleaseStatus.ACTIVATION_FAILED,
            activated_at=None,
            smoke_report={"passed": False},
            compatibility_report={"compatible": True},
        )
        session.commit()

    loaded: list[str] = []

    @contextmanager
    def _session_scope(_settings) -> Generator[Session, None, None]:
        session = Session(engine, expire_on_commit=False)
        try:
            yield session
            session.commit()
        finally:
            session.close()

    class FakeTritonClient:
        def __init__(self, *, endpoint_url: str, timeout_seconds: int) -> None:
            self.endpoint_url = endpoint_url
            self.timeout_seconds = timeout_seconds

    class FakeRepositoryManager:
        def __init__(self, _settings: PlatformSettings) -> None:
            pass

        def stage_release(self, **_kwargs: object) -> None:
            return None

        def load_release(self, **kwargs: object) -> None:
            loaded.append(str(kwargs.get("stable_model_name")))

    monkeypatch.setattr("deployment_controller.lifecycle.session_scope", _session_scope)
    monkeypatch.setattr(
        "deployment_controller.lifecycle.get_deployment_package_detail",
        lambda _session, *, model_version_id: SimpleNamespace(
            serving_manifest={"model_version_id": model_version_id}
        ),
    )
    monkeypatch.setattr("deployment_controller.lifecycle.TritonRepositoryManager", FakeRepositoryManager)
    monkeypatch.setattr("deployment_controller.lifecycle.TritonHttpControlClient", FakeTritonClient)

    app = FastAPI()
    app.state.settings = settings

    asyncio.run(reload_active_releases(app))

    assert loaded == ["rt__recoverable"]
    with Session(engine, expire_on_commit=False) as session:
        recoverable = session.get(DeploymentRelease, "rel_recoverable")
        explicit_failed = session.get(DeploymentRelease, "rel_explicit_failed")
    assert recoverable is not None
    assert recoverable.status == DeploymentReleaseStatus.ACTIVATED.value
    assert explicit_failed is not None
    assert explicit_failed.status == DeploymentReleaseStatus.ACTIVATION_FAILED.value


def test_startup_unloads_deactivated_model_without_removing_release(
    engine,
    settings: PlatformSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Session(engine, expire_on_commit=False) as session:
        _seed_release(
            session,
            release_id="rel_deactivated",
            version_id="mvr_deactivated",
            model_name="rt__deactivated",
            status=DeploymentReleaseStatus.DEACTIVATED,
        )
        session.commit()

    unloaded: list[str] = []

    @contextmanager
    def _session_scope(_settings) -> Generator[Session, None, None]:
        session = Session(engine, expire_on_commit=False)
        try:
            yield session
            session.commit()
        finally:
            session.close()

    class FakeTritonClient:
        def __init__(self, *, endpoint_url: str, timeout_seconds: int) -> None:
            self.endpoint_url = endpoint_url
            self.timeout_seconds = timeout_seconds

        def repository_index(self, *, ready_only: bool = False) -> list[dict[str, str]]:
            _ = ready_only
            return [{"name": "rt__deactivated", "version": "1", "state": "READY"}]

        def unload_model(self, model_name: str) -> None:
            unloaded.append(model_name)

    class FakeRepositoryManager:
        def __init__(self, _settings: PlatformSettings) -> None:
            pass

    monkeypatch.setattr("deployment_controller.lifecycle.session_scope", _session_scope)
    monkeypatch.setattr(
        "deployment_controller.lifecycle.TritonRepositoryManager",
        FakeRepositoryManager,
    )
    monkeypatch.setattr(
        "deployment_controller.lifecycle.TritonHttpControlClient",
        FakeTritonClient,
    )

    app = FastAPI()
    app.state.settings = settings

    asyncio.run(reload_active_releases(app))

    assert unloaded == ["rt__deactivated"]
    with Session(engine, expire_on_commit=False) as session:
        release = session.get(DeploymentRelease, "rel_deactivated")
    assert release is not None
    assert release.status == DeploymentReleaseStatus.DEACTIVATED.value


def _seed_release(
    session: Session,
    *,
    release_id: str,
    version_id: str,
    model_name: str,
    triton_model_version: int = 1,
    status: DeploymentReleaseStatus = DeploymentReleaseStatus.ACTIVATED,
    retired: bool = False,
    activated_at: datetime | None = None,
    smoke_report: dict[str, object] | None = None,
    compatibility_report: dict[str, object] | None = None,
) -> None:
    session.add(
        DeploymentRelease(
            id=release_id,
            target_path=TargetPath.REALTIME.value,
            model_family_id=f"mfy_{version_id}",
            model_version_id=version_id,
            triton_model_name=model_name,
            triton_model_version=triton_model_version,
            status=status.value,
            activated_at=activated_at,
            retired_at=datetime.now(timezone.utc) if retired else None,
            smoke_report=smoke_report or {},
            compatibility_report=compatibility_report or {},
        )
    )
