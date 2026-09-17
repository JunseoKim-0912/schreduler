from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.daily_actual_log import DailyActualLog
from app.models.user import User
from app.schemas.daily_actual_log import DailyActualLogCreate, DailyActualLogUpdate


def _ensure_user_exists(db: Session, data: DailyActualLogCreate | DailyActualLogUpdate) -> None:
    user_id = getattr(data, "user_id", None)
    if user_id is not None and db.get(User, user_id) is None:
        raise ValueError(f"user_id {user_id} does not exist")


def create_daily_actual_log(db: Session, data: DailyActualLogCreate) -> DailyActualLog:
    _ensure_user_exists(db, data)
    daily_log = DailyActualLog(**data.model_dump())
    db.add(daily_log)
    db.commit()
    db.refresh(daily_log)
    return daily_log


def get_daily_actual_log(db: Session, daily_log_id: int) -> DailyActualLog | None:
    return db.get(DailyActualLog, daily_log_id)


def list_daily_actual_logs(db: Session, user_id: int | None = None) -> list[DailyActualLog]:
    stmt = select(DailyActualLog)
    if user_id is not None:
        stmt = stmt.where(DailyActualLog.user_id == user_id)
    return list(db.execute(stmt).scalars().all())


def update_daily_actual_log(
    db: Session, daily_log_id: int, data: DailyActualLogUpdate
) -> DailyActualLog | None:
    daily_log = db.get(DailyActualLog, daily_log_id)
    if daily_log is None:
        return None

    _ensure_user_exists(db, data)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(daily_log, field, value)

    db.commit()
    db.refresh(daily_log)
    return daily_log


def delete_daily_actual_log(db: Session, daily_log_id: int) -> bool:
    daily_log = db.get(DailyActualLog, daily_log_id)
    if daily_log is None:
        return False

    db.delete(daily_log)
    db.commit()
    return True
