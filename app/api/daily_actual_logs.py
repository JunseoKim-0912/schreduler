from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.exceptions import NotFoundError
from app.core.openapi import LLM_ERRORS, NOT_FOUND
from app.models.daily_actual_log import DailyActualLog
from app.schemas.daily_actual_log import (
    DailyActualLogCreate,
    DailyActualLogRead,
    DailyActualLogUpdate,
)
from app.schemas.daily_checkin import DailyCheckinMessageRequest, DailyCheckinMessageResponse
from app.services import daily_actual_log_service
from app.services.daily_checkin import handle_daily_checkin_message

router = APIRouter(prefix="/daily-actual-logs", tags=["daily-actual-logs"])


@router.post("", response_model=DailyActualLogRead, status_code=status.HTTP_201_CREATED, summary="하루 실제 기록 생성", responses=NOT_FOUND)
def create_daily_actual_log(
    data: DailyActualLogCreate, db: Session = Depends(get_db)
) -> DailyActualLog:
    """그날 실제로 한 일을 기록한다 (FR-8). 보통 저녁 체크인 알림을 받은 뒤 클라이언트가 보낸다."""
    return daily_actual_log_service.create_daily_actual_log(db, data)


# 정적 경로라서 상관없지만, GET/{daily_log_id}류 경로와 겹치지 않게 이 라우트를
# 위쪽에 둔다.
@router.post("/checkin", response_model=DailyCheckinMessageResponse, summary="저녁 체크인 대화 한 턴", responses={**NOT_FOUND, **LLM_ERRORS})
def post_daily_checkin_message(
    data: DailyCheckinMessageRequest, db: Session = Depends(get_db)
) -> DailyCheckinMessageResponse:
    """FR-8 저녁 9시 체크인 대화 한 턴. 그날 놓친 일정 위주로 LLM이 대화한다."""
    return handle_daily_checkin_message(
        db, data.user_id, data.utterance, data.date, data.conversation_id
    )


@router.get("", response_model=list[DailyActualLogRead], summary="하루 실제 기록 목록")
def list_daily_actual_logs(
    user_id: int | None = Query(default=None, description="이 사용자의 것만 조회"),
    db: Session = Depends(get_db),
) -> list[DailyActualLog]:
    """`user_id`로 특정 사용자의 기록만 거를 수 있다."""
    return daily_actual_log_service.list_daily_actual_logs(db, user_id=user_id)


@router.get("/{daily_log_id}", response_model=DailyActualLogRead, summary="하루 실제 기록 조회", responses=NOT_FOUND)
def get_daily_actual_log(daily_log_id: int, db: Session = Depends(get_db)) -> DailyActualLog:
    """기록 하나를 조회한다."""
    daily_log = daily_actual_log_service.get_daily_actual_log(db, daily_log_id)
    if daily_log is None:
        raise NotFoundError("Daily actual log not found")
    return daily_log


@router.put("/{daily_log_id}", response_model=DailyActualLogRead, summary="하루 실제 기록 수정", responses=NOT_FOUND)
def update_daily_actual_log(
    daily_log_id: int, data: DailyActualLogUpdate, db: Session = Depends(get_db)
) -> DailyActualLog:
    """보낸 필드만 수정한다."""
    daily_log = daily_actual_log_service.update_daily_actual_log(db, daily_log_id, data)
    if daily_log is None:
        raise NotFoundError("Daily actual log not found")
    return daily_log


@router.delete("/{daily_log_id}", status_code=status.HTTP_204_NO_CONTENT, summary="하루 실제 기록 삭제", responses=NOT_FOUND)
def delete_daily_actual_log(daily_log_id: int, db: Session = Depends(get_db)) -> None:
    """기록을 삭제한다."""
    deleted = daily_actual_log_service.delete_daily_actual_log(db, daily_log_id)
    if not deleted:
        raise NotFoundError("Daily actual log not found")
