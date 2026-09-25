from __future__ import annotations

from datetime import date as dt_date

from pydantic import BaseModel, ConfigDict

from app.schemas.common import NonEmptyStr


class DailyCheckinMessageRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "user_id": 1,
                    "utterance": "오늘 너무 피곤해서 스터디를 못 했어"
                }
            ]
        },
    )

    user_id: int
    utterance: NonEmptyStr
    date: dt_date | None = None  # 생략하면 오늘 날짜
    conversation_id: int | None = None  # 생략하면 새 대화를 시작


class DailyCheckinMessageResponse(BaseModel):
    reply: str
    summary: str
    conversation_id: int | None  # 페르소나 미선택 시 대화가 저장되지 않아 None
