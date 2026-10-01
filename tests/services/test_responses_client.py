import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app.core.config import settings
from app.services.llm_client import (
    RESPONSES_URL,
    FunctionTool,
    LLMConfigError,
    LLMRequestError,
    LLMResponseParsingError,
    ResponsesClient,
    build_responses_payload,
    resolve_reasoning_effort,
    user_message,
    validate_assistant_settings,
    with_tool_outputs,
)
from tests.fake_responses import FakeResponsesClient, call, calls, say
from tests.llm_scope import llm_scope  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[2]

SEARCH_TOOL = FunctionTool(
    name="search_events",
    description="Find events by title",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
)


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "assistant_model", "gpt-5.4-mini")
    monkeypatch.setattr(settings, "assistant_reasoning_effort", "low")


def _client(handler) -> ResponsesClient:
    return ResponsesClient(httpx.Client(transport=httpx.MockTransport(handler)))


def _tool_call_body() -> dict:
    return {
        "id": "resp_1",
        "status": "completed",
        "model": "gpt-5.4-mini-2026-03-17",
        "reasoning": {"effort": "low"},
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAA"},
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "search_events",
                "arguments": '{"query": "ECE360"}',
                "status": "completed",
            },
        ],
        "usage": {
            "input_tokens": 1200,
            "input_tokens_details": {"cached_tokens": 1024},
            "output_tokens": 90,
            "output_tokens_details": {"reasoning_tokens": 64},
        },
    }


def _text_body(text: str) -> dict:
    return {
        "id": "resp_2",
        "status": "completed",
        "model": "gpt-5.4-mini-2026-03-17",
        "output": [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": []}]}
        ],
        "usage": {"input_tokens": 1300, "output_tokens": 20},
    }


def _create(client: ResponsesClient, input_items: list | None = None):
    return client.create(
        task="assistant",
        instruction_blocks=["[role] You are a scheduler.", "[ranges] Lecture Period"],
        tools=[SEARCH_TOOL],
        input_items=input_items or [user_message("ECE360 찾아줘")],
    )


def test_request_puts_fixed_prefix_first_and_sets_reasoning_without_temperature() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_tool_call_body())

    _create(_client(handler))

    request = seen[0]
    assert str(request.url) == RESPONSES_URL
    assert request.headers["authorization"] == "Bearer test-key"
    body = json.loads(request.content)
    assert body["model"] == "gpt-5.4-mini"
    assert body["instructions"] == "[role] You are a scheduler.\n\n[ranges] Lecture Period"
    assert body["tools"] == [
        {
            "type": "function",
            "name": "search_events",
            "description": "Find events by title",
            "parameters": SEARCH_TOOL.parameters,
            "strict": True,
        }
    ]
    assert body["input"] == [{"role": "user", "content": "ECE360 찾아줘"}]
    assert body["reasoning"] == {"effort": "low"}
    assert body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["prompt_cache_key"].startswith("assistant:")
    assert "temperature" not in body
    assert list(body)[:3] == ["model", "instructions", "tools"]


def test_cache_key_follows_instructions_and_tools_but_not_input() -> None:
    base = build_responses_payload("assistant", ["fixed"], [SEARCH_TOOL], [user_message("a")])
    other_input = build_responses_payload("assistant", ["fixed"], [SEARCH_TOOL], [user_message("b")])
    other_tools = build_responses_payload("assistant", ["fixed"], [], [user_message("a")])
    other_rules = build_responses_payload("assistant", ["changed"], [SEARCH_TOOL], [user_message("a")])

    assert base["prompt_cache_key"] == other_input["prompt_cache_key"]
    assert base["prompt_cache_key"] != other_tools["prompt_cache_key"]
    assert base["prompt_cache_key"] != other_rules["prompt_cache_key"]


def test_client_overrides_model_and_effort() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_text_body("ok"))

    client = ResponsesClient(httpx.Client(transport=httpx.MockTransport(handler)), model="gpt-5.4-nano", reasoning_effort="medium")
    _create(client)

    assert bodies[0]["model"] == "gpt-5.4-nano"
    assert bodies[0]["reasoning"] == {"effort": "medium"}


def test_parses_tool_calls_usage_and_latency() -> None:
    result = _create(_client(lambda request: httpx.Response(200, json=_tool_call_body())))

    assert result.response_id == "resp_1"
    assert result.text == ""
    assert [(c.call_id, c.name) for c in result.tool_calls] == [("call_1", "search_events")]
    assert result.tool_calls[0].parsed_arguments() == {"query": "ECE360"}
    assert result.usage is not None
    assert (result.usage.input_tokens, result.usage.cached_tokens) == (1200, 1024)
    assert (result.usage.output_tokens, result.usage.reasoning_tokens) == (90, 64)
    assert result.latency_ms >= 0
    assert result.model == "gpt-5.4-mini-2026-03-17"


