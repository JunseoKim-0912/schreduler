from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models.daily_actual_log import DailyActualLog
from app.schemas.daily_actual_log import (
    DailyActualLogCreate,
    DailyActualLogRead,
    DailyActualLogUpdate,
)
from app.schemas.daily_checkin import DailyCheckinMessageRequest, DailyCheckinMessageResponse
from app.services import daily_actual_log_service
from app.services.daily_checkin import handle_daily_checkin_message
from app.services.llm_client import (
    LLMClientError,
    LLMConfigError,
    LLMRequestError,
    LLMResponseParsingError,
)

router = APIRouter(prefix="/daily-actual-logs", tags=["daily-actual-logs"])


@router.post("", response_model=DailyActualLogRead, status_code=status.HTTP_201_CREATED)
def create_daily_actual_log(
    data: DailyActualLogCreate, db: Session = Depends(get_db)
) -> DailyActualLog:
    try:
        return daily_actual_log_service.create_daily_actual_log(db, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# 정적 경로라서 상관없지만, GET/{daily_log_id}류 경로와 겹치지 않게 이 라우트를
# 위쪽에 둔다.
@router.post("/checkin", response_model=DailyCheckinMessageResponse)
def post_daily_checkin_message(
    data: DailyCheckinMessageRequest, db: Session = Depends(get_db)
) -> DailyCheckinMessageResponse:
    """FR-8 저녁 9시 체크인 대화 한 턴. 그날 놓친 일정 위주로 LLM이 대화한다."""
    try:
        return handle_daily_checkin_message(db, data.user_id, data.utterance, data.date)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except LLMConfigError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    except LLMResponseParsingError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except LLMRequestError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except LLMClientError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get("", response_model=list[DailyActualLogRead])
def list_daily_actual_logs(
    user_id: int | None = Query(default=None), db: Session = Depends(get_db)
) -> list[DailyActualLog]:
    return daily_actual_log_service.list_daily_actual_logs(db, user_id=user_id)


@router.get("/{daily_log_id}", response_model=DailyActualLogRead)
def get_daily_actual_log(daily_log_id: int, db: Session = Depends(get_db)) -> DailyActualLog:
    daily_log = daily_actual_log_service.get_daily_actual_log(db, daily_log_id)
    if daily_log is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Daily actual log not found")
    return daily_log


@router.put("/{daily_log_id}", response_model=DailyActualLogRead)
def update_daily_actual_log(
    daily_log_id: int, data: DailyActualLogUpdate, db: Session = Depends(get_db)
) -> DailyActualLog:
    try:
        daily_log = daily_actual_log_service.update_daily_actual_log(db, daily_log_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if daily_log is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Daily actual log not found")
    return daily_log


@router.delete("/{daily_log_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_daily_actual_log(daily_log_id: int, db: Session = Depends(get_db)) -> None:
    deleted = daily_actual_log_service.delete_daily_actual_log(db, daily_log_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Daily actual log not found")
