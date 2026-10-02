"""Checks that run before every request.

- Origin check (CSRF): a POST/PUT/PATCH/DELETE whose Origin (or, without one, Referer) is another site is refused.
  Requests with neither header are let through — browsers always send Origin on cross-site writes, and non-browser
  clients (curl, the mobile app) send neither. SameSite=Lax on the session cookie is the second layer.
- Old clients that still send user_id in the query or JSON body get 422 instead of having it silently ignored.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from app.core.config import settings
from app.i18n import accept_language, render_message

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _origin(url: str) -> str | None:
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def origin_allowed(request: Request) -> bool:
    header = request.headers.get("origin")
    if header is None:
        referer = request.headers.get("referer")
        if referer is None:
            return True
        header = referer
    origin = _origin(header)  # "null" (sandboxed frames, file://) has no scheme and is refused
    if origin is None:
        return False
    if settings.allowed_origins:
        return origin in settings.allowed_origins
    return urlsplit(origin).netloc == request.headers.get("host", "").lower()


async def check_origin(request: Request, call_next: RequestResponseEndpoint) -> Response:
    if request.method in UNSAFE_METHODS and not origin_allowed(request):
        language = accept_language(request.headers.get("accept-language"))
        return JSONResponse(status_code=403, content={"detail": render_message("auth.bad_origin", language)})
    return await call_next(request)


def _user_id_error(location: str, language: str) -> RequestValidationError:
    return RequestValidationError(
        [{"loc": [location, "user_id"], "msg": render_message("request.user_id_not_allowed", language), "type": "user_id_not_allowed"}]
    )


async def reject_user_id(request: Request) -> None:
    language = accept_language(request.headers.get("accept-language"))
    if "user_id" in request.query_params:
        raise _user_id_error("query", language)
    if request.method in UNSAFE_METHODS and "json" in request.headers.get("content-type", ""):
        try:
            body = json.loads(await request.body() or b"null")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return  # left to the normal body validation
        if isinstance(body, dict) and "user_id" in body:
            raise _user_id_error("body", language)


def install_request_guards(app: FastAPI) -> None:
    app.middleware("http")(check_origin)
