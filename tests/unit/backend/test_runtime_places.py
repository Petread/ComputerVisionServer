from pathlib import Path

import pytest
from cv_contracts.places import AreaWrite
from cv_contracts.runtime import RuntimeChannelCreateRequest, RuntimeChannelUpdateRequest
from cv_db.channel_store import create_runtime_channel, update_runtime_channel
from cv_db.models import RuntimeChannel
from cv_db.place_store import delete_place, list_places, save_place
from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


def test_structure_uniqueness_move_and_restrict_delete(sqlite_engine: Engine) -> None:
    with Session(sqlite_engine) as session:
        area = save_place(session, name="  Sägewerk  ")
        other = save_place(session, name="Sortierung")
        facility = save_place(session, name="Linie 1", area_id=area)
        with pytest.raises(ValueError, match="vergeben"):
            save_place(session, name="SÄGEWERK")
        with pytest.raises(ValueError, match="vergeben"):
            save_place(session, name="linie 1", area_id=area)
        with pytest.raises(ValueError, match="Anlagen"):
            delete_place(session, area, facility=False)
        save_place(session, place_id=facility, name="Linie 2", area_id=other)
        delete_place(session, area, facility=False)
        places = list_places(session)
        assert places.facilities[0].area_id == other
        assert places.facilities[0].name == "Linie 2"
        delete_place(session, facility, facility=True)
        delete_place(session, other, facility=False)
        assert list_places(session).areas == []


def test_legacy_channel_assignment_rename_and_unassignment(sqlite_engine: Engine) -> None:
    with Session(sqlite_engine) as session:
        area = save_place(session, name="Produktion")
        facility = save_place(session, name="Anlage", area_id=area)
        channel = create_runtime_channel(
            session,
            RuntimeChannelCreateRequest(
                name="Kamera",
                technical_place="Altbezeichnung",
                source_type="rtsp",
                source_config={"url": "rtsp://example.test/live"},
                mode="realtime",
            ),
            created_by="test",
        )
        assert channel.facility_id is None
        assert channel.technical_place == "Altbezeichnung"
        assigned = update_runtime_channel(
            session,
            channel.channel_id,
            RuntimeChannelUpdateRequest(facility_id=facility),
            updated_by="test",
        )
        assert assigned.facility_id == facility
        assert assigned.technical_place == "Anlage"
        with pytest.raises(ValueError, match="Kanäle"):
            delete_place(session, facility, facility=True)
        save_place(session, name="Umbenannt", area_id=area, place_id=facility)
        row = session.get(RuntimeChannel, channel.channel_id)
        assert row.technical_place == "Umbenannt"
        unchanged = update_runtime_channel(
            session,
            channel.channel_id,
            RuntimeChannelUpdateRequest(description="Neue Beschreibung"),
            updated_by="test",
        )
        assert unchanged.facility_id == facility
        detached = update_runtime_channel(
            session,
            channel.channel_id,
            RuntimeChannelUpdateRequest(facility_id=None),
            updated_by="test",
        )
        assert detached.facility_id is None
        assert detached.technical_place == "Umbenannt"
        delete_place(session, facility, facility=True)


def test_legacy_place_edit_clears_structured_assignment(sqlite_engine: Engine) -> None:
    with Session(sqlite_engine) as session:
        area = save_place(session, name="Bereich")
        facility = save_place(session, name="Anlage", area_id=area)
        channel = create_runtime_channel(
            session,
            RuntimeChannelCreateRequest(
                name="Kamera",
                technical_place="Alt",
                facility_id=facility,
                source_type="rtsp",
                source_config={"url": "rtsp://example.test/live"},
                mode="realtime",
            ),
            created_by="test",
        )
        assert channel.technical_place == "Anlage"
        changed = update_runtime_channel(
            session,
            channel.channel_id,
            RuntimeChannelUpdateRequest(technical_place="Andere Position"),
            updated_by="test",
        )
        assert changed.facility_id is None
        assert changed.technical_place == "Andere Position"
        with pytest.raises(ValueError, match="nicht gefunden"):
            update_runtime_channel(
                session,
                channel.channel_id,
                RuntimeChannelUpdateRequest(facility_id="missing"),
                updated_by="test",
            )


@pytest.mark.parametrize("name", ["", "   ", "x" * 161])
def test_place_contract_rejects_invalid_names(name: str) -> None:
    with pytest.raises(ValidationError):
        AreaWrite(name=name)


def test_place_migration_is_additive_postgresql_ddl() -> None:
    import importlib.util
    from io import StringIO

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    root = Path(__file__).resolve().parents[3]
    path = root / "backend/migrations/versions/20260907_0037_runtime_places.py"
    spec = importlib.util.spec_from_file_location("workspace_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        module.upgrade()
    sql = output.getvalue()
    assert module.down_revision == "20260820_0036"
    assert "CREATE TABLE runtime_areas" in sql
    assert "CREATE TABLE runtime_facilities" in sql
    assert "ADD COLUMN facility_id" in sql
    assert "ON DELETE RESTRICT" in sql
    assert "DEFAULT now()" in sql
    assert "DROP " not in sql
    assert "UPDATE " not in sql


def test_places_api_auth_csrf_and_audit(sqlite_engine: Engine, tmp_path: Path) -> None:
    from admin_api.main import app as admin_app
    from fastapi.testclient import TestClient
    from runtime_admin_api.main import app as runtime_app
    from tests.conftest import build_test_settings, configure_sqlite_factories
    from tests.unit.backend.test_runtime_channel_management import _copy_auth_cookies, _login

    configure_sqlite_factories(sqlite_engine, "admin-api", "runtime-admin-api")
    admin_app.state.settings = build_test_settings("admin-api", tmp_path)
    runtime_app.state.settings = build_test_settings("runtime-admin-api", tmp_path)
    with TestClient(admin_app) as admin, TestClient(runtime_app) as client:
        assert client.get("/api/v1/runtime/places").status_code == 401
        csrf = _login(admin)
        _copy_auth_cookies(admin, client, admin_app.state.settings)
        assert client.post("/api/v1/runtime/areas", json={"name": "Werk"}).status_code == 401
        headers = {"X-CSRF-Token": csrf}
        created = client.post("/api/v1/runtime/areas", json={"name": "Werk"}, headers=headers)
        assert created.status_code == 201
        area = created.json()["areas"][0]["id"]
        facility = client.post(
            "/api/v1/runtime/facilities", json={"name": "Linie", "area_id": area}, headers=headers
        )
        assert facility.status_code == 201
        assert client.delete(f"/api/v1/runtime/areas/{area}", headers=headers).status_code == 422
        assert client.get("/api/v1/runtime/places").json()["facilities"][0]["name"] == "Linie"
        # Existing audit API exposes the event written by this service.
        from cv_db import AuditEvent
        from sqlalchemy import select

        with Session(sqlite_engine) as session:
            assert (
                session.scalar(
                    select(AuditEvent).where(AuditEvent.event_type == "runtime.place.saved.v1")
                )
                is not None
            )
