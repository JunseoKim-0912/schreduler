from fastapi.testclient import TestClient

from app.core.scheduler import scheduler, shutdown_scheduler, start_scheduler
from app.main import app


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


def test_app_lifespan_starts_and_stops_the_scheduler() -> None:
    assert scheduler.running is False

    with TestClient(app) as client:
        assert scheduler.running is True
        client.get("/health")

    assert scheduler.running is False
