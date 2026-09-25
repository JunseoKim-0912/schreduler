from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator

from app.core.exceptions import InvalidInputError
from app.models.enums import ChildEventKind, EventType, Importance
from app.schemas.common import NonEmptyStr, PartialUpdate, RecurrenceRule


def validate_recurrence(is_recurring: bool, recurrence_rule: str | None) -> None:
    """is_recurring과 recurrence_rule은 함께 켜지고 함께 꺼진다. 한쪽만 있으면 반복 인스턴스가 조용히 안 생긴다."""
    if is_recurring and not recurrence_rule:
        raise InvalidInputError("recurring events require recurrence_rule")
    if not is_recurring and recurrence_rule:
        raise InvalidInputError("recurrence_rule is only allowed when is_recurring is true")


class InvalidEventTimesError(InvalidInputError):
    """event_type과 start_time/end_time 조합이 규칙에 맞지 않을 때."""


def validate_event_times(event_type: EventType, start_time: datetime | None, end_time: datetime | None) -> None:
    """SCHEDULED는 start_time/end_time이 둘 다 필요하고, DEADLINE은 start_time이 반드시 null이다."""
    if end_time is None:
        raise InvalidEventTimesError("end_time is required")
    if event_type == EventType.DEADLINE:
        if start_time is not None:
            raise InvalidEventTimesError("deadline events must have start_time = null (end_time is the deadline)")
        return
    if start_time is None:
        raise InvalidEventTimesError("scheduled events require both start_time and end_time")
    if end_time <= start_time:
        raise InvalidEventTimesError("end_time must be after start_time")


class EventCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "user_id": 1,
                    "title": "알고리즘 스터디",
                    "event_type": "scheduled",
                    "start_time": "2026-09-01T19:00:00",
                    "end_time": "2026-09-01T21:00:00",
                    "importance": 3,
                    "is_recurring": True,
                    "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU",
                    "date_range_id": 1
                },
                {
                    "user_id": 1,
                    "title": "과제 제출",
                    "event_type": "deadline",
                    "end_time": "2026-09-25T23:59:00",
                    "importance": 4
                }
            ]
        },
    )

    user_id: int
    title: NonEmptyStr
    event_type: EventType = EventType.SCHEDULED
    start_time: datetime | None = None
    end_time: datetime
    importance: Importance | None = None
    is_recurring: bool = False
    recurrence_rule: RecurrenceRule | None = None
    date_range_id: int | None = None
    parent_event_id: int | None = None
    child_kind: ChildEventKind | None = None
    location_id: int | None = None

    @model_validator(mode="after")
    def check_times(self) -> "EventCreate":
        validate_event_times(self.event_type, self.start_time, self.end_time)
        validate_recurrence(self.is_recurring, self.recurrence_rule)
        return self


class EventUpdate(PartialUpdate):
    NULLABLE_FIELDS = frozenset(
        {"start_time", "importance", "recurrence_rule", "date_range_id", "parent_event_id", "child_kind", "location_id"}
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "title": "알고리즘 스터디 (심화)",
                    "end_time": "2026-09-01T21:30:00"
                }
            ]
        },
    )

    title: NonEmptyStr | None = None
    event_type: EventType | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    importance: Importance | None = None
    is_recurring: bool | None = None
    recurrence_rule: RecurrenceRule | None = None
    date_range_id: int | None = None
    parent_event_id: int | None = None
    child_kind: ChildEventKind | None = None
    location_id: int | None = None

    @model_validator(mode="after")
    def check_times(self) -> "EventUpdate":
        # 부분 수정이라 여기서는 요청에 담긴 값끼리만 검사한다. 기존 값과 합친 최종 상태는
        # event_service.update_event가 validate_event_times로 다시 검사한다.
        if self.event_type == EventType.DEADLINE and self.start_time is not None:
            raise ValueError("deadline events must have start_time = null (end_time is the deadline)")
        if (
            self.event_type == EventType.SCHEDULED
            and "start_time" in self.model_fields_set
            and self.start_time is None
        ):
            raise ValueError("scheduled events require both start_time and end_time")
        if (
            self.start_time is not None
            and self.end_time is not None
            and self.end_time <= self.start_time
        ):
            raise ValueError("end_time must be after start_time")
        return self


class EventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    title: str
    event_type: EventType
    start_time: datetime | None
    end_time: datetime
    importance: Importance | None
    is_recurring: bool
    recurrence_rule: str | None
    date_range_id: int | None
    parent_event_id: int | None
    child_kind: ChildEventKind | None
    location_id: int | None
