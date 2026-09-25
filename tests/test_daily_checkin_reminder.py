import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.scheduler import scheduler, shutdown_scheduler, start_scheduler
from app.models import Base, User
from app.services import daily_checkin as daily_checkin_module
from app.services.daily_checkin import (
    DAILY_CHECKIN_JOB_ID,
    register_daily_checkin_job,
    send_daily_checkin_reminders,
)


@pytest.fixture(autouse=True)
def running_scheduler():
    start_scheduler()
    yield
    scheduler.remove_all_jobs()
    shutdown_scheduler()


@pytest.fixture
def engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def _cron_field(trigger, name: str) -> str:
    for field in trigger.fields:
        if field.name == name:
            return str(field)
    raise KeyError(name)


def test_register_daily_checkin_job_adds_daily_9pm_cron_job() -> None:
    register_daily_checkin_job()

    job = scheduler.get_job(DAILY_CHECKIN_JOB_ID)

    assert job is not None
    assert _cron_field(job.trigger, "hour") == "21"
    assert _cron_field(job.trigger, "minute") == "0"


def test_register_daily_checkin_job_is_idempotent() -> None:
    register_daily_checkin_job()
    register_daily_checkin_job()

    jobs = [job for job in scheduler.get_jobs() if job.id == DAILY_CHECKIN_JOB_ID]
    assert len(jobs) == 1


def test_send_daily_checkin_reminders_skips_users_without_device_token(
    engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    testing_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(daily_checkin_module, "SessionLocal", testing_session_local)

    with Session(engine) as session:
        session.add_all(
            [
                User(name="June", preferred_language="ko"),
                User(name="Other", preferred_language="en"),
            ]
        )
        session.commit()

    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        daily_checkin_module,
        "send_push_notification",
        lambda token, title, body: sent.append((token, title, body)),
    )

    with caplog.at_level("INFO", logger="app.services.daily_checkin"):
        send_daily_checkin_reminders()

    assert sent == []
    skip_logs = [r for r in caplog.records if "device_token 없음" in r.message]
    assert len(skip_logs) == 2


def test_send_daily_checkin_to_user_sends_when_device_token_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        daily_checkin_module,
        "send_push_notification",
        lambda token, title, body: sent.append((token, title, body)),
    )

    user = User(id=1, name="June", preferred_language="ko")
    user.fcm_token = "fake-device-token"

    daily_checkin_module._send_daily_checkin_to_user(user)

    assert sent == [("fake-device-token", "[Schreduler] 오늘 하루 체크인", "오늘 하루 어떻게 보내셨나요?")]
