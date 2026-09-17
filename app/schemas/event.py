from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator

from app.models.enums import ChildEventKind, Importance


class EventCreate(BaseModel):
    user_id: int
    title: str
    start_time: datetime
    end_time: datetime
    importance: Importance | None = None
    is_recurring: bool = False
    recurrence_rule: str | None = None
    date_range_id: int | None = None
    parent_event_id: int | None = None
    child_kind: ChildEventKind | None = None
    location_id: int | None = None

    @model_validator(mode="after")
    def check_time_range(self) -> "EventCreate":
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class EventUpdate(BaseModel):
    title: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    importance: Importance | None = None
    is_recurring: bool | None = None
    recurrence_rule: str | None = None
    date_range_id: int | None = None
    parent_event_id: int | None = None
    child_kind: ChildEventKind | None = None
    location_id: int | None = None

    @model_validator(mode="after")
    def check_time_range(self) -> "EventUpdate":
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
    start_time: datetime
    end_time: datetime
    importance: Importance | None
    is_recurring: bool
    recurrence_rule: str | None
    date_range_id: int | None
    parent_event_id: int | None
    child_kind: ChildEventKind | None
    location_id: int | None
