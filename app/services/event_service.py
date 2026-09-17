from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.event import Event
from app.models.important_date_range import ImportantDateRange
from app.models.location import Location
from app.models.user import User
from app.schemas.event import EventCreate, EventUpdate


def _ensure_references_exist(db: Session, data: EventCreate | EventUpdate) -> None:
    user_id = getattr(data, "user_id", None)
    if user_id is not None and db.get(User, user_id) is None:
        raise ValueError(f"user_id {user_id} does not exist")
    if data.date_range_id is not None and db.get(ImportantDateRange, data.date_range_id) is None:
        raise ValueError(f"date_range_id {data.date_range_id} does not exist")
    if data.parent_event_id is not None and db.get(Event, data.parent_event_id) is None:
        raise ValueError(f"parent_event_id {data.parent_event_id} does not exist")
    if data.location_id is not None and db.get(Location, data.location_id) is None:
        raise ValueError(f"location_id {data.location_id} does not exist")


def create_event(db: Session, data: EventCreate) -> Event:
    _ensure_references_exist(db, data)
    event = Event(**data.model_dump())
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def get_event(db: Session, event_id: int) -> Event | None:
    return db.get(Event, event_id)


def list_events(db: Session, user_id: int | None = None) -> list[Event]:
    stmt = select(Event)
    if user_id is not None:
        stmt = stmt.where(Event.user_id == user_id)
    return list(db.execute(stmt).scalars().all())


def update_event(db: Session, event_id: int, data: EventUpdate) -> Event | None:
    event = db.get(Event, event_id)
    if event is None:
        return None

    _ensure_references_exist(db, data)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(event, field, value)

    db.commit()
    db.refresh(event)
    return event


def delete_event(db: Session, event_id: int) -> bool:
    event = db.get(Event, event_id)
    if event is None:
        return False

    db.delete(event)
    db.commit()
    return True
