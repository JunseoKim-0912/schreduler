import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.scheduler import scheduler, shutdown_scheduler, start_scheduler
from app.models import Base, User
from app.services import sleep_checkin as sleep_checkin_module
from app.services.sleep_checkin import (
    SLEEP_CHECKIN_JOB_ID,
    register_sleep_checkin_job,
    send_daily_sleep_checkin_reminders,
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


def test_register_sleep_checkin_job_adds_daily_8am_cron_job() -> None:
    register_sleep_checkin_job()

    job = scheduler.get_job(SLEEP_CHECKIN_JOB_ID)

    assert job is not None
    assert _cron_field(job.trigger, "hour") == "8"
    assert _cron_field(job.trigger, "minute") == "0"


def test_register_sleep_checkin_job_is_idempotent() -> None:
    register_sleep_checkin_job()
    register_sleep_checkin_job()

    jobs = [job for job in scheduler.get_jobs() if job.id == SLEEP_CHECKIN_JOB_ID]
    assert len(jobs) == 1


def test_send_daily_sleep_checkin_reminders_skips_users_without_device_token(
    engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """User에 아직 fcm_token 필드가 없어서(디바이스 등록 전) 지금은 아무에게도
    실제 발송이 안 되고 로그만 남아야 한다."""
    testing_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(sleep_checkin_module, "SessionLocal", testing_session_local)

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
        sleep_checkin_module,
        "send_push_notification",
        lambda token, title, body: sent.append((token, title, body)),
    )

    with caplog.at_level("INFO", logger="app.services.sleep_checkin"):
        send_daily_sleep_checkin_reminders()

    assert sent == []
    skip_logs = [r for r in caplog.records if "device_token 없음" in r.message]
    assert len(skip_logs) == 2  # 사용자 2명 모두에 대해 스킵 로그가 남아야 함


def test_send_sleep_checkin_to_user_sends_when_device_token_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fcm_token 필드가 나중에 실제로 생겼을 때 이 함수가 어떻게 동작할지를
    유저 단위 헬퍼에 직접 device_token 속성을 심어서 검증한다."""
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        sleep_checkin_module,
        "send_push_notification",
        lambda token, title, body: sent.append((token, title, body)),
    )

    user = User(id=1, name="June", preferred_language="ko")
    user.fcm_token = "fake-device-token"  # 아직 실제 컬럼은 아니지만 속성으로 심어봄

    sleep_checkin_module._send_sleep_checkin_to_user(user)

    assert sent == [
        ("fake-device-token", "[Schreduler] 수면 체크인", "어제 몇 시에 주무셨고 오늘 몇 시에 일어나셨나요?")
    ]
