"""Signed-in requests for API tests.

Each test client overrides get_db; these helpers open a session through that override, start a real server-side
session for the user and hand back the cookie — the same path a browser takes after POST /auth/login.

    client.get("/tasks", headers=as_user(user_id))   # one request as this user
    sign_in(client, user_id)                          # every later request on this client
    client.post("/personas", json=..., headers=as_admin())
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import cast

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import SESSION_COOKIE
from app.core.db import get_db
from app.main import app
from app.models.user import User
from app.services import auth_service

ADMIN_EMAIL = "admin@test.local"


def _with_db() -> tuple[Session, Iterator[Session]]:
    override = cast(Callable[[], Iterator[Session]], app.dependency_overrides[get_db])
    sessions = override()
    return next(sessions), sessions


def session_token(user_id: int) -> str:
    db, sessions = _with_db()
    try:
        user = db.get(User, user_id)
        assert user is not None, f"user {user_id} does not exist"
        token = auth_service.start_session(db, user)
        db.commit()
        return token
    finally:
        sessions.close()


def as_admin() -> dict[str, str]:
    """Cookie of an admin (created on first use) — persona create/update/delete need one."""
    db, sessions = _with_db()
    try:
        admin = db.execute(select(User).where(User.email == ADMIN_EMAIL)).scalar_one_or_none()
        if admin is None:
            admin = User(name="Admin", email=ADMIN_EMAIL, is_admin=True)
            db.add(admin)
            db.commit()
        admin_id = admin.id
    finally:
        sessions.close()
    return as_user(admin_id)


def as_user(user_id: int) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE}={session_token(user_id)}"}


def sign_in(client: TestClient, user_id: int) -> TestClient:
    client.cookies.set(SESSION_COOKIE, session_token(user_id))
    return client
