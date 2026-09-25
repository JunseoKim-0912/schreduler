from __future__ import annotations

from datetime import date as dt_date

from pydantic import BaseModel


class DailyPointsResult(BaseModel):
    user_id: int
    date: dt_date
    total_instances: int
    done_instances: int
    is_perfect_day: bool
    base_points: float
    streak_days: int
    streak_multiplier: float
    points_earned: float


class PointsSummaryRead(BaseModel):
    user_id: int
    today: DailyPointsResult  # 자정 잡이 기록하기 전이라 현재까지 상태로 실시간 계산한 값
    week_start: dt_date
    week_points: float
    total_points: float
    current_streak_days: int
