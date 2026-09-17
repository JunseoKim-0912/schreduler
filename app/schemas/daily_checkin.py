from __future__ import annotations

from datetime import date as dt_date

from pydantic import BaseModel


class DailyCheckinMessageRequest(BaseModel):
    user_id: int
    utterance: str
    date: dt_date | None = None  # 생략하면 오늘 날짜


class DailyCheckinMessageResponse(BaseModel):
    reply: str
    summary: str
