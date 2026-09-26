from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

import httpx
from sqlalchemy.orm import Session

from app.core.exceptions import InvalidInputError, NotFoundError
from app.i18n import render_message
from app.models.enums import ActionSource
from app.models.important_date_range import ImportantDateRange
from app.models.user import User
from app.schemas.event_command import CommandResult, CommandTarget
from app.schemas.event_parse import EventDraft, EventParseRequest, EventParseResponse, NewDateRangeDraft
from app.services import date_range_command_service as ranges
from app.services.llm_client import (
    ClarifyingQuestion,
    EventSlotFillResult,
    LLMResponseParsingError,
    SlotName,
    fill_event_slots_for_user,
)
from app.services.common import require
from app.services.recurrence import build_recurrence_rule
from app.services.event_command_service import (
    CommandDescription,
    Target,
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


def _repeat_until_question(db: Session, user: User) -> ClarifyingQuestion:
    """반복 종료 질문. 제안할 기간이 있으면 그 기간을 쓰거나 다른 날짜를 말할 수 있게 한 번에 묻는다."""
    lang = user.preferred_language
    suggestion = ranges.current_range(db, user.id, date.today())
    if suggestion is None:
        return ClarifyingQuestion(slot="date_range_id", question=render_message("command.ask_repeat_until_none", lang))
    question = render_message(
        "command.ask_repeat_until",
        lang,
        name=suggestion.name,
        start=ranges.format_day(suggestion.start_date),
        end=ranges.format_day(suggestion.end_date),
    )
    return ClarifyingQuestion(slot="date_range_id", question=question)


def _enforce_date_range_rules(db: Session, session: SlotFillSession, user: User) -> None:
    """새 반복 기간이 등록된 기간과 이름이 같으면 그 기간을 쓴다. 날짜를 알아볼 수 없으면 다시 묻는다.
    반복 종료를 물을 때는 LLM 질문 대신 '기존 기간까지? 다른 날짜면 말해 달라'를 한 번에 묻는다."""
    if session.new_date_range is not None:
        name = session.new_date_range.get("name")
        existing = ranges.find_by_name(db, user.id, name) if name else None
        if existing is not None and ranges.same_name(existing.name, name):
            session.date_range_id, session.new_date_range = existing.id, None
        else:
            try:
                _resolve_new_range(db, session, user)
            except InvalidInputError:
                session.new_date_range = None
                if "date_range_id" not in session.missing_slots:
                    session.missing_slots.append("date_range_id")
        if session.new_date_range is not None or session.date_range_id is not None:
            _drop_slots(session, ("date_range_id",))

    if _is_recurring(session) and "date_range_id" in session.missing_slots:
        session.clarifying_questions = [
            _repeat_until_question(db, user) if q.slot == "date_range_id" else q for q in session.clarifying_questions
        ]
        if not any(q.slot == "date_range_id" for q in session.clarifying_questions):
            session.clarifying_questions.append(_repeat_until_question(db, user))


def _resolve_new_range(db: Session, session: SlotFillSession, user: User) -> NewDateRangeDraft:
    """새 반복 기간을 실제 날짜와 이름으로. 시작일을 말하지 않았으면 제안했던(오늘이 속한) 기간의 시작일, 그것도
    없으면 일정 날짜나 오늘부터. 이름을 말하지 않았으면 '2026-2학기 (~12/8)'처럼 붙인다."""
    raw = session.new_date_range or {}
    today = date.today()
    suggestion = ranges.current_range(db, user.id, today)
    if suggestion is not None:
        default_start = suggestion.start_date
    elif session.date:
        default_start = resolve_event_date(session.date, today)
    else:
        default_start = today
    start, end = ranges.resolve_range_dates(raw.get("start_date"), raw["end_date"], today, default_start)
    name = raw.get("name")
    auto_named = not name
    if auto_named:
        name = ranges.auto_range_name(db, user.id, start, end, user.preferred_language)
    return NewDateRangeDraft(name=name, start_date=start, end_date=end, auto_named=auto_named)


def _enforce_create_rules(session: SlotFillSession, utterance: str, known_before: dict[str, object], language: str) -> None:
    """LLM 결과를 세션에 합친 뒤 백엔드가 보장하는 규칙: 단발로 답했으면 단발 유지, 애매한 시각은 한 번 묻기,
    단발 일정은 날짜 필수."""
    if session.one_off:
        _make_one_off(session)
        session.new_date_range = None
    if _is_recurring(session):
        # 반복 일정의 첫 날짜는 선택이다(말하지 않으면 반복 기간의 시작일부터). LLM이 물으려 해도 묻지 않는다.
        _drop_slots(session, ("date",))

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


def _build_event_draft(db: Session, session: SlotFillSession, user: User) -> EventDraft:
    recurring = session.frequency is not None
    if not session.title or not session.start_time or not session.end_time or not (recurring or session.date):
        raise LLMResponseParsingError(
            "세션이 is_complete인데 필수 슬롯(title/start_time/end_time, 단발이면 date) 중 "
            f"일부가 비어 있습니다: {session!r}"
        )

    new_range = _resolve_new_range(db, session, user) if recurring and session.new_date_range else None
    if session.date:
        day = resolve_event_date(session.date, date.today())
    elif new_range is not None:
        day = new_range.start_date
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
        date_range_id=session.date_range_id if recurring and new_range is None else None,
        new_date_range=new_range,
    )


