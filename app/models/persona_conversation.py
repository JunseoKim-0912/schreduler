from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Date, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.clock import local_wall_now
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.persona import Persona
    from app.models.user import User


class PersonaConversation(Base):
    """FR-9 페르소나 대화 로그. context_type은 어떤 상황에서의 대화인지 나타낸다
    (예: "event_start_check", "daily_checkin"). messages는 대화 turn들의 JSON 배열이다."""

    __tablename__ = "persona_conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    persona_id: Mapped[str] = mapped_column(ForeignKey("personas.name"))
    context_type: Mapped[str] = mapped_column(String(50))
    messages: Mapped[list[Any]] = mapped_column(JSON)
    # (사용자, 페르소나, 상황, 날짜)로 "오늘 대화"를 찾는다. 날짜가 바뀌면 새 대화가 된다 (체크인 요약이 그날 기준).
    conversation_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=local_wall_now)

    user: Mapped["User"] = relationship(back_populates="persona_conversations")
    persona: Mapped["Persona"] = relationship(back_populates="conversations")
