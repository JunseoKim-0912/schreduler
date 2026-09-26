from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.models import Base, ChildEventKind, Event, EventInstance, EventInstanceStatus, ImportantDateRange, Location, User


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def client(engine):
    testing_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def user_id(engine) -> int:
    with Session(engine) as session:
        user = User(name="June", preferred_language="ko")
        session.add(user)
        session.commit()
        return user.id


def _payload(user_id: int, **overrides: object) -> dict[str, object]:
    payload = {
        "user_id": user_id,
        "title": "수업",
        "start_time": "2026-09-17T09:00:00",
        "end_time": "2026-09-17T10:00:00",
    }
    payload.update(overrides)
    return payload


def test_create_event_returns_201(client: TestClient, user_id: int) -> None:
    response = client.post("/events", json=_payload(user_id))

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "수업"
    assert body["user_id"] == user_id
    assert body["importance"] is None
    assert "id" in body


def test_create_event_with_unknown_user_returns_404(client: TestClient) -> None:
    response = client.post("/events", json=_payload(user_id=999))

    assert response.status_code == 404


def test_create_event_with_invalid_time_range_returns_422(client: TestClient, user_id: int) -> None:
    response = client.post(
        "/events",
        json=_payload(user_id, start_time="2026-09-17T10:00:00", end_time="2026-09-17T09:00:00"),
    )

    assert response.status_code == 422


