from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel

from app.core.config import settings
from app.core.exceptions import AppError
from app.i18n import Language, non_compliance_category_label, to_language
from app.models.enums import NonComplianceCategory
from app.schemas.persona import PersonaRead

logger = logging.getLogger(__name__)

CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"

PromptTask = Literal["compliance_feedback", "daily_checkin"]
LANGUAGE_NAMES: dict[Language, str] = {"ko": "한국어(Korean)", "en": "영어(English)"}

# 지시문·요약이 한국어여도 모델이 따라 쓰지 않도록, 대상 언어로 쓴 지시를 한 번 더 붙인다.
_NATIVE_LANGUAGE_RULES: dict[Language, str] = {
    "ko": "반드시 한국어로만 답하세요.",
    "en": "Respond only in English.",
}
DEFAULT_PERSONA_BLOCK = "[페르소나]\n일정 관리 앱의 다정한 코치. 나무라지 않고 공감하며 격려한다."

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


def _prompt_cache_key(task: PromptTask | ResponsesTask, cacheable_prefix: str) -> str:
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


class TokenUsage(BaseModel):
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int  # reasoning_tokens를 포함한 값 (과금 기준)
    reasoning_tokens: int = 0

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
        reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
    )


class ChatUsageRecord(BaseModel):
    """Chat Completions 호출 한 번의 사용량. collect_chat_usage() 안에서만 모인다 (비교 스크립트용)."""

    task: str
    requested_model: str
    response_model: str | None
    reasoning_effort: str | None  # 요청에 넣은 값. None이면 보내지 않았다 = API 기본값
    usage: TokenUsage | None
    latency_ms: int


_chat_usage_sink: ContextVar[list[ChatUsageRecord] | None] = ContextVar("chat_usage_sink", default=None)


@contextmanager
def collect_chat_usage() -> Iterator[list[ChatUsageRecord]]:
    """이 블록 안(같은 스레드·컨텍스트)에서 성공한 Chat Completions 호출의 사용량을 목록으로 모은다."""
    records: list[ChatUsageRecord] = []
    token = _chat_usage_sink.set(records)
    try:
        yield records
    finally:
        _chat_usage_sink.reset(token)


def _log_usage(payload: dict[str, object], usage: TokenUsage | None) -> None:
    cache_key = payload.get("prompt_cache_key", "-")
    if usage is None:
        logger.info("[LLM usage] cache_key=%s usage 정보 없음", cache_key)
        return
    hit_ratio = usage.cached_tokens / usage.prompt_tokens if usage.prompt_tokens else 0.0
    logger.info(
        "[LLM usage] cache_key=%s prompt=%d cached=%d (%.0f%%) uncached=%d completion=%d reasoning=%d",
        cache_key,
        usage.prompt_tokens,
        usage.cached_tokens,
        hit_ratio * 100,
        usage.uncached_prompt_tokens,
        usage.completion_tokens,
        usage.reasoning_tokens,
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
    started = time.perf_counter()
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
    sink = _chat_usage_sink.get()
    if sink is not None:
        sink.append(
            ChatUsageRecord(
                task=str(payload.get("prompt_cache_key", "-")).split(":")[0],
                requested_model=str(payload.get("model")),
                response_model=body.get("model"),
                reasoning_effort=payload.get("reasoning_effort"),  # type: ignore[arg-type]
                usage=usage,
                latency_ms=round((time.perf_counter() - started) * 1000),
            )
        )
    return ChatCompletionResult(content=content, usage=usage)


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
    history: list["ConversationTurn"] | None = None,
    http_client: httpx.Client | None = None,
) -> str:
    """FR-8 저녁 9시 체크인 대화 한 턴을 생성한다.

    summary는 app.services.context_builder.build_daily_checkin_summary가 만든
    하루 요약이다 (완료한 일정은 개수만, 놓친 일정은 제목/시간/사유까지 상세히
    담겨 있다 - "컨텍스트 동적 로딩"). 요약은 매일 바뀌므로 캐시 프리픽스가 아닌
    user 메시지에 발화와 함께 넣는다.
    """
    payload = build_daily_checkin_payload(summary, utterance, persona=persona, language=language, history=history)
    content = _call_chat_completion(payload, http_client)
    return content.strip()


