from __future__ import annotations

import hashlib
import json
import logging
from datetime import date
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AppError
from app.i18n import Language, non_compliance_category_label, to_language
from app.models.enums import Importance, NonComplianceCategory
from app.models.event import Event
from app.models.important_date_range import ImportantDateRange
from app.models.user import User
from app.schemas.persona import PersonaRead

logger = logging.getLogger(__name__)

CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"

PromptTask = Literal["event_slot_fill", "compliance_feedback", "daily_checkin"]
LANGUAGE_NAMES: dict[Language, str] = {"ko": "한국어(Korean)", "en": "영어(English)"}

# 지시문·요약이 한국어여도 모델이 따라 쓰지 않도록, 대상 언어로 쓴 지시를 한 번 더 붙인다.
_NATIVE_LANGUAGE_RULES: dict[Language, str] = {
    "ko": "반드시 한국어로만 답하세요.",
    "en": "Respond only in English.",
}
_NATIVE_QUESTION_LANGUAGE_RULES: dict[Language, str] = {
    "ko": "되묻는 질문은 반드시 한국어로만 작성하세요.",
    "en": "Write every clarifying question in English only.",
}

DEFAULT_PERSONA_BLOCK = "[페르소나]\n일정 관리 앱의 다정한 코치. 나무라지 않고 공감하며 격려한다."

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

# FR-2 v3.6 의도 분류와 삭제·수정 대상 "설명". LLM은 이벤트 ID를 고르지 않는다 — 어떤 이벤트인지는
# 백엔드(event_command_service)가 이 설명으로 DB에서 결정적으로 찾는다.
Intent = Literal["create", "delete", "update", "unknown"]
_COMMAND_FIELDS: dict[str, dict[str, object]] = {
    "intent": {"type": "string", "enum": ["create", "delete", "update", "unknown"]},
    "target_title": {"type": ["string", "null"], "description": "삭제/수정할 일정 제목"},
    "target_date": {"type": ["string", "null"], "description": "특정 날짜 YYYY-MM-DD"},
    "target_all": {"type": "boolean", "description": "'전부/모두'면 true"},
    "target_scope": {"type": ["string", "null"], "enum": ["instance", "series", None]},
    "new_start_time": {"type": ["string", "null"], "description": "24시간제 HH:MM"},
    "new_end_time": {"type": ["string", "null"], "description": "24시간제 HH:MM"},
    "new_title": {"type": ["string", "null"]},
    "new_importance": {"type": ["integer", "null"], "enum": [1, 2, 3, 4, 5, 6, None]},
}

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
            **_COMMAND_FIELDS,
        },
        "required": list(SLOT_NAMES) + ["missing_slots", "clarifying_questions"] + list(_COMMAND_FIELDS),
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
    # v3.6: 새 필드가 없는 응답(기존 테스트의 가짜 응답 등)은 일정 추가(create)로 본다.
    intent: Intent = "create"
    target_title: str | None = None
    target_date: date | None = None
    target_all: bool = False
    target_scope: Literal["instance", "series"] | None = None
    new_start_time: str | None = None
    new_end_time: str | None = None
    new_title: str | None = None
    new_importance: Importance | None = None

    @property
    def is_complete(self) -> bool:
        return not self.missing_slots


class LLMClientError(AppError):
    """LLM 클라이언트 관련 에러의 공통 베이스. 상태 코드는 하위 클래스가 정하고,
    app.core.exceptions의 전역 핸들러가 HTTP 응답으로 바꾼다."""

    status_code = 502
    log_level = logging.WARNING


class LLMConfigError(LLMClientError):
    """LLM_API_KEY 등 필수 설정이 빠졌을 때. 서버 설정 문제이지 요청 자체의
    잘못이 아니다."""

    status_code = 500
    log_level = logging.ERROR


class LLMRequestError(LLMClientError):
    """LLM API 호출 자체가 실패했을 때 (네트워크 오류, 4xx/5xx 응답 등). 진짜
    upstream 문제이므로 502 Bad Gateway가 적절하다."""


class LLMResponseParsingError(LLMClientError):
    """LLM이 응답은 했지만 JSON 파싱에 실패했거나 우리가 기대한 스키마와 다를 때.
    upstream이 완전히 죽은 게 아니라 우리가 처리 못 할 데이터를 줬다는 뜻이므로
    502가 아니라 422로 다뤄야 한다."""

    status_code = 422


