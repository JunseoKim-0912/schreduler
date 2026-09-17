from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models.sleep_log import SleepLog
from app.schemas.sleep_log import SleepLogCreate, SleepLogRead, SleepLogUpdate
from app.services import sleep_log_service

router = APIRouter(prefix="/sleep-logs", tags=["sleep-logs"])


@router.post("", response_model=SleepLogRead, status_code=status.HTTP_201_CREATED)
def create_sleep_log(data: SleepLogCreate, db: Session = Depends(get_db)) -> SleepLog:
    try:
        return sleep_log_service.create_sleep_log(db, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("", response_model=list[SleepLogRead])
def list_sleep_logs(
    user_id: int | None = Query(default=None), db: Session = Depends(get_db)
) -> list[SleepLog]:
    return sleep_log_service.list_sleep_logs(db, user_id=user_id)


@router.get("/{sleep_log_id}", response_model=SleepLogRead)
def get_sleep_log(sleep_log_id: int, db: Session = Depends(get_db)) -> SleepLog:
    sleep_log = sleep_log_service.get_sleep_log(db, sleep_log_id)
    if sleep_log is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sleep log not found")
    return sleep_log


@router.put("/{sleep_log_id}", response_model=SleepLogRead)
def update_sleep_log(
    sleep_log_id: int, data: SleepLogUpdate, db: Session = Depends(get_db)
) -> SleepLog:
    try:
        sleep_log = sleep_log_service.update_sleep_log(db, sleep_log_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if sleep_log is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sleep log not found")
    return sleep_log


@router.delete("/{sleep_log_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sleep_log(sleep_log_id: int, db: Session = Depends(get_db)) -> None:
    deleted = sleep_log_service.delete_sleep_log(db, sleep_log_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sleep log not found")
