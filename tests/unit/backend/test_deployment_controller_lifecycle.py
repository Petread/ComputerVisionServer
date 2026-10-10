from __future__ import annotations

import asyncio
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
from deployment_controller.lifecycle import (
    reload_active_releases,
    reload_active_releases_until_ready,
)


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


@pytest.fixture()
def serving(engine, settings, monkeypatch):
    import cv_deploy.runtime as runtime

    state = {"ready": {}, "loads": [], "unloads": [], "fail": False}

    @contextmanager
    def scope(_settings):
        with Session(engine, expire_on_commit=False) as session:
            yield session
            session.commit()

    class Client:
        def __init__(self, **kwargs):
            pass

        def repository_index(self, **kwargs):
            return [
                {"name": name, "version": str(version), "state": "READY"}
                for name, versions in state["ready"].items()
                for version in versions
            ]

        def unload_model(self, name):
            state["unloads"].append(name)
            state["ready"].pop(name, None)

        def is_model_ready(self, name, version):
            return version in state["ready"].get(name, set())

    class Manager:
        def __init__(self, _settings):
            pass

        def _render_target_config(self, **kwargs):
            return "compatible"

        def stage_release(self, **kwargs):
            pass

        def load_required_versions(self, **kwargs):
            if state["fail"]:
                raise RuntimeError("GPU unavailable")
            name, versions = kwargs["stable_model_name"], set(kwargs["versions"])
            state["loads"].append((name, versions))
            state["ready"][name] = versions

        def smoke_test_report(self, **kwargs):
            return {"passed": True}

    monkeypatch.setattr("deployment_controller.lifecycle.session_scope", scope)
    monkeypatch.setattr(runtime, "TritonHttpControlClient", Client)
    monkeypatch.setattr(runtime, "TritonRepositoryManager", Manager)
    monkeypatch.setattr(
        runtime,
        "get_deployment_package_detail",
        lambda *args, **kwargs: SimpleNamespace(serving_manifest={"task_type": "detection"}),
    )
    app = FastAPI()
    app.state.settings = settings
    return app, state


def add_execution(session, execution_id, release_ids, *, status="running"):
    from cv_db.models import RuntimeExecution, RuntimeChannel

    session.add(
        RuntimeChannel(
            id="chn_" + execution_id,
            slug=execution_id,
            name=execution_id,
            technical_place="test",
            active_runtime_execution_id=execution_id,
            desired_state="running",
            observed_state="running",
        )
    )
    row = RuntimeExecution(
        id=execution_id,
        channel_id="chn_" + execution_id,
        pipeline_version_id="plv_test",
        executor_service="realtime-executor",
        status=status,
        desired_state="running",
        actual_state=status,
        runtime_plan={
            "binding_signature": {
                "model_bindings": [
                    {"deployment_release_id": release_id} for release_id in release_ids
                ]
            }
        },
    )
    session.add(row)
    return row


