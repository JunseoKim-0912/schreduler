"""/assistant/* 엔드포인트. LLM은 tests/fake_responses 대본으로 바꾼다."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.models import Base, Event, User
from app.services.assistant import agent
from tests.fake_responses import FakeResponsesClient, call, say

STUDY = {
    "event_type": "scheduled",
    "title": "스터디",
    "date": "2026-10-02",
    "start_time": "11:00",
    "end_time": "13:00",
    "importance": 1,
    "recurrence": None,
    "date_range": None,
    "location": None,
    "inferred_fields": ["importance"],
    "draft_id": None,
}


@pytest.fixture(autouse=True)
def frozen():
    with freeze_time("2026-09-27 18:00:00"):  # UTC → 토론토 14:00 (일)
        yield


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
    def use(*steps) -> FakeResponsesClient:
        fake = FakeResponsesClient(list(steps))
        monkeypatch.setattr(agent, "ResponsesClient", lambda: fake)
        return fake

    return use


def _events(engine) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(Event))


def test_chat_returns_a_proposal_and_confirm_executes_with_undoable_action(client, engine, users, script) -> None:
    me, _ = users
    script(call("propose_create_event", **STUDY), say("이렇게 만들까요?"))
    response = client.post("/assistant/chat", json={"message": "10월 2일 11:00-1:00 스터디 추가해줘"}, headers={"X-User-Id": str(me)})

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "이렇게 만들까요?"
    assert body["executed"] == []
    card = body["proposal"]["items"][0]
    assert card["time_display"] == "오전 11:00 – 오후 1:00 (2시간)"
    assert card["inferred_fields"] == ["importance"]
    assert _events(engine) == 0

    confirm = client.post(
        "/assistant/confirm", json={"session_id": body["session_id"], "token": body["proposal"]["token"]}, headers={"X-User-Id": str(me)}
    )
    assert confirm.status_code == 200
    action_id = confirm.json()["executed"][0]["action_id"]
    assert _events(engine) == 1

    again = client.post(
        "/assistant/confirm", json={"session_id": body["session_id"], "token": body["proposal"]["token"]}, headers={"X-User-Id": str(me)}
    )
    assert again.status_code == 409

    undo = client.post(f"/actions/{action_id}/undo", headers={"X-User-Id": str(me)})
    assert undo.status_code == 200
    assert _events(engine) == 0


def test_cancel_and_current_session(client, engine, users, script) -> None:
    me, other = users
    script(call("propose_create_event", **STUDY), say("이렇게 만들까요?"))
    body = client.post("/assistant/chat", json={"message": "스터디"}, headers={"X-User-Id": str(me)}).json()

    current = client.get("/assistant/sessions/current", headers={"X-User-Id": str(me)}).json()
    assert current["session_id"] == body["session_id"]
    assert [m["role"] for m in current["messages"]] == ["user", "assistant"]
    assert current["proposal"]["token"] == body["proposal"]["token"]
    assert client.get("/assistant/sessions/current", headers={"X-User-Id": str(other)}).json() == {
        "session_id": None, "messages": [], "proposal": None,
    }

    headers = {"X-User-Id": str(other)}
    token_body = {"session_id": body["session_id"], "token": body["proposal"]["token"]}
    assert client.post("/assistant/cancel", json=token_body, headers=headers).status_code == 404
    assert client.post("/assistant/chat", json={"session_id": body["session_id"], "message": "hi"}, headers=headers).status_code == 404

    cancelled = client.post("/assistant/cancel", json=token_body, headers={"X-User-Id": str(me)})
    assert cancelled.status_code == 200 and cancelled.json()["reply"] == "제안을 취소했어요."
    assert client.get("/assistant/sessions/current", headers={"X-User-Id": str(me)}).json()["proposal"] is None
    assert _events(engine) == 0


def test_new_session_and_header_required(client, users) -> None:
    me, _ = users
    created = client.post("/assistant/sessions", headers={"X-User-Id": str(me)})
    assert created.status_code == 201 and created.json()["messages"] == []
    assert client.post("/assistant/chat", json={"message": "x"}).status_code == 401


def test_expired_confirm_is_410(client, users, script) -> None:
    me, _ = users
    script(call("propose_create_event", **STUDY), say("초안"))
    body = client.post("/assistant/chat", json={"message": "스터디"}, headers={"X-User-Id": str(me)}).json()

    with freeze_time("2026-09-27 18:31:00"):
        response = client.post(
            "/assistant/confirm", json={"session_id": body["session_id"], "token": body["proposal"]["token"]}, headers={"X-User-Id": str(me)}
        )
    assert response.status_code == 410
