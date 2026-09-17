from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.models.enums import Importance
from app.services.llm_client import ClarifyingQuestion, SlotName


class EventParseRequest(BaseModel):
    user_id: int
    utterance: str
    session_id: str | None = None


class EventDraft(BaseModel):
    """모든 슬롯이 채워졌을 때 반환하는 완성된 이벤트 초안.

    EventCreate와 같은 모양이라 이 draft를 그대로 POST /events에 넘기면 이벤트가
    바로 생성된다 (아직 이 draft 자체가 DB에 저장된 상태는 아니다 — 클라이언트가
    확인 후 실제로 POST해야 한다). start_time/end_time은 반복 시작일
    (date_range.start_date, 없으면 오늘)과 슬롯필링된 HH:MM을 합친 전체
    datetime이고, recurrence_rule은 frequency/by_day로 조립한 RRULE 문자열이다.
    """

    user_id: int
    title: str
    start_time: datetime
    end_time: datetime
    importance: Importance | None
    is_recurring: bool = True
    recurrence_rule: str
    date_range_id: int | None


class EventParseResponse(BaseModel):
    session_id: str
    is_complete: bool
    next_question: ClarifyingQuestion | None = None
    missing_slots: list[SlotName] = []
    draft: EventDraft | None = None