def test_deployed_model_without_started_channel_is_not_loaded(engine, serving):
    app, state = serving
    with Session(engine) as session:
        _seed_release(session, release_id="rel_one", version_id="mvr_one", model_name="rt__one")
        session.add(
            DeploymentTarget(
                target_path="realtime",
                active_release_id="rel_one",
                updated_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    asyncio.run(reload_active_releases(app))
    assert state["loads"] == []


def test_direct_operator_demand_loads_an_activated_model_without_a_channel(engine, serving):
    from cv_deploy.runtime import reconcile_runtime_models

    app, state = serving
    with Session(engine) as session:
        _seed_release(session, release_id="rel_operator", version_id="mvr_operator", model_name="rt__operator")
        session.commit()

    with Session(engine, expire_on_commit=False) as session:
        reconcile_runtime_models(
            session,
            app.state.settings,
            extra_demanded_release_ids={"rel_operator"},
        )
        session.commit()

    assert state["loads"] == [("rt__operator", {1})]


def test_start_loads_only_required_versions_and_last_stop_unloads(engine, serving):
    from cv_db.models import RuntimeExecution

    app, state = serving
    with Session(engine) as session:
        for version in (1, 2, 3):
            _seed_release(
                session,
                release_id=f"rel_{version}",
                version_id=f"mvr_{version}",
                model_name="rt__shared",
                triton_model_version=version,
            )
        add_execution(session, "rex_a", ["rel_1"])
        add_execution(session, "rex_b", ["rel_1", "rel_2"])
        session.commit()
    asyncio.run(reload_active_releases(app))
    assert state["loads"] == [("rt__shared", {1, 2})]
    with Session(engine) as session:
        session.get(RuntimeExecution, "rex_a").status = "stopped"
        session.commit()
    asyncio.run(reload_active_releases(app))
    assert state["unloads"] == []
    assert len(state["loads"]) == 1
    with Session(engine) as session:
        session.get(RuntimeExecution, "rex_b").status = "stopped"
        session.commit()
    asyncio.run(reload_active_releases(app))
    assert state["unloads"] == ["rt__shared"]


def test_draining_trigger_retains_models_until_completed(engine, serving):
    app, state = serving
    with Session(engine) as session:
        _seed_release(session, release_id="rel_one", version_id="mvr_one", model_name="rt__one")
        execution = add_execution(session, "rex_trigger", [], status="draining")
        execution.runtime_plan = {
            "trigger_pipelines": [
                {
                    "pipeline_version_id": "plv_trigger",
                    "model_bindings": [{"deployment_release_id": "rel_one"}],
                }
            ]
        }
        session.commit()
    asyncio.run(reload_active_releases(app))
    assert state["ready"] == {"rt__one": {1}}


def test_stopping_one_selected_version_preserves_other_channel_version(engine, serving):
    from cv_db.models import RuntimeExecution

    app, state = serving
    with Session(engine) as session:
        for version in (1, 2, 3):
            _seed_release(
                session,
                release_id=f"rel_{version}",
                version_id=f"mvr_{version}",
                model_name="rt__shared",
                triton_model_version=version,
            )
        add_execution(session, "rex_old", ["rel_1"])
        add_execution(session, "rex_new", ["rel_3"])
        session.commit()
    asyncio.run(reload_active_releases(app))
    assert state["ready"] == {"rt__shared": {1, 3}}
    with Session(engine) as session:
        session.get(RuntimeExecution, "rex_old").status = "stopped"
        session.commit()
    asyncio.run(reload_active_releases(app))
    assert state["ready"] == {"rt__shared": {3}}
    assert state["loads"] == [("rt__shared", {1, 3}), ("rt__shared", {3})]
    assert state["unloads"] == []


def test_recovery_unloads_previously_deployed_unused_model(engine, serving):
    app, state = serving
    with Session(engine) as session:
        _seed_release(
            session, release_id="rel_unused", version_id="mvr_unused", model_name="rt__unused"
        )
        session.commit()
    state["ready"] = {"rt__unused": {1}}
    asyncio.run(reload_active_releases(app))
    assert state["unloads"] == ["rt__unused"]
    with Session(engine) as session:
        assert session.get(DeploymentRelease, "rel_unused").status == "activated"


def test_load_failure_does_not_undeploy_release_and_retries(engine, serving, monkeypatch):
    app, state = serving
    with Session(engine) as session:
        _seed_release(session, release_id="rel_one", version_id="mvr_one", model_name="rt__one")
        add_execution(session, "rex_a", ["rel_one"])
        session.commit()
    state["fail"] = True
    assert asyncio.run(reload_active_releases_until_ready(app, attempts=2)) is True
    with Session(engine) as session:
        assert session.get(DeploymentRelease, "rel_one").status == "activated"
    state["fail"] = False
    assert asyncio.run(reload_active_releases_until_ready(app)) is False
    assert state["ready"] == {"rt__one": {1}}


def test_unused_model_is_unloaded_even_when_another_model_fails_to_load(engine, serving):
    app, state = serving
    with Session(engine) as session:
        _seed_release(session, release_id="rel_needed", version_id="mvr_needed", model_name="rt__a")
        _seed_release(session, release_id="rel_unused", version_id="mvr_unused", model_name="rt__z")
        add_execution(session, "rex_needed", ["rel_needed"])
        session.commit()
    state["ready"] = {"rt__z": {1}}
    state["fail"] = True
    assert asyncio.run(reload_active_releases_until_ready(app)) is True
    assert state["unloads"] == ["rt__z"]
    assert state["ready"] == {}


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
