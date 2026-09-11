from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from cv_db import Base, create_audit_event
import cv_db.repositories as repositories
import cv_db.session as session_module
from cv_observability import create_health_router
from cv_status import HealthState
from tests.conftest import build_test_settings


def test_health_router_distinguishes_live_and_ready_semantics() -> None:
    app = FastAPI()

    async def live_check():
        return ("process", HealthState.OK, "alive")

    async def ready_check():
        return ("redis", HealthState.DOWN, "redis unavailable")

    async def startup_check():
        return ("startup", HealthState.OK, "startup complete")

    app.include_router(
        create_health_router(
            service_name="test-service",
            live_checks=[live_check],
            readiness_checks=[ready_check],
            startup_checks=[startup_check],
        )
    )

    with TestClient(app) as client:
        live_response = client.get("/health/live")
        ready_response = client.get("/health/ready")
        startup_response = client.get("/health/startup")

    assert live_response.status_code == 200
    assert live_response.json()["state"] == HealthState.OK.value
    assert ready_response.status_code == 503
    assert ready_response.json()["state"] == HealthState.DOWN.value
    assert startup_response.status_code == 200
    assert startup_response.json()["state"] == HealthState.OK.value


def test_audit_events_are_exported_to_append_only_jsonl(tmp_path: Path) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as session:
        session.info["audit_root"] = str(tmp_path / "audit")
        event_id = create_audit_event(
            session,
            event_type="system.test_event.v1",
            aggregate_type="system",
            aggregate_id="test",
            source_service="test-suite",
            actor_type="system",
            actor_id="test-suite",
            payload={"message": "hello"},
        )
        session.commit()

    exports = sorted((tmp_path / "audit" / "events").glob("*.jsonl"))
    assert exports
    lines = exports[0].read_text(encoding="utf-8").strip().splitlines()
    payload = json.loads(lines[-1])
    assert payload["event_id"] == event_id
    assert payload["event_type"] == "system.test_event.v1"
    assert payload["payload"]["message"] == "hello"


def test_audit_export_io_error_does_not_abort_event_commit(monkeypatch, tmp_path: Path) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    def fail_export(*_args, **_kwargs) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(repositories, "_append_audit_export", fail_export)

    with Session(engine, expire_on_commit=False) as session:
        session.info["audit_root"] = str(tmp_path / "audit")
        event_id = create_audit_event(
            session,
            event_type="system.test_event.v1",
            aggregate_type="system",
            aggregate_id="test",
            source_service="test-suite",
            actor_type="system",
            actor_id="test-suite",
            payload={"message": "hello"},
        )
        session.commit()
        export_errors = list(session.info["audit_export_errors"])

    with Session(engine, expire_on_commit=False) as session:
        row = session.get(repositories.AuditEvent, event_id)

    assert row is not None
    assert export_errors[0]["event_id"] == event_id
    assert "Input/output error" in export_errors[0]["error"]


def test_session_factory_recreates_engine_after_pid_change(monkeypatch, tmp_path: Path) -> None:
    settings = build_test_settings("training-worker", tmp_path)
    session_module._session_factories.clear()
    session_module._session_factory_pids.clear()
    pid = {"value": 101}
    engine_one = Mock(name="engine_one")
    engine_two = Mock(name="engine_two")
    create_engine = Mock(side_effect=[engine_one, engine_two])

    monkeypatch.setattr(session_module.os, "getpid", lambda: pid["value"])
    monkeypatch.setattr(session_module, "create_engine", create_engine)

    first_factory = session_module.get_session_factory(settings)
    second_factory = session_module.get_session_factory(settings)

    assert first_factory is second_factory
    assert create_engine.call_count == 1

    pid["value"] = 202
    third_factory = session_module.get_session_factory(settings)

    assert third_factory is not first_factory
    assert create_engine.call_count == 2
    session_module._session_factories.clear()
    session_module._session_factory_pids.clear()
