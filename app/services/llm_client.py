from __future__ import annotations

import json
from datetime import date
from typing import Literal

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.enums import Importance
from app.models.important_date_range import ImportantDateRange

CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"

SlotName = Literal[
    "title", "day_of_week", "start_time", "end_time", "importance", "date_range_id"
]

SLOT_NAMES: tuple[SlotName, ...] = (
    "title",
    "day_of_week",
    "start_time",
    "end_time",
    "importance",
    "date_range_id",
)

# FR-2 슬롯필링 결과의 JSON 스키마. day_of_week는 RRULE BYDAY 2글자 코드로 통일해
# app/services/recurrence.py가 기대하는 형식과 바로 이어지게 한다.
_EVENT_SLOT_JSON_SCHEMA = {
    "name": "event_slot_fill",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "title": {"type": ["string", "null"]},
            "day_of_week": {
                "type": ["string", "null"],
                "enum": ["MO", "TU", "WE", "TH", "FR", "SA", "SU", None],
            },
            "start_time": {
                "type": ["string", "null"],
                "description": "24시간제 HH:MM",
            },
            "end_time": {
                "type": ["string", "null"],
                "description": "24시간제 HH:MM",
            },
            "importance": {
                "type": ["integer", "null"],
                "enum": [1, 2, 3, 4, 5, 6, None],
                "description": "null=없음(수면), 1~5, 6=MAX",
            },
            "date_range_id": {"type": ["integer", "null"]},
            "missing_slots": {
                "type": "array",
                "items": {"type": "string", "enum": list(SLOT_NAMES)},
            },
            "clarifying_questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "slot": {"type": "string", "enum": list(SLOT_NAMES)},
                        "question": {"type": "string"},
                    },
                    "required": ["slot", "question"],
                    "additionalProperties": False,
                },
            },
        },
        "required": list(SLOT_NAMES) + ["missing_slots", "clarifying_questions"],
        "additionalProperties": False,
    },
}


class DateRangeOption(BaseModel):
    """LLM에게 후보로 제시할, 사용자가 이미 등록해둔 ImportantDateRange 하나."""

    id: int
    name: str
    start_date: date
    end_date: date


class ClarifyingQuestion(BaseModel):
    slot: SlotName
    question: str


class EventSlotFillResult(BaseModel):
    """FR-2 슬롯필링 결과. 값이 None이어도 missing_slots에 없으면 "명시적으로 없음"
    (예: importance=None은 수면)이고, missing_slots에 있으면 "아직 모름 -> 되물어야 함"이다.
    """

    title: str | None = None
    day_of_week: Literal["MO", "TU", "WE", "TH", "FR", "SA", "SU"] | None = None
    start_time: str | None = None
    end_time: str | None = None
    importance: Importance | None = None
    date_range_id: int | None = None
    missing_slots: list[SlotName] = Field(default_factory=list)
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.missing_slots


class LLMClientError(RuntimeError):
    """LLM_API_KEY 미설정, API 호출 실패, 응답 파싱 실패 등을 감싸는 공통 예외."""


def _build_system_prompt() -> str:
    return (
        "너는 일정 관리 앱의 자연어 이벤트 파서다. 사용자의 발화에서 다음 슬롯을 "
        "추출해 JSON으로만 답한다: title, day_of_week, start_time, end_time, "
        "importance, date_range_id.\n"
        "- day_of_week는 요일이 언급된 경우에만 MO/TU/WE/TH/FR/SA/SU 중 하나로 채운다.\n"
        "- start_time, end_time은 24시간제 HH:MM 형식이다.\n"
        "- importance는 null(없음/수면), 1(개인 여가), 2(타인 연관 약속), "
        "3(의무이지만 출석 체크 없음), 4(공식적 의무/평가), 5(반드시 지켜야 함), "
        "6(MAX, 5보다 예외적으로 중요) 중 하나다. 발화에서 유추할 수 없으면 슬롯을 "
        "채우지 말고 missing_slots에 넣는다.\n"
        "- date_range_id는 함께 제공되는 후보 목록의 id 중 하나만 쓸 수 있다. 후보가 "
        "없거나 어떤 후보를 말하는지 애매하면 절대 추측하지 말고 date_range_id를 "
        "null로 두고 missing_slots에 추가한다 (모호한 기간 표현은 항상 명시적으로 "
        "확인한다).\n"
        "- 확실하게 알아낸 슬롯은 missing_slots에 넣지 않는다. 값을 못 정한 슬롯만 "
        "missing_slots에 넣고, 그 각각에 대해 사용자에게 되물을 자연스러운 한국어 "
        "질문을 clarifying_questions에 함께 준다.\n"
        "- '이미 확정된 슬롯'이 함께 주어질 수 있다. 이는 이전 대화 턴에서 이미 "
        "알아낸 값이다. 최신 발화가 그 값을 바꾸라고 명시하지 않는 한 그대로 결과에 "
        "포함하고 missing_slots에 넣지 않는다. 최신 발화가 다른 값으로 정정하면 그 "
        "값으로 덮어쓴다."
    )


