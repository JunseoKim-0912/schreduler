"""Make changes "through the assistant" in API tests without a real model.

The fake LLM (tests/fake_responses) replays a script of tool calls; the helpers send one message to
POST /assistant/chat and press [만들기] (POST /assistant/confirm), the same path the app uses.

    body = run(client, monkeypatch, user_id, find(query="물리 퀴즈"), update([(event_id, None)], start_time="18:00"))
    action_id = only_action(body)

Update and delete drafts only accept ids that search_events returned in the session, so put find(...) first.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.services.assistant import agent
from tests.fake_responses import FakeResponsesClient, ScriptedCall, call, say
from tests.auth_helpers import as_user

Targets = list[tuple[int, int | None]]


def find(**args: Any) -> ScriptedCall:
    return call("search_events", **{"query": None, "date_from": None, "date_to": None, "weekday": None, **args})


def create(**args: Any) -> ScriptedCall:
    defaults = {
        "event_type": "scheduled", "date": None, "start_time": None, "end_time": None, "importance": None,
        "recurrence": None, "date_range": None, "location": None, "inferred_fields": [], "draft_id": None,
    }
    return call("propose_create_event", **{**defaults, **args})


def update(targets: Targets, scope: str = "series", **changes: Any) -> ScriptedCall:
    fields = {"title": None, "date": None, "start_time": None, "end_time": None, "importance": None, "location": None}
    return call(
        "propose_update_event",
        target_ids=_ids(targets), scope=scope, changes={**fields, **changes}, inferred_fields=[], draft_id=None,
    )


def delete(targets: Targets, scope: str = "series") -> ScriptedCall:
    return call("propose_delete_event", target_ids=_ids(targets), scope=scope, inferred_fields=[], draft_id=None)


def date_range(action: str, name: str, **args: Any) -> ScriptedCall:
    # call()의 첫 인자 이름이 name이라 직접 만든다.
    values = {"action": action, "name": name, "new_name": None, "start_date": None, "end_date": None, "mode": None,
              "inferred_fields": [], "draft_id": None, **args}
    return ScriptedCall("propose_date_range", values)


def _ids(targets: Targets) -> list[dict[str, int | None]]:
    return [{"event_id": event_id, "instance_id": instance_id} for event_id, instance_id in targets]


def _headers(user_id: int) -> dict[str, str]:
    return as_user(user_id)


def propose(client: TestClient, monkeypatch: pytest.MonkeyPatch, user_id: int, *tool_calls: ScriptedCall, message: str = "요청") -> dict:
    """한 턴: 도구를 하나씩 부른 뒤 짧게 답한다. 제안 카드가 담긴 /assistant/chat 응답을 돌려준다."""
    fake = FakeResponsesClient([*tool_calls, say("이렇게 할까요?")])
    monkeypatch.setattr(agent, "ResponsesClient", lambda: fake)
    response = client.post("/assistant/chat", json={"message": message}, headers=_headers(user_id))
    assert response.status_code == 200, response.text
    assert fake.finished
    return response.json()


def confirm(client: TestClient, user_id: int, chat: dict) -> dict:
    assert chat["proposal"], f"no proposal: {chat['reply']}"
    response = client.post(
        "/assistant/confirm", json={"session_id": chat["session_id"], "token": chat["proposal"]["token"]}, headers=_headers(user_id)
    )
    assert response.status_code == 200, response.text
    return response.json()


def run(client: TestClient, monkeypatch: pytest.MonkeyPatch, user_id: int, *tool_calls: ScriptedCall, message: str = "요청") -> dict:
    return confirm(client, user_id, propose(client, monkeypatch, user_id, *tool_calls, message=message))


def only_action(confirmed: dict) -> int:
    [action] = confirmed["executed"]
    return action["action_id"]


def undo(client: TestClient, user_id: int, action_id: int):
    return client.post(f"/actions/{action_id}/undo", headers=_headers(user_id))
