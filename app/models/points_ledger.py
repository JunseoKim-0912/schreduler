from __future__ import annotations

from datetime import date as dt_date
from typing import TYPE_CHECKING

from sqlalchemy import Date, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class PointsLedger(Base):
    """FR-10 규칙성 기반 포인트. points_earned = base_points * streak_multiplier
    (3일 x1.1, 7일 x1.25, 14일 x1.5 - 기획서 6절 열린 질문 #2, 세부 숫자는 미확정)."""

    __tablename__ = "points_ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    date: Mapped[dt_date] = mapped_column(Date)
    base_points: Mapped[float] = mapped_column(Float)
    streak_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    points_earned: Mapped[float] = mapped_column(Float)

    user: Mapped["User"] = relationship(back_populates="points_ledger_entries")
