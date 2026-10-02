"""v3.8 반복 간격(INTERVAL)의 회차: 기간을 바꿔도 격주 리듬 유지. 초안 단계는 tests/services/assistant/test_agent.py."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.models import Base, Event, EventInstance, ImportantDateRange, Location, User
from app.models.enums import EventInstanceStatus
from app.services.recurrence import build_recurrence_rule
from tests.auth_helpers import as_user, sign_in

@pytest.fixture(autouse=True)
def frozen_today():
    with freeze_time("2026-09-26 09:00:00"):
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
def ids(engine, client) -> dict[str, int]:
    with Session(engine) as session:
        user = User(name="June", preferred_language="ko")
        session.add(user)
        session.flush()
        period = ImportantDateRange(user_id=user.id, name="Lecture period", start_date=date(2026, 9, 8), end_date=date(2026, 12, 8))
        session.add(period)
        session.commit()
        sign_in(client, user.id)
        return {"user": user.id, "period": period.id}


def _dates(engine, event_id: int) -> list[date]:
    with Session(engine) as session:
        return list(
            session.execute(
                select(EventInstance.date)
                .where(EventInstance.event_id == event_id, EventInstance.status != EventInstanceStatus.CANCELLED)
                .order_by(EventInstance.date)
            ).scalars()
        )


BIWEEKLY_FROM_922 = [date(2026, 9, 22), date(2026, 10, 6), date(2026, 10, 20), date(2026, 11, 3), date(2026, 11, 17), date(2026, 12, 1)]


def _post_event(client: TestClient, range_id: int, **extra) -> int:
    body = {
        "title": "ECE360 Lab", "start_time": "2026-09-22T09:00:00", "end_time": "2026-09-22T12:00:00",
        "is_recurring": True, "recurrence_rule": "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU", "date_range_id": range_id, **extra,
    }
    response = client.post("/events", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_extending_the_period_keeps_the_biweekly_rhythm(client, engine, ids):
    short = client.post(
        "/date-ranges", json={"name": "짧은 기간", "start_date": "2026-09-08", "end_date": "2026-10-25"}, headers=as_user(ids["user"])
    ).json()["id"]
    event_id = _post_event(client, short)
    assert _dates(engine, event_id) == BIWEEKLY_FROM_922[:3]

    client.put(f"/date-ranges/{short}", json={"start_date": "2026-09-01", "end_date": "2026-12-08"})

    assert _dates(engine, event_id) == BIWEEKLY_FROM_922, "늘린 뒤에도 10/27·11/10이 아니라 11/3·11/17 리듬, 9/22 이전 회차 없음"


def test_extending_the_period_backwards_does_not_create_past_instances(client, engine, ids):
    event_id = _post_event(client, ids["period"], start_time="2026-09-01T09:00:00", end_time="2026-09-01T12:00:00")
    before = _dates(engine, event_id)
    assert before[0] == date(2026, 9, 15), "POST /events는 기존대로 기간 안의 회차를 모두 만든다"

    client.put(f"/date-ranges/{ids['period']}", json={"start_date": "2026-09-01"})

    assert _dates(engine, event_id) == before, "기간을 늘려도 오늘(9/26) 이전 회차(9/1)는 새로 만들지 않는다"


def test_travel_child_is_biweekly_too(client, engine, ids):
    with Session(engine) as session:
        location = Location(user_id=ids["user"], name="Bahen", default_travel_minutes=20)
        session.add(location)
        session.commit()
        location_id = location.id

    event_id = _post_event(client, ids["period"], location_id=location_id)

    with Session(engine) as session:
        child = session.execute(select(Event).where(Event.parent_event_id == event_id)).scalar_one()
        assert child.recurrence_rule == "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU"
    assert _dates(engine, child.id) == BIWEEKLY_FROM_922


def test_recurring_deadline_uses_the_same_rhythm(client, engine, ids):
    task = client.post(
        "/tasks",
        json={"title": "랩 리포트", "end_time": "2026-09-22T23:59:00", "recurrence_rule": "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU",
              "date_range_id": ids["period"]},
        headers=as_user(ids["user"]),
    ).json()

    assert _dates(engine, task["event_id"]) == BIWEEKLY_FROM_922


def test_existing_weekly_event_keeps_its_instances(client, engine, ids):
    """예전 방식(기간 시작일 기준)으로 만들어진 매주 일정: 기간을 바꿔도 이미 있는 회차는 그대로 두고 매주로 이어진다."""
    event_id = _post_event(client, ids["period"], recurrence_rule="FREQ=WEEKLY;BYDAY=TU")
    with Session(engine) as session:
        for legacy in (date(2026, 9, 8), date(2026, 9, 15)):  # 예전 생성 방식이 만든 시작일 이전 회차
            session.add(EventInstance(event_id=event_id, date=legacy, status=EventInstanceStatus.PENDING))
        session.commit()
    before = _dates(engine, event_id)

    client.put(f"/date-ranges/{ids['period']}", json={"end_date": "2026-12-15"})

    after = _dates(engine, event_id)
    assert after[: len(before)] == before and after[-1] == date(2026, 12, 15)
    assert all((b - a).days == 7 for a, b in zip(after[1:], after[2:])), "매주 간격 유지"


def test_build_recurrence_rule():
    assert build_recurrence_rule("WEEKLY", ["TU"], 2) == "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU"
    assert build_recurrence_rule("WEEKLY", ["TU"], 1) == "FREQ=WEEKLY;BYDAY=TU"
    assert build_recurrence_rule("DAILY", None, None) == "FREQ=DAILY"
