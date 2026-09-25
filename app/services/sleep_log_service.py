from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import InvalidInputError
from app.models.sleep_log import SleepLog
from app.models.user import User
from app.schemas.sleep_log import SleepLogCreate, SleepLogUpdate
from app.services.common import require


def create_sleep_log(db: Session, data: SleepLogCreate) -> SleepLog:
    require(db, User, data.user_id, "user_id")
    sleep_log = SleepLog(**data.model_dump())
    db.add(sleep_log)
    db.commit()
    db.refresh(sleep_log)
    return sleep_log


def get_sleep_log(db: Session, sleep_log_id: int) -> SleepLog | None:
    return db.get(SleepLog, sleep_log_id)


def list_sleep_logs(db: Session, user_id: int | None = None) -> list[SleepLog]:
    stmt = select(SleepLog)
    if user_id is not None:
        stmt = stmt.where(SleepLog.user_id == user_id)
    return list(db.execute(stmt).scalars().all())


def update_sleep_log(db: Session, sleep_log_id: int, data: SleepLogUpdate) -> SleepLog | None:
    sleep_log = db.get(SleepLog, sleep_log_id)
    if sleep_log is None:
        return None

    changes = data.model_dump(exclude_unset=True)
    bedtime = changes.get("actual_bedtime", sleep_log.actual_bedtime)
    wake_time = changes.get("actual_wake_time", sleep_log.actual_wake_time)
    if wake_time <= bedtime:
        raise InvalidInputError("actual_wake_time must be after actual_bedtime")
    for field, value in changes.items():
        setattr(sleep_log, field, value)

    db.commit()
    db.refresh(sleep_log)
    return sleep_log


def delete_sleep_log(db: Session, sleep_log_id: int) -> bool:
    sleep_log = db.get(SleepLog, sleep_log_id)
    if sleep_log is None:
        return False

    db.delete(sleep_log)
    db.commit()
    return True