def test_get_event_returns_created_event(client: TestClient, user_id: int) -> None:
    created = client.post("/events", json=_payload(user_id)).json()

    response = client.get(f"/events/{created['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_get_event_not_found_returns_404(client: TestClient) -> None:
    response = client.get("/events/999")

    assert response.status_code == 404


def test_list_events_filters_by_user(client: TestClient, engine) -> None:
    with Session(engine) as session:
        user1 = User(name="June", preferred_language="ko")
        user2 = User(name="Other", preferred_language="en")
        session.add_all([user1, user2])
        session.commit()
        user1_id, user2_id = user1.id, user2.id

    client.post("/events", json=_payload(user1_id, title="이벤트1"))
    client.post("/events", json=_payload(user2_id, title="이벤트2"))

    response = client.get("/events", params={"user_id": user1_id})

    assert response.status_code == 200
    titles = [event["title"] for event in response.json()]
    assert titles == ["이벤트1"]


def test_update_event_applies_partial_changes(client: TestClient, user_id: int) -> None:
    created = client.post("/events", json=_payload(user_id)).json()

    response = client.put(f"/events/{created['id']}", json={"title": "변경된 제목"})

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "변경된 제목"
    assert body["start_time"] == created["start_time"]  # 건드리지 않은 필드는 그대로


def test_update_event_not_found_returns_404(client: TestClient) -> None:
    response = client.put("/events/999", json={"title": "x"})

    assert response.status_code == 404


def test_delete_event_removes_it(client: TestClient, user_id: int) -> None:
    created = client.post("/events", json=_payload(user_id)).json()

    delete_response = client.delete(f"/events/{created['id']}")
    get_response = client.get(f"/events/{created['id']}")

    assert delete_response.status_code == 204
    assert get_response.status_code == 404


def test_delete_event_not_found_returns_404(client: TestClient) -> None:
    response = client.delete("/events/999")

    assert response.status_code == 404


def test_creating_recurring_event_generates_instances_within_date_range(
    client: TestClient, engine, user_id: int
) -> None:
    with Session(engine) as session:
        date_range = ImportantDateRange(
            user_id=user_id,
            name="2026 가을학기",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
        )
        session.add(date_range)
        session.commit()
        date_range_id = date_range.id

    response = client.post(
        "/events",
        json=_payload(
            user_id,
            title="월요일 수업",
            is_recurring=True,
            recurrence_rule="FREQ=WEEKLY;BYDAY=MO",  # 매주 월요일
            date_range_id=date_range_id,
        ),
    )
    assert response.status_code == 201
    event_id = response.json()["id"]

    with Session(engine) as session:
        instances = (
            session.execute(select(EventInstance).where(EventInstance.event_id == event_id))
            .scalars()
            .all()
        )

    # 2026년 9월의 월요일: 7, 14, 21, 28 -> 4개, 전부 date_range 안에 있어야 함
    assert len(instances) == 4
    assert all(instance.date.weekday() == 0 for instance in instances)
    assert all(date(2026, 9, 1) <= instance.date <= date(2026, 9, 30) for instance in instances)


def test_creating_non_recurring_event_generates_one_instance(
    client: TestClient, engine, user_id: int
) -> None:
    response = client.post("/events", json=_payload(user_id))
    event_id = response.json()["id"]

    with Session(engine) as session:
        instances = (
            session.execute(select(EventInstance).where(EventInstance.event_id == event_id))
            .scalars()
            .all()
        )

    assert [(i.date, i.status) for i in instances] == [(date(2026, 9, 17), EventInstanceStatus.PENDING)]


def test_creating_event_with_location_via_api_auto_creates_travel_child(
    client: TestClient, engine, user_id: int
) -> None:
    with Session(engine) as session:
        location = Location(user_id=user_id, name="학교", default_travel_minutes=25)
        session.add(location)
        session.commit()
        location_id = location.id

    response = client.post("/events", json=_payload(user_id, location_id=location_id))
    assert response.status_code == 201
    event_id = response.json()["id"]

    with Session(engine) as session:
        child = (
            session.execute(select(Event).where(Event.parent_event_id == event_id))
            .scalars()
            .one()
        )

        assert child.child_kind == ChildEventKind.TRAVEL
        assert child.location_id == location_id
        assert child.end_time == datetime.fromisoformat("2026-09-17T09:00:00")
        assert child.start_time == datetime.fromisoformat("2026-09-17T08:35:00")

        # 단발 부모의 child도 단발 일정이라 부모와 같은 날짜에 회차 하나
        child_instances = (
            session.execute(select(EventInstance).where(EventInstance.event_id == child.id))
            .scalars()
            .all()
        )
        assert [i.date for i in child_instances] == [date(2026, 9, 17)]


def test_creating_recurring_event_with_location_auto_creates_child_and_its_instances(
    client: TestClient, engine, user_id: int
) -> None:
    with Session(engine) as session:
        location = Location(user_id=user_id, name="학교", default_travel_minutes=25)
        date_range = ImportantDateRange(
            user_id=user_id,
            name="2026 가을학기",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
        )
        session.add_all([location, date_range])
        session.commit()
        location_id, date_range_id = location.id, date_range.id

    response = client.post(
        "/events",
        json=_payload(
            user_id,
            title="월요일 수업",
            location_id=location_id,
            is_recurring=True,
            recurrence_rule="FREQ=WEEKLY;BYDAY=MO",  # 매주 월요일
            date_range_id=date_range_id,
        ),
    )
    assert response.status_code == 201
    event_id = response.json()["id"]

    with Session(engine) as session:
        parent_instance_dates = sorted(
            session.execute(select(EventInstance.date).where(EventInstance.event_id == event_id))
            .scalars()
            .all()
        )
        assert len(parent_instance_dates) == 4  # 2026년 9월 매주 월요일: 7, 14, 21, 28

        child = (
            session.execute(select(Event).where(Event.parent_event_id == event_id))
            .scalars()
            .one()
        )
        assert child.child_kind == ChildEventKind.TRAVEL
        assert child.is_recurring is True
        assert child.recurrence_rule == "FREQ=WEEKLY;BYDAY=MO"
        assert child.date_range_id == date_range_id

        child_instance_dates = sorted(
            session.execute(select(EventInstance.date).where(EventInstance.event_id == child.id))
            .scalars()
            .all()
        )
        # child(이동시간)도 부모와 정확히 같은 날짜들에 EventInstance가 생겨야 함
        assert child_instance_dates == parent_instance_dates
