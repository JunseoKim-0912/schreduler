from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.core.scheduler import scheduler, shutdown_scheduler, start_scheduler
from app.main import app
from app.models import Base, Event, EventInstance, EventInstanceStatus, EventType, ImportantDateRange, User
from app.schemas.event import EventCreate, EventUpdate, InvalidEventTimesError, validate_event_times
from app.services.notification import DEADLINE_REMINDER_OFFSET, schedule_event_instance_notifications
from app.services.recurrence import generate_event_instances


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
def session(engine):
    with Session(engine) as session:
        yield session


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


@pytest.fixture
def running_scheduler():
    # 등록한 잡이 (날짜가 지나) 즉시 "놓친 잡"으로 처리돼 사라지지 않도록 pause 상태로 둔다.
    start_scheduler()
    scheduler.pause()
    yield scheduler
    scheduler.remove_all_jobs()
    shutdown_scheduler()


def _deadline_payload(user_id: int, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "user_id": user_id,
        "title": "과제 제출",
        "event_type": "deadline",
        "end_time": "2026-09-25T23:59:00",
    }
    payload.update(overrides)
    return payload


# --- (1) deadline 이벤트는 start_time이 반드시 null ---


def test_create_deadline_with_start_time_is_rejected(client: TestClient, user_id: int) -> None:
    response = client.post("/events", json=_deadline_payload(user_id, start_time="2026-09-25T09:00:00"))

    assert response.status_code == 422
    assert "start_time = null" in response.text


def test_create_deadline_without_start_time_succeeds(client: TestClient, user_id: int) -> None:
    response = client.post("/events", json=_deadline_payload(user_id))

    assert response.status_code == 201
    body = response.json()
    assert body["event_type"] == "deadline"
    assert body["start_time"] is None
    assert body["end_time"] == "2026-09-25T23:59:00"


def test_deadline_schema_rejects_start_time() -> None:
    with pytest.raises(ValidationError, match="start_time = null"):
        EventCreate(
            user_id=1,
            title="과제",
            event_type=EventType.DEADLINE,
            start_time=datetime(2026, 9, 25, 9, 0),
            end_time=datetime(2026, 9, 25, 23, 59),
        )
    with pytest.raises(ValidationError, match="start_time = null"):
        EventUpdate(event_type=EventType.DEADLINE, start_time=datetime(2026, 9, 25, 9, 0))


def test_update_to_deadline_requires_clearing_start_time(client: TestClient, user_id: int) -> None:
    event_id = client.post(
        "/events",
        json={
            "user_id": user_id,
            "title": "수업",
            "start_time": "2026-09-17T09:00:00",
            "end_time": "2026-09-17T10:00:00",
        },
    ).json()["id"]

    response = client.put(f"/events/{event_id}", json={"event_type": "deadline"})
    assert response.status_code == 422

    response = client.put(f"/events/{event_id}", json={"event_type": "deadline", "start_time": None})
    assert response.status_code == 200
    assert response.json()["event_type"] == "deadline"
    assert response.json()["start_time"] is None


