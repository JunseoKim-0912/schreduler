"""Daily LLM budget and usage logging (app/services/llm_usage.py). The LLM is a fake or an httpx MockTransport."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.clock import utc_now_naive
from app.core.config import settings
from app.core.db import get_db
from app.main import app
from app.models import AssistantMessage, AssistantTurnLog, Base, Event, EventInstance, EventInstanceStatus, LlmUsageLog, User
from app.scripts import usage_report
from app.services import llm_client, llm_usage, notification
from app.services.assistant import agent
from app.services.llm_client import ResponsesClient, ResponsesUsage
from app.services.llm_pricing import PRICES, UnknownModelPriceError, call_cost, price_for, validate_configured_models
from tests.fake_responses import FakeResponsesClient, call, say

ROOT = Path(__file__).resolve().parents[2]
NOW_UTC = "2026-10-01 16:00:00"  # Toronto 12:00 (EDT)
STUDY = {
    "event_type": "scheduled",
    "title": "Study",
    "date": "2026-10-02",
    "start_time": "11:00",
    "end_time": "13:00",
    "importance": 1,
    "recurrence": None,
    "date_range": None,
    "location": None,
    "inferred_fields": [],
    "draft_id": None,
}
_REAL_HTTPX_CLIENT = httpx.Client


@pytest.fixture(autouse=True)
def frozen():
    with freeze_time(NOW_UTC):
        yield


@pytest.fixture(autouse=True)
def llm_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "gpt-5.6-luna")
    monkeypatch.setattr(settings, "assistant_model", "gpt-5.6-luna")
    monkeypatch.setattr(settings, "assistant_reasoning_effort", "medium")
    monkeypatch.setattr(settings, "app_timezone", "America/Toronto")
    monkeypatch.setattr(settings, "llm_daily_budget_per_user_usd", 1.0)
    monkeypatch.setattr(settings, "llm_daily_budget_total_usd", 5.0)
    monkeypatch.setattr(settings, "llm_daily_budget_admin_usd", None)


@pytest.fixture
def engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def client(engine):
    local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def users(engine) -> tuple[int, int]:
    with Session(engine) as session:
        me, other = User(name="June", preferred_language="ko"), User(name="Kim", preferred_language="en")
        session.add_all([me, other])
        session.commit()
        return me.id, other.id


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch):
    def use(*steps, usage: ResponsesUsage | None = None) -> FakeResponsesClient:
        fake = FakeResponsesClient(list(steps)) if usage is None else FakeResponsesClient(list(steps), usage=usage)
        monkeypatch.setattr(agent, "ResponsesClient", lambda: fake)
        return fake

    return use


def _headers(user_id: int) -> dict[str, str]:
    return {"X-User-Id": str(user_id)}


def _spend(engine, user_id: int | None, cost: float, when: datetime | None = None, feature: str = "assistant") -> None:
    with Session(engine) as session:
        session.add(
            LlmUsageLog(
                user_id=user_id, feature=feature, model="gpt-5.6-luna", input_tokens=0, cached_tokens=0,
                output_tokens=0, reasoning_tokens=0, cost_usd=cost, created_at=when or utc_now_naive(),
            )
        )
        session.commit()


def _rows(engine) -> list[LlmUsageLog]:
    with Session(engine) as session:
        return list(session.execute(select(LlmUsageLog).order_by(LlmUsageLog.id)).scalars())


def _missed_instance(engine, user_id: int) -> int:
    with Session(engine) as session:
        event = Event(user_id=user_id, title="Gym", start_time=datetime(2026, 9, 30, 7, 0), end_time=datetime(2026, 9, 30, 8, 0))
        session.add(event)
        session.flush()
        instance = EventInstance(event_id=event.id, date=date(2026, 9, 30), status=EventInstanceStatus.MISSED)
        session.add(instance)
        session.commit()
        return instance.id


def _patch_chat_http(monkeypatch: pytest.MonkeyPatch, text: str, usage: dict | None = None) -> list[dict]:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        body = {"model": "gpt-5.6-luna", "choices": [{"message": {"content": text}}]}
        if usage is not None:
            body["usage"] = usage
        return httpx.Response(200, json=body)

    monkeypatch.setattr(llm_client.httpx, "Client", lambda *a, **kw: _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler)))
    return seen


# --- cost ----------------------------------------------------------------------------------


def test_cost_follows_the_price_table() -> None:
    price = PRICES["gpt-5.6-luna"]
    cost = call_cost("gpt-5.6-luna", input_tokens=10_000, cached_tokens=4_000, output_tokens=2_000)
    assert cost == pytest.approx((6_000 * price.input + 4_000 * price.cached + 2_000 * price.output) / 1_000_000)
    assert cost == pytest.approx(0.00368)
    # reasoning tokens are already part of output_tokens, so they are priced at the output rate exactly once
    assert call_cost("gpt-5.4-mini", 1_000_000, 0, 1_000_000) == pytest.approx(0.75 + 4.50)


def test_dated_snapshots_use_the_base_price_and_unknown_models_fail() -> None:
    assert price_for("gpt-5.6-luna-2026-08-01") == PRICES["gpt-5.6-luna"]
    with pytest.raises(UnknownModelPriceError):
        price_for("gpt-9-ultra")
    with pytest.raises(UnknownModelPriceError):
        price_for("gpt-5.6-lunatic")


def test_startup_refuses_a_model_without_a_price(monkeypatch: pytest.MonkeyPatch) -> None:
    validate_configured_models()
    monkeypatch.setattr(settings, "assistant_model", "brand-new-model")
    with pytest.raises(UnknownModelPriceError, match="ASSISTANT_MODEL"):
        validate_configured_models()


def test_eval_script_shares_the_price_table() -> None:
    from app.scripts import eval_assistant

    assert not hasattr(eval_assistant, "PRICES")
    assert eval_assistant.call_cost is call_cost


# --- every call site records ----------------------------------------------------------------


def test_assistant_call_is_logged_through_the_real_responses_client(client, engine, users, monkeypatch) -> None:
    me, _ = users
    body = {
        "id": "resp_1",
        "status": "completed",
        "model": "gpt-5.6-luna-2026-08-01",
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hi!"}]}],
        "usage": {
            "input_tokens": 1_000,
            "input_tokens_details": {"cached_tokens": 400},
            "output_tokens": 50,
            "output_tokens_details": {"reasoning_tokens": 20},
        },
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    monkeypatch.setattr(agent, "ResponsesClient", lambda: ResponsesClient(httpx.Client(transport=transport)))

    response = client.post("/assistant/chat", json={"message": "hello"}, headers=_headers(me))

    assert response.status_code == 200
    [row] = _rows(engine)
    assert (row.user_id, row.feature, row.model) == (me, "assistant", "gpt-5.6-luna")
    assert (row.input_tokens, row.cached_tokens, row.output_tokens, row.reasoning_tokens) == (1_000, 400, 50, 20)
    assert row.cost_usd == pytest.approx(call_cost("gpt-5.6-luna", 1_000, 400, 50))


def test_checkin_call_is_logged(client, engine, users, monkeypatch) -> None:
    me, _ = users
    usage = {"prompt_tokens": 800, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens": 30,
             "completion_tokens_details": {"reasoning_tokens": 10}}
    _patch_chat_http(monkeypatch, "Good job today.", usage)

    response = client.post("/daily-actual-logs/checkin", json={"user_id": me, "utterance": "I was tired today"})

    assert response.status_code == 200
    [row] = _rows(engine)
    assert (row.user_id, row.feature, row.input_tokens, row.output_tokens, row.reasoning_tokens) == (me, "checkin", 800, 30, 10)
    assert row.cost_usd == pytest.approx(call_cost("gpt-5.6-luna", 800, 0, 30))


def test_compliance_feedback_call_is_logged_and_the_category_shortcut_is_not(client, engine, users, monkeypatch) -> None:
    me, _ = users
    usage = {"prompt_tokens": 500, "prompt_tokens_details": {"cached_tokens": 100}, "completion_tokens": 40}
    _patch_chat_http(monkeypatch, "That happens.", usage)

    shortcut = client.post("/compliance-reports", json={"event_instance_id": _missed_instance(engine, me), "reason_category": "overslept"})
    with_text = client.post(
        "/compliance-reports",
        json={"event_instance_id": _missed_instance(engine, me), "reason_category": "other", "reason_text": "The bus never came"},
    )

    assert shortcut.status_code == with_text.status_code == 201
    [row] = _rows(engine)
    assert (row.user_id, row.feature, row.input_tokens, row.cached_tokens, row.output_tokens) == (me, "compliance", 500, 100, 40)


def test_usage_without_a_usage_block_is_still_logged_as_a_call(client, engine, users, monkeypatch) -> None:
    me, _ = users
    _patch_chat_http(monkeypatch, "ok", usage=None)
    client.post("/daily-actual-logs/checkin", json={"user_id": me, "utterance": "I was tired today"})
    [row] = _rows(engine)
    assert (row.feature, row.input_tokens, row.cost_usd) == ("checkin", 0, 0.0)


def test_a_billed_but_malformed_answer_is_logged_even_though_the_request_fails(client, engine, users, monkeypatch) -> None:
    me, _ = users

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 300, "completion_tokens": 5}})

    monkeypatch.setattr(llm_client.httpx, "Client", lambda *a, **kw: _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler)))
    response = client.post("/daily-actual-logs/checkin", json={"user_id": me, "utterance": "I was tired today"})

    assert response.status_code == 422
    [row] = _rows(engine)
    assert (row.feature, row.input_tokens) == ("checkin", 300)


def test_a_call_outside_a_usage_scope_is_refused() -> None:
    with pytest.raises(llm_usage.MissingUsageScopeError):
        llm_client.post_chat_completion({"model": "gpt-5.6-luna", "messages": []}, _REAL_HTTPX_CLIENT())


def test_every_llm_entry_point_in_app_is_a_known_call_site() -> None:
    """New call sites must be wrapped in usage_scope (otherwise MissingUsageScopeError) and added to this list."""
    entry = re.compile(r"(?<!def )\b(post_chat_completion|post_responses|_call_chat_completion|generate_compliance_feedback|"
                       r"generate_daily_checkin_reply|ResponsesClient)\(")
    found = set()
    for path in (ROOT / "app").rglob("*.py"):
        for name in entry.findall(path.read_text(encoding="utf-8")):
            found.add((path.relative_to(ROOT).as_posix(), name))
    assert found == {
        ("app/services/llm_client.py", "post_chat_completion"),  # _call_chat_completion
        ("app/services/llm_client.py", "_call_chat_completion"),  # the two generate_* helpers
        ("app/services/llm_client.py", "post_responses"),  # ResponsesClient.create
        ("app/services/assistant/agent.py", "ResponsesClient"),  # usage_scope "assistant"
        ("app/services/daily_checkin.py", "generate_daily_checkin_reply"),  # usage_scope "checkin"
        ("app/services/compliance_report_service.py", "generate_compliance_feedback"),  # usage_scope "compliance"
        ("app/scripts/compare_prompt_cache.py", "post_chat_completion"),  # usage_scope "script"
        ("app/scripts/eval_assistant.py", "ResponsesClient"),  # through agent.chat
    }


# --- limits ---------------------------------------------------------------------------------


def test_user_limit_blocks_that_user_only(client, engine, users, script) -> None:
    me, other = users
    _spend(engine, me, 1.0)
    script(say("Hi"))

    blocked = client.post("/assistant/chat", json={"message": "hello"}, headers=_headers(me))
    allowed = client.post("/assistant/chat", json={"message": "hello"}, headers=_headers(other))

    assert blocked.status_code == 429
    body = blocked.json()
    assert body["reason"] == "user_limit"
    assert body["limit_usd"] == 1.0
    assert body["resets_at"] == "2026-10-02T00:00:00-04:00"
    assert body["detail"] == "오늘 AI 사용 한도에 도달했어요. 자정(토론토 시간)에 다시 열려요."
    assert allowed.status_code == 200
    with Session(engine) as session:
        # the refused turn left nothing behind
        assert session.scalar(select(func.count()).select_from(AssistantMessage).where(AssistantMessage.role == "user")) == 1


def test_just_under_the_limit_is_still_allowed(client, engine, users, script) -> None:
    me, _ = users
    _spend(engine, me, 0.999)
    script(say("Hi"))
    assert client.post("/assistant/chat", json={"message": "hello"}, headers=_headers(me)).status_code == 200


def test_total_limit_blocks_everyone(client, engine, users, monkeypatch) -> None:
    me, other = users
    _spend(engine, None, 5.0, feature="script")
    _patch_chat_http(monkeypatch, "never sent")

    for user_id in (me, other):
        response = client.post("/daily-actual-logs/checkin", json={"user_id": user_id, "utterance": "I was tired today"})
        assert response.status_code == 429
        assert response.json()["reason"] == "total_limit"
    assert client.post("/assistant/chat", json={"message": "hi"}, headers=_headers(other)).json()["detail"].startswith(
        "The app has reached today's overall AI usage limit"
    )
    assert len(_rows(engine)) == 1


def test_admin_uses_the_admin_limit_only_when_it_is_set(client, engine, users, script, monkeypatch) -> None:
    me, _ = users
    with Session(engine) as session:
        session.get(User, me).is_admin = True
        session.commit()
    _spend(engine, me, 1.5)
    script(say("Hi"))

    assert client.post("/assistant/chat", json={"message": "hi"}, headers=_headers(me)).status_code == 429
    monkeypatch.setattr(settings, "llm_daily_budget_admin_usd", 3.0)
    assert client.get("/usage/today", headers=_headers(me)).json()["limit_usd"] == 3.0
    assert client.post("/assistant/chat", json={"message": "hi"}, headers=_headers(me)).status_code == 200


def test_limit_resets_at_app_timezone_midnight(client, engine, users, script) -> None:
    me, _ = users
    with freeze_time("2026-10-02 03:59:00"):  # Toronto 23:59 on Oct 1
        _spend(engine, me, 1.0)
        script(say("Hi"))
        assert client.post("/assistant/chat", json={"message": "hi"}, headers=_headers(me)).status_code == 429
    with freeze_time("2026-10-02 04:00:00"):  # Toronto 00:00 on Oct 2
        script(say("Hi"))
        assert client.post("/assistant/chat", json={"message": "hi"}, headers=_headers(me)).status_code == 200
        usage = client.get("/usage/today", headers=_headers(me)).json()
        assert usage["resets_at"] == "2026-10-03T00:00:00-04:00"
        assert usage["spent_usd"] < 0.01


def test_usage_today_reports_spend_limit_and_total_block(client, engine, users) -> None:
    me, other = users
    _spend(engine, me, 0.12)
    _spend(engine, other, 0.30)
    _spend(engine, me, 9.0, when=datetime(2026, 9, 30, 12, 0))  # yesterday

    body = client.get("/usage/today", headers=_headers(me)).json()
    assert body == {
        "spent_usd": 0.12,
        "limit_usd": 1.0,
        "total_blocked": False,
        "resets_at": "2026-10-02T00:00:00-04:00",
        "timezone": "America/Toronto",
    }
    _spend(engine, other, 4.58)
    assert client.get("/usage/today", headers=_headers(me)).json()["total_blocked"] is True


# --- assistant turn ---------------------------------------------------------------------------


def test_reaching_the_limit_mid_turn_shows_the_draft_and_stops(client, engine, users, script) -> None:
    me, _ = users
    _spend(engine, me, 0.5)
    # the first call costs ~$0.60, so the second one must not be made
    fake = script(call("propose_create_event", **STUDY), say("never reached"), usage=ResponsesUsage(input_tokens=100, output_tokens=500_000))

    response = client.post("/assistant/chat", json={"message": "add study Oct 2 11-1"}, headers=_headers(me))

    assert response.status_code == 200
    body = response.json()
    assert len(fake.requests) == 1
    assert body["proposal"]["items"][0]["title"] == "Study"
    assert body["reply"] == "오늘 AI 사용 한도에 도달해서 지금까지 만든 초안을 보여드려요. 확인해 주세요."
    with Session(engine) as session:
        assert session.scalar(select(AssistantTurnLog.stop_reason)) == "budget_limit"
    assert len(_rows(engine)) == 2  # the seeded spend and the one call


def test_reaching_the_limit_mid_turn_without_a_draft_explains_why(client, engine, users, script) -> None:
    me, _ = users
    _spend(engine, me, 0.5)
    script(call("search_events", query="quiz"), say("never reached"), usage=ResponsesUsage(input_tokens=100, output_tokens=500_000))

    body = client.post("/assistant/chat", json={"message": "move the quiz"}, headers=_headers(me)).json()

    assert body["proposal"] is None
    assert body["reply"] == "오늘 AI 사용 한도에 도달해서 여기서 멈췄어요. 자정(토론토 시간)에 다시 열려요."


# --- features that don't need the LLM -----------------------------------------------------------


def test_llm_free_features_keep_working_at_the_limit(client, engine, users, script) -> None:
    me, other = users
    script(call("propose_create_event", **STUDY), say("OK?"))
    chat = client.post("/assistant/chat", json={"message": "add study"}, headers=_headers(me)).json()
    script(call("propose_create_event", **{**STUDY, "title": "Gym"}), say("OK?"))
    second = client.post("/assistant/chat", json={"message": "add gym"}, headers=_headers(other)).json()
    _spend(engine, None, 5.0, feature="script")  # everyone is now blocked

    assert client.post("/assistant/chat", json={"message": "hi"}, headers=_headers(me)).status_code == 429

    confirmed = client.post("/assistant/confirm", json={"session_id": chat["session_id"], "token": chat["proposal"]["token"]}, headers=_headers(me))
    assert confirmed.status_code == 200
    action_id = confirmed.json()["executed"][0]["action_id"]
    assert client.post(f"/actions/{action_id}/undo", headers=_headers(me)).status_code == 200
    cancelled = client.post("/assistant/cancel", json={"session_id": second["session_id"], "token": second["proposal"]["token"]}, headers=_headers(other))
    assert cancelled.status_code == 200

    task = client.post("/tasks", json={"title": "Essay", "end_time": "2026-10-03T23:59:00", "importance": 3}, headers=_headers(me))
    assert task.status_code == 201
    assert client.get("/tasks", headers=_headers(me)).status_code == 200
    assert client.put(f"/tasks/{task.json()['event_instance_id']}/complete", headers=_headers(me)).status_code == 200
    calendar = client.get("/event-instances", params={"start": "2026-10-01", "end": "2026-10-07"}, headers=_headers(me))
    assert calendar.status_code == 200
    event = client.post("/events", json={"user_id": me, "title": "Lab", "start_time": "2026-10-05T09:00:00", "end_time": "2026-10-05T12:00:00"})
    assert event.status_code == 201

    shortcut = client.post("/compliance-reports", json={"event_instance_id": _missed_instance(engine, me), "reason_category": "overslept"})
    assert shortcut.status_code == 201
    assert shortcut.json()["llm_triggered"] is False
    with_text = client.post(
        "/compliance-reports",
        json={"event_instance_id": _missed_instance(engine, me), "reason_category": "other", "reason_text": "bus"},
    )
    assert with_text.status_code == 429


def test_notifications_are_sent_at_the_limit(engine, users, monkeypatch) -> None:
    me, _ = users
    _spend(engine, None, 5.0, feature="script")
    instance_id = _missed_instance(engine, me)
    with Session(engine) as session:
        session.get(EventInstance, instance_id).status = EventInstanceStatus.PENDING
        session.commit()
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(notification, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(notification, "send_push_notification", lambda token, title, body: sent.append((token, title, body)))
    monkeypatch.setattr(User, "fcm_token", "device-1", raising=False)

    notification._send_notification(instance_id, "start")

    assert sent and sent[0][0] == "device-1"


# --- bookkeeping ---------------------------------------------------------------------------------


def test_rows_survive_when_the_request_fails_after_a_paid_call(engine, users) -> None:
    me, _ = users
    with Session(engine) as db:
        with pytest.raises(RuntimeError):
            with llm_usage.usage_scope(db, me, "checkin"):
                llm_usage.record_llm_call("gpt-5.6-luna", 100, 0, 10, 0)
                raise RuntimeError("crash after the call")
    assert [row.feature for row in _rows(engine)] == ["checkin"]


def test_usage_report_groups_by_day_user_and_feature(engine, users, monkeypatch, capsys) -> None:
    me, other = users
    _spend(engine, me, 0.10)
    _spend(engine, me, 0.05)
    _spend(engine, me, 0.20, feature="checkin")
    _spend(engine, other, 0.30)
    _spend(engine, None, 0.01, feature="script")
    _spend(engine, me, 0.40, when=datetime(2026, 9, 30, 3, 30))  # Toronto Sep 29 23:30
    _spend(engine, me, 0.50, when=datetime(2026, 9, 20, 12, 0))  # outside 7 days
    monkeypatch.setattr(usage_report, "SessionLocal", sessionmaker(bind=engine))

    assert usage_report.main(["--days", "7"]) == 0

    out = capsys.readouterr().out
    lines = [line.split() for line in out.splitlines()]
    assert ["2026-10-01", str(me), "June", "assistant", "2", "0", "0", "0", "0.1500"] in lines
    assert ["2026-10-01", str(me), "June", "checkin", "1", "0", "0", "0", "0.2000"] in lines
    assert ["2026-10-01", "(script)", "script", "1", "0", "0", "0", "0.0100"] in lines
    assert ["2026-09-29", str(me), "June", "assistant", "1", "0", "0", "0", "0.4000"] in lines
    assert "0.5000" not in out
    assert "Total over 7 day(s): 1.0600 USD" in out
