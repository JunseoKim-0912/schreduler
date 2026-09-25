import json
import logging

import httpx
import pytest

from app.core.config import settings
from app.scripts.compare_prompt_cache import build_payload_factory, bust_prompt_cache, run_mode, summarize
from app.services.llm_client import LLMRequestError, LLMResponseParsingError, TokenUsage, post_chat_completion


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_api_key", "test-key")


def _fake_openai(prefix_cached_tokens: int = 1024) -> tuple[httpx.Client, list[dict]]:
    """같은 system 프리픽스를 두 번째로 받으면 cached_tokens를 돌려주는 가짜 OpenAI."""
    seen_prefixes: set[str] = set()
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        prefix = payload["messages"][0]["content"]
        cached = prefix_cached_tokens if prefix in seen_prefixes else 0
        seen_prefixes.add(prefix)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 1500,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": cached},
                },
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


def test_post_chat_completion_logs_usage(caplog: pytest.LogCaptureFixture) -> None:
    client, _ = _fake_openai()
    payload = build_payload_factory("daily_checkin", None, "ko")()

    with caplog.at_level(logging.INFO, logger="app.services.llm_client"):
        post_chat_completion(payload, client)
        result = post_chat_completion(payload, client)

    assert result.usage == TokenUsage(prompt_tokens=1500, cached_tokens=1024, completion_tokens=20)
    usage_logs = [r.getMessage() for r in caplog.records if "[LLM usage]" in r.getMessage()]
    assert "cached=0 (0%)" in usage_logs[0]
    assert "cached=1024 (68%) uncached=476" in usage_logs[1]
    assert payload["prompt_cache_key"] in usage_logs[1]


def test_missing_usage_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        )
    )

    with caplog.at_level(logging.INFO, logger="app.services.llm_client"):
        result = post_chat_completion(build_payload_factory("daily_checkin", None, "ko")(), client)

    assert result.usage is None
    assert "usage 정보 없음" in caplog.text


def test_bust_prompt_cache_changes_prefix_and_drops_key() -> None:
    payload = build_payload_factory("compliance_feedback", None, "ko")()

    first, second = bust_prompt_cache(payload), bust_prompt_cache(payload)

    assert "prompt_cache_key" not in first
    assert first["messages"][0]["content"] != second["messages"][0]["content"]
    assert first["messages"][0]["content"].endswith(payload["messages"][0]["content"])
    assert first["messages"][1] == payload["messages"][1]
    assert "prompt_cache_key" in payload  # 원본은 건드리지 않는다


def test_run_mode_compares_cached_and_uncached(caplog: pytest.LogCaptureFixture) -> None:
    client, requests = _fake_openai()
    factory = build_payload_factory("daily_checkin", None, "ko")

    uncached = run_mode("uncached", factory, 3, client)
    cached = run_mode("cached", factory, 3, client)

    assert [u.cached_tokens for u in uncached] == [0, 0, 0]
    assert [u.cached_tokens for u in cached] == [0, 1024, 1024]
    assert all("prompt_cache_key" not in r for r in requests[:3])
    assert all("prompt_cache_key" in r for r in requests[3:])

    with caplog.at_level(logging.INFO, logger="compare_prompt_cache"):
        summarize({"uncached": uncached, "cached": cached})

    assert "캐시 미적중 입력 토큰 2048개 감소 (46%)" in caplog.text
    assert "OpenAI 캐시 최소 길이" not in caplog.text


def test_summarize_warns_when_prompt_too_short(caplog: pytest.LogCaptureFixture) -> None:
    short = [TokenUsage(prompt_tokens=300, cached_tokens=0, completion_tokens=10)]

    with caplog.at_level(logging.INFO, logger="compare_prompt_cache"):
        summarize({"uncached": short, "cached": short})

    assert "OpenAI 캐시 최소 길이(1024)" in caplog.text


def test_network_error_becomes_llm_request_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LLMRequestError, match="호출에 실패"):
        post_chat_completion(build_payload_factory("daily_checkin", None, "ko")(), httpx.Client(transport=httpx.MockTransport(handler)))


def test_malformed_body_becomes_parsing_error() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": []})))

    with pytest.raises(LLMResponseParsingError, match="형식이 예상과 다릅니다"):
        post_chat_completion(build_payload_factory("daily_checkin", None, "ko")(), client)
