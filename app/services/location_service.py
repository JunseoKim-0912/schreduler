from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError
from app.models.event import Event
from app.models.location import Location
from app.models.user import User
from app.schemas.location import LocationCreate, LocationUpdate
from app.services.common import require


def create_location(db: Session, data: LocationCreate) -> Location:
    require(db, User, data.user_id, "user_id")
    location = Location(**data.model_dump())
    db.add(location)
    db.commit()
    db.refresh(location)
    return location


def get_location(db: Session, location_id: int) -> Location | None:
    return db.get(Location, location_id)


def list_locations(db: Session, user_id: int | None = None) -> list[Location]:
    stmt = select(Location)
    if user_id is not None:
        stmt = stmt.where(Location.user_id == user_id)
    return list(db.execute(stmt).scalars().all())


def update_location(db: Session, location_id: int, data: LocationUpdate) -> Location | None:
    location = db.get(Location, location_id)
    if location is None:
        return None

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(location, field, value)

    db.commit()
    db.refresh(location)
    return location


def delete_location(db: Session, location_id: int) -> bool:
    location = db.get(Location, location_id)
    if location is None:
        return False

    in_use = db.scalar(select(func.count()).select_from(Event).where(Event.location_id == location_id))
    if in_use:
        raise ConflictError(f"location {location_id} is used by {in_use} events")

    db.delete(location)
    db.commit()
    return True