def test_db_check_constraint_blocks_deadline_with_start_time(session: Session, user_id: int) -> None:
    session.add(
        Event(
            user_id=user_id,
            title="직접 생성",
            event_type=EventType.DEADLINE,
            start_time=datetime(2026, 9, 25, 9, 0),
            end_time=datetime(2026, 9, 25, 23, 59),
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


# --- (2) scheduled 이벤트는 start_time/end_time 둘 다 필수 ---


def test_scheduled_is_the_default_type(client: TestClient, user_id: int) -> None:
    response = client.post(
        "/events",
        json={"user_id": user_id, "title": "수업", "start_time": "2026-09-17T09:00:00", "end_time": "2026-09-17T10:00:00"},
    )

    assert response.status_code == 201
    assert response.json()["event_type"] == "scheduled"


@pytest.mark.parametrize(
    "payload",
    [
        {"end_time": "2026-09-17T10:00:00"},
        {"start_time": None, "end_time": "2026-09-17T10:00:00"},
        {"start_time": "2026-09-17T09:00:00"},
        {"event_type": "scheduled", "end_time": "2026-09-17T10:00:00"},
    ],
)
def test_scheduled_requires_both_times(client: TestClient, user_id: int, payload: dict) -> None:
    response = client.post("/events", json={"user_id": user_id, "title": "수업", **payload})

    assert response.status_code == 422


def test_scheduled_still_requires_end_after_start(client: TestClient, user_id: int) -> None:
    response = client.post(
        "/events",
        json={"user_id": user_id, "title": "수업", "start_time": "2026-09-17T10:00:00", "end_time": "2026-09-17T09:00:00"},
    )

    assert response.status_code == 422


def test_update_cannot_clear_start_time_of_scheduled_event(client: TestClient, user_id: int) -> None:
    event_id = client.post(
        "/events",
        json={"user_id": user_id, "title": "수업", "start_time": "2026-09-17T09:00:00", "end_time": "2026-09-17T10:00:00"},
    ).json()["id"]

    assert client.put(f"/events/{event_id}", json={"start_time": None}).status_code == 422
    assert client.put(f"/events/{event_id}", json={"event_type": "scheduled", "start_time": None}).status_code == 422


# --- (3) deadline 알림: 리마인더(1일 전) + 마감 시각 완료 확인만 ---


def _instance(session: Session, user_id: int, event_type: EventType) -> EventInstance:
    event = Event(
        user_id=user_id,
        title="과제 제출",
        event_type=event_type,
        start_time=None if event_type == EventType.DEADLINE else datetime(2026, 9, 25, 9, 0),
        end_time=datetime(2026, 9, 25, 23, 59),
    )
    session.add(event)
    session.flush()
    instance = EventInstance(event_id=event.id, date=date(2026, 9, 25), status=EventInstanceStatus.PENDING)
    session.add(instance)
    session.commit()
    return instance


def _jobs(instance: EventInstance) -> dict[str, datetime]:
    prefix = f"event_instance_{instance.id}_"
    return {
        job.id.removeprefix(prefix): job.next_run_time.replace(tzinfo=None)
        for job in scheduler.get_jobs()
        if job.id.startswith(prefix)
    }


def test_deadline_registers_reminder_and_completion_check_only(
    session: Session, user_id: int, running_scheduler
) -> None:
    instance = _instance(session, user_id, EventType.DEADLINE)

    schedule_event_instance_notifications(instance)

    jobs = _jobs(instance)
    assert set(jobs) == {"deadline_reminder", "end"}
    assert jobs["end"] == datetime(2026, 9, 25, 23, 59)
    assert jobs["deadline_reminder"] == datetime(2026, 9, 24, 23, 59)
    assert DEADLINE_REMINDER_OFFSET.total_seconds() == 1440 * 60
    job_kinds = {job.args[1] for job in scheduler.get_jobs()}
    assert "start" not in job_kinds


def test_scheduled_notifications_are_unchanged(session: Session, user_id: int, running_scheduler) -> None:
    instance = _instance(session, user_id, EventType.SCHEDULED)

    schedule_event_instance_notifications(instance)

    assert _jobs(instance) == {"start": datetime(2026, 9, 25, 9, 0), "end": datetime(2026, 9, 25, 23, 59)}


# --- (4) 반복 deadline (매주 금요일 마감) ---


def test_weekly_friday_deadline_creates_instance_per_friday(session: Session, user_id: int) -> None:
    date_range = ImportantDateRange(
        user_id=user_id, name="2026 가을학기", start_date=date(2026, 9, 1), end_date=date(2026, 9, 30)
    )
    session.add(date_range)
    session.flush()
    event = Event(
        user_id=user_id,
        title="주간 리포트 제출",
        event_type=EventType.DEADLINE,
        start_time=None,
        end_time=datetime(2026, 9, 4, 23, 59),
        is_recurring=True,
        recurrence_rule="FREQ=WEEKLY;BYDAY=FR",
        date_range_id=date_range.id,
    )
    session.add(event)
    session.flush()

    created = generate_event_instances(session, event)

    fridays = [date(2026, 9, 4), date(2026, 9, 11), date(2026, 9, 18), date(2026, 9, 25)]
    assert sorted(instance.date for instance in created) == fridays
    assert generate_event_instances(session, event) == []


def test_recurring_deadline_via_api(client: TestClient, engine, user_id: int) -> None:
    with Session(engine) as session:
        date_range = ImportantDateRange(
            user_id=user_id, name="10월", start_date=date(2026, 10, 1), end_date=date(2026, 10, 31)
        )
        session.add(date_range)
        session.commit()
        date_range_id = date_range.id

    response = client.post(
        "/events",
        json=_deadline_payload(
            user_id,
            end_time="2026-10-02T18:00:00",
            is_recurring=True,
            recurrence_rule="FREQ=WEEKLY;BYDAY=FR",
            date_range_id=date_range_id,
        ),
    )
    assert response.status_code == 201

    with Session(engine) as session:
        dates = session.execute(
            select(EventInstance.date).where(EventInstance.event_id == response.json()["id"]).order_by(EventInstance.date)
        ).scalars().all()
    assert dates == [date(2026, 10, 2), date(2026, 10, 9), date(2026, 10, 16), date(2026, 10, 23), date(2026, 10, 30)]


# --- 부분 수정 스키마 검증 ---


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"event_type": None}, "event_type cannot be null"),
        ({"end_time": None}, "end_time cannot be null"),
        ({"start_time": "2026-09-17T10:00:00", "end_time": "2026-09-17T09:00:00"}, "end_time must be after start_time"),
    ],
)
def test_event_update_rejects_invalid_partial_payloads(payload: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        EventUpdate.model_validate(payload)


def test_validate_event_times_requires_end_time() -> None:
    with pytest.raises(InvalidEventTimesError, match="end_time is required"):
        validate_event_times(EventType.DEADLINE, None, None)
