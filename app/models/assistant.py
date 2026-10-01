"""일정 어시스턴트(v4)의 대화·제안 저장소 (docs/assistant_design.md §7).

대화 상태는 OpenAI 쪽(previous_response_id)이 아니라 여기에 둔다. 초안·확인 토큰처럼 정확해야 하는 상태도 같다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.user import User


class AssistantSession(Base):
    __tablename__ = "assistant_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # search_events가 이 세션에서 돌려준 ID. 제안 도구는 이 ID만 대상으로 받는다(지어낸 ID 거절).
    seen_ids: Mapped[dict[str, Any]] = mapped_column(JSON, default=lambda: {"events": [], "instances": []})
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)

    user: Mapped[User] = relationship()
    messages: Mapped[list[AssistantMessage]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="AssistantMessage.id"
    )
    proposals: Mapped[list[PendingProposal]] = relationship(back_populates="session", cascade="all, delete-orphan")
    turn_logs: Mapped[list[AssistantTurnLog]] = relationship(back_populates="session", cascade="all, delete-orphan")


class AssistantMessage(Base):
    """user: {"text"}. assistant: LLM 응답 하나 — {"output": 원본 output 배열(암호화된 reasoning 포함), "text",
    "response_id"}. tool: 도구 결과 하나 — {"call_id", "name", "output"}."""

    __tablename__ = "assistant_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("assistant_sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)

    session: Mapped[AssistantSession] = relationship(back_populates="messages")


class PendingProposal(Base):
    """한 턴에서 만든 성공한 초안 묶음. proposals는 [{draft_id, kind, card, payload}] — card는 화면용,
    payload는 실행용이다. status: pending / confirmed / cancelled / expired / superseded."""

    __tablename__ = "pending_proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("assistant_sessions.id"), index=True)
    token: Mapped[str] = mapped_column(String(64), unique=True)
    proposals: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    shown_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="pending")

    session: Mapped[AssistantSession] = relationship(back_populates="proposals")


class AssistantTurnLog(Base):
    """턴마다 비용·지연 추적 (LLM 호출 수, 토큰, 턴 전체 지연)."""

    __tablename__ = "assistant_turn_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("assistant_sessions.id"), index=True)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str] = mapped_column(String(100))
    reasoning_effort: Mapped[str] = mapped_column(String(16))
    # 정상 종료가 아니면 이유 (llm_call_limit / time_limit / llm_error / budget_limit)
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)

    session: Mapped[AssistantSession] = relationship(back_populates="turn_logs")
