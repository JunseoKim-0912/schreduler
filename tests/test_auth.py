"""Sign-up, sign-in, sessions, the Origin check and the user_id guard."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import SESSION_COOKIE
from app.core.config import settings
from app.core.db import get_db
from app.main import app
from app.models import Base, User, UserSession

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def signup_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "signup_mode", "invite")
    monkeypatch.setattr(settings, "invite_code", "spring-2026")
    monkeypatch.setattr(settings, "allowed_origins", ())
    monkeypatch.setattr(settings, "session_cookie_secure", False)


@pytest.fixture
def engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def client(engine):
    local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _signup(client: TestClient, email: str = "June@Example.com", password: str = PASSWORD, code: str | None = "spring-2026"):
    return client.post("/auth/signup", json={"email": email, "password": password, "invite_code": code})


def _login(client: TestClient, email: str = "june@example.com", password: str = PASSWORD, **kwargs):
    return client.post("/auth/login", json={"email": email, "password": password}, **kwargs)


# --- sign-up ---------------------------------------------------------------------------------


def test_signup_with_invite_code_signs_in_and_stores_only_a_hash(client: TestClient, engine) -> None:
    response = _signup(client)

    assert response.status_code == 201
    assert response.json()["email"] == "june@example.com"  # lowercased
    cookie = response.headers["set-cookie"]
    assert f"{SESSION_COOKIE}=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" not in cookie
    assert client.get("/auth/me").json()["email"] == "june@example.com"
    with Session(engine) as session:
        user = session.execute(select(User)).scalar_one()
        assert user.password_hash.startswith("$argon2id$") and PASSWORD not in user.password_hash
        stored = session.execute(select(UserSession.token_hash)).scalar_one()
        assert stored not in cookie  # only the SHA-256 is stored


def test_secure_flag_follows_the_setting(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "session_cookie_secure", True)
    assert "Secure" in _signup(client).headers["set-cookie"]


@pytest.mark.parametrize("code", [None, "", "wrong", "spring-2026x"])
def test_wrong_invite_code_is_refused(client: TestClient, engine, code: str | None) -> None:
    response = _signup(client, code=code)

    assert response.status_code == 403
    with Session(engine) as session:
        assert session.execute(select(User)).first() is None


def test_closed_mode_refuses_everyone_even_with_the_code(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "signup_mode", "closed")
    assert _signup(client).status_code == 403


def test_open_mode_needs_no_code(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "signup_mode", "open")
    assert _signup(client, code=None).status_code == 201


def test_invite_mode_without_a_configured_code_lets_nobody_in(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "invite_code", None)
    assert _signup(client, code="").status_code == 403


def test_duplicate_email_is_a_conflict_regardless_of_case(client: TestClient) -> None:
    _signup(client)
    assert _signup(TestClient(app), email="JUNE@example.com ").status_code == 409


@pytest.mark.parametrize(("email", "password"), [("june@example.com", "short7!"), ("not-an-email", PASSWORD), ("a@b", PASSWORD)])
def test_signup_validates_email_and_password_length(client: TestClient, email: str, password: str) -> None:
    assert _signup(client, email=email, password=password).status_code == 422


def test_unknown_signup_mode_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.auth_service import validate_signup_settings

    monkeypatch.setattr(settings, "signup_mode", "public")
    with pytest.raises(RuntimeError, match="SIGNUP_MODE"):
        validate_signup_settings()


# --- sign-in / sign-out / expiry ----------------------------------------------------------------


def test_login_logout_and_login_again(client: TestClient, engine) -> None:
    _signup(client)
    client.cookies.clear()
    assert client.get("/auth/me").status_code == 401

    response = _login(client, email="  JUNE@example.com")
    assert response.status_code == 200
    assert response.json()["last_login_at"] is not None
    assert client.get("/auth/me").status_code == 200
    token = client.cookies.get(SESSION_COOKIE)

    assert client.post("/auth/logout").status_code == 204
    assert client.get("/auth/me").status_code == 401
    # the old cookie is dead on the server too, not just forgotten by this browser
    assert client.get("/auth/me", headers={"Cookie": f"{SESSION_COOKIE}={token}"}).status_code == 401
    with Session(engine) as session:
        # only the sign-up session (whose cookie was cleared, not logged out) is left
        assert len(session.execute(select(UserSession)).all()) == 1

    assert _login(client).status_code == 200
    assert client.get("/auth/me").status_code == 200


def test_logout_without_a_session_is_fine(client: TestClient) -> None:
    assert client.post("/auth/logout").status_code == 204


def test_wrong_password_and_unknown_email_get_the_same_answer(client: TestClient) -> None:
    _signup(client)
    client.cookies.clear()
    wrong_password = _login(client, password="not the password")
    unknown_email = _login(client, email="nobody@example.com")

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json() == {"detail": "이메일 또는 비밀번호가 틀렸어요."}
    english = _login(client, password="nope", headers={"Accept-Language": "en-US,en;q=0.9"})
    assert english.json() == {"detail": "The email or password is incorrect."}


def test_account_without_a_password_cannot_sign_in(client: TestClient, engine) -> None:
    with Session(engine) as session:
        session.add(User(name="Old", email="old@example.com"))
        session.commit()
    assert _login(client, email="old@example.com", password="").status_code == 401


def test_session_expires_after_30_days(client: TestClient) -> None:
    with freeze_time("2026-10-01 12:00:00") as frozen:
        _signup(client)
        frozen.tick(timedelta(days=29, hours=23))
        assert client.get("/auth/me").status_code == 200
        frozen.tick(timedelta(hours=1))
        assert client.get("/auth/me").status_code == 401


def test_password_is_never_logged(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        _signup(client)
        _login(client, password=PASSWORD + " typo")
        _login(client)
    assert PASSWORD not in caplog.text


# --- lockout ---------------------------------------------------------------------------------


def test_ten_failures_for_one_email_lock_it_for_a_while(client: TestClient) -> None:
    with freeze_time("2026-10-01 12:00:00") as frozen:
        _signup(client)
        for _ in range(10):
            assert _login(client, password="wrong password").status_code == 401

        locked = _login(client)  # even the right password
        assert locked.status_code == 429
        assert 0 < locked.json()["retry_after_seconds"] <= 600

        frozen.tick(timedelta(minutes=10, seconds=1))
        assert _login(client).status_code == 200


def test_ten_failures_from_one_ip_lock_other_emails_too(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _signup(client)
    for index in range(10):
        _login(client, email=f"guess{index}@example.com")
    assert _login(client).status_code == 429


def test_failures_below_the_limit_do_not_lock(client: TestClient) -> None:
    _signup(client)
    for _ in range(9):
        _login(client, password="wrong password")
    assert _login(client).status_code == 200


# --- not signed in --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/events"), ("GET", "/tasks"), ("POST", "/assistant/chat"), ("GET", "/usage/today"), ("GET", "/personas"), ("GET", "/points/summary")],
)
def test_signed_out_requests_get_401_and_the_old_header_does_nothing(client: TestClient, engine, method: str, path: str) -> None:
    with Session(engine) as session:
        session.add(User(name="June"))
        session.commit()
    assert client.request(method, path).status_code == 401
    assert client.request(method, path, headers={"X-User-Id": "1"}).status_code == 401


def test_health_needs_no_sign_in(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


# --- Origin check (CSRF) ------------------------------------------------------------------------


def test_cross_site_writes_are_refused(client: TestClient) -> None:
    _signup(client)

    evil = client.post("/tasks", json={"title": "x", "end_time": "2026-10-03T12:00:00"}, headers={"Origin": "https://evil.example"})
    referer = client.post("/tasks", json={"title": "x", "end_time": "2026-10-03T12:00:00"}, headers={"Referer": "https://evil.example/page"})
    null_origin = client.post("/auth/logout", headers={"Origin": "null"})

    assert evil.status_code == referer.status_code == null_origin.status_code == 403
    assert client.get("/tasks").json() == []
    assert client.get("/auth/me").status_code == 200  # the logout was refused too


def test_same_site_and_headerless_writes_pass(client: TestClient) -> None:
    _signup(client)
    body = {"title": "x", "end_time": "2026-10-03T12:00:00"}

    assert client.post("/tasks", json=body, headers={"Origin": "http://testserver"}).status_code == 201
    assert client.post("/tasks", json=body, headers={"Referer": "http://testserver/app/"}).status_code == 201
    assert client.post("/tasks", json=body).status_code == 201  # curl, the mobile app


def test_allowed_origins_setting_replaces_the_same_host_rule(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _signup(client)
    monkeypatch.setattr(settings, "allowed_origins", ("https://schreduler.example.com",))
    body = {"title": "x", "end_time": "2026-10-03T12:00:00"}

    assert client.post("/tasks", json=body, headers={"Origin": "https://schreduler.example.com"}).status_code == 201
    assert client.post("/tasks", json=body, headers={"Origin": "http://testserver"}).status_code == 403


def test_signup_and_login_are_protected_too(client: TestClient) -> None:
    assert _signup(client).status_code == 201
    client.cookies.clear()
    assert _login(client, headers={"Origin": "https://evil.example"}).status_code == 403


def test_reads_are_not_origin_checked(client: TestClient) -> None:
    _signup(client)
    assert client.get("/tasks", headers={"Origin": "https://evil.example"}).status_code == 200


# --- old clients sending user_id ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/events", {"user_id": 1, "title": "x", "start_time": "2026-10-02T09:00:00", "end_time": "2026-10-02T10:00:00"}),
        ("POST", "/locations", {"user_id": 1, "name": "Home", "default_travel_minutes": 5}),
        ("POST", "/daily-actual-logs/checkin", {"user_id": 1, "utterance": "hi"}),
        ("PUT", "/users/me/language", {"user_id": 2, "language": "en"}),
    ],
)
def test_user_id_in_the_body_is_rejected(client: TestClient, method: str, path: str, body: dict) -> None:
    _signup(client)
    response = client.request(method, path, json=body)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "user_id"]


@pytest.mark.parametrize("path", ["/events?user_id=1", "/locations?user_id=2", "/compliance-reports/stats?user_id=1&days=7"])
def test_user_id_in_the_query_is_rejected(client: TestClient, path: str) -> None:
    _signup(client)
    response = client.get(path)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "user_id"]
