from __future__ import annotations

from datetime import date as dt_date

from pydantic import BaseModel


class CommandTarget(BaseModel):
    """변경이 영향을 준 일정 하나. event_instance_id가 null이면 반복 전체(시리즈)를 가리킨다."""

    event_id: int | None
    event_instance_id: int | None = None
    title: str
    date: dt_date | None = None
    is_recurring: bool = False


class NewDateRangeDraft(BaseModel):
    """일정과 함께 확정할 때 새로 만들 반복 기간. 같은 이름의 기간이 그사이 생겼으면 그것을 쓴다."""

    name: str
    start_date: dt_date
    end_date: dt_date


class NewLocationDraft(BaseModel):
    """일정과 함께 확정할 때 새로 등록할 장소. 같은 이름의 장소가 그사이 생겼으면 그것을 쓴다."""

    name: str
    default_travel_minutes: int
