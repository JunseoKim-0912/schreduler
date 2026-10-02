"""Who is making the request: the server-side session behind the HttpOnly cookie. There is no other way in."""

from __future__ import annotations

from fastapi import Depends, Request, Response
from fastapi.security import APIKeyCookie
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.i18n import accept_language, render_message
from app.models.user import User
from app.services import auth_service
from app.services.auth_service import ForbiddenError, UnauthorizedError

SESSION_COOKIE = "schreduler_session"

session_cookie = APIKeyCookie(name=SESSION_COOKIE, auto_error=False, description="Set by POST /auth/login or /auth/signup")


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(auth_service.SESSION_TTL.total_seconds()),
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax", secure=settings.session_cookie_secure)


def get_current_user(request: Request, token: str | None = Depends(session_cookie), db: Session = Depends(get_db)) -> User:
    user = auth_service.user_for_token(db, token) if token else None
    if user is None:
        raise UnauthorizedError(render_message("auth.not_signed_in", accept_language(request.headers.get("accept-language"))))
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise ForbiddenError(render_message("auth.admin_only", user.preferred_language))
    return user
