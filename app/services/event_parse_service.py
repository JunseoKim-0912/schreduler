from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

import httpx
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.i18n import render_message
from app.models.enums import ActionSource
from app.models.important_date_range import ImportantDateRange
from app.models.user import User
from app.schemas.event_command import CommandResult, CommandTarget
from app.schemas.event_parse import EventDraft, EventParseRequest, EventParseResponse
from app.services.llm_client import ClarifyingQuestion, LLMResponseParsingError, SlotName, fill_event_slots_for_user
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


RECURRENCE_SLOTS: tuple[SlotName, ...] = ("frequency", "by_day", "date_range_id")

# 반복 여부를 물었을 때 단발이라는 답. LLM이 다시 반복을 묻더라도 백엔드가 단발로 고정한다.
_ONE_OFF_ANSWER = re.compile(
    r"반복\s*(없|안|하지\s*않|x)|한\s*번|이번\s*만|단발|일회|딱\s*하루|once|one[- ]?time|no\s+repeat|not\s+recurring|don'?t\s+repeat",
    re.IGNORECASE,
)
# "8시", "8:30"처럼 시각만 있고 오전/오후를 알 수 없는 표현. "1시간"은 길이라서 뺀다.
_CLOCK_HOUR = re.compile(r"(?<![\d:])(\d{1,2})\s*(?:시(?!간)|:[0-5]\d)")
_MERIDIEM_WORD = re.compile(r"오전|오후|아침|점심|저녁|밤|새벽|낮|정오|자정|(?<![a-z])[ap]\.?m(?![a-z])", re.IGNORECASE)
_PM_ANSWER = re.compile(r"오후|저녁|밤|낮|(?<![a-z])p\.?m(?![a-z])", re.IGNORECASE)
_AM_ANSWER = re.compile(r"오전|아침|새벽|(?<![a-z])a\.?m(?![a-z])", re.IGNORECASE)
_DATE_SLOT = re.compile(r"(?:(\d{4})-)?(\d{1,2})-(\d{1,2})")


def ambiguous_hours(utterance: str) -> set[int]:
    """오전/오후를 알 수 없는 시(1~11). 발화에 오전·오후·저녁 같은 말이 하나라도 있으면 애매하지 않다고 본다.
    12시는 보통 낮 12시라 묻지 않고, 13시 이상은 24시간제라 분명하다."""
    if _MERIDIEM_WORD.search(utterance):
        return set()
    return {int(h) for h in _CLOCK_HOUR.findall(utterance) if 1 <= int(h) <= 11}


def meridiem_answer(utterance: str) -> bool | None:
    """오전/오후 질문에 대한 답: 오후면 True, 오전이면 False, 알 수 없으면 None."""
    pm, am = bool(_PM_ANSWER.search(utterance)), bool(_AM_ANSWER.search(utterance))
    return pm if pm != am else None


def resolve_event_date(value: str, today: date) -> date:
    """date 슬롯 값을 날짜로. 연도 없는 MM-DD는 오늘 이후(오늘 포함) 가장 가까운 그 날짜로 정한다."""
    match = _DATE_SLOT.fullmatch(value.strip())
    if match is None:
        raise LLMResponseParsingError(f"LLM이 채운 날짜 슬롯이 YYYY-MM-DD/MM-DD 형식이 아닙니다: {value!r}")
    year, month, day = match.groups()
    try:
        if year:
            return date(int(year), int(month), int(day))
        for candidate_year in range(today.year, today.year + 5):  # 2월 29일은 다음 윤년까지 찾는다
            try:
                candidate = date(candidate_year, int(month), int(day))
            except ValueError:
                continue
            if candidate >= today:
                return candidate
    except ValueError:
        pass
    raise LLMResponseParsingError(f"LLM이 채운 날짜 슬롯이 올바른 날짜가 아닙니다: {value!r}")


def _is_recurring(session: SlotFillSession) -> bool:
    return session.frequency is not None or "frequency" in session.missing_slots


def _drop_slots(session: SlotFillSession, slots: tuple[SlotName, ...]) -> None:
    session.missing_slots = [slot for slot in session.missing_slots if slot not in slots]
    session.clarifying_questions = [q for q in session.clarifying_questions if q.slot not in slots]


def _make_one_off(session: SlotFillSession) -> None:
    session.one_off = True
    session.frequency = None
    session.by_day = None
    session.date_range_id = None
    _drop_slots(session, RECURRENCE_SLOTS)


def _apply_meridiem(session: SlotFillSession, pm: bool) -> None:
    """되물은 오전/오후 답으로 시작 시각을 정하고, 종료 시각도 같은 만큼 옮긴다 (길이 유지)."""
    hour = session.meridiem_hour + (12 if pm else 0)
    start = datetime.combine(date.today(), _parse_hhmm(session.start_time))
    delta = start.replace(hour=hour) - start
    session.start_time = f"{hour:02d}:{start.minute:02d}"
    if session.end_time:
        session.end_time = (datetime.combine(date.today(), _parse_hhmm(session.end_time)) + delta).strftime("%H:%M")
    session.meridiem_hour = None


