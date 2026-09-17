from __future__ import annotations

from datetime import date, datetime, time

import httpx
from sqlalchemy.orm import Session

from app.models.important_date_range import ImportantDateRange
from app.schemas.event_parse import EventDraft, EventParseRequest, EventParseResponse
from app.services.llm_client import LLMResponseParsingError, fill_event_slots_for_user
from app.services.recurrence import build_recurrence_rule
from app.services.slot_fill_session import SlotFillSession, create_session, get_session


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


def _to_response(db: Session, session: SlotFillSession) -> EventParseResponse:
    if session.is_complete:
        draft = _build_event_draft(db, session)
        return EventParseResponse(session_id=session.session_id, is_complete=True, draft=draft)

    next_question = session.clarifying_questions[0] if session.clarifying_questions else None
    return EventParseResponse(
        session_id=session.session_id,
        is_complete=False,
        next_question=next_question,
        missing_slots=list(session.missing_slots),
    )


def parse_event_utterance(
    db: Session,
    data: EventParseRequest,
    *,
    http_client: httpx.Client | None = None,
) -> EventParseResponse:
    """FR-2 슬롯필링 한 턴을 처리한다: 세션을 찾거나 만들고, LLM을 호출해 슬롯을
    채운 뒤, 아직 부족하면 다음 질문을, 다 채워졌으면 이벤트 초안을 반환한다.
    """
    if data.session_id is None:
        session = create_session(data.user_id)
    else:
        session = get_session(data.session_id)
        if session is None:
            raise ValueError(f"session_id {data.session_id} does not exist")
        if session.user_id != data.user_id:
            raise ValueError(f"session_id {data.session_id} belongs to a different user")

    result = fill_event_slots_for_user(
        db,
        data.user_id,
        data.utterance,
        known_slots=session.known_slots(),
        http_client=http_client,
    )
    session.apply(result)

    return _to_response(db, session)