def build_daily_checkin_payload(
    summary: str,
    utterance: str,
    *,
    persona: PersonaRead | None = None,
    language: str = "ko",
    history: list["ConversationTurn"] | None = None,
) -> dict[str, object]:
    instructions = (
        "너는 일정 관리 앱의 페르소나다. 사용자와 저녁 체크인 대화를 나눈다. "
        "사용자 메시지에 오늘 하루 요약이 함께 온다 (완료한 일정은 개수만 적혀 있고, "
        "놓친 일정만 제목·시간·사유가 상세히 적혀 있다). 놓친 일정이 있다면 그것 "
        "위주로 묻고 격려하라. 놓친 일정이 없다면 짧게 칭찬하라. 아래 페르소나의 "
        "성격과 말투로 1~3문장 답하라. "
        "오늘 요약(계획 개수, 완료·놓친 일정)은 대화의 첫 답변에서만 짚는다. '이전 대화'가 있으면 이미 요약을 "
        "말한 것이니 되풀이하지 말고 사용자가 방금 한 말에 반응하라. "
        "사용자가 체크인과 무관한 작업(요리 레시피, 코드 작성, 번역, 숙제 풀이 등)을 부탁하면 캐릭터를 유지한 채 "
        "그 작업은 하지 않고, 오늘 하루 이야기로 자연스럽게 돌아오라."
    )
    lines = [f"오늘 요약:\n{summary}"]
    if history:
        lines.append("이전 대화:\n" + "\n".join(f"{'사용자' if role == 'user' else '페르소나'}: {text}" for role, text in history))
    lines.append(f"사용자 발화: {utterance}")
    user_message = "\n\n".join(lines)

    return _build_persona_prompt("daily_checkin", instructions, persona, language, user_message)


# --- Responses API (일정 어시스턴트) -------------------------------------------------
# gpt-5.6-luna는 Chat Completions에서 도구와 추론을 함께 쓸 수 없어서 어시스턴트는 /v1/responses를 쓴다.
# 대화 상태는 우리 DB가 갖고(previous_response_id 미사용) store=false로 호출하므로, 도구 결과를 넣어 이어서
# 부를 때 직전 응답의 output(암호화된 reasoning 포함)을 입력에 그대로 다시 넣어야 추론이 이어진다.

RESPONSES_URL = "https://api.openai.com/v1/responses"
RESPONSES_TIMEOUT_SECONDS = 20.0

ResponsesTask = Literal["assistant", "assistant_probe"]
ResponsesInputItem = dict[str, Any]

# 모델별로 받는 reasoning.effort 값 (OpenAI 모델 문서 기준, 2026-09 확인). 날짜 접미사가 붙은 스냅샷 이름도
# 접두사로 맞춘다. 목록에 없는 모델은 검증하지 않고 그대로 보낸다 (API가 판단).
REASONING_EFFORTS_BY_MODEL: dict[str, tuple[str, ...]] = {
    "gpt-5.6-luna": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-5.4-nano": ("none", "low", "medium", "high", "xhigh"),
    "gpt-5.4-mini": ("none", "low", "medium", "high", "xhigh"),
    "gpt-5-nano": ("minimal", "low", "medium", "high"),
    "gpt-5-mini": ("minimal", "low", "medium", "high"),
}
# "추론 최소"를 뜻하는 두 이름은 모델마다 하나만 받으므로 서로 바꿔 준다.
_LOWEST_EFFORT_ALIASES = {"none": "minimal", "minimal": "none"}


def resolve_reasoning_effort(model: str, effort: str) -> str:
    """모델이 받는 effort 값으로 검증·변환한다. 지원하지 않는 값이면 LLMConfigError."""
    effort = effort.strip().lower()
    allowed = next(
        (values for prefix, values in sorted(REASONING_EFFORTS_BY_MODEL.items(), key=lambda kv: -len(kv[0])) if model.startswith(prefix)),
        None,
    )
    if allowed is None or effort in allowed:
        return effort
    alias = _LOWEST_EFFORT_ALIASES.get(effort)
    if alias in allowed:
        return alias
    raise LLMConfigError(
        f"ASSISTANT_REASONING_EFFORT={effort!r}는 {model}에서 지원하지 않습니다 (가능한 값: {', '.join(allowed)})"
    )


