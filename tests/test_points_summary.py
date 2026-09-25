from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.models import Base, Event, EventInstance, EventInstanceStatus, Importance, PointsLedger, User

TODAY = date(2026, 9, 24)  # 목요일 -> 이번 주는 9/21(월)부터
NOW = "2026-09-24 15:00:00"


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


@pytest.fixture
def user_id(engine) -> int:
    with Session(engine) as session:
        user = User(name="June", preferred_language="ko")
        session.add(user)
        session.commit()
        return user.id


def _ledger(engine, user_id: int, day: date, points: float) -> None:
    with Session(engine) as session:
        session.add(PointsLedger(user_id=user_id, date=day, base_points=points, streak_multiplier=1.0, points_earned=points))
        session.commit()


def _instance(engine, user_id: int, day: date, status: EventInstanceStatus, importance: Importance) -> None:
    with Session(engine) as session:
        event = Event(
            user_id=user_id,
            title="일정",
            start_time=datetime(day.year, day.month, day.day, 9),
            end_time=datetime(day.year, day.month, day.day, 10),
            importance=importance,
        )
        session.add(event)
        session.flush()
        session.add(EventInstance(event_id=event.id, date=day, status=status))
        session.commit()


def _summary(client: TestClient, user_id: int) -> dict:
    response = client.get("/points/summary", headers={"X-User-Id": str(user_id)})
    assert response.status_code == 200, response.text
    return response.json()


@freeze_time(NOW)
def test_summary_splits_today_week_and_total(client: TestClient, engine, user_id: int) -> None:
    _ledger(engine, user_id, date(2026, 9, 20), 100)  # 지난주 일요일
    _ledger(engine, user_id, date(2026, 9, 21), 10)  # 이번 주 월요일
    _ledger(engine, user_id, date(2026, 9, 23), 5.5)
    _instance(engine, user_id, TODAY, EventInstanceStatus.DONE, Importance.MUST)
    _instance(engine, user_id, TODAY, EventInstanceStatus.PENDING, Importance.MAX)

    body = _summary(client, user_id)

    assert body["today"]["date"] == "2026-09-24"
    assert body["today"]["points_earned"] == 5  # 지금까지 완료한 MUST만
    assert body["week_start"] == "2026-09-21"
    assert body["week_points"] == 20.5  # 10 + 5.5 + 오늘 5
    assert body["total_points"] == 120.5  # 100 + 10 + 5.5 + 오늘 5


@freeze_time(NOW)
def test_today_ledger_row_is_not_double_counted(client: TestClient, engine, user_id: int) -> None:
    _ledger(engine, user_id, TODAY, 999)  # 수동 실행 등으로 오늘 행이 이미 있는 경우
    _instance(engine, user_id, TODAY, EventInstanceStatus.DONE, Importance.MUST)

    body = _summary(client, user_id)

    assert body["week_points"] == 5
    assert body["total_points"] == 5


@freeze_time("2026-09-21 08:00:00")
def test_monday_week_contains_only_today(client: TestClient, engine, user_id: int) -> None:
    _ledger(engine, user_id, date(2026, 9, 20), 50)

    body = _summary(client, user_id)

    assert body["week_start"] == "2026-09-21"
    assert body["week_points"] == 0
    assert body["total_points"] == 50


@freeze_time(NOW)
def test_new_user_has_all_zeros(client: TestClient, user_id: int) -> None:
    body = _summary(client, user_id)

    assert (body["today"]["points_earned"], body["week_points"], body["total_points"]) == (0, 0, 0)
    assert body["current_streak_days"] == 0


@freeze_time(NOW)
def test_current_streak_waits_for_pending_today(client: TestClient, engine, user_id: int) -> None:
    for offset in (3, 2, 1):
        _instance(engine, user_id, TODAY - timedelta(days=offset), EventInstanceStatus.DONE, Importance.LEISURE)
    _instance(engine, user_id, TODAY, EventInstanceStatus.PENDING, Importance.LEISURE)

    assert _summary(client, user_id)["current_streak_days"] == 3


@freeze_time(NOW)
def test_current_streak_includes_today_once_all_done(client: TestClient, engine, user_id: int) -> None:
    for offset in (2, 1, 0):
        _instance(engine, user_id, TODAY - timedelta(days=offset), EventInstanceStatus.DONE, Importance.LEISURE)

    body = _summary(client, user_id)

    assert body["current_streak_days"] == 3
    assert body["today"]["streak_multiplier"] == 1.1


@freeze_time(NOW)
def test_current_streak_breaks_on_missed_today(client: TestClient, engine, user_id: int) -> None:
    for offset in (3, 2, 1):
        _instance(engine, user_id, TODAY - timedelta(days=offset), EventInstanceStatus.DONE, Importance.LEISURE)
    _instance(engine, user_id, TODAY, EventInstanceStatus.MISSED, Importance.LEISURE)
    _instance(engine, user_id, TODAY, EventInstanceStatus.PENDING, Importance.LEISURE)

    assert _summary(client, user_id)["current_streak_days"] == 0


@freeze_time(NOW)
def test_summary_is_scoped_to_current_user(client: TestClient, engine, user_id: int) -> None:
    with Session(engine) as session:
        other = User(name="Other", preferred_language="ko")
        session.add(other)
        session.commit()
        other_id = other.id
    _ledger(engine, other_id, date(2026, 9, 22), 77)

    assert _summary(client, user_id)["total_points"] == 0
    assert _summary(client, other_id)["total_points"] == 77


def test_summary_requires_user_header(client: TestClient) -> None:
    assert client.get("/points/summary").status_code == 401
    assert client.get("/points/summary", headers={"X-User-Id": "999"}).status_code == 404