def _to_response(db: Session, session: SlotFillSession, user: User) -> EventParseResponse:
    if session.is_complete:
        draft = _build_event_draft(db, session, user)
        lang = user.preferred_language
        message = render_message("command.confirm_create", lang)
        if draft.new_date_range is not None:
            new_range = draft.new_date_range
            message = render_message(
                "command.confirm_create_with_range",
                lang,
                name=new_range.name,
                start=ranges.format_day(new_range.start_date),
                end=ranges.format_day(new_range.end_date),
            )
            if new_range.auto_named:
                message += " " + render_message("range.auto_named", lang, name=new_range.name)
        # v3.6: 초안은 클라이언트가 확인하면 POST /events/commands/confirm으로 서버가 만든다 (되돌리기 기록 포함).
        pending = create_pending_action(user.id, "create", {"draft": draft.model_dump(mode="json")})
        return EventParseResponse(
            session_id=session.session_id,
            is_complete=True,
            draft=draft,
            message=message,
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


def _handle_range_command(db: Session, user: User, session: SlotFillSession, result: EventSlotFillResult) -> EventParseResponse:
    """반복 기간 만들기·바꾸기·지우기·보여주기 (target_kind=date_range)."""
    lang = user.preferred_language
    base = {"session_id": session.session_id, "is_complete": False, "intent": result.intent}

    def reply(message: str, command: CommandResult | None = None) -> EventParseResponse:
        return EventParseResponse(**base, message=message, command=command)

    def executed(action_type: str, action) -> EventParseResponse:
        message = render_message("command.executed", lang, summary=action.summary_text)
        return reply(message, command_result(action_type, "executed", action_id=action.id, target_kind="date_range"))

    if result.intent == "list":
        return reply(ranges.list_message(db, user))

    today = date.today()
    if result.intent == "create":
        if not result.range_end:
            return reply(render_message("range.need_dates", lang))
        try:
            start, end = ranges.resolve_range_dates(result.range_start, result.range_end, today, today)
        except InvalidInputError:
            return reply(render_message("range.invalid_dates", lang))
        name = result.range_name or ranges.auto_range_name(db, user.id, start, end, lang)
        existing = ranges.find_by_name(db, user.id, name)
        if existing is not None and ranges.same_name(existing.name, name):
            return reply(
                render_message(
                    "range.exists", lang, name=existing.name,
                    start=ranges.format_day(existing.start_date), end=ranges.format_day(existing.end_date),
                )
            )
        _, action = ranges.create_range(db, user, name, start, end, ActionSource.NL)
        response = executed("create", action)
        if not result.range_name:
            response.message += " " + render_message("range.auto_named", lang, name=name)
        return response

    if not result.range_name:
        return reply(render_message("range.need_name", lang))
    date_range = ranges.find_by_name(db, user.id, result.range_name)
    if date_range is None:
        return reply(
            render_message("range.not_found", lang, name=result.range_name),
            command_result(result.intent, "not_found", target_kind="date_range"),
        )

    if result.intent == "update":
        if not (result.range_start or result.range_end or result.range_new_name):
            return reply(render_message("range.nothing_to_update", lang))
        try:
            start = ranges.parse_day(result.range_start, date_range.start_date.year) if result.range_start else None
            end = ranges.parse_day(result.range_end, (start or date_range.start_date).year) if result.range_end else None
            updated = ranges.update_range(db, user, date_range, name=result.range_new_name, start=start, end=end, source=ActionSource.NL)
        except InvalidInputError:
            return reply(render_message("range.invalid_dates", lang))
        return executed("update", updated.action)

    events = ranges.events_using(db, date_range.id)
    if not events:
        return executed("delete", ranges.delete_range(db, user, date_range, None, ActionSource.NL))
    # 사용 중인 기간은 바로 지우지 않는다: 쓰는 일정을 보여주고 기간만 지울지, 일정도 지울지 고르게 한다.
    pending = create_pending_action(user.id, "delete_range", {"date_range_id": date_range.id})
    message = render_message(
        "range.in_use", lang, name=date_range.name, count=len(events), events=", ".join(e.title for e in events)
    )
    command = command_result(
        "delete",
        "needs_confirmation",
        targets=[Target(event) for event in events],
        pending=pending,
        target_kind="date_range",
        options=["range_only", "with_events"],
    )
    return reply(message, command)


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
    """FR-2 자연어 한 턴을 처리하고, 이 턴의 사용자 말과 앱의 답을 세션의 최근 대화에 남긴다."""
    user = require(db, User, data.user_id, "user_id")
    session = _get_or_create_session(data)
    response = _parse_turn(db, user, session, data.utterance, http_client)
    session.remember("user", data.utterance)
    session.remember("assistant", response.next_question.question if response.next_question else response.message)
    return response


def _get_or_create_session(data: EventParseRequest) -> SlotFillSession:
    if data.session_id is None:
        return create_session(data.user_id)
    session = get_session(data.session_id)
    if session is None:
        raise NotFoundError(f"session_id {data.session_id} does not exist")
    if session.user_id != data.user_id:
        raise NotFoundError(f"session_id {data.session_id} belongs to a different user")
    return session


def _parse_turn(
    db: Session, user: User, session: SlotFillSession, utterance: str, http_client: httpx.Client | None
) -> EventParseResponse:
    """FR-2 자연어 한 턴.

    LLM이 의도(create/delete/update/unknown)를 먼저 분류한다. create는 기존 슬롯필링(부족하면 되묻고, 다
    채워지면 초안 + 확인 토큰), delete/update는 event_command_service가 대상을 찾아 실행하거나 되묻는다.
    """
    if _answer_locally(session, utterance):
        return _to_response(db, session, user)

    known_before = session.known_slots()
    result = fill_event_slots_for_user(
        db,
        user.id,
        utterance,
        known_slots=known_before,
        pending_command=_pending_command_prompt(session),
        history=session.history,
        http_client=http_client,
    )

    if result.target_kind == "date_range" and result.intent in ("create", "update", "delete", "list"):
        session.command = None
        return _handle_range_command(db, user, session, result)
    if result.intent == "list":
        result = result.model_copy(update={"intent": "unknown"})  # 일정 목록은 캘린더 탭에서 본다
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
    _enforce_create_rules(session, utterance, known_before, user.preferred_language)
    _enforce_date_range_rules(db, session, user)
    return _to_response(db, session, user)
