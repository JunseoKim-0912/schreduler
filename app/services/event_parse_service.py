from __future__ import annotations

from datetime import date, datetime, time

import httpx
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.i18n import render_message
from app.models.enums import ActionSource
from app.models.important_date_range import ImportantDateRange
from app.models.user import User
from app.schemas.event_command import CommandResult, CommandTarget
from app.schemas.event_parse import EventDraft, EventParseRequest, EventParseResponse
from app.services.llm_client import LLMResponseParsingError, fill_event_slots_for_user
from app.services.common import require
from app.services.recurrence import build_recurrence_rule
from app.services.event_command_service import (
    CommandDescription,
    command_result,
    execute,
    request_confirmation,
    resolve,
    to_command_target,
)
from app.services.slot_fill_session import SlotFillSession, create_pending_action, create_session, get_session


def _parse_hhmm(value: str) -> time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise LLMResponseParsingError(
            f"LLM이 채운 시각 슬롯이 HH:MM 형식이 아닙니다: {value!r}"
        ) from exc


def _resolve_anchor_date(db: Session, date_range_id: int | None) -> date:
    """이벤트의 반복 시작일로 쓸 날짜. date_range_id가 있으면 그 기간의
    start_date를, 없으면(또는 이미 지워졌으면) 오늘을 anchor로 쓴다."""
    if date_range_id is not None:
        date_range = db.get(ImportantDateRange, date_range_id)
        if date_range is not None:
            return date_range.start_date
    return date.today()


def _build_event_draft(db: Session, session: SlotFillSession) -> EventDraft:
    if not session.title or not session.start_time or not session.end_time or not session.frequency:
        raise LLMResponseParsingError(
            "세션이 is_complete인데 필수 슬롯(title/start_time/end_time/frequency) 중 "
            f"일부가 비어 있습니다: {session!r}"
        )

    anchor_date = _resolve_anchor_date(db, session.date_range_id)
    start_time = datetime.combine(anchor_date, _parse_hhmm(session.start_time))
    end_time = datetime.combine(anchor_date, _parse_hhmm(session.end_time))
    recurrence_rule = build_recurrence_rule(session.frequency, session.by_day)

    return EventDraft(
        user_id=session.user_id,
        title=session.title,
        start_time=start_time,
        end_time=end_time,
        importance=session.importance,
        is_recurring=True,
        recurrence_rule=recurrence_rule,
        date_range_id=session.date_range_id,
    )


def _to_response(db: Session, session: SlotFillSession, user: User) -> EventParseResponse:
    if session.is_complete:
        draft = _build_event_draft(db, session)
        # v3.6: 초안은 클라이언트가 확인하면 POST /events/commands/confirm으로 서버가 만든다 (되돌리기 기록 포함).
        pending = create_pending_action(user.id, "create", {"draft": draft.model_dump(mode="json")})
        return EventParseResponse(
            session_id=session.session_id,
            is_complete=True,
            draft=draft,
            message=render_message("command.confirm_create", user.preferred_language),
            command=CommandResult(
                action="create",
                status="needs_confirmation",
                affected=[CommandTarget(event_id=None, title=draft.title, date=draft.start_time.date(), is_recurring=True)],
                affected_count=1,
                confirmation_token=pending.token,
                expires_at=pending.expires_at,
            ),
        )

    next_question = session.clarifying_questions[0] if session.clarifying_questions else None
    return EventParseResponse(
        session_id=session.session_id,
        is_complete=False,
        next_question=next_question,
        missing_slots=list(session.missing_slots),
    )


def _handle_command(db: Session, user: User, session: SlotFillSession, desc: CommandDescription) -> EventParseResponse:
    """삭제·수정: 대상을 찾아 1개면 바로 실행, 여러 개면 확인 토큰, 애매하면 되묻는다."""
    resolution = resolve(db, user, desc)
    base = {"session_id": session.session_id, "intent": desc.intent}

    if resolution.status != "ready":
        # 되묻는 동안 요청을 세션에 남겨 두고, 다음 턴에 사용자의 답으로 보완한다.
        session.command = desc.to_dict()
        session.command_candidates = [
            f"{i}) {c.title} ({c.date})" for i, c in enumerate(map(to_command_target, resolution.candidates), start=1)
        ]
        status = "not_found" if resolution.status == "not_found" else "needs_clarification"
        return EventParseResponse(
            **base,
            is_complete=False,
            message=resolution.message,
            command=command_result(desc.intent, status, candidates=resolution.candidates),
        )

    session.command = None
    session.command_candidates = []
    if len(resolution.targets) == 1:
        result = execute(db, user, desc, resolution.targets, ActionSource.NL)
        return EventParseResponse(
            **base,
            is_complete=True,
            message=result.message,
            command=command_result(desc.intent, "executed", affected=result.affected, action_id=result.action.id),
        )

    pending, message = request_confirmation(user, desc, resolution.targets)
    return EventParseResponse(
        **base,
        is_complete=False,
        message=message,
        command=command_result(desc.intent, "needs_confirmation", targets=resolution.targets, pending=pending),
    )


def _pending_command_prompt(session: SlotFillSession) -> str | None:
    if session.command is None:
        return None
    text = CommandDescription.from_dict(session.command).describe_for_prompt()
    if session.command_candidates:
        text += " / 후보: " + ", ".join(session.command_candidates)
    return text


def parse_event_utterance(
    db: Session,
    data: EventParseRequest,
    *,
    http_client: httpx.Client | None = None,
) -> EventParseResponse:
    """FR-2 자연어 한 턴을 처리한다.

    LLM이 의도(create/delete/update/unknown)를 먼저 분류한다. create는 기존 슬롯필링(부족하면 되묻고, 다
    채워지면 초안 + 확인 토큰), delete/update는 event_command_service가 대상을 찾아 실행하거나 되묻는다.
    """
    # 없는 사용자면 세션을 만들거나 LLM을 부르기 전에 404로 끝낸다.
    user = require(db, User, data.user_id, "user_id")
    if data.session_id is None:
        session = create_session(data.user_id)
    else:
        session = get_session(data.session_id)
        if session is None:
            raise NotFoundError(f"session_id {data.session_id} does not exist")
        if session.user_id != data.user_id:
            raise NotFoundError(f"session_id {data.session_id} belongs to a different user")

    result = fill_event_slots_for_user(
        db,
        data.user_id,
        data.utterance,
        known_slots=session.known_slots(),
        pending_command=_pending_command_prompt(session),
        http_client=http_client,
    )

    if session.command is not None and result.intent != "create":
        # 되묻기에 대한 답: 이전 요청을 새로 알게 된 값으로 보완한다 (unknown으로 분류돼도 같은 요청으로 본다).
        desc = CommandDescription.from_dict(session.command).merged(CommandDescription.from_llm(result))
        return _handle_command(db, user, session, desc)
    if result.intent in ("delete", "update"):
        return _handle_command(db, user, session, CommandDescription.from_llm(result))

    session.command = None
    session.command_candidates = []
    if result.intent == "unknown":
        if session.known_slots():
            # 일정 추가 대화 중에 알아듣지 못한 답이 오면, 모은 슬롯을 지우지 않고 같은 질문을 다시 한다.
            return _to_response(db, session, user)
        return EventParseResponse(
            session_id=session.session_id,
            is_complete=False,
            intent="unknown",
            message=render_message("command.unknown", user.preferred_language),
        )

    session.apply(result)
    return _to_response(db, session, user)