def validate_assistant_settings() -> str:
    """앱 시작 시 호출: 어시스턴트 모델과 effort 조합이 맞는지 확인하고 실제로 보낼 effort를 돌려준다."""
    return resolve_reasoning_effort(settings.assistant_model, settings.assistant_reasoning_effort)


class FunctionTool(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]
    strict: bool = True

    def to_api(self) -> dict[str, Any]:
        return {"type": "function", **self.model_dump()}


class ToolCall(BaseModel):
    call_id: str
    name: str
    arguments: str  # 모델이 준 JSON 문자열 그대로

    def parsed_arguments(self) -> dict[str, Any]:
        try:
            value = json.loads(self.arguments)
        except json.JSONDecodeError as exc:
            raise LLMResponseParsingError(f"도구 {self.name} 인자가 JSON이 아닙니다: {self.arguments!r}") from exc
        if not isinstance(value, dict):
            raise LLMResponseParsingError(f"도구 {self.name} 인자가 객체가 아닙니다: {self.arguments!r}")
        return value


class ResponsesUsage(BaseModel):
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


class ResponsesResult(BaseModel):
    response_id: str
    status: str
    text: str
    tool_calls: list[ToolCall]
    # 다음 호출 입력에 다시 넣을 원본 output 항목들 (reasoning, function_call, message)
    output_items: list[ResponsesInputItem]
    usage: ResponsesUsage | None
    latency_ms: int
    model: str
    reasoning_effort: str | None


class ResponsesCaller(Protocol):
    """ResponsesClient와 테스트용 가짜 클라이언트가 함께 따르는 인터페이스."""

    def create(
        self,
        *,
        task: ResponsesTask,
        instruction_blocks: list[str],
        tools: list[FunctionTool],
        input_items: list[ResponsesInputItem],
    ) -> ResponsesResult: ...


def user_message(text: str) -> ResponsesInputItem:
    return {"role": "user", "content": text}


def with_tool_outputs(
    input_items: list[ResponsesInputItem], result: ResponsesResult, outputs: dict[str, object]
) -> list[ResponsesInputItem]:
    """도구 결과를 넣어 이어서 부를 입력: 지금까지의 입력 + 직전 응답의 output 전체 + call_id별 도구 결과."""
    missing = [call.call_id for call in result.tool_calls if call.call_id not in outputs]
    if missing:
        raise ValueError(f"결과가 없는 도구 호출이 있습니다: {missing}")
    results: list[ResponsesInputItem] = []
    for call in result.tool_calls:
        value = outputs[call.call_id]
        output = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        results.append({"type": "function_call_output", "call_id": call.call_id, "output": output})
    return [*input_items, *result.output_items, *results]


def build_responses_payload(
    task: ResponsesTask,
    instruction_blocks: list[str],
    tools: list[FunctionTool],
    input_items: list[ResponsesInputItem],
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, object]:
    """고정 지시(instructions)와 도구 정의가 프롬프트 앞(캐시 프리픽스)에 오고, 매번 바뀌는 문맥·대화는
    input에만 둔다. temperature는 넣지 않는다 (추론 모델이 거부한다)."""
    instructions = "\n\n".join(instruction_blocks)
    tool_defs = [tool.to_api() for tool in tools]
    cacheable_prefix = instructions + json.dumps(tool_defs, ensure_ascii=False, sort_keys=True)
    model = model or settings.assistant_model
    return {
        "model": model,
        "instructions": instructions,
        "tools": tool_defs,
        "input": input_items,
        "reasoning": {"effort": resolve_reasoning_effort(model, reasoning_effort or settings.assistant_reasoning_effort)},
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": _prompt_cache_key(task, cacheable_prefix),
    }


