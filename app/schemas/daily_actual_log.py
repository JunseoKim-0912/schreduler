from __future__ import annotations

from datetime import date as dt_date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import NonEmptyStr, PartialUpdate


class DailyActualLogCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "user_id": 1,
                    "date": "2026-09-24",
                    "summary_text": "스터디는 못 했지만 운동은 했다",
                    "actual_events": [
                        {
                            "title": "헬스",
                            "start": "19:00",
                            "end": "20:00"
                        }
                    ]
                }
            ]
        },
    )

    user_id: int
    date: dt_date
    summary_text: NonEmptyStr
    actual_events: list[Any] = Field(default_factory=list)


class DailyActualLogUpdate(PartialUpdate):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "summary_text": "스터디 대신 운동을 했다"
                }
            ]
        },
    )

    date: dt_date | None = None
    summary_text: NonEmptyStr | None = None
    actual_events: list[Any] | None = None


class DailyActualLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    date: dt_date
    summary_text: str
    actual_events: list[Any]
