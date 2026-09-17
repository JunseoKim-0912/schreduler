from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.scheduler import scheduler, shutdown_scheduler, start_scheduler
from app.models import Base, Event, EventInstance, EventInstanceStatus, User
from app.services import notification as notification_module
from app.services.notification import schedule_event_instance_notifications


@pytest.fixture(autouse=True)
def running_scheduler():
    # APScheduler는 스케줄러가 실제로 돌고 있어야 같은 id로 add_job했을 때
    # replace_existing이 제대로 동작한다 (멈춰 있으면 pending 큐에 계속 쌓임).
    start_scheduler()
    yield
    scheduler.remove_all_jobs()
    shutdown_scheduler()


@pytest.fixture
def engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session(engine):
    with Session(engine) as session:
        yield session


def _make_instance(session: Session) -> EventInstance:
    user = User(name="June", preferred_language="ko")
    session.add(user)
    session.flush()

    event = Event(
        user_id=user.id,
        title="수업",
        start_time=datetime(2026, 9, 21, 9, 0),
        end_time=datetime(2026, 9, 21, 10, 0),
    )
    session.add(event)
    session.flush()

    instance = EventInstance(
        event_id=event.id, date=date(2026, 9, 21), status=EventInstanceStatus.PENDING
    )
    session.add(instance)
    session.commit()
    return instance


def test_schedule_event_instance_notifications_registers_start_and_end_jobs(
    session: Session,
) -> None:
    instance = _make_instance(session)

    schedule_event_instance_notifications(instance)

    start_job = scheduler.get_job(f"event_instance_{instance.id}_start")
    end_job = scheduler.get_job(f"event_instance_{instance.id}_end")

    assert start_job is not None
    assert end_job is not None
    assert start_job.trigger.run_date.replace(tzinfo=None) == datetime(2026, 9, 21, 9, 0)
    assert end_job.trigger.run_date.replace(tzinfo=None) == datetime(2026, 9, 21, 10, 0)
    assert start_job.args == (instance.id, "start")
    assert end_job.args == (instance.id, "end")


def test_schedule_event_instance_notifications_is_idempotent(session: Session) -> None:
    instance = _make_instance(session)

    schedule_event_instance_notifications(instance)
    schedule_event_instance_notifications(instance)

    jobs = [
        job
        for job in scheduler.get_jobs()
        if job.id.startswith(f"event_instance_{instance.id}_")
    ]
    assert len(jobs) == 2  # 두 번 호출해도 start/end 각각 하나씩만 남음


def test_schedule_event_instance_notifications_for_different_instances_do_not_clash(
    session: Session,
) -> None:
    instance1 = _make_instance(session)
    instance2 = _make_instance(session)

    schedule_event_instance_notifications(instance1)
    schedule_event_instance_notifications(instance2)

    assert scheduler.get_job(f"event_instance_{instance1.id}_start") is not None
    assert scheduler.get_job(f"event_instance_{instance2.id}_start") is not None


@pytest.fixture
def patched_session_local(engine, monkeypatch: pytest.MonkeyPatch):
    testing_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(notification_module, "SessionLocal", testing_session_local)
    return testing_session_local


def test_send_notification_logs_and_skips_without_device_token(
    session: Session, patched_session_local, caplog: pytest.LogCaptureFixture
) -> None:
    """User에 fcm_token이 없는 지금 상태에서는 실제 발송 없이 로그만 남아야 한다."""
    instance = _make_instance(session)

    with caplog.at_level("INFO", logger="app.services.notification"):
        notification_module._send_notification(instance.id, "start")

    assert any("수업" in record.message for record in caplog.records)
    assert any("start" in record.message for record in caplog.records)
    assert any("device_token 없음" in record.message for record in caplog.records)


def test_send_notification_warns_when_instance_missing(
    patched_session_local, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="app.services.notification"):
        notification_module._send_notification(999, "start")

    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_send_push_notification_skips_when_firebase_not_configured(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(notification_module.settings, "firebase_credentials_path", None)

    with caplog.at_level("INFO", logger="app.services.notification"):
        notification_module.send_push_notification("fake-device-token", "제목", "내용")

    assert any("FCM 발송 생략" in record.message for record in caplog.records)


def test_send_push_notification_logs_success_when_send_succeeds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(notification_module, "_get_firebase_app", lambda: object())
    monkeypatch.setattr(notification_module.messaging, "send", lambda message, app: "message-id-123")

    with caplog.at_level("INFO", logger="app.services.notification"):
        notification_module.send_push_notification("fake-device-token", "제목", "내용")

    assert any("FCM 발송 성공" in record.message for record in caplog.records)


def test_send_push_notification_logs_warning_when_send_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _raise(message, app):
        raise RuntimeError("invalid registration token")

    monkeypatch.setattr(notification_module, "_get_firebase_app", lambda: object())
    monkeypatch.setattr(notification_module.messaging, "send", _raise)

    with caplog.at_level("WARNING", logger="app.services.notification"):
        notification_module.send_push_notification("fake-device-token", "제목", "내용")

    assert any("FCM 발송 실패" in record.message for record in caplog.records)
