from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class UsageTodayRead(BaseModel):
    spent_usd: float = Field(description="이 사용자가 오늘(APP_TIMEZONE 자정 기준) 쓴 LLM 비용", examples=[0.12])
    limit_usd: float = Field(description="이 사용자의 하루 한도 (관리자는 LLM_DAILY_BUDGET_ADMIN_USD가 있으면 그 값)", examples=[1.0])
    total_blocked: bool = Field(description="전체 하루 한도에 도달했는지 (데모 계정은 데모 전체 한도, 그 외는 일반 전체 한도)")
    resets_at: datetime = Field(description="한도가 다시 열리는 시각 (다음 APP_TIMEZONE 자정, 시간대 포함)")
    is_demo: bool = Field(default=False, description="데모 계정이면 true — 한도는 DEMO_LLM_BUDGET_* 값이고 막히면 데모 안내가 나온다")
    timezone: str = Field(description="하루 기준 시간대 (APP_TIMEZONE, IANA 이름)", examples=["America/Toronto"])
