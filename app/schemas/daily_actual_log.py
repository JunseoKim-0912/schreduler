from __future__ import annotations

from datetime import date as dt_date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DailyActualLogCreate(BaseModel):
    user_id: int
    date: dt_date
    summary_text: str
    actual_events: list[Any] = Field(default_factory=list)


class DailyActualLogUpdate(BaseModel):
    date: dt_date | None = None
    summary_text: str | None = None
    actual_events: list[Any] | None = None


class DailyActualLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    date: dt_date
    summary_text: str
    actual_events: list[Any]
