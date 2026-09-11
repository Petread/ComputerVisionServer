from cv_contracts.places import AreaDetail, FacilityDetail, PlaceList
from cv_core import generate_prefixed_id
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from cv_db.models import RuntimeArea, RuntimeChannel, RuntimeFacility


def list_places(session: Session) -> PlaceList:
    return PlaceList(
        areas=[
            AreaDetail(id=r.id, name=r.name)
            for r in session.scalars(select(RuntimeArea).order_by(RuntimeArea.name))
        ],
        facilities=[
            FacilityDetail(id=r.id, name=r.name, area_id=r.area_id)
            for r in session.scalars(select(RuntimeFacility).order_by(RuntimeFacility.name))
        ],
    )


def require_facility(session: Session, facility_id: str) -> RuntimeFacility:
    row = session.get(RuntimeFacility, facility_id, with_for_update=True)
    if row is None:
        raise ValueError("Anlage nicht gefunden.")
    return row


def save_place(
    session: Session, *, name: str, area_id: str | None = None, place_id: str | None = None
) -> str:
    name = name.strip()
    if not name or len(name) > 160:
        raise ValueError("Bezeichnung muss zwischen 1 und 160 Zeichen enthalten.")
    model = RuntimeFacility if area_id is not None else RuntimeArea
    if area_id is not None and session.get(RuntimeArea, area_id, with_for_update=True) is None:
        raise ValueError("Bereich nicht gefunden.")
    query = select(model).where(model.name_key == name.casefold())
    if area_id is not None:
        query = query.where(RuntimeFacility.area_id == area_id)
    duplicate = session.scalar(query)
    if duplicate is not None and duplicate.id != place_id:
        raise ValueError("Diese Bezeichnung ist im Bereich bereits vergeben.")
    row = session.get(model, place_id, with_for_update=True) if place_id else None
    if place_id and row is None:
        raise ValueError("Struktureintrag nicht gefunden.")
    if row is None:
        row = model(id=generate_prefixed_id("fac" if area_id is not None else "area"))
        session.add(row)
    row.name = name
    row.name_key = name.casefold()
    if area_id is not None:
        row.area_id = area_id
        session.execute(
            update(RuntimeChannel)
            .where(RuntimeChannel.facility_id == row.id)
            .values(technical_place=name)
        )
    session.flush()
    return row.id


def delete_place(session: Session, place_id: str, *, facility: bool) -> None:
    model = RuntimeFacility if facility else RuntimeArea
    row = session.get(model, place_id, with_for_update=True)
    if row is None:
        raise ValueError("Struktureintrag nicht gefunden.")
    if facility:
        if session.scalar(
            select(RuntimeChannel.id)
            .where(RuntimeChannel.facility_id == place_id, RuntimeChannel.deleted_at.is_(None))
            .limit(1)
        ):
            raise ValueError("Dieser Anlage sind noch Kanäle zugeordnet.")
        session.execute(
            update(RuntimeChannel)
            .where(RuntimeChannel.facility_id == place_id)
            .values(facility_id=None)
        )
    elif session.scalar(
        select(RuntimeFacility.id).where(RuntimeFacility.area_id == place_id).limit(1)
    ):
        raise ValueError("Der Bereich enthält noch Anlagen.")
    session.delete(row)
    session.flush()
