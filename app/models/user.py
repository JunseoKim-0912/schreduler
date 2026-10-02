from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, false, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.clock import utc_now_naive
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.daily_actual_log import DailyActualLog
    from app.models.engagement_state import EngagementState
    from app.models.event import Event
    from app.models.important_date_range import ImportantDateRange
    from app.models.location import Location
    from app.models.persona import Persona
    from app.models.persona_conversation import PersonaConversation
    from app.models.points_ledger import PointsLedger
    from app.models.sleep_log import SleepLog


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("preferred_language IN ('ko', 'en')", name="ck_users_preferred_language"),
        UniqueConstraint("email", name="uq_users_email"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    selected_persona_id: Mapped[str | None] = mapped_column(
        ForeignKey("personas.name"), nullable=True
    )
    preferred_language: Mapped[str] = mapped_column(String(2), default="ko")
    telegram_chat_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    telegram_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)
    # Admins manage personas and get LLM_DAILY_BUDGET_ADMIN_USD when it is set.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Stored lowercased. Null only for accounts made before sign-in existed, until create_admin attaches one.
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Naive UTC, like the other audit timestamps.
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_naive, server_default=func.current_timestamp())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    important_date_ranges: Mapped[list["ImportantDateRange"]] = relationship(back_populates="user")
    events: Mapped[list["Event"]] = relationship(back_populates="user")
    locations: Mapped[list["Location"]] = relationship(back_populates="user")
    selected_persona: Mapped["Persona | None"] = relationship(back_populates="users")
    engagement_states: Mapped[list["EngagementState"]] = relationship(back_populates="user")
    sleep_logs: Mapped[list["SleepLog"]] = relationship(back_populates="user")
    daily_actual_logs: Mapped[list["DailyActualLog"]] = relationship(back_populates="user")
    persona_conversations: Mapped[list["PersonaConversation"]] = relationship(back_populates="user")
    points_ledger_entries: Mapped[list["PointsLedger"]] = relationship(back_populates="user")
