"""Settings that only matter in a deployment: scheduler switch and time zone, Firebase JSON, volume prep, warnings."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import request_guards
from app.core.config import settings
from app.core.db import get_db
from app.core.scheduler import scheduler
from app.main import app
from app.models import Base
from app.scripts import prepare_volume
from app.services import notification


@pytest.fixture
def lifespan_client(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    local = sessionmaker(bind=engine)
    monkeypatch.setattr(notification, "SessionLocal", local)
    app.dependency_overrides[get_db] = lambda: iter([local()])

    def run():
        return TestClient(app)

    yield run
    app.dependency_overrides.clear()
    for job in scheduler.get_jobs():
        job.remove()


def test_scheduler_runs_jobs_in_app_timezone() -> None:
    assert str(scheduler.timezone) == settings.app_timezone


def test_run_scheduler_true_starts_the_jobs_including_the_backup(lifespan_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "run_scheduler", True)
    with lifespan_client():
        assert scheduler.running
        assert {"sqlite_backup", "daily_points_calculation", "daily_evening_checkin", "daily_sleep_checkin", "demo_cleanup"} <= {
            job.id for job in scheduler.get_jobs()
        }
    assert not scheduler.running


def test_run_scheduler_false_starts_nothing(lifespan_client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(settings, "run_scheduler", False)
    with caplog.at_level(logging.WARNING), lifespan_client() as client:
        assert not scheduler.running
        assert scheduler.get_job("sqlite_backup") is None
        assert client.get("/health").status_code == 200
    assert "RUN_SCHEDULER=false" in caplog.text


@pytest.mark.parametrize(
    ("secure", "origins", "warned"),
    [(True, (), True), (True, ("https://app.example.com",), False), (False, (), False)],
)
def test_secure_cookie_without_allowed_origins_warns(monkeypatch, caplog, secure: bool, origins: tuple[str, ...], warned: bool) -> None:
    monkeypatch.setattr(settings, "session_cookie_secure", secure)
    monkeypatch.setattr(settings, "allowed_origins", origins)
    with caplog.at_level(logging.WARNING):
        request_guards.warn_about_cookie_settings()
    assert ("ALLOWED_ORIGINS is empty" in caplog.text) is warned


SERVICE_ACCOUNT = {"type": "service_account", "project_id": "demo", "private_key": "-----BEGIN PRIVATE KEY-----\nsecret\n"}


def test_firebase_credentials_from_json_env_win_over_the_path(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr(settings, "firebase_credentials_json", json.dumps(SERVICE_ACCOUNT))
    monkeypatch.setattr(settings, "firebase_credentials_path", "/nowhere.json")
    monkeypatch.setattr(notification.firebase_admin, "get_app", lambda: (_ for _ in ()).throw(ValueError()))
    monkeypatch.setattr(notification.credentials, "Certificate", lambda value: seen.append(value) or "cert")
    monkeypatch.setattr(notification.firebase_admin, "initialize_app", lambda cert: f"app:{cert}")

    assert notification._get_firebase_app() == "app:cert"
    assert seen == [SERVICE_ACCOUNT]


def test_broken_firebase_json_is_reported_without_its_content(monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "firebase_credentials_json", '{"private_key": "secret-material", ')
    monkeypatch.setattr(notification.firebase_admin, "get_app", lambda: (_ for _ in ()).throw(ValueError()))

    with caplog.at_level(logging.ERROR):
        assert notification._get_firebase_app() is None
    assert "FIREBASE_CREDENTIALS_JSON" in caplog.text and "secret-material" not in caplog.text


def test_prepare_volume_creates_the_database_and_backup_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "backup_dir", None)
    url = f"sqlite:///{(tmp_path / 'data' / 'schreduler.db').as_posix()}"

    folders = prepare_volume.prepare(url)

    assert (tmp_path / "data").is_dir() and (tmp_path / "data" / "backups").is_dir()
    assert len(folders) == 2
    assert prepare_volume.prepare("postgresql+psycopg://u:p@db/schreduler") == []
