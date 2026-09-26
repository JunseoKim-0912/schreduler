from __future__ import annotations

from datetime import date as dt_date
from datetime import datetime

from pydantic import BaseModel

from app.models.enums import ChildEventKind, CompletionMethod, EventInstanceStatus, EventType, Importance


class EventInstanceRead(BaseModel):
    """캘린더 한 칸에 그릴 일정 회차. 시각은 회차별 변경(override)을 반영한 실제 값이다."""

    # 회차가 없는 단발성 이벤트(/events로 만든 비반복 일정)는 null — 완료 처리할 회차가 없다.
    event_instance_id: int | None
    event_id: int
    date: dt_date
    status: EventInstanceStatus
    completion_method: CompletionMethod | None
    title: str
    event_type: EventType
    importance: Importance | None
    is_recurring: bool
    recurrence_rule: str | None
    parent_event_id: int | None
    child_kind: ChildEventKind | None
    start_time: datetime | None  # deadline이면 null
    end_time: datetime  # deadline이면 마감 일시
    time_overridden: bool  # 이 회차만 시간이 바뀌었는지
