"""Chat Completions 경로(페르소나 대화·체크인·미준수 피드백)의 사용량 기록."""

import httpx
import pytest

from app.core.config import settings
from app.services.llm_client import TokenUsage, collect_chat_usage, post_chat_completion


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "llm_api_key", "test-key")


def _usage_client(usage: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "gpt-5.6-luna-2026-09-01", "choices": [{"message": {"content": "ok"}}], "usage": usage})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_chat_usage_reads_reasoning_tokens() -> None:
    client = _usage_client(
        {"prompt_tokens": 900, "completion_tokens": 300, "completion_tokens_details": {"reasoning_tokens": 256}}
    )

    result = post_chat_completion({"model": "gpt-5.6-luna", "messages": []}, client)

    assert result.usage == TokenUsage(prompt_tokens=900, cached_tokens=0, completion_tokens=300, reasoning_tokens=256)


def test_collect_chat_usage_records_calls_only_inside_the_block() -> None:
    client = _usage_client({"prompt_tokens": 1500, "completion_tokens": 40, "prompt_tokens_details": {"cached_tokens": 1024}})
    payload = {"model": "gpt-5.6-luna", "messages": [], "prompt_cache_key": "daily_checkin:abc"}

    post_chat_completion(payload, client)
    with collect_chat_usage() as records:
        post_chat_completion(payload, client)
    post_chat_completion(payload, client)

    assert len(records) == 1
    record = records[0]
    assert record.task == "daily_checkin"
    assert (record.requested_model, record.response_model, record.reasoning_effort) == ("gpt-5.6-luna", "gpt-5.6-luna-2026-09-01", None)
    assert record.usage == TokenUsage(prompt_tokens=1500, cached_tokens=1024, completion_tokens=40)
    assert record.latency_ms >= 0
