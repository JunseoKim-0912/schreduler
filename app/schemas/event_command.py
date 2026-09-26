from __future__ import annotations

from datetime import date as dt_date
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.common import NonEmptyStr

CommandAction = Literal["create", "delete", "update"]
CommandStatus = Literal["executed", "needs_confirmation", "needs_clarification", "not_found"]


class CommandTarget(BaseModel):
    """영향받는(또는 후보) 일정 하나. event_instance_id가 null이면 반복 전체(시리즈)를 가리킨다."""

    event_id: int | None  # 아직 만들지 않은 초안(create 확인 대기)은 null
    event_instance_id: int | None = None
    title: str
    date: dt_date | None = None
    is_recurring: bool = False


class CommandResult(BaseModel):
    action: CommandAction
    status: CommandStatus
    scope: Literal["instance", "series"] | None = None
    affected: list[CommandTarget] = []
    affected_count: int = 0
    candidates: list[CommandTarget] = []
    # needs_confirmation일 때: POST /events/commands/confirm에 보낼 토큰과 만료 시각(발급 후 10분)
    confirmation_token: str | None = None
    expires_at: datetime | None = None
    # executed일 때: 되돌리기(POST /actions/{id}/undo)에 쓸 변경 기록 id
    action_id: int | None = None


class CommandConfirmRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"user_id": 1, "token": "3f2b…(needs_confirmation 응답의 토큰)"}]})

    user_id: int
    token: NonEmptyStr


class CommandConfirmResponse(BaseModel):
    message: str
    command: CommandResult
