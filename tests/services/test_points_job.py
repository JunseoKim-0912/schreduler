from datetime import date, datetime

import pytest
from freezegun import freeze_time
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.scheduler import scheduler, shutdown_scheduler, start_scheduler
from app.models import Base, Event, EventInstance, EventInstanceStatus, Importance, PointsLedger, User
from app.services import points as points_module
from app.services.points import (
    DAILY_POINTS_JOB_ID,
    DAILY_POINTS_MISFIRE_GRACE_SECONDS,
    register_daily_points_job,
    run_daily_points_job,
)


@pytest.fixture
def running_scheduler():
    start_scheduler()
    yield
    scheduler.remove_all_jobs()
    shutdown_scheduler()


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(points_module, "SessionLocal", sessionmaker(bind=engine, autoflush=False))
    return engine


def _cron_field(trigger, name: str) -> str:
    for field in trigger.fields:
        if field.name == name:
            return str(field)
    raise KeyError(name)


def _user_with_done_event(session: Session, name: str, day: date, importance: Importance) -> int:
    user = User(name=name, preferred_language="ko")
    session.add(user)
    session.flush()
    event = Event(
        user_id=user.id,
        title="일정",
        start_time=datetime(day.year, day.month, day.day, 9),
        end_time=datetime(day.year, day.month, day.day, 10),
        importance=importance,
    )
    session.add(event)
    session.flush()
    session.add(EventInstance(event_id=event.id, date=day, status=EventInstanceStatus.DONE))
    session.commit()
    return user.id


@pytest.mark.usefixtures("running_scheduler")
def test_register_daily_points_job_runs_at_midnight() -> None:
    register_daily_points_job()

    job = scheduler.get_job(DAILY_POINTS_JOB_ID)
    assert job is not None
    assert _cron_field(job.trigger, "hour") == "0"
    assert _cron_field(job.trigger, "minute") == "0"
    assert job.misfire_grace_time == DAILY_POINTS_MISFIRE_GRACE_SECONDS
    assert job.coalesce is True


@pytest.mark.usefixtures("running_scheduler")
def test_register_daily_points_job_is_idempotent() -> None:
    register_daily_points_job()
    register_daily_points_job()

    assert len([job for job in scheduler.get_jobs() if job.id == DAILY_POINTS_JOB_ID]) == 1


@freeze_time("2026-09-25 00:00:05")
def test_job_records_previous_day_for_every_user(engine) -> None:
    yesterday = date(2026, 9, 24)
    with Session(engine) as session:
        june = _user_with_done_event(session, "June", yesterday, Importance.MUST)
        alex = _user_with_done_event(session, "Alex", yesterday, Importance.MAX)
        _user_with_done_event(session, "Today", date(2026, 9, 25), Importance.MUST)

    run_daily_points_job()

    with Session(engine) as session:
        entries = {e.user_id: e for e in session.query(PointsLedger).all()}
    assert {e.date for e in entries.values()} == {yesterday}
    assert entries[june].points_earned == 5
    assert entries[alex].points_earned == 10
    assert len(entries) == 3  # 전날 일정이 없는 사용자도 0점으로 기록된다


@freeze_time("2026-09-25 00:00:05")
def test_job_rerun_updates_instead_of_duplicating(engine) -> None:
    with Session(engine) as session:
        _user_with_done_event(session, "June", date(2026, 9, 24), Importance.MUST)

    run_daily_points_job()
    run_daily_points_job()

    with Session(engine) as session:
        assert session.query(PointsLedger).count() == 1


def test_job_continues_after_one_user_fails(engine, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    day = date(2026, 9, 24)
    with Session(engine) as session:
        broken = _user_with_done_event(session, "Broken", day, Importance.MUST)
        ok = _user_with_done_event(session, "Ok", day, Importance.MUST)

    real_record = points_module.record_daily_points

    def flaky_record(db: Session, user_id: int, target_date: date) -> PointsLedger:
        if user_id == broken:
            raise RuntimeError("boom")
        return real_record(db, user_id, target_date)

    monkeypatch.setattr(points_module, "record_daily_points", flaky_record)

    run_daily_points_job(day)

    with Session(engine) as session:
        assert [e.user_id for e in session.query(PointsLedger).all()] == [ok]
    assert "계산 실패" in caplog.text
