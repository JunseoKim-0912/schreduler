from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.common import NonEmptyStr


class AssistantChatRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"session_id": None, "message": "10월 2일 11:00-1:00 스터디 추가해줘"}]}
    )

    session_id: int | None = None  # 생략하면 새 대화
    message: NonEmptyStr


class AssistantTokenRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"session_id": 1, "token": "Qm9…(proposal.token)"}]})

    session_id: int
    token: NonEmptyStr


class ProposalWarning(BaseModel):
    draft_id: str
    code: str
    message: str  # 사용자 언어


class AssistantProposal(BaseModel):
    """확인 카드. items는 초안 카드 목록이다 — kind별 모양은 /docs의 assistant 설명 참고.
    공통 필드: draft_id, kind, warnings, inferred_fields(추정 배지를 붙일 항목 이름)."""

    token: str
    expires_at: datetime
    items: list[dict[str, Any]]
    warnings: list[ProposalWarning]


class ExecutedAction(BaseModel):
    action_id: int  # POST /actions/{action_id}/undo로 되돌린다
    summary: str


class AssistantChatResponse(BaseModel):
    session_id: int
    reply: str
    # 이번 턴에 새로 만든 제안 (없으면 null — 이전 제안이 아직 대기 중일 수 있다)
    proposal: AssistantProposal | None = None
    # 이번 턴에 채팅 승인("좋아")으로 실행된 변경
    executed: list[ExecutedAction] = []


class AssistantConfirmResponse(BaseModel):
    session_id: int
    reply: str
    executed: list[ExecutedAction] = []


class AssistantMessageRead(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    created_at: datetime


class AssistantSessionRead(BaseModel):
    session_id: int | None  # 오늘 대화가 없으면 null
    messages: list[AssistantMessageRead] = []
    proposal: AssistantProposal | None = None  # 아직 대기 중인 제안
