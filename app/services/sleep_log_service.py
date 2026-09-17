from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.sleep_log import SleepLog
from app.models.user import User
from app.schemas.sleep_log import SleepLogCreate, SleepLogUpdate


def _ensure_user_exists(db: Session, data: SleepLogCreate | SleepLogUpdate) -> None:
    user_id = getattr(data, "user_id", None)
    if user_id is not None and db.get(User, user_id) is None:
        raise ValueError(f"user_id {user_id} does not exist")


def create_sleep_log(db: Session, data: SleepLogCreate) -> SleepLog:
    _ensure_user_exists(db, data)
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

    _ensure_user_exists(db, data)
    for field, value in data.model_dump(exclude_unset=True).items():
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
