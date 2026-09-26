from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Literal

from app.core.exceptions import ExpiredError, InvalidInputError, NotFoundError
from app.models.enums import Importance
from app.services.llm_client import ClarifyingQuestion, EventSlotFillResult, SLOT_NAMES, SlotName

HISTORY_TURNS = 10

# 초기 버전: 프로세스 인메모리 dict로 세션을 보관한다. 서버 재시작/멀티 워커에서는
# 유지되지 않으므로, 나중에 필요해지면 이 저장소만 교체하면 되도록 함수 인터페이스
# (create/get/update/delete_session) 뒤에 숨겨뒀다.


@dataclass
class SlotFillSession:
    """진행 중인 FR-2 슬롯필링 대화 하나의 상태. session_id로 여러 턴에 걸쳐 재사용된다."""

    session_id: str
    user_id: int
    title: str | None = None
    date: str | None = None  # YYYY-MM-DD 또는 연도 없는 MM-DD
    frequency: str | None = None
    by_day: list[str] | None = None
    start_time: str | None = None
    end_time: str | None = None
    importance: Importance | None = None
    date_range_id: int | None = None
    # 등록된 기간 대신 대화 중에 새로 만들 반복 기간 ({name, start_date, end_date}). 이벤트를 확정할 때 함께 만든다.
    new_date_range: dict[str, Any] | None = None
    missing_slots: list[SlotName] = field(default_factory=lambda: list(SLOT_NAMES))
    clarifying_questions: list[ClarifyingQuestion] = field(default_factory=list)
    # 되묻는 중인 삭제·수정 요청 (event_command_service.CommandDescription을 dict로). 다음 턴의 답으로 보완한다.
    command: dict[str, Any] | None = None
    # 되물을 때 보여준 후보 ("1) 물리 퀴즈 (2026-09-26)" 등). 다음 턴 프롬프트에 넣는다.
    command_candidates: list[str] = field(default_factory=list)
    # 반복 여부를 물었을 때 "반복 없이/한 번만"이라고 답했으면, 이후 LLM이 다시 반복을 물어도 단발로 고정한다.
    one_off: bool = False
    # "8시"처럼 오전/오후가 애매해 되묻는 중인 시(1~11). 한 번만 묻는다.
    meridiem_hour: int | None = None
    meridiem_asked: bool = False
    # 같은 대화의 최근 말(사용자·앱). 다음 LLM 호출에 넘겨 이미 답한 것을 다시 묻지 않게 한다.
    history: list[tuple[Literal["user", "assistant"], str]] = field(default_factory=list)

    def remember(self, role: Literal["user", "assistant"], text: str | None) -> None:
        if text:
            self.history = [*self.history, (role, text)][-HISTORY_TURNS:]

    @property
    def is_complete(self) -> bool:
        return not self.missing_slots and self.meridiem_hour is None

    def known_slots(self) -> dict[str, object]:
        """missing_slots에 없는(=이미 확정된) 슬롯만 {슬롯명: 값}으로 반환한다.

        이걸 다음 fill_event_slots/fill_event_slots_for_user 호출의 known_slots
        인자로 넘기면, 이전 턴에 알아낸 정보를 이번 턴에도 잃지 않는다.
        """
        known: dict[str, object] = {slot: getattr(self, slot) for slot in SLOT_NAMES if slot not in self.missing_slots}
        if self.new_date_range is not None:
            known["new_date_range"] = self.new_date_range
        return known

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
        if result.new_date_range is not None:
            self.new_date_range = result.new_date_range.model_dump()
            self.date_range_id = None
        elif "date_range_id" not in result.missing_slots and result.date_range_id is not None:
            self.new_date_range = None
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
    """테스트 등에서 인메모리 저장소(세션과 확인 대기 요청)를 초기화할 때 쓴다."""
    with _lock:
        _sessions.clear()
        _pending_actions.clear()


# --- 확인 대기 중인 요청 -----------------------------------------------------------
# 여러 개를 한꺼번에 지우거나 바꾸는 요청, 그리고 자연어로 만든 일정 초안은 바로 실행하지 않고
# 토큰을 발급한다. POST /events/commands/confirm으로 토큰을 보내면 실행된다. 세션과 같은 인메모리
# 저장소라 서버를 재시작하면 사라진다.

PENDING_ACTION_TTL = timedelta(minutes=10)
PendingKind = Literal["create", "delete", "update", "delete_range"]


@dataclass
class PendingAction:
    token: str
    user_id: int
    kind: PendingKind
    payload: dict[str, Any]
    created_at: datetime

    @property
    def expires_at(self) -> datetime:
        return self.created_at + PENDING_ACTION_TTL


_pending_actions: dict[str, PendingAction] = {}


def create_pending_action(user_id: int, kind: PendingKind, payload: dict[str, Any]) -> PendingAction:
    now = datetime.now()
    pending = PendingAction(token=str(uuid.uuid4()), user_id=user_id, kind=kind, payload=payload, created_at=now)
    with _lock:
        for token in [t for t, p in _pending_actions.items() if p.expires_at <= now]:
            del _pending_actions[token]
        _pending_actions[pending.token] = pending
    return pending


def take_pending_action(token: str, user_id: int, *, needs_option: bool = False) -> PendingAction:
    """토큰을 꺼낸다 (한 번만 쓸 수 있다). 없거나 다른 사용자 것이면 404, 10분이 지났으면 410.
    needs_option=False인데 선택지가 필요한 요청(사용 중인 기간 삭제)이면 토큰을 그대로 두고 422."""
    with _lock:
        pending = _pending_actions.get(token)
        if pending is None or pending.user_id != user_id:
            raise NotFoundError("confirmation token does not exist")
        if pending.kind == "delete_range" and not needs_option:
            raise InvalidInputError("option is required: range_only or with_events")
        del _pending_actions[token]
    if pending.expires_at <= datetime.now():
        raise ExpiredError("confirmation token expired — please make the request again")
    return pending


def clear_all_pending_actions() -> None:
    with _lock:
        _pending_actions.clear()
