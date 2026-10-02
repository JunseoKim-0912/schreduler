from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError
from app.models.event import Event
from app.models.location import Location
from app.models.user import User
from app.schemas.location import LocationCreate, LocationUpdate
from app.services.common import require_owned


def create_location(db: Session, user: User, data: LocationCreate) -> Location:
    location = Location(**data.model_dump(), user_id=user.id)
    db.add(location)
    db.commit()
    db.refresh(location)
    return location


def get_location(db: Session, user: User, location_id: int) -> Location:
    return require_owned(db, Location, location_id, user.id, "location")


def list_locations(db: Session, user: User) -> list[Location]:
    return list(db.execute(select(Location).where(Location.user_id == user.id)).scalars().all())


def update_location(db: Session, location: Location, data: LocationUpdate) -> Location:
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(location, field, value)

    db.commit()
    db.refresh(location)
    return location


def delete_location(db: Session, location: Location) -> None:
    in_use = db.scalar(select(func.count()).select_from(Event).where(Event.location_id == location.id))
    if in_use:
        raise ConflictError(f"location {location.id} is used by {in_use} events")

    db.delete(location)
    db.commit()