_EVENT_SLOT_INSTRUCTIONS = (
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
    "missing_slots에 넣고, 그 각각에 대해 사용자에게 되물을 자연스러운 질문을 "
    "clarifying_questions에 함께 준다 (질문 언어는 [질문 언어] 블록을 따른다).\n"
    "- '이미 확정된 슬롯'이 함께 주어질 수 있다. 이는 이전 대화 턴에서 이미 "
    "알아낸 값이다. 최신 발화가 그 값을 바꾸라고 명시하지 않는 한 그대로 결과에 "
    "포함하고 missing_slots에 넣지 않는다. 최신 발화가 다른 값으로 정정하면 그 "
    "값으로 덮어쓴다.\n"
    "[의도 분류] 먼저 intent를 정한다: 새 일정을 만들려는 발화는 create, 기존 일정을 없애려는 발화"
    "(삭제·취소·없애줘)는 delete, 기존 일정의 시간·제목·중요도를 바꾸려는 발화는 update, 일정 관리와 "
    "무관하면 unknown이다.\n"
    "- create일 때만 위의 슬롯 규칙을 따른다. create가 아니면 슬롯 필드는 모두 null, missing_slots와 "
    "clarifying_questions는 빈 배열로 둔다.\n"
    "- delete/update면 대상을 '설명'만 한다: target_title에는 '등록된 일정 제목' 목록 중 사용자가 말한 "
    "일정과 가장 가까운 제목을 그대로 적는다(목록에 없으면 사용자가 말한 표현). 특정 날짜를 말하면 "
    "target_date(YYYY-MM-DD, '오늘'·'내일'·'이번 주 금요일'은 오늘 날짜 기준으로 계산)를 적는다. "
    "'전부/모두/다'면 target_all=true. '이번만/그날만'이면 target_scope=instance, '반복 전체/매번/앞으로 "
    "전부'면 series, 알 수 없으면 null.\n"
    "- update면 바꿀 값만 new_start_time/new_end_time(HH:MM)/new_title/new_importance에 채우고 "
    "나머지는 null로 둔다. 시작 시각만 말하면 new_end_time은 null로 둔다(지속 시간은 백엔드가 유지한다).\n"
    "- create와 unknown이면 target_* 는 null(target_all은 false), new_* 는 null이다.\n"
    "- '진행 중인 요청'이 함께 주어지면 이전 턴의 삭제·수정 요청이다. 사용자의 답을 반영해 같은 intent로 "
    "target_*/new_* 를 다시 채운다(번호로 고르면 그 후보의 제목과 날짜를 적는다)."
)


def _prompt_cache_key(task: PromptTask, cacheable_prefix: str) -> str:
    digest = hashlib.sha256(cacheable_prefix.encode("utf-8")).hexdigest()[:16]
    return f"{task}:{digest}"


def _build_payload(
    task: PromptTask,
    cacheable_blocks: list[str],
    dynamic_content: str,
    **extra: object,
) -> dict[str, object]:
    """모든 LLM 요청 payload를 만드는 단일 진입점.

    프롬프트 캐시는 프리픽스가 바이트 단위로 같아야 적중하므로, 자주 안 바뀌는
    블록(작업 지시 → 페르소나 → 사용자별 목록 순, 공유 범위가 넓은 것부터)은 system
    메시지에 모으고 매 요청마다 바뀌는 값은 마지막 user 메시지에만 둔다.
    prompt_cache_key는 같은 프리픽스끼리 같은 캐시 서버로 라우팅되게 한다.
    """
    cacheable_prefix = "\n\n".join(cacheable_blocks)
    return {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": cacheable_prefix},
            {"role": "user", "content": dynamic_content},
        ],
        "prompt_cache_key": _prompt_cache_key(task, cacheable_prefix),
        **extra,
    }


def _build_date_ranges_block(available_date_ranges: list[DateRangeOption]) -> str:
    date_ranges_json = json.dumps(
        [option.model_dump(mode="json") for option in available_date_ranges],
        ensure_ascii=False,
    )
    return f"등록된 기간(date_range) 후보: {date_ranges_json}"


