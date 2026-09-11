"""Technical place master data in the existing runtime service."""

from cv_contracts import SessionUser
from cv_contracts.places import AreaWrite, FacilityWrite, PlaceList
from cv_core import PlatformSettings
from cv_db import session_scope
from cv_db.place_store import delete_place, list_places, save_place
from cv_utils import PlatformError
from fastapi import APIRouter, Request
from sqlalchemy.exc import IntegrityError

from runtime_admin_api.api.routes import ChannelReader, ChannelWriter, SettingsDep
from runtime_admin_api.audit import write_runtime_audit_event

router = APIRouter(tags=["runtime"])


@router.get("/runtime/places", response_model=PlaceList)
def get_places(_current_user: ChannelReader, settings: SettingsDep) -> PlaceList:
    with session_scope(settings) as session:
        return list_places(session)


def _write(
    settings: PlatformSettings,
    request: Request,
    current_user: SessionUser,
    payload: AreaWrite | FacilityWrite | None,
    place_id: str | None = None,
    *,
    facility: bool = False,
    delete: bool = False,
) -> PlaceList:
    try:
        with session_scope(settings) as session:
            if delete:
                assert place_id is not None
                delete_place(session, place_id, facility=facility)
            else:
                assert payload is not None
                place_id = save_place(session, place_id=place_id, **payload.model_dump())
            result = list_places(session)
    except (ValueError, IntegrityError) as exc:
        detail = (
            str(exc)
            if isinstance(exc, ValueError)
            else "Eintrag bereits vorhanden oder noch in Verwendung."
        )
        raise PlatformError(
            code="runtime_place_invalid",
            message="Technischer Platz konnte nicht gespeichert werden.",
            detail=detail,
            status_code=422,
        ) from exc
    assert place_id is not None
    write_runtime_audit_event(
        settings=settings,
        request=request,
        current_user=current_user,
        event_type=f"runtime.place.{'deleted' if delete else 'saved'}.v1",
        aggregate_type="runtime_facility" if facility else "runtime_area",
        aggregate_id=place_id,
        payload=payload.model_dump() if payload else {},
    )
    return result


@router.post("/runtime/areas", response_model=PlaceList, status_code=201)
def create_area(
    payload: AreaWrite, request: Request, current_user: ChannelWriter, settings: SettingsDep
) -> PlaceList:
    return _write(settings, request, current_user, payload)


@router.put("/runtime/areas/{place_id}", response_model=PlaceList)
def update_area(
    place_id: str,
    payload: AreaWrite,
    request: Request,
    current_user: ChannelWriter,
    settings: SettingsDep,
) -> PlaceList:
    return _write(settings, request, current_user, payload, place_id)


@router.delete("/runtime/areas/{place_id}", response_model=PlaceList)
def remove_area(
    place_id: str, request: Request, current_user: ChannelWriter, settings: SettingsDep
) -> PlaceList:
    return _write(settings, request, current_user, None, place_id, delete=True)


@router.post("/runtime/facilities", response_model=PlaceList, status_code=201)
def create_facility(
    payload: FacilityWrite, request: Request, current_user: ChannelWriter, settings: SettingsDep
) -> PlaceList:
    return _write(settings, request, current_user, payload, facility=True)


@router.put("/runtime/facilities/{place_id}", response_model=PlaceList)
def update_facility(
    place_id: str,
    payload: FacilityWrite,
    request: Request,
    current_user: ChannelWriter,
    settings: SettingsDep,
) -> PlaceList:
    return _write(settings, request, current_user, payload, place_id, facility=True)


@router.delete("/runtime/facilities/{place_id}", response_model=PlaceList)
def remove_facility(
    place_id: str, request: Request, current_user: ChannelWriter, settings: SettingsDep
) -> PlaceList:
    return _write(settings, request, current_user, None, place_id, facility=True, delete=True)
