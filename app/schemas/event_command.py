from __future__ import annotations

from datetime import date as dt_date
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.common import NonEmptyStr

CommandAction = Literal["create", "delete", "update"]
CommandStatus = Literal["executed", "needs_confirmation", "needs_clarification", "not_found", "cancelled"]
TargetKind = Literal["event", "date_range"]
# 사용 중인 반복 기간을 지울 때 고르는 처리: 기간만(일정은 이미 만들어진 마지막 회차에서 끝남) / 일정도 함께
RangeDeleteOption = Literal["range_only", "with_events"]


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
    target_kind: TargetKind = "event"
    scope: Literal["instance", "series"] | None = None
    affected: list[CommandTarget] = []
    affected_count: int = 0
    candidates: list[CommandTarget] = []
    # needs_confirmation일 때: POST /events/commands/confirm에 보낼 토큰과 만료 시각(발급 후 10분)
    confirmation_token: str | None = None
    expires_at: datetime | None = None
    # 확인할 때 하나를 골라 POST /events/commands/confirm의 option으로 보낸다 (사용 중인 기간 삭제)
    options: list[RangeDeleteOption] = []
    # executed일 때: 되돌리기(POST /actions/{id}/undo)에 쓸 변경 기록 id
    action_id: int | None = None


class CommandConfirmRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"user_id": 1, "token": "3f2b…(needs_confirmation 응답의 토큰)"}]})

    user_id: int
    token: NonEmptyStr
    option: RangeDeleteOption | None = None  # command.options가 있을 때 그중 하나


class CommandConfirmResponse(BaseModel):
    message: str
    command: CommandResult
