from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.models import (
    Base,
    CompletionMethod,
    Event,
    EventInstance,
    EventInstanceStatus,
    EventType,
    ImportantDateRange,
    User,
)

NOW = "2026-09-24 12:00:00"


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
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


def _add_user(engine, name: str = "June") -> int:
    with Session(engine) as session:
        user = User(name=name, preferred_language="ko")
        session.add(user)
        session.commit()
        return user.id


@pytest.fixture
def user_id(engine) -> int:
    return _add_user(engine)


def _headers(user_id: int) -> dict[str, str]:
    return {"X-User-Id": str(user_id)}


def _create_task(client: TestClient, user_id: int, **fields: object) -> dict:
    body = {"title": "과제 제출", "end_time": "2026-09-25T23:59:00", **fields}
    response = client.post("/tasks", json=body, headers=_headers(user_id))
    assert response.status_code == 201, response.text
    return response.json()


def _date_range(engine, user_id: int, start: date, end: date) -> int:
    with Session(engine) as session:
        date_range = ImportantDateRange(user_id=user_id, name="학기", start_date=start, end_date=end)
        session.add(date_range)
        session.commit()
        return date_range.id


# --- POST /tasks ---


@freeze_time(NOW)
def test_create_task_is_stored_as_deadline_event(client: TestClient, engine, user_id: int) -> None:
    task = _create_task(client, user_id, importance=4)

    with Session(engine) as session:
        event = session.get(Event, task["event_id"])
        assert event.event_type == EventType.DEADLINE
        assert event.start_time is None
        assert event.end_time == datetime(2026, 9, 25, 23, 59)
        assert event.user_id == user_id
        assert event.is_recurring is False
        instances = session.execute(select(EventInstance).where(EventInstance.event_id == event.id)).scalars().all()
        assert [(i.date, i.status) for i in instances] == [(date(2026, 9, 25), EventInstanceStatus.PENDING)]

    assert task["event_instance_id"] == instances[0].id
    assert task["due_at"] == "2026-09-25T23:59:00"
    assert (task["completed"], task["overdue"], task["importance"]) == (False, False, 4)


@freeze_time(NOW)
def test_create_task_is_also_visible_through_events_api(client: TestClient, user_id: int) -> None:
    task = _create_task(client, user_id)

    event = client.get(f"/events/{task['event_id']}").json()

    assert event["event_type"] == "deadline"
    assert event["start_time"] is None


@freeze_time(NOW)
def test_create_recurring_task_generates_instances(client: TestClient, engine, user_id: int) -> None:
    date_range_id = _date_range(engine, user_id, date(2026, 9, 1), date(2026, 9, 30))

    task = _create_task(
        client,
        user_id,
        title="주간 리포트",
        end_time="2026-09-04T18:00:00",
        recurrence_rule="FREQ=WEEKLY;BYDAY=FR",
        date_range_id=date_range_id,
    )

    with Session(engine) as session:
        dates = session.execute(
            select(EventInstance.date).where(EventInstance.event_id == task["event_id"]).order_by(EventInstance.date)
        ).scalars().all()
    assert dates == [date(2026, 9, 4), date(2026, 9, 11), date(2026, 9, 18), date(2026, 9, 25)]
    assert task["is_recurring"] is True
    assert task["due_at"] == "2026-09-04T18:00:00"  # 가장 이른 미완료 회차
    assert task["overdue"] is True


def test_create_recurring_task_requires_date_range(client: TestClient, user_id: int) -> None:
    response = client.post(
        "/tasks",
        json={"title": "주간 리포트", "end_time": "2026-09-04T18:00:00", "recurrence_rule": "FREQ=WEEKLY;BYDAY=FR"},
        headers=_headers(user_id),
    )
    assert response.status_code == 422


def test_create_task_rejects_unknown_date_range(client: TestClient, user_id: int) -> None:
    response = client.post(
        "/tasks",
        json={
            "title": "주간 리포트",
            "end_time": "2026-09-04T18:00:00",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=FR",
            "date_range_id": 999,
        },
        headers=_headers(user_id),
    )
    assert response.status_code == 404


