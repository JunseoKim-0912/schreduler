"""Accounts, passwords and server-side sessions.

Passwords are hashed with argon2 and never logged. The session cookie holds a random token; only its SHA-256 is
stored, so a copy of the database can't be used to sign in.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import secrets
from datetime import datetime, timedelta
from functools import cache
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.clock import utc_now_naive
from app.core.config import settings
from app.core.exceptions import AppError, ConflictError
from app.i18n import Language, render_message
from app.models.auth import LoginFailure, UserSession
from app.models.user import User

logger = logging.getLogger(__name__)

SESSION_TTL = timedelta(days=30)
LOCKOUT_WINDOW = timedelta(minutes=10)
MAX_FAILURES = 10
SIGNUP_MODES = ("closed", "invite", "open")

_hasher = PasswordHasher()


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN


class TooManyLoginFailures(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS

    def __init__(self, message: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds

    def response_fields(self) -> dict[str, Any]:
        return {"retry_after_seconds": self.retry_after_seconds}


def validate_signup_settings() -> None:
    """Called at startup: an unknown SIGNUP_MODE, or invite mode without a code, is a configuration mistake."""
    if settings.signup_mode not in SIGNUP_MODES:
        raise RuntimeError(f"SIGNUP_MODE={settings.signup_mode!r} — use one of {', '.join(SIGNUP_MODES)}")
    if settings.signup_mode == "invite" and not settings.invite_code:
        logger.warning("[auth] SIGNUP_MODE=invite but INVITE_CODE is empty, so nobody can sign up")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


@cache
def _dummy_hash() -> str:
    return _hasher.hash(secrets.token_hex(16))


def _password_matches(password_hash: str | None, password: str) -> bool:
    try:
        # Unknown emails are checked against a throwaway hash so both cases take the same time.
        return _hasher.verify(password_hash or _dummy_hash(), password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- sessions ---------------------------------------------------------------------------------


def session_expires_at(user: User, now: datetime) -> datetime:
    """A demo account's session ends with the account; everyone else gets SESSION_TTL."""
    if user.is_demo and user.demo_expires_at is not None:
        return min(user.demo_expires_at, now + SESSION_TTL)
    return now + SESSION_TTL


def start_session(db: Session, user: User, now: datetime | None = None) -> str:
    """New session for this user; returns the cookie token. Also drops the user's expired sessions."""
    now = now or utc_now_naive()
    db.execute(delete(UserSession).where(UserSession.user_id == user.id, UserSession.expires_at <= now))
    token = secrets.token_urlsafe(32)
    db.add(UserSession(user_id=user.id, token_hash=_token_hash(token), created_at=now, expires_at=session_expires_at(user, now)))
    return token


def user_for_token(db: Session, token: str, now: datetime | None = None) -> User | None:
    now = now or utc_now_naive()
    session = db.execute(
        select(UserSession).where(UserSession.token_hash == _token_hash(token), UserSession.expires_at > now)
    ).scalar_one_or_none()
    if session is None:
        return None
    user = session.user
    # Between expiry and the hourly cleanup an expired demo account still exists; it must not be usable.
    if user.is_demo and (user.demo_expires_at is None or user.demo_expires_at <= now):
        return None
    return user


def end_session(db: Session, token: str) -> None:
    db.execute(delete(UserSession).where(UserSession.token_hash == _token_hash(token)))
    db.commit()


# --- sign-up / sign-in --------------------------------------------------------------------------


def signup(db: Session, email: str, password: str, invite_code: str | None, language: Language) -> tuple[User, str]:
    if settings.signup_mode == "closed":
        raise ForbiddenError(render_message("auth.signup_closed", language))
    if settings.signup_mode == "invite" and not (
        settings.invite_code and invite_code and hmac.compare_digest(invite_code.strip(), settings.invite_code)
    ):
        raise ForbiddenError(render_message("auth.invite_invalid", language))
    email = normalize_email(email)
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise ConflictError(render_message("auth.email_taken", language))
    now = utc_now_naive()
    user = User(
        name=email.split("@")[0][:100],
        email=email,
        password_hash=hash_password(password),
        preferred_language=language,
        created_at=now,
        last_login_at=now,
    )
    db.add(user)
    db.flush()
    token = start_session(db, user, now)
    db.commit()
    logger.info("[auth] signed up user_id=%s", user.id)
    return user, token


def _failures_since(db: Session, column: Any, value: str, since: datetime) -> list[datetime]:
    return list(
        db.execute(select(LoginFailure.created_at).where(column == value, LoginFailure.created_at > since).order_by(LoginFailure.created_at)).scalars()
    )


def _check_lockout(db: Session, email: str, ip: str | None, now: datetime, language: Language) -> None:
    since = now - LOCKOUT_WINDOW
    counted = [_failures_since(db, LoginFailure.email, email, since)]
    if ip is not None:
        counted.append(_failures_since(db, LoginFailure.ip, ip, since))
    for times in counted:
        if len(times) >= MAX_FAILURES:
            # Opens again once enough failures have aged out of the window.
            reopens = times[len(times) - MAX_FAILURES] + LOCKOUT_WINDOW
            seconds = max(int((reopens - now).total_seconds()), 1)
            raise TooManyLoginFailures(render_message("auth.too_many_attempts", language, minutes=math.ceil(seconds / 60)), seconds)


def login(db: Session, email: str, password: str, ip: str | None, language: Language) -> tuple[User, str]:
    """One message for every failure (unknown email, wrong password, no password yet) so accounts can't be probed.
    After 10 failures within 10 minutes for the same email — or the same IP, when the IP is known (ip is None behind
    an untrusted proxy) — further attempts are refused for a while."""
    now = utc_now_naive()
    email = normalize_email(email)
    db.execute(delete(LoginFailure).where(LoginFailure.created_at <= now - LOCKOUT_WINDOW))
    _check_lockout(db, email, ip, now, language)

    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None or not _password_matches(user.password_hash, password):
        # "-" never matches a real address, so an unknown IP is never counted.
        db.add(LoginFailure(email=email, ip=ip or "-", created_at=now))
        db.commit()
        logger.info("[auth] sign-in failed ip=%s", ip or "-")
        raise UnauthorizedError(render_message("auth.login_failed", language))

    if _hasher.check_needs_rehash(user.password_hash or ""):
        user.password_hash = hash_password(password)
    user.last_login_at = now
    token = start_session(db, user, now)
    db.commit()
    logger.info("[auth] signed in user_id=%s", user.id)
    return user, token


def set_password(db: Session, user: User, password: str) -> None:
    """Sets a new password and signs the user out everywhere (create_admin's reset). Demo accounts never get one."""
    if user.is_demo:
        raise ForbiddenError(render_message("demo.account_locked", user.preferred_language))
    user.password_hash = hash_password(password)
    db.execute(delete(UserSession).where(UserSession.user_id == user.id))

