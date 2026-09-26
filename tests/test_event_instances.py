"""캘린더용 일정 회차 API: GET /event-instances, PUT /event-instances/{id}/complete, DELETE /event-instances/{id}."""

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.models import ActionHistory, Base, Event, EventInstance, User
from app.models.enums import EventInstanceStatus


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


def _user(engine, name: str) -> int:
    with Session(engine) as session:
        user = User(name=name, preferred_language="ko")
        session.add(user)
        session.commit()
        return user.id


@pytest.fixture
def user_id(engine) -> int:
    return _user(engine, "June")


def _headers(user_id: int) -> dict[str, str]:
    return {"X-User-Id": str(user_id)}


def _weekly(client: TestClient, user_id: int, title: str = "물리 퀴즈", byday: str = "SA") -> int:
    date_range = client.post(
        "/date-ranges",
        json={"user_id": user_id, "name": "가을학기", "start_date": "2026-09-01", "end_date": "2026-10-31"},
    ).json()["id"]
    response = client.post(
        "/events",
        json={
            "user_id": user_id,
            "title": title,
            "start_time": "2026-09-05T17:00:00",
            "end_time": "2026-09-05T18:30:00",
            "importance": 4,
            "is_recurring": True,
            "recurrence_rule": f"FREQ=WEEKLY;BYDAY={byday}",
            "date_range_id": date_range,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def _one_off(client: TestClient, user_id: int, title: str, start: str, end: str) -> int:
    response = client.post("/events", json={"user_id": user_id, "title": title, "start_time": start, "end_time": end})
    assert response.status_code == 201
    return response.json()["id"]


def _instance_id(engine, event_id: int, day: date) -> int:
    with Session(engine) as session:
        return session.execute(
            select(EventInstance.id).where(EventInstance.event_id == event_id, EventInstance.date == day)
        ).scalar_one()


def _week(client: TestClient, user_id: int, start: str = "2026-09-21", end: str = "2026-09-27"):
    return client.get("/event-instances", params={"start": start, "end": end}, headers=_headers(user_id))


def test_lists_instances_in_range_with_event_info_in_time_order(client, engine, user_id):
    quiz = _weekly(client, user_id)
    lecture = _weekly(client, user_id, "물리 강의", "MO")
    task = client.post(
        "/tasks", json={"title": "과제 제출", "end_time": "2026-09-24T23:59:00", "importance": 6}, headers=_headers(user_id)
    ).json()

    response = _week(client, user_id)

    assert response.status_code == 200
    items = response.json()
    assert [(i["title"], i["date"]) for i in items] == [
        ("물리 강의", "2026-09-21"),
        ("과제 제출", "2026-09-24"),
        ("물리 퀴즈", "2026-09-26"),
    ]
    lecture_item, task_item, quiz_item = items
    assert lecture_item["event_id"] == lecture and quiz_item["event_id"] == quiz
    assert quiz_item == {
        "event_instance_id": _instance_id(engine, quiz, date(2026, 9, 26)),
        "event_id": quiz,
        "date": "2026-09-26",
        "status": "pending",
        "completion_method": None,
        "title": "물리 퀴즈",
        "event_type": "scheduled",
        "importance": 4,
        "is_recurring": True,
        "recurrence_rule": "FREQ=WEEKLY;BYDAY=SA",
        "parent_event_id": None,
        "child_kind": None,
        "start_time": "2026-09-26T17:00:00",
        "end_time": "2026-09-26T18:30:00",
        "time_overridden": False,
    }
    assert task_item["event_type"] == "deadline"
    assert task_item["start_time"] is None and task_item["end_time"] == "2026-09-24T23:59:00"
    assert task_item["event_instance_id"] == task["event_instance_id"]


def test_range_edges_are_inclusive(client, engine, user_id):
    _weekly(client, user_id)

    assert [i["date"] for i in _week(client, user_id, "2026-09-26", "2026-09-26").json()] == ["2026-09-26"]
    assert _week(client, user_id, "2026-09-27", "2026-10-02").json() == []


def test_only_the_current_users_instances(client, engine, user_id):
    _weekly(client, user_id)
    other = _user(engine, "Other")
    _weekly(client, other, "남의 일정")

    titles = {i["title"] for i in _week(client, user_id).json()}

    assert titles == {"물리 퀴즈"}


def test_cancelled_instances_are_excluded(client, engine, user_id):
    quiz = _weekly(client, user_id)
    instance_id = _instance_id(engine, quiz, date(2026, 9, 26))
    with Session(engine) as session:
        session.get(EventInstance, instance_id).status = EventInstanceStatus.CANCELLED
        session.commit()

    assert _week(client, user_id).json() == []
    assert len(_week(client, user_id, "2026-09-28", "2026-10-04").json()) == 1


def test_instance_time_override_is_reflected(client, engine, user_id):
    quiz = _weekly(client, user_id)
    with Session(engine) as session:
        instance = session.get(EventInstance, _instance_id(engine, quiz, date(2026, 9, 26)))
        instance.start_time_override = datetime(2026, 9, 26, 18, 0)
        instance.end_time_override = datetime(2026, 9, 26, 19, 30)
        session.commit()

    [item] = _week(client, user_id).json()
    [next_week] = _week(client, user_id, "2026-10-03", "2026-10-03").json()

    assert (item["start_time"], item["end_time"], item["time_overridden"]) == ("2026-09-26T18:00:00", "2026-09-26T19:30:00", True)
    assert (next_week["start_time"], next_week["time_overridden"]) == ("2026-10-03T17:00:00", False)


def test_one_off_events_have_their_single_instance(client, engine, user_id):
    event_id = _one_off(client, user_id, "치과", "2026-09-23T10:00:00", "2026-09-23T11:00:00")
    _one_off(client, user_id, "다음 주 치과", "2026-09-30T10:00:00", "2026-09-30T11:00:00")

    [item] = _week(client, user_id).json()

    assert item["event_id"] == event_id
    assert item["event_instance_id"] == _instance_id(engine, event_id, date(2026, 9, 23))
    assert (item["date"], item["status"], item["start_time"]) == ("2026-09-23", "pending", "2026-09-23T10:00:00")


def test_legacy_one_off_events_without_instances_are_still_included(client, engine, user_id):
    """회차 생성 이전에 만들어져 백필하지 않은(지난 날짜) 단발 일정도 캘린더에는 보인다."""
    with Session(engine) as session:
        event = Event(user_id=user_id, title="옛 치과", start_time=datetime(2026, 9, 22, 10), end_time=datetime(2026, 9, 22, 11))
        session.add(event)
        session.commit()
        event_id = event.id

    [item] = _week(client, user_id).json()

    assert (item["event_id"], item["event_instance_id"], item["date"]) == (event_id, None, "2026-09-22")


@pytest.mark.parametrize(
    ("start", "end"),
    [("2026-09-27", "2026-09-21"), ("2026-09-01", "2026-11-30"), ("2026-9-1", "2026-09-07")],
)
def test_invalid_ranges_are_rejected(client, user_id, start, end):
    assert _week(client, user_id, start, end).status_code == 422


def test_requires_user_header(client):
    assert client.get("/event-instances", params={"start": "2026-09-21", "end": "2026-09-27"}).status_code == 401


def test_complete_scheduled_instance(client, engine, user_id):
    quiz = _weekly(client, user_id)
    instance_id = _instance_id(engine, quiz, date(2026, 9, 19))

    response = client.put(f"/event-instances/{instance_id}/complete", headers=_headers(user_id))

    assert response.status_code == 200
    assert (response.json()["status"], response.json()["completion_method"]) == ("done", "manual")
    assert client.put(f"/event-instances/{instance_id}/complete", headers=_headers(user_id)).status_code == 200
    points = client.get("/points/summary", headers=_headers(user_id)).json()
    assert points["total_points"] > 0, "지난 회차 완료는 포인트 원장에 바로 반영된다"


def test_complete_rejects_other_users_and_cancelled_instances(client, engine, user_id):
    quiz = _weekly(client, user_id)
    instance_id = _instance_id(engine, quiz, date(2026, 9, 26))
    other = _user(engine, "Other")

    assert client.put(f"/event-instances/{instance_id}/complete", headers=_headers(other)).status_code == 404
    assert client.put("/event-instances/9999/complete", headers=_headers(user_id)).status_code == 404

    assert client.delete(f"/event-instances/{instance_id}", headers=_headers(user_id)).status_code == 204
    assert client.put(f"/event-instances/{instance_id}/complete", headers=_headers(user_id)).status_code == 409


def test_delete_one_instance_records_undoable_action(client, engine, user_id):
    quiz = _weekly(client, user_id)
    instance_id = _instance_id(engine, quiz, date(2026, 9, 26))

    response = client.delete(f"/event-instances/{instance_id}", headers=_headers(user_id))

    assert response.status_code == 204
    assert _week(client, user_id).json() == []
    assert len(_week(client, user_id, "2026-10-03", "2026-10-03").json()) == 1, "다른 회차는 그대로"
    with Session(engine) as session:
        action = session.get(ActionHistory, int(response.headers["X-Action-Id"]))
        assert (action.source.value, action.action_type.value) == ("ui", "delete")

    assert client.post(f"/actions/{action.id}/undo", headers=_headers(user_id)).status_code == 200
    assert [i["event_instance_id"] for i in _week(client, user_id).json()] == [instance_id]
    other = _user(engine, "Other")
    assert client.delete(f"/event-instances/{instance_id}", headers=_headers(other)).status_code == 404


def test_delete_already_cancelled_instance_conflicts(client, engine, user_id):
    quiz = _weekly(client, user_id)
    instance_id = _instance_id(engine, quiz, date(2026, 9, 26))
    client.delete(f"/event-instances/{instance_id}", headers=_headers(user_id))

    assert client.delete(f"/event-instances/{instance_id}", headers=_headers(user_id)).status_code == 409