def test_create_task_rejects_start_time_field(client: TestClient, user_id: int) -> None:
    response = client.post(
        "/tasks",
        json={"title": "x", "end_time": "2026-09-25T23:59:00", "start_time": "2026-09-25T09:00:00"},
        headers=_headers(user_id),
    )
    assert response.status_code == 422


# --- GET /tasks ---


@freeze_time(NOW)
def test_list_tasks_sorted_by_due_and_only_own_deadlines(client: TestClient, engine, user_id: int) -> None:
    other_id = _add_user(engine, "Other")
    _create_task(client, user_id, title="셋째", end_time="2026-10-01T09:00:00")
    _create_task(client, user_id, title="첫째", end_time="2026-09-20T09:00:00")
    _create_task(client, user_id, title="둘째", end_time="2026-09-25T09:00:00")
    _create_task(client, other_id, title="남의 것", end_time="2026-09-21T09:00:00")
    client.post(
        "/events",
        json={"user_id": user_id, "title": "일반 일정", "start_time": "2026-09-22T09:00:00", "end_time": "2026-09-22T10:00:00"},
    )

    tasks = client.get("/tasks", headers=_headers(user_id)).json()

    assert [t["title"] for t in tasks] == ["첫째", "둘째", "셋째"]


@freeze_time(NOW)
@pytest.mark.parametrize(
    ("end_time", "complete", "expected_overdue"),
    [
        ("2026-09-23T23:59:00", False, True),  # 마감 지남 + 미완료
        ("2026-09-23T23:59:00", True, False),  # 마감 지났지만 완료
        ("2026-09-24T11:59:00", False, True),  # 오늘, 1분 전 마감
        ("2026-09-24T12:00:00", False, False),  # 지금이 정확히 마감 시각 -> 아직 overdue 아님
        ("2026-09-25T23:59:00", False, False),  # 마감 전
    ],
)
def test_overdue_flag(
    client: TestClient, user_id: int, end_time: str, complete: bool, expected_overdue: bool
) -> None:
    task = _create_task(client, user_id, end_time=end_time)
    if complete:
        client.put(f"/tasks/{task['event_instance_id']}/complete", headers=_headers(user_id))

    [listed] = client.get("/tasks", headers=_headers(user_id)).json()

    assert listed["overdue"] is expected_overdue
    assert listed["completed"] is complete
    assert listed["status"] == ("done" if complete else "pending")


@freeze_time(NOW)
def test_recurring_task_shows_oldest_unfinished_then_advances(client: TestClient, engine, user_id: int) -> None:
    date_range_id = _date_range(engine, user_id, date(2026, 9, 1), date(2026, 9, 30))
    task = _create_task(
        client, user_id, end_time="2026-09-18T18:00:00", recurrence_rule="FREQ=WEEKLY;BYDAY=FR", date_range_id=date_range_id
    )
    [listed] = client.get("/tasks", headers=_headers(user_id)).json()
    assert (listed["due_at"], listed["overdue"]) == ("2026-09-04T18:00:00", True)

    for _ in range(3):  # 9/4, 9/11, 9/18 회차 완료
        current = client.get("/tasks", headers=_headers(user_id)).json()[0]
        client.put(f"/tasks/{current['event_instance_id']}/complete", headers=_headers(user_id))

    [listed] = client.get("/tasks", headers=_headers(user_id)).json()
    assert listed["event_id"] == task["event_id"]
    assert (listed["due_at"], listed["overdue"], listed["completed"]) == ("2026-09-25T18:00:00", False, False)


@freeze_time(NOW)
def test_all_instances_done_shows_latest_as_completed(client: TestClient, user_id: int) -> None:
    task = _create_task(client, user_id, end_time="2026-09-20T09:00:00")
    client.put(f"/tasks/{task['event_instance_id']}/complete", headers=_headers(user_id))

    [listed] = client.get("/tasks", headers=_headers(user_id)).json()

    assert (listed["completed"], listed["overdue"]) == (True, False)


