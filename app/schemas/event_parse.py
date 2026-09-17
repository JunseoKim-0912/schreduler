from __future__ import annotations

from pydantic import BaseModel

from app.models.enums import Importance
from app.services.llm_client import ClarifyingQuestion, SlotName


class EventParseRequest(BaseModel):
    user_id: int
    utterance: str
    session_id: str | None = None


class EventDraft(BaseModel):
    """모든 슬롯이 채워졌을 때 반환하는 완성된 이벤트 초안. 아직 DB에 저장되지는
    않는다 — 클라이언트가 확인 후 POST /events로 실제 생성해야 한다."""

    title: str
    day_of_week: str | None
    start_time: str
    end_time: str
    importance: Importance | None
    date_range_id: int | None


class EventParseResponse(BaseModel):
    session_id: str
    is_complete: bool
    next_question: ClarifyingQuestion | None = None
    missing_slots: list[SlotName] = []
    draft: EventDraft | None = None
