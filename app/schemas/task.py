from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator

from app.models.enums import EventInstanceStatus, Importance
from app.schemas.common import NonEmptyStr, RecurrenceRule


class TaskCreate(BaseModel):
    # deadline 전용 축약형이라 start_time 등 다른 Event 필드를 보내면 조용히 버리지 않고 거부한다.
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "title": "과제 제출",
                    "end_time": "2026-09-25T23:59:00",
                    "importance": 4
                },
                {
                    "title": "주간 리포트 제출",
                    "end_time": "2026-09-04T18:00:00",
                    "importance": 3,
                    "recurrence_rule": "FREQ=WEEKLY;BYDAY=FR",
                    "date_range_id": 1
                }
            ]
        },
    )

    title: NonEmptyStr
    end_time: datetime
    importance: Importance | None = None
    recurrence_rule: RecurrenceRule | None = None
    date_range_id: int | None = None

    @model_validator(mode="after")
    def check_recurrence(self) -> "TaskCreate":
        # 반복 인스턴스는 date_range 구간 안에서만 생성되므로, 없으면 완료 처리할 인스턴스가 하나도 안 생긴다.
        if self.recurrence_rule is not None and self.date_range_id is None:
            raise ValueError("recurring tasks require date_range_id")
        return self


class TaskRead(BaseModel):
    event_id: int
    event_instance_id: int | None  # 인스턴스가 없는 옛 deadline 이벤트(/events로 만든 단발성)는 null
    title: str
    importance: Importance | None
    due_at: datetime  # 이 인스턴스의 실제 마감 일시 (인스턴스 날짜 + end_time 시각)
    status: EventInstanceStatus | None
    completed: bool
    overdue: bool
    is_recurring: bool
    recurrence_rule: str | None
    date_range_id: int | None
