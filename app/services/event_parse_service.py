from __future__ import annotations

import httpx
from sqlalchemy.orm import Session

from app.schemas.event_parse import EventDraft, EventParseRequest, EventParseResponse
from app.services.llm_client import fill_event_slots_for_user
from app.services.slot_fill_session import SlotFillSession, create_session, get_session


def _to_response(session: SlotFillSession) -> EventParseResponse:
    if session.is_complete:
        draft = EventDraft(
            title=session.title,
            day_of_week=session.day_of_week,
            start_time=session.start_time,
            end_time=session.end_time,
            importance=session.importance,
            date_range_id=session.date_range_id,
        )
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

    return _to_response(session)
