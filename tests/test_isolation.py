"""Signed in as user A, nothing of user B can be read, changed, deleted or undone — for every kind of resource.

Someone else's id answers 404 (the same as an id that doesn't exist), and B's rows are untouched afterwards.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.models import (
    ActionHistory,
    AssistantSession,
    Base,
    ComplianceReport,
    DailyActualLog,
    Event,
    EventInstance,
    EventInstanceStatus,
    ImportantDateRange,
    LlmUsageLog,
    Location,
    PendingProposal,
    Persona,
    PersonaConversation,
    PointsLedger,
    SleepLog,
    User,
)
from tests.assistant_flow import create, propose
from tests.auth_helpers import as_user

TABLES = (Event, EventInstance, ImportantDateRange, Location, SleepLog, DailyActualLog, ComplianceReport, AssistantSession,
          PendingProposal, ActionHistory, PersonaConversation, PointsLedger, LlmUsageLog)


@pytest.fixture(autouse=True)
def frozen():
    with freeze_time("2026-10-01 16:00:00"):  # Toronto 12:00
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
        a, b = User(name="A", preferred_language="en"), User(name="B", preferred_language="en")
        session.add_all([a, b, Persona(name="Hana", display_name={"ko": "하나", "en": "Hana"}, description={"ko": "설명", "en": "desc"})])
        session.commit()
        return a.id, b.id


@pytest.fixture
def b_data(client: TestClient, engine, users, monkeypatch: pytest.MonkeyPatch) -> dict[str, int | str]:
    """Everything B can own, made through the API as B where possible."""
    _, b = users
    hb = as_user(b)
    ids: dict[str, int | str] = {}
    ids["range"] = client.post("/date-ranges", json={"name": "Fall", "start_date": "2026-09-01", "end_date": "2026-12-20"}, headers=hb).json()["id"]
    ids["location"] = client.post("/locations", json={"name": "Lab", "default_travel_minutes": 10}, headers=hb).json()["id"]
    ids["event"] = client.post(
        "/events", json={"title": "B's lecture", "start_time": "2026-10-02T09:00:00", "end_time": "2026-10-02T10:00:00"}, headers=hb
    ).json()["id"]
    [instance] = client.get("/event-instances", params={"start": "2026-10-02", "end": "2026-10-02"}, headers=hb).json()
    ids["instance"] = instance["event_instance_id"]
    ids["task"] = client.post("/tasks", json={"title": "B's essay", "end_time": "2026-10-03T23:59:00"}, headers=hb).json()["event_instance_id"]
    ids["sleep"] = client.post(
        "/sleep-logs", json={"date": "2026-10-01", "actual_bedtime": "2026-09-30T23:00:00", "actual_wake_time": "2026-10-01T07:00:00"}, headers=hb
    ).json()["id"]
    ids["daily"] = client.post("/daily-actual-logs", json={"date": "2026-09-30", "summary_text": "B's day"}, headers=hb).json()["id"]
    with Session(engine) as session:
        missed = Event(user_id=b, title="B's gym", start_time=datetime(2026, 9, 30, 7), end_time=datetime(2026, 9, 30, 8))
        session.add(missed)
        session.flush()
        missed_instance = EventInstance(event_id=missed.id, date=date(2026, 9, 30), status=EventInstanceStatus.MISSED)
        session.add_all([missed_instance, PointsLedger(user_id=b, date=date(2026, 9, 30), base_points=5, points_earned=5)])
        session.add(LlmUsageLog(user_id=b, feature="assistant", model="gpt-5.6-luna", input_tokens=1, cached_tokens=0,
                                output_tokens=1, reasoning_tokens=0, cost_usd=0.5, created_at=datetime(2026, 10, 1, 15)))
        session.commit()
        ids["missed_instance"] = missed_instance.id
    assert client.post(
        "/compliance-reports", json={"event_instance_id": ids["missed_instance"], "reason_category": "overslept"}, headers=hb
    ).status_code == 201
    client.put("/users/me/persona", json={"persona_name": "Hana"}, headers=hb)
    ids["conversation"] = client.post("/personas/Hana/conversations", headers=hb).json()["id"]
    chat = propose(client, monkeypatch, b, create(title="B's study", date="2026-10-02", start_time="13:00", end_time="14:00"))
    ids["session"], ids["token"] = chat["session_id"], chat["proposal"]["token"]
    deleted = client.post("/events", json={"title": "B's old", "start_time": "2026-10-05T09:00:00", "end_time": "2026-10-05T10:00:00"}, headers=hb)
    ids["action"] = int(client.delete(f"/events/{deleted.json()['id']}", headers=hb).headers["X-Action-Id"])
    return ids


def _snapshot(engine) -> dict[str, int]:
    with Session(engine) as session:
        counts = {model.__tablename__: session.scalar(select(func.count()).select_from(model)) for model in TABLES}
        counts["b_event_title"] = session.scalar(select(Event.title).where(Event.title == "B's lecture")) is not None
        counts["pending"] = session.scalar(select(func.count()).select_from(PendingProposal).where(PendingProposal.status == "pending"))
        counts["undone"] = session.scalar(select(func.count()).select_from(ActionHistory).where(ActionHistory.undone_at.is_not(None)))
        return counts


def _requests(ids: dict[str, int | str]) -> list[tuple[str, str, dict]]:
    """(method, path, kwargs) of every attempt A makes on B's things."""
    return [
        ("GET", f"/events/{ids['event']}", {}),
        ("PUT", f"/events/{ids['event']}", {"json": {"title": "hacked"}}),
        ("DELETE", f"/events/{ids['event']}", {}),
        ("PUT", f"/event-instances/{ids['instance']}/complete", {}),
        ("DELETE", f"/event-instances/{ids['instance']}", {}),
        ("PUT", f"/tasks/{ids['task']}/complete", {}),
        ("GET", f"/date-ranges/{ids['range']}", {}),
        ("PUT", f"/date-ranges/{ids['range']}", {"json": {"name": "hacked"}}),
        ("DELETE", f"/date-ranges/{ids['range']}", {"params": {"mode": "with_events"}}),
        ("GET", f"/locations/{ids['location']}", {}),
        ("PUT", f"/locations/{ids['location']}", {"json": {"name": "hacked"}}),
        ("DELETE", f"/locations/{ids['location']}", {}),
        ("GET", f"/sleep-logs/{ids['sleep']}", {}),
        ("PUT", f"/sleep-logs/{ids['sleep']}", {"json": {"date": "2026-01-01"}}),
        ("DELETE", f"/sleep-logs/{ids['sleep']}", {}),
        ("GET", f"/daily-actual-logs/{ids['daily']}", {}),
        ("PUT", f"/daily-actual-logs/{ids['daily']}", {"json": {"summary_text": "hacked"}}),
        ("DELETE", f"/daily-actual-logs/{ids['daily']}", {}),
        ("POST", "/compliance-reports", {"json": {"event_instance_id": ids["missed_instance"], "reason_category": "forgot"}}),
        ("POST", "/assistant/confirm", {"json": {"session_id": ids["session"], "token": ids["token"]}}),
        ("POST", "/assistant/cancel", {"json": {"session_id": ids["session"], "token": ids["token"]}}),
        ("POST", "/assistant/chat", {"json": {"message": "hi", "session_id": ids["session"]}}),
        ("POST", f"/actions/{ids['action']}/undo", {}),
        ("GET", f"/users/me/persona-conversations/{ids['conversation']}", {}),
        ("POST", "/daily-actual-logs/checkin", {"json": {"utterance": "hello there", "conversation_id": ids["conversation"]}}),
        ("POST", "/events", {"json": {"title": "x", "start_time": "2026-10-06T09:00:00", "end_time": "2026-10-06T10:00:00", "location_id": ids["location"]}}),
        ("POST", "/events", {"json": {"title": "x", "end_time": "2026-10-06T10:00:00", "event_type": "deadline", "is_recurring": True,
                                      "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU", "date_range_id": ids["range"]}}),
        ("POST", "/tasks", {"json": {"title": "x", "end_time": "2026-10-06T10:00:00", "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU", "date_range_id": ids["range"]}}),
    ]


