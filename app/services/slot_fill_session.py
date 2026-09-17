from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from threading import Lock

from app.models.enums import Importance
from app.services.llm_client import ClarifyingQuestion, EventSlotFillResult, SLOT_NAMES, SlotName

# 초기 버전: 프로세스 인메모리 dict로 세션을 보관한다. 서버 재시작/멀티 워커에서는
# 유지되지 않으므로, 나중에 필요해지면 이 저장소만 교체하면 되도록 함수 인터페이스
# (create/get/update/delete_session) 뒤에 숨겨뒀다.


@dataclass
class SlotFillSession:
    """진행 중인 FR-2 슬롯필링 대화 하나의 상태. session_id로 여러 턴에 걸쳐 재사용된다."""

    session_id: str
    user_id: int
    title: str | None = None
    day_of_week: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    importance: Importance | None = None
    date_range_id: int | None = None
    missing_slots: list[SlotName] = field(default_factory=lambda: list(SLOT_NAMES))
    clarifying_questions: list[ClarifyingQuestion] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.missing_slots

    def known_slots(self) -> dict[str, object]:
        """missing_slots에 없는(=이미 확정된) 슬롯만 {슬롯명: 값}으로 반환한다.

        이걸 다음 fill_event_slots/fill_event_slots_for_user 호출의 known_slots
        인자로 넘기면, 이전 턴에 알아낸 정보를 이번 턴에도 잃지 않는다.
        """
        return {slot: getattr(self, slot) for slot in SLOT_NAMES if slot not in self.missing_slots}

    def apply(self, result: EventSlotFillResult) -> None:
        """새 LLM 슬롯필링 결과를 세션에 병합한다.

        result.missing_slots에 없는 슬롯만 세션 값을 덮어쓴다 (missing인 슬롯은 이전
        턴에 알아낸 값을 그대로 유지). fill_event_slots를 호출할 때 known_slots()를
        함께 넘겨야, LLM이 이전 턴에 확정된 슬롯을 다시 missing으로 표시하거나
        null로 덮어쓰지 않는다.
        """
        for slot in SLOT_NAMES:
            if slot not in result.missing_slots:
                setattr(self, slot, getattr(result, slot))
        self.missing_slots = list(result.missing_slots)
        self.clarifying_questions = list(result.clarifying_questions)


_sessions: dict[str, SlotFillSession] = {}
_lock = Lock()


def create_session(user_id: int) -> SlotFillSession:
    session = SlotFillSession(session_id=str(uuid.uuid4()), user_id=user_id)
    with _lock:
        _sessions[session.session_id] = session
    return session


def get_session(session_id: str) -> SlotFillSession | None:
    with _lock:
        return _sessions.get(session_id)


def update_session(session_id: str, result: EventSlotFillResult) -> SlotFillSession | None:
    with _lock:
        session = _sessions.get(session_id)
        if session is None:
            return None
        session.apply(result)
        return session


def delete_session(session_id: str) -> None:
    with _lock:
        _sessions.pop(session_id, None)


def clear_all_sessions() -> None:
    """테스트 등에서 인메모리 저장소를 초기화할 때 쓴다."""
    with _lock:
        _sessions.clear()
