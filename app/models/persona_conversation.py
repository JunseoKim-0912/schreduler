from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
    persona_id: Mapped[str] = mapped_column(ForeignKey("personas.persona_id"))
    context_type: Mapped[str] = mapped_column(String(50))
    messages: Mapped[list[Any]] = mapped_column(JSON)

    user: Mapped["User"] = relationship(back_populates="persona_conversations")
    persona: Mapped["Persona"] = relationship(back_populates="conversations")
