"""No GET endpoint writes to the database.

SameSite=Lax still sends the session cookie on a top-level GET from another site, so a GET that changed data could be
triggered by any page the user visits. Every GET in the API is called here, with data behind it (including an expired
proposal, which GET /assistant/sessions/current used to mark as expired), and any INSERT/UPDATE/DELETE fails the test.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.models import Base, PendingProposal, Persona, User
from tests.assistant_flow import create, propose
from tests.auth_helpers import as_user

WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)


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


def _get_paths(ids: dict[str, int]) -> list[str]:
    values = {"event_id": ids["event"], "date_range_id": ids["range"], "location_id": ids["location"], "sleep_log_id": ids["sleep"],
              "daily_log_id": ids["daily"], "name": "Hana", "persona_id": "Hana", "conversation_id": ids["conversation"]}
    queries = {"/event-instances": "?start=2026-09-28&end=2026-10-11"}
    paths = []
    for path, operations in app.openapi()["paths"].items():
        if "get" in operations:
            filled = re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]), path)
            paths.append(filled + queries.get(path, ""))
    return paths


def test_no_get_endpoint_writes(client: TestClient, engine, monkeypatch: pytest.MonkeyPatch) -> None:
    with freeze_time("2026-10-01 16:00:00"):
        with Session(engine) as session:
            user = User(name="June", preferred_language="ko")
            session.add_all([user, Persona(name="Hana", display_name={"ko": "하나", "en": "Hana"}, description={"ko": "설명", "en": "desc"})])
            session.commit()
            user_id = user.id
        headers = as_user(user_id)
        ids = {
            "range": client.post("/date-ranges", json={"name": "Fall", "start_date": "2026-09-01", "end_date": "2026-12-20"}, headers=headers).json()["id"],
            "location": client.post("/locations", json={"name": "Lab", "default_travel_minutes": 10}, headers=headers).json()["id"],
            "event": client.post("/events", json={"title": "Lecture", "start_time": "2026-10-02T09:00:00", "end_time": "2026-10-02T10:00:00"}, headers=headers).json()["id"],
            "sleep": client.post("/sleep-logs", json={"date": "2026-10-01", "actual_bedtime": "2026-09-30T23:00:00", "actual_wake_time": "2026-10-01T07:00:00"}, headers=headers).json()["id"],
            "daily": client.post("/daily-actual-logs", json={"date": "2026-09-30", "summary_text": "day"}, headers=headers).json()["id"],
        }
        client.put("/users/me/persona", json={"persona_name": "Hana"}, headers=headers)
        ids["conversation"] = client.post("/personas/Hana/conversations", headers=headers).json()["id"]
        propose(client, monkeypatch, user_id, create(title="Study", date="2026-10-02", start_time="13:00", end_time="14:00"))

    paths = _get_paths(ids)  # outside freeze_time: the OpenAPI schema can't be built from frozen datetimes
    writes: list[str] = []

    def record(conn, cursor, statement, *args) -> None:
        if WRITE.match(statement):
            writes.append(statement.split("\n")[0])

    # an hour later the proposal (30-minute lifetime) has expired
    with freeze_time("2026-10-01 17:00:00"):
        event.listen(engine, "before_cursor_execute", record)
        try:
            statuses = {path: client.get(path, headers=headers).status_code for path in paths}
        finally:
            event.remove(engine, "before_cursor_execute", record)

    assert {path: code for path, code in statuses.items() if code != 200} == {}
    assert writes == []
    assert statuses["/assistant/sessions/current"] == 200
    with Session(engine) as session:
        assert session.scalar(select(PendingProposal.status)) == "pending"  # left for the next chat turn to expire


def test_the_session_view_hides_an_expired_proposal_without_saving(client: TestClient, engine, monkeypatch: pytest.MonkeyPatch) -> None:
    with Session(engine) as session:
        user = User(name="June", preferred_language="ko")
        session.add(user)
        session.commit()
        user_id = user.id
    with freeze_time("2026-10-01 16:00:00"):
        propose(client, monkeypatch, user_id, create(title="Study", date="2026-10-02", start_time="13:00", end_time="14:00"))
        assert client.get("/assistant/sessions/current", headers=as_user(user_id)).json()["proposal"] is not None
    with freeze_time("2026-10-01 16:31:00"):
        assert client.get("/assistant/sessions/current", headers=as_user(user_id)).json()["proposal"] is None
