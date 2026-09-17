from __future__ import annotations

from datetime import date as dt_date
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class SleepLog(Base):
    """FR-7 실제 수면 시간 체크. 기상 직후 어제 취침/오늘 기상 시각을 묻는다."""

    __tablename__ = "sleep_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    date: Mapped[dt_date] = mapped_column(Date)
    actual_bedtime: Mapped[datetime]
    actual_wake_time: Mapped[datetime]

    user: Mapped["User"] = relationship(back_populates="sleep_logs")
