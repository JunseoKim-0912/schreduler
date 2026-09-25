"""도메인 예외와 HTTP 응답 매핑.

서비스는 HTTP를 모르고 아래 예외만 던진다. 라우터마다 try/except로 상태 코드를 붙이지 않고
register_exception_handlers가 한 곳에서 변환한다.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code: int = status.HTTP_400_BAD_REQUEST
    # 클라이언트 잘못(4xx)은 INFO, 외부 연동·설정 문제는 하위 클래스에서 WARNING/ERROR로 올린다.
    log_level: int = logging.INFO


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT


class InvalidInputError(AppError, ValueError):
    """요청 값끼리는 맞지만 기존 데이터와 합쳤을 때 규칙에 어긋나는 경우 등.

    ValueError이기도 해서 Pydantic validator 안에서 던지면 일반 422 검증 오류로 바뀐다.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT


def _error_response(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    logger.log(
        exc.log_level,
        "%s %s -> %s %s: %s",
        request.method,
        request.url.path,
        exc.status_code,
        type(exc).__name__,
        exc,
    )
    return _error_response(exc.status_code, str(exc))


async def _handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
    # 서비스의 사전 검사를 빠져나간 제약 위반(동시 요청, FK 등). 원문에는 SQL이 섞여 있어 응답에는 싣지 않는다.
    logger.warning("%s %s -> 409 (DB 제약 위반): %s", request.method, request.url.path, exc.orig)
    return _error_response(status.HTTP_409_CONFLICT, "요청이 기존 데이터와 충돌합니다 (DB 제약 위반)")


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("%s %s -> 500 처리되지 않은 예외", request.method, request.url.path)
    return _error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, "서버 내부 오류가 발생했습니다")


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(IntegrityError, _handle_integrity_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
