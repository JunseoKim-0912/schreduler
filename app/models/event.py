from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import ChildEventKind, Importance

if TYPE_CHECKING:
    from app.models.event_instance import EventInstance
    from app.models.important_date_range import ImportantDateRange
    from app.models.location import Location
    from app.models.user import User


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200))
    start_time: Mapped[datetime]
    end_time: Mapped[datetime]
    # NULL == "없음" (수면), 그 외에는 3절 중요도 체계(1~5, MAX)를 따른다.
    importance: Mapped[Importance | None] = mapped_column(
        Enum(Importance, native_enum=False, name="importance"), nullable=True
    )
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    recurrence_rule: Mapped[str | None] = mapped_column(String(200), nullable=True)
    date_range_id: Mapped[int | None] = mapped_column(
        ForeignKey("important_date_ranges.id"), nullable=True
    )
    parent_event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), nullable=True)
    # parent_event_id가 있을 때만 의미 있음 — TRAVEL(자동 이동시간) vs CUSTOM(사용자 지정 준비 등).
    child_kind: Mapped[ChildEventKind | None] = mapped_column(
        Enum(ChildEventKind, native_enum=False, name="child_event_kind"), nullable=True
    )
    # 이 이벤트가 열리는 장소. FR-5의 이동시간 Child 이벤트 생성 시 Location.default_travel_minutes를 참조한다.
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), nullable=True)

    user: Mapped["User"] = relationship(back_populates="events")
    date_range: Mapped["ImportantDateRange | None"] = relationship(back_populates="events")
    parent_event: Mapped["Event | None"] = relationship(
        remote_side="Event.id", back_populates="child_events"
    )
    child_events: Mapped[list["Event"]] = relationship(back_populates="parent_event")
    instances: Mapped[list["EventInstance"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    location: Mapped["Location | None"] = relationship(back_populates="events")
