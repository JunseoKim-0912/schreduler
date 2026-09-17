from __future__ import annotations

from datetime import date as dt_date
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Date, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class DailyActualLog(Base):
    """FR-8 매일 밤 9시 하루 요약 체크인. 계획 캘린더와 별도 레이어로 "실제 하루"를
    기록한다. actual_events는 그날 실제로 있었던 일들을 담은 JSON이다."""

    __tablename__ = "daily_actual_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    date: Mapped[dt_date] = mapped_column(Date)
    summary_text: Mapped[str] = mapped_column(Text)
    actual_events: Mapped[list[Any]] = mapped_column(JSON)

    user: Mapped["User"] = relationship(back_populates="daily_actual_logs")