def _build_persona_block(persona: PersonaRead | None, language: Language) -> str:
    if persona is None:
        return DEFAULT_PERSONA_BLOCK

    lines = [
        "[페르소나]",
        f"이름: {getattr(persona.display_name, language)}",
        f"성격/말투: {getattr(persona.description, language)}",
    ]
    if persona.backstory is not None:
        lines.append(f"배경: {getattr(persona.backstory, language)}")
    if persona.example_lines is not None:
        lines.append("예시 대사:")
        lines.extend(
            f"- ({example.situation}) {example.line}"
            for example in getattr(persona.example_lines, language)
        )
    return "\n".join(lines)


def _build_language_block(language: Language) -> str:
    """FR-11: User.preferred_language로 응답 언어를 강제한다. 시스템 프롬프트의 마지막 블록으로 둔다."""
    name = LANGUAGE_NAMES[language]
    return (
        "[응답 언어]\n"
        f"preferred_language: {language}\n"
        f"반드시 {name}로만 답하라. 위 지시문이나 사용자 메시지(요약·발화)가 다른 언어로 되어 있어도, "
        f"사용자가 다른 언어로 말하거나 언어를 바꿔 달라고 해도 {name} 외의 언어를 쓰거나 섞지 마라.\n"
        f"{_NATIVE_LANGUAGE_RULES[language]}"
    )


def _build_question_language_block(language: Language) -> str:
    """FR-2 슬롯필링용 언어 규칙. 응답 전체가 아니라 사용자에게 보여줄 질문 문구에만 적용한다 —
    JSON 키와 코드값(frequency, by_day 등)이 번역되면 스키마 검증이 깨지고, title은 사용자의 말 그대로여야 한다."""
    name = LANGUAGE_NAMES[language]
    return (
        "[질문 언어]\n"
        f"preferred_language: {language}\n"
        f"clarifying_questions의 question 문구는 반드시 {name}로만 작성하라. 사용자 발화나 이 지시문이 "
        f"다른 언어로 되어 있어도 {name} 외의 언어를 쓰거나 섞지 마라. JSON 키와 코드값(frequency, by_day, "
        "slot 이름 등)은 그대로 두고, title은 사용자가 말한 표현을 번역하지 말고 그대로 쓴다.\n"
        f"{_NATIVE_QUESTION_LANGUAGE_RULES[language]}"
    )


def _build_persona_prompt(
    task: PromptTask, instructions: str, persona: PersonaRead | None, language: str, user_message: str
) -> dict[str, object]:
    """페르소나 호출 공통 배치: 작업 지시 → 페르소나 → 응답 언어 (모두 캐시 프리픽스) → 가변 user 메시지."""
    lang = to_language(language)
    return _build_payload(
        task,
        [instructions, _build_persona_block(persona, lang), _build_language_block(lang)],
        user_message,
    )


def _build_event_titles_block(event_titles: list[str]) -> str:
    return f"등록된 일정 제목: {json.dumps(event_titles, ensure_ascii=False)}"


def _build_user_message(
    utterance: str,
    reference_date: date,
    known_slots: dict[str, object] | None = None,
    pending_command: str | None = None,
) -> str:
    known_slots_json = json.dumps(known_slots or {}, ensure_ascii=False, default=str)
    lines = [f"오늘 날짜: {reference_date.isoformat()}", f"이미 확정된 슬롯: {known_slots_json}"]
    if pending_command:
        lines.append(f"진행 중인 요청: {pending_command}")
    lines.append(f"사용자 발화: {utterance}")
    return "\n".join(lines)


def _build_request_payload(
    utterance: str,
    available_date_ranges: list[DateRangeOption],
    reference_date: date,
    known_slots: dict[str, object] | None = None,
    language: str = "ko",
    event_titles: list[str] | None = None,
    pending_command: str | None = None,
) -> dict[str, object]:
    # 공유 범위 순: 작업 지시(전체 공통) → 질문 언어(언어별) → 등록된 기간·일정 제목(사용자별, 자주 안 바뀜)
    return _build_payload(
        "event_slot_fill",
        [
            _EVENT_SLOT_INSTRUCTIONS,
            _build_question_language_block(to_language(language)),
            _build_date_ranges_block(available_date_ranges),
            _build_event_titles_block(event_titles or []),
        ],
        _build_user_message(utterance, reference_date, known_slots, pending_command),
        response_format={"type": "json_schema", "json_schema": _EVENT_SLOT_JSON_SCHEMA},
    )


