from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cv_db import Base, WebhookEvent
from cv_db.session import _session_factories
from webhook_receiver.main import app


class _FakeQueue:
    enqueued: list[dict[str, object]]

    def __init__(self, *_args, **_kwargs) -> None:
        self.enqueued = _FAKE_ENQUEUES

    def enqueue(self, job_name: str, **kwargs) -> None:
        self.enqueued.append({"job_name": job_name, **kwargs})


_FAKE_ENQUEUES: list[dict[str, object]] = []


def test_webhook_receiver_persists_event_and_deduplicates(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    _session_factories["webhook-receiver"] = sessionmaker(bind=engine, expire_on_commit=False)
    _FAKE_ENQUEUES.clear()

    secret_path = tmp_path / "labelstudio-webhook.secret"
    secret_path.write_text("super-secret", encoding="utf-8")

    monkeypatch.setattr("webhook_receiver.api.routes.Redis.from_url", lambda _url: object())
    monkeypatch.setattr("webhook_receiver.api.routes.Queue", _FakeQueue)

    with TestClient(app) as client:
        app.state.settings.label_studio.webhook_auth.enabled = True
        app.state.settings.label_studio.webhook_auth.secret_file = str(secret_path)
        payload = {
            "action": "ANNOTATION_CREATED",
            "project_id": 101,
            "annotation": {"id": 55, "result": []},
        }
        response = client.post(
            "/api/v1/webhooks/label-studio",
            json=payload,
            headers={"X-Label-Studio-Signature": "super-secret"},
        )
        duplicate = client.post(
            "/api/v1/webhooks/label-studio",
            json=payload,
            headers={"X-Label-Studio-Signature": "super-secret"},
        )

    session = sessionmaker(bind=engine, expire_on_commit=False)()
    events = session.scalars(select(WebhookEvent)).all()
    session.close()

    assert response.status_code == 200
    assert duplicate.status_code == 200
    assert response.json()["duplicate"] is False
    assert duplicate.json()["duplicate"] is True
    assert len(events) == 1
    assert events[0].signature_valid is True
    assert events[0].payload_sha256
    assert len(_FAKE_ENQUEUES) == 1
    assert _FAKE_ENQUEUES[0]["job_name"] == "control_worker.jobs.webhook.process_labelstudio_event"


def test_webhook_receiver_rejects_invalid_signature_and_records_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    _session_factories["webhook-receiver"] = sessionmaker(bind=engine, expire_on_commit=False)

    secret_path = tmp_path / "labelstudio-webhook.secret"
    secret_path.write_text("super-secret", encoding="utf-8")

    monkeypatch.setattr("webhook_receiver.api.routes.Redis.from_url", lambda _url: object())
    monkeypatch.setattr("webhook_receiver.api.routes.Queue", _FakeQueue)

    with TestClient(app) as client:
        app.state.settings.label_studio.webhook_auth.enabled = True
        app.state.settings.label_studio.webhook_auth.secret_file = str(secret_path)
        response = client.post(
            "/api/v1/webhooks/label-studio",
            json={"action": "ANNOTATION_UPDATED", "project_id": 101, "annotation": {"id": 56}},
            headers={"X-Label-Studio-Signature": "wrong-secret"},
        )

    session = sessionmaker(bind=engine, expire_on_commit=False)()
    event = session.scalar(select(WebhookEvent))
    session.close()

    assert response.status_code == 403
    assert response.json()["code"] == "webhook_signature_invalid"
    assert event is not None
    assert event.signature_valid is False
    assert event.processing_status == "failed"
