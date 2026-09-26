import pytest
from fastapi.testclient import TestClient

from app.core.scheduler import scheduler, shutdown_scheduler, start_scheduler
from app.main import app
from app.services import notification


def test_start_and_shutdown_scheduler_toggle_running_state() -> None:
    start_scheduler()
    assert scheduler.running is True

    shutdown_scheduler()
    assert scheduler.running is False


def test_start_scheduler_is_idempotent() -> None:
    start_scheduler()
    start_scheduler()  # 이미 돌고 있어도 에러 없이 그냥 넘어가야 함

    assert scheduler.running is True

    shutdown_scheduler()


def test_app_lifespan_starts_and_stops_the_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    # 시작 시 알림 job 재등록은 실제 DB(SessionLocal)를 읽으므로 여기서는 호출만 확인한다.
    registered = []
    monkeypatch.setattr(notification, "register_upcoming_notifications", lambda: registered.append(True))
    assert scheduler.running is False

    with TestClient(app) as client:
        assert scheduler.running is True
        client.get("/health")

    assert scheduler.running is False
    assert registered == [True]
