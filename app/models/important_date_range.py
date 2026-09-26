from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.event import Event
    from app.models.user import User


class ImportantDateRange(Base):
    __tablename__ = "important_date_ranges"
    # 되돌리기가 삭제된 기간을 원래 id로 다시 넣으므로, SQLite도 지운 id를 재사용하지 않게 한다.
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(100))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)

    user: Mapped["User"] = relationship(back_populates="important_date_ranges")
    events: Mapped[list["Event"]] = relationship(back_populates="date_range")
