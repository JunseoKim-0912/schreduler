from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.db import get_db
from app.core.openapi import CURRENT_USER
from app.models.user import User
from app.schemas.usage import UsageTodayRead
from app.services import llm_usage

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("/today", response_model=UsageTodayRead, summary="오늘 AI(LLM) 사용량과 한도", responses=CURRENT_USER)
def get_usage_today(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> UsageTodayRead:
    """입력창 옆 사용량 표시용. `spent_usd >= limit_usd`이거나 `total_blocked`면 LLM을 부르는 요청은 429로 거절된다."""
    current = llm_usage.budget_status(db, user)
    return UsageTodayRead(
        spent_usd=round(current.spent_usd, 6),
        limit_usd=current.limit_usd,
        total_blocked=current.total_blocked,
        resets_at=current.resets_at,
        is_demo=user.is_demo,
        timezone=settings.app_timezone,
    )
