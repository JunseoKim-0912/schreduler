"""같은 LLM 요청을 반복 호출해 캐싱 적용/미적용 시 입력 토큰 사용량을 로그로 비교한다.

- cached:   실제 서비스와 같은 payload (고정 프리픽스 + prompt_cache_key)
- uncached: 매 요청마다 system 메시지 맨 앞에 고유 nonce를 붙이고 prompt_cache_key를 빼서
            프리픽스가 절대 일치하지 않게 만든 payload (캐싱 미적용 상황 재현)

실제 LLM API를 호출하므로 LLM_API_KEY가 필요하고 비용이 발생한다.

실행 (프로젝트 루트에서):
    python -m app.scripts.compare_prompt_cache --task daily_checkin --persona Rordon --repeat 5
"""

import argparse
import logging
import sys
import uuid
from collections.abc import Callable
from datetime import date
from typing import Literal

import httpx

from app.core.db import SessionLocal
from app.models import NonComplianceCategory, Persona
from app.schemas.persona import PersonaRead
from app.services.llm_client import (
    LLMClientError,
    TokenUsage,
    _build_request_payload,
    build_compliance_feedback_payload,
    build_daily_checkin_payload,
    post_chat_completion,
)

logger = logging.getLogger("compare_prompt_cache")

Task = Literal["daily_checkin", "compliance_feedback", "slot_fill"]
Mode = Literal["cached", "uncached"]

# OpenAI는 프리픽스가 이 길이 이상일 때만 캐시한다.
OPENAI_MIN_CACHEABLE_TOKENS = 1024

SAMPLE_SUMMARY = "오늘 계획한 3개 중 2개 완료.\n놓친 일정:\n- 알고리즘 스터디 (21:00~22:00) - 사유: 피로/무기력"
SAMPLE_UTTERANCE = "오늘 좀 피곤해서 스터디를 못 했어"
SAMPLE_REASON = "버스가 20분이나 안 와서 늦었어요"
SAMPLE_SLOT_UTTERANCE = "매주 화요일 저녁 7시에 헬스"


def build_payload_factory(
    task: Task, persona: PersonaRead | None, language: str
) -> Callable[[], dict[str, object]]:
    if task == "daily_checkin":
        return lambda: build_daily_checkin_payload(
            SAMPLE_SUMMARY, SAMPLE_UTTERANCE, persona=persona, language=language
        )
    if task == "compliance_feedback":
        return lambda: build_compliance_feedback_payload(
            NonComplianceCategory.OTHER, SAMPLE_REASON, persona=persona, language=language
        )
    return lambda: _build_request_payload(SAMPLE_SLOT_UTTERANCE, [], date.today())


def bust_prompt_cache(payload: dict[str, object]) -> dict[str, object]:
    messages = [dict(m) for m in payload["messages"]]  # type: ignore[union-attr]
    messages[0]["content"] = f"[request-id {uuid.uuid4()}]\n{messages[0]['content']}"
    busted = {k: v for k, v in payload.items() if k != "prompt_cache_key"}
    busted["messages"] = messages
    return busted


def run_mode(
    mode: Mode,
    payload_factory: Callable[[], dict[str, object]],
    repeat: int,
    http_client: httpx.Client | None = None,
) -> list[TokenUsage]:
    usages: list[TokenUsage] = []
    for i in range(1, repeat + 1):
        payload = payload_factory()
        if mode == "uncached":
            payload = bust_prompt_cache(payload)
        result = post_chat_completion(payload, http_client)
        if result.usage is None:
            logger.warning("[%s #%d] 응답에 usage가 없어 집계에서 제외", mode, i)
            continue
        usages.append(result.usage)
        logger.info(
            "[%s #%d] prompt=%d cached=%d uncached=%d",
            mode,
            i,
            result.usage.prompt_tokens,
            result.usage.cached_tokens,
            result.usage.uncached_prompt_tokens,
        )
    return usages


def summarize(results: dict[Mode, list[TokenUsage]]) -> None:
    totals: dict[Mode, tuple[int, int, int]] = {}
    for mode, usages in results.items():
        prompt = sum(u.prompt_tokens for u in usages)
        cached = sum(u.cached_tokens for u in usages)
        totals[mode] = (prompt, cached, prompt - cached)
        ratio = cached / prompt * 100 if prompt else 0.0
        logger.info(
            "[요약 %-8s] 요청 %d회 | 입력 %d | 캐시 적중 %d (%.0f%%) | 캐시 미적중 입력 %d",
            mode,
            len(usages),
            prompt,
            cached,
            ratio,
            prompt - cached,
        )

    if "cached" in totals and "uncached" in totals:
        saved = totals["uncached"][2] - totals["cached"][2]
        base = totals["uncached"][2]
        logger.info(
            "[비교] 캐싱 적용 시 캐시 미적중 입력 토큰 %d개 감소 (%.0f%%)",
            saved,
            saved / base * 100 if base else 0.0,
        )

    max_prompt = max((u.prompt_tokens for usages in results.values() for u in usages), default=0)
    if max_prompt < OPENAI_MIN_CACHEABLE_TOKENS:
        logger.warning(
            "입력이 %d토큰으로 OpenAI 캐시 최소 길이(%d)보다 짧아 두 모드 모두 캐시가 적중하지 않습니다. "
            "예시 대사/배경이 채워진 페르소나로 다시 측정해 보세요.",
            max_prompt,
            OPENAI_MIN_CACHEABLE_TOKENS,
        )


def load_persona(name: str) -> PersonaRead:
    with SessionLocal() as session:
        persona = session.get(Persona, name)
        if persona is None:
            raise SystemExit(
                f"페르소나 '{name}'를 DB에서 찾을 수 없습니다. python -m app.scripts.seed_personas를 먼저 실행하세요."
            )
        return PersonaRead.model_validate(persona)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--task", choices=["daily_checkin", "compliance_feedback", "slot_fill"], default="daily_checkin")
    parser.add_argument("--persona", help="DB에 있는 페르소나 name (생략 시 기본 코치)")
    parser.add_argument("--language", choices=["ko", "en"], default="ko")
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    persona = load_persona(args.persona) if args.persona else None
    payload_factory = build_payload_factory(args.task, persona, args.language)

    try:
        results: dict[Mode, list[TokenUsage]] = {
            "uncached": run_mode("uncached", payload_factory, args.repeat),
            "cached": run_mode("cached", payload_factory, args.repeat),
        }
    except LLMClientError as exc:
        logger.error("LLM 호출 실패: %s", exc)
        return 1

    summarize(results)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