def test_parses_final_text() -> None:
    result = _create(_client(lambda request: httpx.Response(200, json=_text_body("이렇게 만들까요?"))))

    assert result.text == "이렇게 만들까요?"
    assert result.tool_calls == []
    assert result.usage is not None and result.usage.reasoning_tokens == 0


def test_bad_tool_arguments_raise_parsing_error() -> None:
    body = _tool_call_body()
    body["output"][1]["arguments"] = "{not json"
    result = _create(_client(lambda request: httpx.Response(200, json=body)))

    with pytest.raises(LLMResponseParsingError):
        result.tool_calls[0].parsed_arguments()


def test_tool_outputs_are_appended_after_previous_output_items() -> None:
    bodies: list[dict] = []
    replies = iter([_tool_call_body(), _text_body("ECE360 Lab을 찾았어요.")])

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=next(replies))

    client = _client(handler)
    first_input = [user_message("ECE360 찾아줘")]
    first = _create(client, first_input)
    second_input = with_tool_outputs(first_input, first, {"call_1": {"events": [{"id": 7, "title": "ECE360 Lab"}]}})
    second = _create(client, second_input)

    assert bodies[1]["input"] == [
        {"role": "user", "content": "ECE360 찾아줘"},
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAA"},
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "search_events",
            "arguments": '{"query": "ECE360"}',
            "status": "completed",
        },
        {"type": "function_call_output", "call_id": "call_1", "output": '{"events": [{"id": 7, "title": "ECE360 Lab"}]}'},
    ]
    assert bodies[0]["prompt_cache_key"] == bodies[1]["prompt_cache_key"]
    assert second.text == "ECE360 Lab을 찾았어요."


def test_with_tool_outputs_requires_every_call_answered() -> None:
    first = _create(_client(lambda request: httpx.Response(200, json=_tool_call_body())))

    with pytest.raises(ValueError):
        with_tool_outputs([], first, {})


def test_http_400_raises_request_error_with_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'temperature'"}})

    with pytest.raises(LLMRequestError) as info:
        _create(_client(handler))
    assert "400" in str(info.value)
    assert "temperature" in str(info.value)


def test_timeout_raises_request_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(LLMRequestError) as info:
        _create(_client(handler))
    assert "20" in str(info.value)


def test_non_json_and_missing_output_raise_parsing_error() -> None:
    with pytest.raises(LLMResponseParsingError):
        _create(_client(lambda request: httpx.Response(200, text="<html>")))
    with pytest.raises(LLMResponseParsingError):
        _create(_client(lambda request: httpx.Response(200, json={"id": "resp_x", "status": "completed"})))


def test_missing_api_key_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_api_key", None)

    with pytest.raises(LLMConfigError):
        _create(_client(lambda request: httpx.Response(200, json=_text_body("x"))))


def test_usage_log_includes_reasoning_tokens_and_latency(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.services.llm_client"):
        _create(_client(lambda request: httpx.Response(200, json=_tool_call_body())))

    line = next(r.getMessage() for r in caplog.records if "api=responses" in r.getMessage())
    for fragment in ("input=1200", "cached=1024", "output=90", "reasoning=64", "latency_ms=", "tool_calls=1", "effort=low"):
        assert fragment in line


def test_incomplete_response_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    body = _text_body("")
    body["status"] = "incomplete"
    body["incomplete_details"] = {"reason": "max_output_tokens"}

    with caplog.at_level(logging.WARNING, logger="app.services.llm_client"):
        result = _create(_client(lambda request: httpx.Response(200, json=body)))

    assert result.status == "incomplete"
    assert any("max_output_tokens" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"LLM_MODEL": "main-model"}, ("main-model", "gpt-5.6-luna", "medium")),
        (
            {"LLM_MODEL": "main-model", "ASSISTANT_MODEL": "cheap-model", "ASSISTANT_REASONING_EFFORT": "medium"},
            ("main-model", "cheap-model", "medium"),
        ),
    ],
)
def test_assistant_model_defaults_are_independent_of_llm_model(env: dict[str, str], expected: tuple[str, str, str]) -> None:
    clean = {k: v for k, v in os.environ.items() if k not in ("LLM_MODEL", "ASSISTANT_MODEL", "ASSISTANT_REASONING_EFFORT")}
    code = (
        "import dotenv; dotenv.load_dotenv = lambda *a, **k: None\n"
        "from app.core.config import settings as s\n"
        "print(s.llm_model, s.assistant_model, s.assistant_reasoning_effort)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], env={**clean, **env}, capture_output=True, text=True, check=True, cwd=REPO_ROOT
    )

    assert tuple(out.stdout.split()) == expected