def _build_user_message(
    utterance: str,
    available_date_ranges: list[DateRangeOption],
    reference_date: date,
    known_slots: dict[str, object] | None = None,
) -> str:
    date_ranges_json = json.dumps(
        [option.model_dump(mode="json") for option in available_date_ranges],
        ensure_ascii=False,
    )
    known_slots_json = json.dumps(known_slots or {}, ensure_ascii=False, default=str)
    return (
        f"오늘 날짜: {reference_date.isoformat()}\n"
        f"이미 확정된 슬롯: {known_slots_json}\n"
        f"등록된 기간(date_range) 후보: {date_ranges_json}\n"
        f"사용자 발화: {utterance}"
    )


def _build_request_payload(
    utterance: str,
    available_date_ranges: list[DateRangeOption],
    reference_date: date,
    known_slots: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "model": settings.llm_model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _build_system_prompt()},
            {
                "role": "user",
                "content": _build_user_message(
                    utterance, available_date_ranges, reference_date, known_slots
                ),
            },
        ],
        "response_format": {"type": "json_schema", "json_schema": _EVENT_SLOT_JSON_SCHEMA},
    }


def _parse_response(raw_content: str) -> EventSlotFillResult:
    try:
        data = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise LLMClientError(f"LLM 응답이 유효한 JSON이 아닙니다: {raw_content!r}") from exc

    try:
        return EventSlotFillResult.model_validate(data)
    except Exception as exc:  # pydantic ValidationError 등
        raise LLMClientError(f"LLM 응답이 예상한 스키마와 다릅니다: {data!r}") from exc


def fill_event_slots(
    utterance: str,
    *,
    available_date_ranges: list[DateRangeOption] | None = None,
    reference_date: date | None = None,
    known_slots: dict[str, object] | None = None,
    http_client: httpx.Client | None = None,
) -> EventSlotFillResult:
    """자연어 발화에서 title/day_of_week/start_time/end_time/importance/date_range_id를
    채운다 (FR-2). 확정 못한 슬롯은 결과의 missing_slots/clarifying_questions로 온다.

    available_date_ranges: 사용자가 이미 등록해둔 ImportantDateRange 후보 목록.
        LLM은 이 목록에 있는 id만 date_range_id로 쓸 수 있다.
    known_slots: 멀티턴 대화에서 이전 턴까지 이미 확정된 슬롯 값(예:
        SlotFillSession.known_slots()). 이걸 안 넘기면 이 함수는 매번 최신 발화만
        보고 판단하므로, 이전 턴에 알아낸 정보를 잃어버릴 수 있다.
    http_client: 테스트에서 httpx.MockTransport로 응답을 주입하기 위한 훅. 생략하면
        settings.llm_api_key로 인증한 기본 클라이언트를 새로 만든다.
    """
    if not settings.llm_api_key:
        raise LLMClientError("LLM_API_KEY가 설정되지 않았습니다 (.env 확인)")

    payload = _build_request_payload(
        utterance, available_date_ranges or [], reference_date or date.today(), known_slots
    )
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}

    owns_client = http_client is None
    client = http_client or httpx.Client()
    try:
        response = client.post(CHAT_COMPLETIONS_URL, json=payload, headers=headers, timeout=30)
    except httpx.HTTPError as exc:
        raise LLMClientError(f"LLM API 호출에 실패했습니다: {exc}") from exc
    finally:
        if owns_client:
            client.close()

    if response.status_code >= 400:
        raise LLMClientError(f"LLM API가 오류를 반환했습니다 ({response.status_code}): {response.text}")

    try:
        raw_content = response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise LLMClientError(f"LLM 응답 형식이 예상과 다릅니다: {response.text!r}") from exc

    return _parse_response(raw_content)


def get_date_range_options(db: Session, user_id: int) -> list[DateRangeOption]:
    """사용자가 등록해둔 ImportantDateRange 목록을 슬롯필링 후보로 변환한다.

    "언제까지 반복할까요?" 같은 date_range_id 질문에서 LLM이 실제 존재하는 기간
    중에서만 고르도록(지어내지 못하도록) fill_event_slots_for_user가 이 목록을
    컨텍스트로 넘긴다.
    """
    stmt = (
        select(ImportantDateRange)
        .where(ImportantDateRange.user_id == user_id)
        .order_by(ImportantDateRange.start_date)
    )
    date_ranges = db.execute(stmt).scalars().all()
    return [
        DateRangeOption(
            id=date_range.id,
            name=date_range.name,
            start_date=date_range.start_date,
            end_date=date_range.end_date,
        )
        for date_range in date_ranges
    ]


def fill_event_slots_for_user(
    db: Session,
    user_id: int,
    utterance: str,
    *,
    reference_date: date | None = None,
    known_slots: dict[str, object] | None = None,
    http_client: httpx.Client | None = None,
) -> EventSlotFillResult:
    """fill_event_slots를 호출하되, 이 user_id가 등록해둔 ImportantDateRange 전체를
    date_range_id 후보로 자동으로 함께 제시한다 (FR-2).
    """
    date_range_options = get_date_range_options(db, user_id)
    return fill_event_slots(
        utterance,
        available_date_ranges=date_range_options,
        reference_date=reference_date,
        known_slots=known_slots,
        http_client=http_client,
    )
