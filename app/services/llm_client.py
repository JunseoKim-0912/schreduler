from __future__ import annotations

import json
from datetime import date
from typing import Literal

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.enums import Importance, NonComplianceCategory
from app.models.important_date_range import ImportantDateRange

CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"

SlotName = Literal[
    "title", "frequency", "by_day", "start_time", "end_time", "importance", "date_range_id"
]

SLOT_NAMES: tuple[SlotName, ...] = (
    "title",
    "frequency",
    "by_day",
    "start_time",
    "end_time",
    "importance",
    "date_range_id",
)

# FR-2 슬롯필링 결과의 JSON 스키마. frequency/by_day는 RRULE FREQ/BYDAY 값과 그대로
# 이어지게 해서, event_parse_service가 "FREQ=WEEKLY;BYDAY=MO" 같은 recurrence_rule을
# 바로 조립할 수 있게 한다 (app/services/recurrence.py의 build_recurrence_rule 참고).
_EVENT_SLOT_JSON_SCHEMA = {
    "name": "event_slot_fill",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "title": {"type": ["string", "null"]},
            "frequency": {
                "type": ["string", "null"],
                "enum": ["DAILY", "WEEKLY", "MONTHLY", "YEARLY", None],
            },
            "by_day": {
                "type": ["array", "null"],
                "items": {
                    "type": "string",
                    "enum": ["MO", "TU", "WE", "TH", "FR", "SA", "SU"],
                },
                "description": "frequency가 WEEKLY일 때 반복 요일들 (예: ['MO'])",
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
    frequency: Literal["DAILY", "WEEKLY", "MONTHLY", "YEARLY"] | None = None
    by_day: list[Literal["MO", "TU", "WE", "TH", "FR", "SA", "SU"]] | None = None
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
    """LLM 클라이언트 관련 에러의 공통 베이스. 라우터에서는 아래 구체적인 하위
    클래스를 먼저 잡아서 상황에 맞는 HTTP 상태 코드로 매핑해야 한다."""


class LLMConfigError(LLMClientError):
    """LLM_API_KEY 등 필수 설정이 빠졌을 때. 서버 설정 문제이지 요청 자체의
    잘못이 아니다."""


class LLMRequestError(LLMClientError):
    """LLM API 호출 자체가 실패했을 때 (네트워크 오류, 4xx/5xx 응답 등). 진짜
    upstream 문제이므로 502 Bad Gateway가 적절하다."""


class LLMResponseParsingError(LLMClientError):
    """LLM이 응답은 했지만 JSON 파싱에 실패했거나 우리가 기대한 스키마와 다를 때.
    upstream이 완전히 죽은 게 아니라 우리가 처리 못 할 데이터를 줬다는 뜻이므로
    502가 아니라 422로 다뤄야 한다."""


def _build_system_prompt() -> str:
    return (
        "너는 일정 관리 앱의 자연어 '반복' 이벤트 파서다. 사용자의 발화에서 다음 "
        "슬롯을 추출해 JSON으로만 답한다: title, frequency, by_day, start_time, "
        "end_time, importance, date_range_id. 이 앱에서 자연어로 만드는 이벤트는 "
        "항상 반복 이벤트다.\n"
        "- frequency는 DAILY/WEEKLY/MONTHLY/YEARLY 중 하나다. '매일'이면 DAILY, "
        "'매주'면 WEEKLY다.\n"
        "- by_day는 frequency가 WEEKLY일 때 반복 요일들을 MO/TU/WE/TH/FR/SA/SU "
        "코드의 배열로 담는다 (예: '매주 월요일'이면 [\"MO\"], '매주 화, 목'이면 "
        "[\"TU\", \"TH\"]). DAILY/MONTHLY/YEARLY면 보통 필요 없으니 빈 배열로 둔다.\n"
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
        raise LLMResponseParsingError(f"LLM 응답이 유효한 JSON이 아닙니다: {raw_content!r}") from exc

    try:
        return EventSlotFillResult.model_validate(data)
    except Exception as exc:  # pydantic ValidationError 등
        raise LLMResponseParsingError(f"LLM 응답이 예상한 스키마와 다릅니다: {data!r}") from exc


def _call_chat_completion(payload: dict[str, object], http_client: httpx.Client | None) -> str:
    """OpenAI Chat Completions를 호출해 message.content 문자열을 그대로 반환한다.

    구조화 출력(JSON schema)을 요청했다면 그 JSON 문자열이, 아니면 자유 텍스트가
    온다 — 파싱은 호출부의 책임이다.
    """
    if not settings.llm_api_key:
        raise LLMConfigError("LLM_API_KEY가 설정되지 않았습니다 (.env 확인)")

    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}

    owns_client = http_client is None
    client = http_client or httpx.Client()
    try:
        response = client.post(CHAT_COMPLETIONS_URL, json=payload, headers=headers, timeout=30)
    except httpx.HTTPError as exc:
        raise LLMRequestError(f"LLM API 호출에 실패했습니다: {exc}") from exc
    finally:
        if owns_client:
            client.close()

    if response.status_code >= 400:
        raise LLMRequestError(f"LLM API가 오류를 반환했습니다 ({response.status_code}): {response.text}")

    try:
        return response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise LLMResponseParsingError(f"LLM 응답 형식이 예상과 다릅니다: {response.text!r}") from exc


def fill_event_slots(
    utterance: str,
    *,
    available_date_ranges: list[DateRangeOption] | None = None,
    reference_date: date | None = None,
    known_slots: dict[str, object] | None = None,
    http_client: httpx.Client | None = None,
) -> EventSlotFillResult:
    """자연어 발화에서 title/frequency/by_day/start_time/end_time/importance/
    date_range_id를 채운다 (FR-2). 확정 못한 슬롯은 결과의
    missing_slots/clarifying_questions로 온다.

    available_date_ranges: 사용자가 이미 등록해둔 ImportantDateRange 후보 목록.
        LLM은 이 목록에 있는 id만 date_range_id로 쓸 수 있다.
    known_slots: 멀티턴 대화에서 이전 턴까지 이미 확정된 슬롯 값(예:
        SlotFillSession.known_slots()). 이걸 안 넘기면 이 함수는 매번 최신 발화만
        보고 판단하므로, 이전 턴에 알아낸 정보를 잃어버릴 수 있다.
    http_client: 테스트에서 httpx.MockTransport로 응답을 주입하기 위한 훅. 생략하면
        settings.llm_api_key로 인증한 기본 클라이언트를 새로 만든다.
    """
    payload = _build_request_payload(
        utterance, available_date_ranges or [], reference_date or date.today(), known_slots
    )
    raw_content = _call_chat_completion(payload, http_client)
    return _parse_response(raw_content)


_NON_COMPLIANCE_CATEGORY_LABELS: dict[NonComplianceCategory, str] = {
    NonComplianceCategory.OVERSLEPT: "늦잠/기상 실패",
    NonComplianceCategory.FATIGUE: "피로/무기력",
    NonComplianceCategory.PRIORITY_SHIFT: "우선순위 변경",
    NonComplianceCategory.SCHEDULE_CONFLICT: "일정 충돌",
    NonComplianceCategory.FORGOT: "깜빡함",
    NonComplianceCategory.TRANSIT_ISSUE: "이동/교통 문제",
    NonComplianceCategory.OTHER: "기타",
}


def generate_compliance_feedback(
    category: NonComplianceCategory,
    reason_text: str | None,
    *,
    http_client: httpx.Client | None = None,
) -> str:
    """FR-6: 미준수 사유에 대한 공감형 피드백 한두 문장을 생성한다.

    category가 OTHER이거나 reason_text가 채워진 경우에만 호출부가 이 함수를
    부른다 (버튼 클릭만으로 끝난 경우는 LLM을 아예 호출하지 않는 것이 FR-6의
    "LLM 우회 UI 숏컷"이다).
    """
    label = _NON_COMPLIANCE_CATEGORY_LABELS[category]
    user_message = f"미준수 사유 카테고리: {label}"
    if reason_text:
        user_message += f"\n사용자가 직접 적은 이유: {reason_text}"

    payload = {
        "model": settings.llm_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "너는 일정 관리 앱의 다정한 코치 페르소나다. 사용자가 계획한 "
                    "일정을 지키지 못한 이유를 말했다. 나무라지 말고, 짧게(1~2문장) "
                    "공감하며 다음에는 잘할 수 있다는 격려를 한국어로 건네라."
                ),
            },
            {"role": "user", "content": user_message},
        ],
    }
    content = _call_chat_completion(payload, http_client)
    return content.strip()


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