def _parse_responses_usage(body: dict[str, Any]) -> ResponsesUsage | None:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    return ResponsesUsage(
        input_tokens=usage.get("input_tokens", 0),
        cached_tokens=(usage.get("input_tokens_details") or {}).get("cached_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        reasoning_tokens=(usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0),
    )


def _parse_responses_body(body: dict[str, Any], latency_ms: int) -> ResponsesResult:
    output = body.get("output")
    if not isinstance(output, list):
        raise LLMResponseParsingError(f"Responses API 응답에 output 배열이 없습니다: {body!r}")
    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    try:
        for item in output:
            if item["type"] == "function_call":
                tool_calls.append(ToolCall(call_id=item["call_id"], name=item["name"], arguments=item.get("arguments", "")))
            elif item["type"] == "message":
                texts.extend(part["text"] for part in item.get("content", []) if part.get("type") == "output_text")
    except (KeyError, TypeError) as exc:
        raise LLMResponseParsingError(f"Responses API output 항목 형식이 예상과 다릅니다: {output!r}") from exc
    return ResponsesResult(
        response_id=body.get("id", ""),
        status=body.get("status", ""),
        text="".join(texts),
        tool_calls=tool_calls,
        output_items=output,
        usage=_parse_responses_usage(body),
        latency_ms=latency_ms,
        model=body.get("model", ""),
        reasoning_effort=(body.get("reasoning") or {}).get("effort"),
    )


def _log_responses_usage(payload: dict[str, object], result: ResponsesResult) -> None:
    usage = result.usage
    cache_key = payload.get("prompt_cache_key", "-")
    if usage is None:
        logger.info("[LLM usage] api=responses cache_key=%s latency_ms=%d usage 정보 없음", cache_key, result.latency_ms)
        return
    hit_ratio = usage.cached_tokens / usage.input_tokens if usage.input_tokens else 0.0
    logger.info(
        "[LLM usage] api=responses model=%s effort=%s cache_key=%s input=%d cached=%d (%.0f%%) output=%d "
        "reasoning=%d latency_ms=%d tool_calls=%d status=%s",
        result.model or payload.get("model"),
        result.reasoning_effort,
        cache_key,
        usage.input_tokens,
        usage.cached_tokens,
        hit_ratio * 100,
        usage.output_tokens,
        usage.reasoning_tokens,
        result.latency_ms,
        len(result.tool_calls),
        result.status,
    )


def post_responses(
    payload: dict[str, object],
    http_client: httpx.Client | None = None,
    *,
    timeout: float = RESPONSES_TIMEOUT_SECONDS,
) -> ResponsesResult:
    """/v1/responses 한 번 호출. 도구 호출·텍스트·토큰 사용량·지연 시간을 돌려주고 사용량을 로그로 남긴다."""
    if not settings.llm_api_key:
        raise LLMConfigError("LLM_API_KEY가 설정되지 않았습니다 (.env 확인)")

    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    owns_client = http_client is None
    client = http_client or httpx.Client()
    started = time.perf_counter()
    try:
        response = client.post(RESPONSES_URL, json=payload, headers=headers, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise LLMRequestError(f"LLM API 호출이 {timeout:g}초 안에 끝나지 않았습니다: {exc}") from exc
    except httpx.HTTPError as exc:
        raise LLMRequestError(f"LLM API 호출에 실패했습니다: {exc}") from exc
    finally:
        if owns_client:
            client.close()
    latency_ms = round((time.perf_counter() - started) * 1000)

    if response.status_code >= 400:
        raise LLMRequestError(f"LLM API가 오류를 반환했습니다 ({response.status_code}): {response.text}")
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise LLMResponseParsingError(f"LLM 응답이 JSON이 아닙니다: {response.text!r}") from exc
    if not isinstance(body, dict):
        raise LLMResponseParsingError(f"LLM 응답 형식이 예상과 다릅니다: {response.text!r}")

    result = _parse_responses_body(body, latency_ms)
    _log_responses_usage(payload, result)
    if result.status != "completed":
        logger.warning("[LLM] Responses 응답이 완료되지 않았습니다: status=%s details=%s", result.status, body.get("incomplete_details"))
    return result


class ResponsesClient:
    """실제 /v1/responses 클라이언트. model·reasoning_effort를 생략하면 ASSISTANT_* 설정을 쓴다."""

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout: float = RESPONSES_TIMEOUT_SECONDS,
    ) -> None:
        self.http_client = http_client
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout

    def create(
        self,
        *,
        task: ResponsesTask,
        instruction_blocks: list[str],
        tools: list[FunctionTool],
        input_items: list[ResponsesInputItem],
    ) -> ResponsesResult:
        payload = build_responses_payload(
            task, instruction_blocks, tools, input_items, model=self.model, reasoning_effort=self.reasoning_effort
        )
        return post_responses(payload, self.http_client, timeout=self.timeout)
