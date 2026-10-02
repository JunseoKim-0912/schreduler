"""v3.8 기존 일정의 장소 (PUT /events)와 캘린더 표시. 어시스턴트로 바꾸는 경우는 test_assistant_scenarios에 있다."""

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
from tests.auth_helpers import as_user, sign_in

PENDING, DONE, CANCELLED = EventInstanceStatus.PENDING, EventInstanceStatus.DONE, EventInstanceStatus.CANCELLED


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
        period = ImportantDateRange(user_id=user.id, name="Lecture period", start_date=date(2026, 9, 8), end_date=date(2026, 10, 31))
        bahen = Location(user_id=user.id, name="Bahen", default_travel_minutes=20)
        myhal = Location(user_id=user.id, name="Myhal", default_travel_minutes=30)
        session.add_all([period, bahen, myhal])
        session.commit()
        values = {"user": user.id, "period": period.id, "bahen": bahen.id, "myhal": myhal.id}
    sign_in(client, values["user"])
    values["lecture"] = _lecture(client, values, "MO")
    return values


def _lecture(client: TestClient, ids: dict[str, int], byday: str, title: str = "ESC360 Lecture") -> int:
    response = client.post(
        "/events",
        json={
            "title": title, "start_time": "2026-09-14T11:00:00", "end_time": "2026-09-14T12:00:00",
            "is_recurring": True, "recurrence_rule": f"FREQ=WEEKLY;BYDAY={byday}", "date_range_id": ids["period"],
        }, headers=as_user(ids["user"]),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _child(engine, event_id: int) -> Event | None:
    with Session(engine) as session:
        return session.execute(select(Event).where(Event.parent_event_id == event_id)).scalar_one_or_none()


def _instances(engine, event_id: int) -> dict[date, EventInstanceStatus]:
    with Session(engine) as session:
        rows = session.execute(select(EventInstance.date, EventInstance.status).where(EventInstance.event_id == event_id)).all()
        return {day: status for day, status in rows}





def test_put_events_location_creates_and_detaches_travel_child(client, engine, ids):
    assert client.put(f"/events/{ids['lecture']}", json={"location_id": ids["bahen"]}).status_code == 200
    child = _child(engine, ids["lecture"])
    assert child is not None and len(_instances(engine, child.id)) == len(_instances(engine, ids["lecture"]))

    assert client.put(f"/events/{ids['lecture']}", json={"location_id": None}).status_code == 200

    statuses = _instances(engine, child.id)
    assert statuses[date(2026, 9, 21)] == PENDING and statuses[date(2026, 9, 28)] == CANCELLED


def test_calendar_shows_location(client, engine, ids):
    client.put(f"/events/{ids['lecture']}", json={"location_id": ids["bahen"]})

    items = client.get("/event-instances", params={"start": "2026-09-28", "end": "2026-09-28"}, headers=as_user(ids["user"])).json()

    lecture = next(i for i in items if i["event_id"] == ids["lecture"])
    assert lecture["location_name"] == "Bahen"