@freeze_time(NOW)
@freeze_time(NOW)
def test_deadline_event_from_events_api_gets_instance(client: TestClient, user_id: int) -> None:
    """/events로 만든 단발성 deadline도 /tasks와 똑같이 마감일에 회차가 하나 생긴다."""
    client.post(
        "/events",
        json={"user_id": user_id, "title": "마감", "event_type": "deadline", "end_time": "2026-09-23T09:00:00"},
    )

    [listed] = client.get("/tasks", headers=_headers(user_id)).json()

    assert listed["event_instance_id"] is not None
    assert listed["status"] == "pending"
    assert (listed["completed"], listed["overdue"]) == (False, True)


def test_legacy_deadline_event_without_instance_is_listed(client: TestClient, engine, user_id: int) -> None:
    """회차 생성 이전에 만들어진(백필 안 한 지난) deadline은 인스턴스가 없다 — 목록엔 나오되 인스턴스 필드는 null."""
    with Session(engine) as session:
        session.add(Event(user_id=user_id, title="옛 마감", event_type=EventType.DEADLINE, start_time=None, end_time=datetime(2026, 9, 23, 9)))
        session.commit()

    [listed] = client.get("/tasks", headers=_headers(user_id)).json()

    assert listed["event_instance_id"] is None
    assert listed["status"] is None
    assert (listed["completed"], listed["overdue"]) == (False, True)


def test_list_tasks_requires_user(client: TestClient) -> None:
    assert client.get("/tasks").status_code == 401


# --- PUT /tasks/{event_instance_id}/complete ---


@freeze_time(NOW)
def test_complete_task_marks_instance_done(client: TestClient, engine, user_id: int) -> None:
    task = _create_task(client, user_id)

    response = client.put(f"/tasks/{task['event_instance_id']}/complete", headers=_headers(user_id))

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["completed"], body["overdue"]) == ("done", True, False)
    with Session(engine) as session:
        instance = session.get(EventInstance, task["event_instance_id"])
        assert instance.status == EventInstanceStatus.DONE
        assert instance.completion_method == CompletionMethod.MANUAL


@freeze_time(NOW)
def test_complete_task_is_idempotent(client: TestClient, user_id: int) -> None:
    task = _create_task(client, user_id)
    url = f"/tasks/{task['event_instance_id']}/complete"

    first = client.put(url, headers=_headers(user_id))
    second = client.put(url, headers=_headers(user_id))

    assert first.json() == second.json()


@freeze_time(NOW)
def test_complete_task_feeds_points(client: TestClient, user_id: int) -> None:
    task = _create_task(client, user_id, end_time="2026-09-24T23:59:00", importance=5)
    assert client.get("/points/summary", headers=_headers(user_id)).json()["today"]["points_earned"] == 0

    client.put(f"/tasks/{task['event_instance_id']}/complete", headers=_headers(user_id))

    today = client.get("/points/summary", headers=_headers(user_id)).json()["today"]
    assert (today["points_earned"], today["is_perfect_day"]) == (5, True)


@freeze_time(NOW)
def test_cannot_complete_other_users_task(client: TestClient, engine, user_id: int) -> None:
    other_id = _add_user(engine, "Other")
    task = _create_task(client, other_id)

    response = client.put(f"/tasks/{task['event_instance_id']}/complete", headers=_headers(user_id))

    assert response.status_code == 404
    with Session(engine) as session:
        assert session.get(EventInstance, task["event_instance_id"]).status == EventInstanceStatus.PENDING


def test_cannot_complete_scheduled_event_instance_via_tasks(client: TestClient, engine, user_id: int) -> None:
    with Session(engine) as session:
        event = Event(
            user_id=user_id,
            title="수업",
            event_type=EventType.SCHEDULED,
            start_time=datetime(2026, 9, 24, 9),
            end_time=datetime(2026, 9, 24, 10),
        )
        session.add(event)
        session.flush()
        instance = EventInstance(event_id=event.id, date=date(2026, 9, 24), status=EventInstanceStatus.PENDING)
        session.add(instance)
        session.commit()
        instance_id = instance.id

    assert client.put(f"/tasks/{instance_id}/complete", headers=_headers(user_id)).status_code == 404


def test_complete_unknown_instance_returns_404(client: TestClient, user_id: int) -> None:
    assert client.put("/tasks/999/complete", headers=_headers(user_id)).status_code == 404
