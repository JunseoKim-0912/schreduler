"""마감이 지난 할 일을 뒤늦게 완료했을 때 포인트가 원장(PointsLedger)과 요약에 반영되는지 검증한다.

자정 잡은 그날 상태로 포인트를 확정하므로, 이후에 지난 날짜의 인스턴스를 완료하면 완료 처리 시점에
그 날짜부터 어제까지를 다시 계산해야 한다 (app/services/event_instance_service.complete_event_instance).
"""

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.models import Base, PointsLedger, User
from app.services.points import record_daily_points

HEADERS = {"X-User-Id": "1"}
TODAY = "2026-09-24 12:00:00"  # 목요일 (이번 주는 9/21 월요일부터)


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(name="June", preferred_language="ko"))
        session.commit()
    return engine


@pytest.fixture
def client(engine) -> Iterator[TestClient]:
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


def _task(client: TestClient, due: str, importance: int = 5) -> int:
    body = client.post(
        "/tasks", headers=HEADERS, json={"title": f"과제 {due}", "end_time": f"{due}T18:00:00", "importance": importance}
    ).json()
    return body["event_instance_id"]


def _complete(client: TestClient, instance_id: int) -> None:
    assert client.put(f"/tasks/{instance_id}/complete", headers=HEADERS).status_code == 200


def _run_midnight_job(engine, *days: date) -> None:
    """자정 잡이 그 날짜들을 이미 기록했다고 가정한다 (당시 상태 그대로)."""
    with Session(engine) as session:
        for day in days:
            record_daily_points(session, 1, day)


def _ledger(engine) -> dict[date, tuple[float, float]]:
    with Session(engine) as session:
        rows = session.execute(select(PointsLedger).where(PointsLedger.user_id == 1)).scalars()
        return {row.date: (row.points_earned, row.streak_multiplier) for row in rows}


def _summary(client: TestClient) -> dict:
    return client.get("/points/summary", headers=HEADERS).json()


@freeze_time(TODAY)
def test_late_completion_of_yesterdays_task_updates_ledger_and_totals(client: TestClient, engine) -> None:
    yesterday_task = _task(client, "2026-09-23", importance=5)
    _run_midnight_job(engine, date(2026, 9, 23))  # 자정 시점엔 미완료 → 0점으로 확정됨
    assert _summary(client)["total_points"] == 0

    _complete(client, yesterday_task)

    assert _ledger(engine)[date(2026, 9, 23)] == (5, 1.0)
    summary = _summary(client)
    assert (summary["total_points"], summary["week_points"]) == (5, 5)
    assert summary["today"]["points_earned"] == 0  # 오늘 일정이 아니므로 오늘 점수는 그대로
    assert summary["current_streak_days"] == 1


@freeze_time(TODAY)
def test_late_completion_recalculates_streak_bonus_of_following_days(client: TestClient, engine) -> None:
    """9/21을 뒤늦게 완료하면 9/21~9/23이 3일 연속이 되어, 이미 기록된 9/23의 배율도 1.1로 바뀌어야 한다."""
    late = _task(client, "2026-09-21", importance=5)
    for due in ("2026-09-22", "2026-09-23"):
        _complete(client, _task(client, due, importance=5))
    _run_midnight_job(engine, date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23))
    assert _ledger(engine)[date(2026, 9, 23)] == (5, 1.0)

    _complete(client, late)

    assert _ledger(engine) == {
        date(2026, 9, 21): (5, 1.0),
        date(2026, 9, 22): (5, 1.0),
        date(2026, 9, 23): (5.5, 1.1),
    }
    summary = _summary(client)
    assert summary["total_points"] == 15.5
    assert summary["current_streak_days"] == 3


@freeze_time(TODAY)
def test_late_completion_from_last_week_counts_in_total_but_not_this_week(client: TestClient, engine) -> None:
    late = _task(client, "2026-09-18", importance=4)  # 지난주 금요일
    _run_midnight_job(engine, date(2026, 9, 18))

    _complete(client, late)

    summary = _summary(client)
    assert summary["total_points"] == 4
    assert summary["week_points"] == 0


@freeze_time(TODAY)
def test_completing_todays_task_does_not_write_todays_ledger_row(client: TestClient, engine) -> None:
    """오늘 분은 요약이 실시간으로 계산하고 자정 잡이 확정한다 — 완료 시점에 원장에 쓰지 않는다."""
    _complete(client, _task(client, "2026-09-24", importance=3))

    assert date(2026, 9, 24) not in _ledger(engine)
    summary = _summary(client)
    assert (summary["today"]["points_earned"], summary["total_points"]) == (3, 3)


@freeze_time(TODAY)
def test_completing_again_does_not_change_points(client: TestClient, engine) -> None:
    late = _task(client, "2026-09-23", importance=5)
    _run_midnight_job(engine, date(2026, 9, 23))

    _complete(client, late)
    _complete(client, late)

    assert _ledger(engine) == {date(2026, 9, 23): (5, 1.0)}
    assert _summary(client)["total_points"] == 5


@freeze_time(TODAY)
def test_late_completion_fills_days_the_midnight_job_missed(client: TestClient, engine) -> None:
    """서버가 꺼져 자정 잡이 못 돈 날이 있어도, 뒤늦은 완료가 그 날부터 어제까지 원장을 채운다."""
    late = _task(client, "2026-09-22", importance=2)

    _complete(client, late)

    assert set(_ledger(engine)) == {date(2026, 9, 22), date(2026, 9, 23)}
    assert _summary(client)["total_points"] == 2