def _parse_response(raw_content: str) -> EventSlotFillResult:
    try:
        data = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise LLMResponseParsingError(f"LLM 응답이 유효한 JSON이 아닙니다: {raw_content!r}") from exc

    try:
        return EventSlotFillResult.model_validate(data)
    except Exception as exc:  # pydantic ValidationError 등
        raise LLMResponseParsingError(f"LLM 응답이 예상한 스키마와 다릅니다: {data!r}") from exc


class TokenUsage(BaseModel):
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int

    @property
    def uncached_prompt_tokens(self) -> int:
        return self.prompt_tokens - self.cached_tokens


class ChatCompletionResult(BaseModel):
    content: str
    usage: TokenUsage | None


def _parse_usage(body: dict[str, Any]) -> TokenUsage | None:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    details = usage.get("prompt_tokens_details") or {}
    return TokenUsage(
        prompt_tokens=usage.get("prompt_tokens", 0),
        cached_tokens=details.get("cached_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
    )


def _log_usage(payload: dict[str, object], usage: TokenUsage | None) -> None:
    cache_key = payload.get("prompt_cache_key", "-")
    if usage is None:
        logger.info("[LLM usage] cache_key=%s usage 정보 없음", cache_key)
        return
    hit_ratio = usage.cached_tokens / usage.prompt_tokens if usage.prompt_tokens else 0.0
    logger.info(
        "[LLM usage] cache_key=%s prompt=%d cached=%d (%.0f%%) uncached=%d completion=%d",
        cache_key,
        usage.prompt_tokens,
        usage.cached_tokens,
        hit_ratio * 100,
        usage.uncached_prompt_tokens,
        usage.completion_tokens,
    )


def _call_chat_completion(payload: dict[str, object], http_client: httpx.Client | None) -> str:
    """OpenAI Chat Completions를 호출해 message.content 문자열을 그대로 반환한다.

    구조화 출력(JSON schema)을 요청했다면 그 JSON 문자열이, 아니면 자유 텍스트가
    온다 — 파싱은 호출부의 책임이다.
    """
    return post_chat_completion(payload, http_client).content


def post_chat_completion(
    payload: dict[str, object], http_client: httpx.Client | None = None
) -> ChatCompletionResult:
    """Chat Completions 호출 결과(content + 토큰 사용량)를 반환하고, 사용량을 로그로 남긴다."""
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
        body = response.json()
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise LLMResponseParsingError(f"LLM 응답 형식이 예상과 다릅니다: {response.text!r}") from exc

    usage = _parse_usage(body)
    _log_usage(payload, usage)
    return ChatCompletionResult(content=content, usage=usage)


def fill_event_slots(
    utterance: str,
    *,
    available_date_ranges: list[DateRangeOption] | None = None,
    reference_date: date | None = None,
    known_slots: dict[str, object] | None = None,
    language: str = "ko",
    event_titles: list[str] | None = None,
    pending_command: str | None = None,
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
    language: 되묻는 질문(clarifying_questions)의 언어. User.preferred_language를 넘긴다.
    http_client: 테스트에서 httpx.MockTransport로 응답을 주입하기 위한 훅. 생략하면
        settings.llm_api_key로 인증한 기본 클라이언트를 새로 만든다.
    """
    payload = _build_request_payload(
        utterance,
        available_date_ranges or [],
        reference_date or date.today(),
        known_slots,
        language,
        event_titles,
        pending_command,
    )
    raw_content = _call_chat_completion(payload, http_client)
    return _parse_response(raw_content)


def generate_compliance_feedback(
    category: NonComplianceCategory,
    reason_text: str | None,
    *,
    persona: PersonaRead | None = None,
    language: str = "ko",
    http_client: httpx.Client | None = None,
) -> str:
    """FR-6: 미준수 사유에 대한 공감형 피드백 한두 문장을 생성한다.

    category가 OTHER이거나 reason_text가 채워진 경우에만 호출부가 이 함수를
    부른다 (버튼 클릭만으로 끝난 경우는 LLM을 아예 호출하지 않는 것이 FR-6의
    "LLM 우회 UI 숏컷"이다).
    """
    payload = build_compliance_feedback_payload(category, reason_text, persona=persona, language=language)
    content = _call_chat_completion(payload, http_client)
    return content.strip()


def build_compliance_feedback_payload(
    category: NonComplianceCategory,
    reason_text: str | None,
    *,
    persona: PersonaRead | None = None,
    language: str = "ko",
) -> dict[str, object]:
    instructions = (
        "너는 일정 관리 앱의 페르소나다. 사용자가 계획한 일정을 지키지 못한 이유를 "
        "말했다. 아래 페르소나의 성격과 말투를 살리되, 사용자가 다음에 다시 해볼 "
        "마음이 들도록 짧게(1~2문장) 답하라."
    )

    # 프롬프트 골격이 한국어라 라벨도 ko로 넣는다. 응답 언어는 [응답 언어] 블록이 정한다.
    label = non_compliance_category_label(category, "ko")
    user_message = f"미준수 사유 카테고리: {label}"
    if reason_text:
        user_message += f"\n사용자가 직접 적은 이유: {reason_text}"

    return _build_persona_prompt("compliance_feedback", instructions, persona, language, user_message)


def generate_daily_checkin_reply(
    summary: str,
    utterance: str,
    *,
    persona: PersonaRead | None = None,
    language: str = "ko",
    http_client: httpx.Client | None = None,
) -> str:
    """FR-8 저녁 9시 체크인 대화 한 턴을 생성한다.

    summary는 app.services.context_builder.build_daily_checkin_summary가 만든
    하루 요약이다 (완료한 일정은 개수만, 놓친 일정은 제목/시간/사유까지 상세히
    담겨 있다 - "컨텍스트 동적 로딩"). 요약은 매일 바뀌므로 캐시 프리픽스가 아닌
    user 메시지에 발화와 함께 넣는다.
    """
    payload = build_daily_checkin_payload(summary, utterance, persona=persona, language=language)
    content = _call_chat_completion(payload, http_client)
    return content.strip()


def build_daily_checkin_payload(
    summary: str,
    utterance: str,
    *,
    persona: PersonaRead | None = None,
    language: str = "ko",
) -> dict[str, object]:
    instructions = (
        "너는 일정 관리 앱의 페르소나다. 사용자와 저녁 체크인 대화를 나눈다. "
        "사용자 메시지에 오늘 하루 요약이 함께 온다 (완료한 일정은 개수만 적혀 있고, "
        "놓친 일정만 제목·시간·사유가 상세히 적혀 있다). 놓친 일정이 있다면 그것 "
        "위주로 묻고 격려하라. 놓친 일정이 없다면 짧게 칭찬하라. 아래 페르소나의 "
        "성격과 말투로 1~3문장 답하라."
    )
    user_message = f"오늘 요약:\n{summary}\n\n사용자 발화: {utterance}"

    return _build_persona_prompt("daily_checkin", instructions, persona, language, user_message)


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
    pending_command: str | None = None,
    http_client: httpx.Client | None = None,
) -> EventSlotFillResult:
    """fill_event_slots를 호출하되, 이 user_id가 등록해둔 ImportantDateRange 전체를
    date_range_id 후보로 자동으로 함께 제시하고, 되묻는 질문은 그 사용자의
    preferred_language로 받는다 (FR-2, FR-11). 삭제·수정 대상을 정확히 짚도록 사용자의
    기존 일정 제목(ID 없이)도 함께 넣는다 (v3.6).
    """
    date_range_options = get_date_range_options(db, user_id)
    user = db.get(User, user_id)
    return fill_event_slots(
        utterance,
        available_date_ranges=date_range_options,
        reference_date=reference_date,
        known_slots=known_slots,
        language=user.preferred_language if user else "ko",
        event_titles=get_event_titles(db, user_id),
        pending_command=pending_command,
        http_client=http_client,
    )


def get_event_titles(db: Session, user_id: int) -> list[str]:
    """사용자의 최상위 일정 제목 목록 (하위 이동시간 일정 제외, 중복 제거·정렬 — 프롬프트 캐시가 흔들리지 않게)."""
    titles = db.execute(
        select(Event.title).where(Event.user_id == user_id, Event.parent_event_id.is_(None)).distinct()
    ).scalars()
    return sorted(titles)
