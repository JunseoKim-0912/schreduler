from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.event import Event
    from app.models.important_date_range import ImportantDateRange
    from app.models.location import Location


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("preferred_language IN ('ko', 'en')", name="ck_users_preferred_language"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    # Persona 모델이 생기면 ForeignKey("personas.persona_id")로 연결한다.
    selected_persona_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    preferred_language: Mapped[str] = mapped_column(String(2), default="ko")
    telegram_chat_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    telegram_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)

    important_date_ranges: Mapped[list["ImportantDateRange"]] = relationship(back_populates="user")
    events: Mapped[list["Event"]] = relationship(back_populates="user")
    locations: Mapped[list["Location"]] = relationship(back_populates="user")