# --- scripted fake -------------------------------------------------------------------


def test_fake_replays_script_and_checks_tool_outputs_are_fed_back() -> None:
    fake = FakeResponsesClient(
        [
            call("search_events", query="ECE360"),
            calls(call("search_events", query="Lab"), call("search_events", query="Lecture")),
            say("두 개 찾았어요."),
        ]
    )
    items = [user_message("ECE360 찾아줘")]

    first = fake.create(task="assistant", instruction_blocks=["x"], tools=[SEARCH_TOOL], input_items=items)
    assert [c.parsed_arguments() for c in first.tool_calls] == [{"query": "ECE360"}]

    items = with_tool_outputs(items, first, {first.tool_calls[0].call_id: {"events": []}})
    second = fake.create(task="assistant", instruction_blocks=["x"], tools=[SEARCH_TOOL], input_items=items)
    assert [c.name for c in second.tool_calls] == ["search_events", "search_events"]
    assert fake.requests[1].tool_outputs() == {first.tool_calls[0].call_id: {"events": []}}

    items = with_tool_outputs(items, second, {c.call_id: "[]" for c in second.tool_calls})
    third = fake.create(task="assistant", instruction_blocks=["x"], tools=[SEARCH_TOOL], input_items=items)
    assert third.text == "두 개 찾았어요."
    assert third.tool_calls == []
    assert fake.finished


def test_fake_fails_when_tool_output_is_missing_or_script_runs_out() -> None:
    fake = FakeResponsesClient([call("search_events", query="x")])
    fake.create(task="assistant", instruction_blocks=[], tools=[SEARCH_TOOL], input_items=[])

    with pytest.raises(AssertionError, match="missing tool outputs"):
        fake.create(task="assistant", instruction_blocks=[], tools=[SEARCH_TOOL], input_items=[])

    exhausted = FakeResponsesClient([say("끝")])
    exhausted.create(task="assistant", instruction_blocks=[], tools=[], input_items=[])
    with pytest.raises(AssertionError, match="script has 1 steps"):
        exhausted.create(task="assistant", instruction_blocks=[], tools=[], input_items=[])


def test_fake_rejects_tools_that_were_not_offered() -> None:
    fake = FakeResponsesClient([call("delete_everything")])

    with pytest.raises(AssertionError, match="not offered"):
        fake.create(task="assistant", instruction_blocks=[], tools=[SEARCH_TOOL], input_items=[])


# --- reasoning effort -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        ("gpt-5.6-luna", "low", "low"),
        ("gpt-5.6-luna", "minimal", "none"),
        ("gpt-5.6-luna", "max", "max"),
        ("gpt-5-nano", "none", "minimal"),
        ("gpt-5-nano-2025-08-07", "LOW", "low"),
        ("gpt-5.4-nano", "xhigh", "xhigh"),
        ("gpt-5.4-mini", "minimal", "none"),
        ("some-future-model", "whatever", "whatever"),
    ],
)
def test_reasoning_effort_is_validated_and_converted_per_model(model: str, effort: str, expected: str) -> None:
    assert resolve_reasoning_effort(model, effort) == expected


@pytest.mark.parametrize(("model", "effort"), [("gpt-5-nano", "xhigh"), ("gpt-5.4-nano", "max"), ("gpt-5.6-luna", "huge")])
def test_unsupported_reasoning_effort_fails_clearly(model: str, effort: str) -> None:
    with pytest.raises(LLMConfigError, match="지원하지 않습니다"):
        resolve_reasoning_effort(model, effort)


def test_startup_validation_uses_assistant_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "assistant_model", "gpt-5-nano")
    monkeypatch.setattr(settings, "assistant_reasoning_effort", "none")
    assert validate_assistant_settings() == "minimal"
    body = build_responses_payload("assistant", ["x"], [], [])
    assert body["reasoning"] == {"effort": "minimal"}

    monkeypatch.setattr(settings, "assistant_reasoning_effort", "max")
    with pytest.raises(LLMConfigError):
        validate_assistant_settings()
