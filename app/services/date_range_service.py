from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, InvalidInputError
from app.models.event import Event
from app.models.important_date_range import ImportantDateRange
from app.models.user import User
from app.schemas.date_range import DateRangeCreate, DateRangeUpdate
from app.services.common import require


def create_date_range(db: Session, data: DateRangeCreate) -> ImportantDateRange:
    require(db, User, data.user_id, "user_id")
    date_range = ImportantDateRange(**data.model_dump())
    db.add(date_range)
    db.commit()
    db.refresh(date_range)
    return date_range


def get_date_range(db: Session, date_range_id: int) -> ImportantDateRange | None:
    return db.get(ImportantDateRange, date_range_id)


def list_date_ranges(db: Session, user_id: int | None = None) -> list[ImportantDateRange]:
    stmt = select(ImportantDateRange)
    if user_id is not None:
        stmt = stmt.where(ImportantDateRange.user_id == user_id)
    return list(db.execute(stmt).scalars().all())


def update_date_range(
    db: Session, date_range_id: int, data: DateRangeUpdate
) -> ImportantDateRange | None:
    date_range = db.get(ImportantDateRange, date_range_id)
    if date_range is None:
        return None

    changes = data.model_dump(exclude_unset=True)
    start_date = changes.get("start_date", date_range.start_date)
    end_date = changes.get("end_date", date_range.end_date)
    if end_date < start_date:
        raise InvalidInputError("end_date must not be before start_date")
    for field, value in changes.items():
        setattr(date_range, field, value)

    db.commit()
    db.refresh(date_range)
    return date_range


def delete_date_range(db: Session, date_range_id: int) -> bool:
    date_range = db.get(ImportantDateRange, date_range_id)
    if date_range is None:
        return False

    in_use = db.scalar(select(func.count()).select_from(Event).where(Event.date_range_id == date_range_id))
    if in_use:
        raise ConflictError(f"date_range {date_range_id} is used by {in_use} events")

    db.delete(date_range)
    db.commit()
    return True
