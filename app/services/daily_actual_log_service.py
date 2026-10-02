from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.daily_actual_log import DailyActualLog
from app.models.user import User
from app.schemas.daily_actual_log import DailyActualLogCreate, DailyActualLogUpdate
from app.services.common import require_owned


def create_daily_actual_log(db: Session, user: User, data: DailyActualLogCreate) -> DailyActualLog:
    daily_log = DailyActualLog(**data.model_dump(), user_id=user.id)
    db.add(daily_log)
    db.commit()
    db.refresh(daily_log)
    return daily_log


def get_daily_actual_log(db: Session, user: User, daily_log_id: int) -> DailyActualLog:
    return require_owned(db, DailyActualLog, daily_log_id, user.id, "daily actual log")


def list_daily_actual_logs(db: Session, user: User) -> list[DailyActualLog]:
    return list(db.execute(select(DailyActualLog).where(DailyActualLog.user_id == user.id)).scalars().all())


def update_daily_actual_log(db: Session, daily_log: DailyActualLog, data: DailyActualLogUpdate) -> DailyActualLog:
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(daily_log, field, value)

    db.commit()
    db.refresh(daily_log)
    return daily_log


def delete_daily_actual_log(db: Session, daily_log: DailyActualLog) -> None:
    db.delete(daily_log)
    db.commit()
