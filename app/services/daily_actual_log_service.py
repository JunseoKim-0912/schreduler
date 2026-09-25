from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.daily_actual_log import DailyActualLog
from app.models.user import User
from app.schemas.daily_actual_log import DailyActualLogCreate, DailyActualLogUpdate
from app.services.common import require


def create_daily_actual_log(db: Session, data: DailyActualLogCreate) -> DailyActualLog:
    require(db, User, data.user_id, "user_id")
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
