from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.exceptions import NotFoundError
from app.core.openapi import NOT_FOUND
from app.models.sleep_log import SleepLog
from app.schemas.sleep_log import SleepLogCreate, SleepLogRead, SleepLogUpdate
from app.services import sleep_log_service

router = APIRouter(prefix="/sleep-logs", tags=["sleep-logs"])


@router.post("", response_model=SleepLogRead, status_code=status.HTTP_201_CREATED, summary="수면 기록 생성", responses=NOT_FOUND)
def create_sleep_log(data: SleepLogCreate, db: Session = Depends(get_db)) -> SleepLog:
    """실제 취침·기상 시각을 기록한다 (FR-7). 기상이 취침보다 늦어야 한다."""
    return sleep_log_service.create_sleep_log(db, data)


@router.get("", response_model=list[SleepLogRead], summary="수면 기록 목록")
def list_sleep_logs(
    user_id: int | None = Query(default=None, description="이 사용자의 것만 조회"),
    db: Session = Depends(get_db),
) -> list[SleepLog]:
    """`user_id`로 특정 사용자의 기록만 거를 수 있다."""
    return sleep_log_service.list_sleep_logs(db, user_id=user_id)


@router.get("/{sleep_log_id}", response_model=SleepLogRead, summary="수면 기록 조회", responses=NOT_FOUND)
def get_sleep_log(sleep_log_id: int, db: Session = Depends(get_db)) -> SleepLog:
    """수면 기록 하나를 조회한다."""
    sleep_log = sleep_log_service.get_sleep_log(db, sleep_log_id)
    if sleep_log is None:
        raise NotFoundError("Sleep log not found")
    return sleep_log


@router.put("/{sleep_log_id}", response_model=SleepLogRead, summary="수면 기록 수정", responses=NOT_FOUND)
def update_sleep_log(
    sleep_log_id: int, data: SleepLogUpdate, db: Session = Depends(get_db)
) -> SleepLog:
    """보낸 필드만 수정한다."""
    sleep_log = sleep_log_service.update_sleep_log(db, sleep_log_id, data)
    if sleep_log is None:
        raise NotFoundError("Sleep log not found")
    return sleep_log


@router.delete("/{sleep_log_id}", status_code=status.HTTP_204_NO_CONTENT, summary="수면 기록 삭제", responses=NOT_FOUND)
def delete_sleep_log(sleep_log_id: int, db: Session = Depends(get_db)) -> None:
    """수면 기록을 삭제한다."""
    deleted = sleep_log_service.delete_sleep_log(db, sleep_log_id)
    if not deleted:
        raise NotFoundError("Sleep log not found")
