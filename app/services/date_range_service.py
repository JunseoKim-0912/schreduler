from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.important_date_range import ImportantDateRange
from app.models.user import User
from app.schemas.date_range import DateRangeCreate, DateRangeUpdate


def _ensure_user_exists(db: Session, data: DateRangeCreate | DateRangeUpdate) -> None:
    user_id = getattr(data, "user_id", None)
    if user_id is not None and db.get(User, user_id) is None:
        raise ValueError(f"user_id {user_id} does not exist")


def create_date_range(db: Session, data: DateRangeCreate) -> ImportantDateRange:
    _ensure_user_exists(db, data)
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

    _ensure_user_exists(db, data)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(date_range, field, value)

    db.commit()
    db.refresh(date_range)
    return date_range


def delete_date_range(db: Session, date_range_id: int) -> bool:
    date_range = db.get(ImportantDateRange, date_range_id)
    if date_range is None:
        return False

    db.delete(date_range)
    db.commit()
    return True
