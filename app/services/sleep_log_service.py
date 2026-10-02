from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import InvalidInputError
from app.models.sleep_log import SleepLog
from app.models.user import User
from app.schemas.sleep_log import SleepLogCreate, SleepLogUpdate
from app.services.common import require_owned


def create_sleep_log(db: Session, user: User, data: SleepLogCreate) -> SleepLog:
    sleep_log = SleepLog(**data.model_dump(), user_id=user.id)
    db.add(sleep_log)
    db.commit()
    db.refresh(sleep_log)
    return sleep_log


def get_sleep_log(db: Session, user: User, sleep_log_id: int) -> SleepLog:
    return require_owned(db, SleepLog, sleep_log_id, user.id, "sleep log")


def list_sleep_logs(db: Session, user: User) -> list[SleepLog]:
    return list(db.execute(select(SleepLog).where(SleepLog.user_id == user.id)).scalars().all())


def update_sleep_log(db: Session, sleep_log: SleepLog, data: SleepLogUpdate) -> SleepLog:
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


def delete_sleep_log(db: Session, sleep_log: SleepLog) -> None:
    db.delete(sleep_log)
    db.commit()