def test_a_cannot_touch_any_of_bs_resources(client: TestClient, engine, users, b_data, monkeypatch) -> None:
    a, _ = users
    before = _snapshot(engine)
    monkeypatch.setattr("app.services.assistant.agent.ResponsesClient", lambda: pytest.fail("A's refused chat must not reach the LLM"))

    answers = {}
    for method, path, kwargs in _requests(b_data):
        response = client.request(method, path, headers=as_user(a), **kwargs)
        answers[f"{method} {path} {kwargs}"] = response.status_code

    assert {key: code for key, code in answers.items() if code not in (403, 404)} == {}
    assert _snapshot(engine) == before


def test_lists_and_summaries_only_show_my_own(client: TestClient, engine, users, b_data) -> None:
    a, _ = users
    ha = as_user(a)

    for path in ["/events", "/date-ranges", "/locations", "/sleep-logs", "/daily-actual-logs", "/tasks", "/actions", "/users/me/persona-conversations"]:
        assert client.get(path, headers=ha).json() == [], path
    assert client.get("/event-instances", params={"start": "2026-09-28", "end": "2026-10-11"}, headers=ha).json() == []
    assert client.get("/compliance-reports/stats", headers=ha).json()["total"] == 0
    assert client.get("/points/summary", headers=ha).json()["total_points"] == 0
    assert client.get("/usage/today", headers=ha).json()["spent_usd"] == 0
    assert client.get("/assistant/sessions/current", headers=ha).json()["session_id"] is None
    assert client.get("/personas/Hana/conversations/current", headers=ha).json() is None


def test_b_still_sees_everything(client: TestClient, users, b_data) -> None:
    _, b = users
    hb = as_user(b)
    assert client.get(f"/events/{b_data['event']}", headers=hb).json()["title"] == "B's lecture"
    assert client.get("/compliance-reports/stats", headers=hb).json()["total"] == 1
    assert client.get("/points/summary", headers=hb).json()["total_points"] == 5
    assert client.get("/assistant/sessions/current", headers=hb).json()["proposal"]["token"] == b_data["token"]
