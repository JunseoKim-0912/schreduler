from __future__ import annotations

from datetime import date as dt_date
from typing import TYPE_CHECKING

from sqlalchemy import Date, Enum, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import CompletionMethod, EventInstanceStatus

if TYPE_CHECKING:
    from app.models.event import Event


class EventInstance(Base):
    __tablename__ = "event_instances"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    date: Mapped[dt_date] = mapped_column(Date)
    status: Mapped[EventInstanceStatus] = mapped_column(
        Enum(EventInstanceStatus, native_enum=False, name="event_instance_status"),
        default=EventInstanceStatus.PENDING,
    )
    completion_method: Mapped[CompletionMethod | None] = mapped_column(
        Enum(CompletionMethod, native_enum=False, name="completion_method"), nullable=True
    )

    event: Mapped["Event"] = relationship(back_populates="instances")