def _answer_locally(session: SlotFillSession, utterance: str) -> bool:
    """일정 추가 중 되물은 질문에 대한 짧은 답(오전/오후, 단발)은 LLM을 부르지 않고 처리한다. 처리했으면 True."""
    if session.command is not None or not session.known_slots():
        return False
    if session.meridiem_hour is not None:
        pm = meridiem_answer(utterance)
        if pm is not None:
            _apply_meridiem(session, pm)
            return True
        session.meridiem_hour = None  # 한 번만 묻는다 — 알아듣지 못하면 처음 해석(오전)대로 두고 LLM에 넘긴다
        return False
    asked = session.clarifying_questions[0].slot if session.clarifying_questions else None
    if asked in RECURRENCE_SLOTS and _ONE_OFF_ANSWER.search(utterance):
        _make_one_off(session)
        return True
    return False


def _enforce_create_rules(session: SlotFillSession, utterance: str, known_before: dict[str, object], language: str) -> None:
    """LLM 결과를 세션에 합친 뒤 백엔드가 보장하는 규칙: 단발로 답했으면 단발 유지, 애매한 시각은 한 번 묻기,
    단발 일정은 날짜 필수."""
    if session.one_off:
        _make_one_off(session)

    new_start = session.start_time and "start_time" not in session.missing_slots and "start_time" not in known_before
    if new_start and not session.meridiem_asked:
        hour = _parse_hhmm(session.start_time).hour % 12
        if hour in ambiguous_hours(utterance):
            session.meridiem_hour = hour
            session.meridiem_asked = True

    if not _is_recurring(session) and "date" not in session.missing_slots:
        valid = False
        if session.date:
            try:
                resolve_event_date(session.date, date.today())
                valid = True
            except LLMResponseParsingError:
                session.date = None
        if not valid:
            session.missing_slots.append("date")
            session.clarifying_questions.append(
                ClarifyingQuestion(slot="date", question=render_message("command.ask_event_date", language))
            )


def _resolve_anchor_date(db: Session, date_range_id: int | None) -> date:
    """이벤트의 반복 시작일로 쓸 날짜. date_range_id가 있으면 그 기간의
    start_date를, 없으면(또는 이미 지워졌으면) 오늘을 anchor로 쓴다."""
    if date_range_id is not None:
        date_range = db.get(ImportantDateRange, date_range_id)
        if date_range is not None:
            return date_range.start_date
    return date.today()


def _build_event_draft(db: Session, session: SlotFillSession) -> EventDraft:
    recurring = session.frequency is not None
    if not session.title or not session.start_time or not session.end_time or not (recurring or session.date):
        raise LLMResponseParsingError(
            "세션이 is_complete인데 필수 슬롯(title/start_time/end_time, 단발이면 date) 중 "
            f"일부가 비어 있습니다: {session!r}"
        )

    if session.date:
        day = resolve_event_date(session.date, date.today())
    else:
        day = _resolve_anchor_date(db, session.date_range_id)
    start_time = datetime.combine(day, _parse_hhmm(session.start_time))
    end_time = datetime.combine(day, _parse_hhmm(session.end_time))
    if end_time <= start_time:  # 23:00–00:30처럼 자정을 넘기면 다음 날 끝난다
        end_time += timedelta(days=1)

    return EventDraft(
        user_id=session.user_id,
        title=session.title,
        start_time=start_time,
        end_time=end_time,
        importance=session.importance,
        is_recurring=recurring,
        recurrence_rule=build_recurrence_rule(session.frequency, session.by_day) if recurring else None,
        date_range_id=session.date_range_id if recurring else None,
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
                affected=[
                    CommandTarget(event_id=None, title=draft.title, date=draft.start_time.date(), is_recurring=draft.is_recurring)
                ],
                affected_count=1,
                confirmation_token=pending.token,
                expires_at=pending.expires_at,
            ),
        )

    missing = list(session.missing_slots)
    if session.meridiem_hour is not None:
        hour = session.meridiem_hour
        next_question = ClarifyingQuestion(
            slot="start_time", question=render_message("command.ask_meridiem", user.preferred_language, hour=hour)
        )
        missing = ["start_time", *[slot for slot in missing if slot != "start_time"]]
    else:
        next_question = session.clarifying_questions[0] if session.clarifying_questions else None
    return EventParseResponse(
        session_id=session.session_id,
        is_complete=False,
        next_question=next_question,
        missing_slots=missing,
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

    if _answer_locally(session, data.utterance):
        return _to_response(db, session, user)

    known_before = session.known_slots()
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
    _enforce_create_rules(session, data.utterance, known_before, user.preferred_language)
    return _to_response(db, session, user)
