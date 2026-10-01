"""A scripted stand-in for ResponsesClient: write down which tools the model calls, in what order, and what it
finally says, and the fake replays exactly that.

    fake = FakeResponsesClient([
        call("search_events", query="ECE360"),
        calls(call("propose_create_event", title="Lab"), call("propose_date_range", action="create")),
        say("이렇게 만들까요?"),
    ])

Each script step is one LLM response. Before replaying a step the fake checks that the request carries a
function_call_output for every tool call of the previous step, so an agent loop that forgets to feed results
back fails loudly. Every request is kept in `requests` for assertions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.services import llm_usage
from app.services.llm_client import (
    FunctionTool,
    ResponsesInputItem,
    ResponsesResult,
    ResponsesTask,
    ResponsesUsage,
    ToolCall,
)


@dataclass(frozen=True)
class ScriptedCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Step:
    tool_calls: tuple[ScriptedCall, ...] = ()
    text: str = ""


def call(name: str, **arguments: Any) -> ScriptedCall:
    return ScriptedCall(name, arguments)


def calls(*scripted: ScriptedCall) -> Step:
    return Step(tool_calls=scripted)


def say(text: str) -> Step:
    return Step(text=text)


@dataclass
class FakeRequest:
    task: ResponsesTask
    instruction_blocks: list[str]
    tools: list[FunctionTool]
    input_items: list[ResponsesInputItem]

    def tool_outputs(self) -> dict[str, Any]:
        """call_id -> decoded tool result that the agent fed back in this request."""
        found: dict[str, Any] = {}
        for item in self.input_items:
            if item.get("type") == "function_call_output":
                try:
                    found[item["call_id"]] = json.loads(item["output"])
                except json.JSONDecodeError:
                    found[item["call_id"]] = item["output"]
        return found


@dataclass
class FakeResponsesClient:
    script: list[ScriptedCall | Step]
    usage: ResponsesUsage = field(default_factory=lambda: ResponsesUsage(input_tokens=100, output_tokens=10))
    requests: list[FakeRequest] = field(default_factory=list)
    _pending_call_ids: list[str] = field(default_factory=list)

    def create(
        self,
        *,
        task: ResponsesTask,
        instruction_blocks: list[str],
        tools: list[FunctionTool],
        input_items: list[ResponsesInputItem],
    ) -> ResponsesResult:
        # Like post_responses: refuse over budget, and log every answered call.
        llm_usage.ensure_budget()
        request = FakeRequest(task, list(instruction_blocks), list(tools), list(input_items))
        self.requests.append(request)
        answered = request.tool_outputs()
        unanswered = [call_id for call_id in self._pending_call_ids if call_id not in answered]
        assert not unanswered, f"request #{len(self.requests)} is missing tool outputs for {unanswered}"

        step_index = len(self.requests) - 1
        assert step_index < len(self.script), f"script has {len(self.script)} steps but got request #{len(self.requests)}"
        step = self.script[step_index]
        if isinstance(step, ScriptedCall):
            step = Step(tool_calls=(step,))

        known = {tool.name for tool in tools}
        output_items: list[ResponsesInputItem] = [
            {"type": "reasoning", "id": f"rs_{step_index}", "summary": [], "encrypted_content": f"enc_{step_index}"}
        ]
        tool_calls: list[ToolCall] = []
        for position, scripted in enumerate(step.tool_calls):
            assert not known or scripted.name in known, f"scripted tool {scripted.name!r} is not offered ({sorted(known)})"
            call_id = f"call_{step_index}_{position}"
            arguments = json.dumps(scripted.arguments, ensure_ascii=False)
            tool_calls.append(ToolCall(call_id=call_id, name=scripted.name, arguments=arguments))
            output_items.append(
                {"type": "function_call", "id": f"fc_{step_index}_{position}", "call_id": call_id, "name": scripted.name, "arguments": arguments}
            )
        if step.text:
            output_items.append(
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": step.text}]}
            )
        self._pending_call_ids = [tool_call.call_id for tool_call in tool_calls]
        llm_usage.record_llm_call(
            settings.assistant_model,
            self.usage.input_tokens,
            self.usage.cached_tokens,
            self.usage.output_tokens,
            self.usage.reasoning_tokens,
        )
        return ResponsesResult(
            response_id=f"resp_fake_{step_index}",
            status="completed",
            text=step.text,
            tool_calls=tool_calls,
            output_items=output_items,
            usage=self.usage,
            latency_ms=0,
            model="fake",
            reasoning_effort="low",
        )

    @property
    def finished(self) -> bool:
        return len(self.requests) == len(self.script)
