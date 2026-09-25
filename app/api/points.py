from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.db import get_db
from app.core.openapi import CURRENT_USER
from app.models.user import User
from app.schemas.points import PointsSummaryRead
from app.services import points as points_service

router = APIRouter(prefix="/points", tags=["points"])


@router.get("/summary", response_model=PointsSummaryRead, summary="포인트 요약 (오늘/이번 주/누적)", responses=CURRENT_USER)
def get_points_summary(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> PointsSummaryRead:
    """오늘은 현재까지 완료 상태로 실시간 계산하고, 이번 주(월요일 시작)와 누적은 어제까지 기록된 포인트에 오늘 값을 더한다.

    `current_streak_days`는 오늘 일정이 남아 있으면 어제까지의 연속 일수, 오늘 전부 완료하면 오늘 포함, 오늘 하나라도 놓치면 0이다.
    """
    return points_service.get_points_summary(db, user.id)
