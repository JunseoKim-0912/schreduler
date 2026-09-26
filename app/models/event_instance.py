from __future__ import annotations

from datetime import date as dt_date
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, Enum, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import CompletionMethod, EventInstanceStatus

if TYPE_CHECKING:
    from app.models.compliance_report import ComplianceReport
    from app.models.event import Event


class EventInstance(Base):
    __tablename__ = "event_instances"
    # 되돌리기가 삭제된 행을 원래 id로 다시 넣으므로, SQLite도 지운 id를 재사용하지 않게 한다.
    __table_args__ = {"sqlite_autoincrement": True}

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
    # 이 회차만 시각을 바꿀 때 쓴다 ("오늘 물리 퀴즈만 6시로"). NULL이면 Event의 시각을 따른다.
    start_time_override: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_time_override: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    event: Mapped["Event"] = relationship(back_populates="instances")
    compliance_reports: Mapped[list["ComplianceReport"]] = relationship(
        back_populates="event_instance", cascade="all, delete-orphan"
    )

    @property
    def effective_start(self) -> datetime | None:
        """이 회차의 실제 시작 시각. deadline 이벤트는 None."""
        if self.start_time_override is not None:
            return self.start_time_override
        if self.event.start_time is None:
            return None
        return datetime.combine(self.date, self.event.start_time.time())

    @property
    def effective_end(self) -> datetime:
        """이 회차의 실제 종료(마감) 시각. scheduled는 시작 + 원래 지속 시간이라 자정을 넘겨도 맞다."""
        if self.end_time_override is not None:
            return self.end_time_override
        event = self.event
        if event.start_time is None:
            return datetime.combine(self.date, event.end_time.time())
        return self.effective_start + (event.end_time - event.start_time)
