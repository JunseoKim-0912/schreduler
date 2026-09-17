from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.location import Location
from app.models.user import User
from app.schemas.location import LocationCreate, LocationUpdate


def _ensure_user_exists(db: Session, data: LocationCreate | LocationUpdate) -> None:
    user_id = getattr(data, "user_id", None)
    if user_id is not None and db.get(User, user_id) is None:
        raise ValueError(f"user_id {user_id} does not exist")


def create_location(db: Session, data: LocationCreate) -> Location:
    _ensure_user_exists(db, data)
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

    _ensure_user_exists(db, data)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(location, field, value)

    db.commit()
    db.refresh(location)
    return location


def delete_location(db: Session, location_id: int) -> bool:
    location = db.get(Location, location_id)
    if location is None:
        return False

    db.delete(location)
    db.commit()
    return True
