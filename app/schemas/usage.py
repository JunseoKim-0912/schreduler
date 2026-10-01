from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class UsageTodayRead(BaseModel):
    spent_usd: float = Field(description="이 사용자가 오늘(APP_TIMEZONE 자정 기준) 쓴 LLM 비용", examples=[0.12])
    limit_usd: float = Field(description="이 사용자의 하루 한도 (관리자는 LLM_DAILY_BUDGET_ADMIN_USD가 있으면 그 값)", examples=[1.0])
    total_blocked: bool = Field(description="앱 전체 하루 한도에 도달해 모든 사용자의 LLM 호출이 막혔는지")
    resets_at: datetime = Field(description="한도가 다시 열리는 시각 (다음 APP_TIMEZONE 자정, 시간대 포함)")
    timezone: str = Field(description="하루 기준 시간대 (APP_TIMEZONE, IANA 이름)", examples=["America/Toronto"])
