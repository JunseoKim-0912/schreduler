from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.persona_conversation import PersonaConversation
    from app.models.user import User


class Persona(Base):
    """가상 AI 캐릭터 페르소나 (FR-9). name이 식별자(PK)이고, 나머지 필드는
    언어별({"ko": ..., "en": ...}) JSON으로 저장한다 (FR-11)."""

    __tablename__ = "personas"

    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    display_name: Mapped[dict[str, Any]] = mapped_column(JSON)
    description: Mapped[dict[str, Any]] = mapped_column(JSON)
    example_lines: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    backstory: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    users: Mapped[list["User"]] = relationship(back_populates="selected_persona")
    conversations: Mapped[list["PersonaConversation"]] = relationship(back_populates="persona")
