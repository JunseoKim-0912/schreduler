from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.core.auth import clear_session_cookie, get_current_user, session_cookie, set_session_cookie
from app.core.clock import utc_now_naive
from app.core.config import settings
from app.core.db import get_db
from app.core.exceptions import NotFoundError
from app.core.request_guards import client_ip
from app.i18n import accept_language
from app.models.user import User
from app.schemas.auth import LoginRequest, MeRead, SignupRequest
from app.services import auth_service, demo_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/signup",
    response_model=MeRead,
    status_code=status.HTTP_201_CREATED,
    summary="회원가입",
    responses={403: {"description": "가입이 닫혀 있거나(SIGNUP_MODE=closed) 초대 코드가 틀림"}, 409: {"description": "이미 가입된 이메일"}},
)
def signup(data: SignupRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> User:
    """`SIGNUP_MODE`가 `invite`면 `invite_code`가 `INVITE_CODE`와 같아야 한다. 가입하면 바로 로그인된다(세션 쿠키)."""
    user, token = auth_service.signup(
        db, data.email, data.password, data.invite_code, accept_language(request.headers.get("accept-language"))
    )
    set_session_cookie(response, token)
    return user


@router.post(
    "/login",
    response_model=MeRead,
    summary="로그인",
    responses={
        401: {"description": "이메일 또는 비밀번호가 틀림 (어느 쪽인지는 알려주지 않는다)"},
        429: {"description": "10분 안에 같은 이메일로(TRUST_PROXY_HEADERS=true면 같은 IP로도) 10회 실패"},
    },
)
def login(data: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> User:
    """성공하면 HttpOnly 세션 쿠키(30일)를 설정한다."""
    user, token = auth_service.login(
        db, data.email, data.password, client_ip(request), accept_language(request.headers.get("accept-language"))
    )
    set_session_cookie(response, token)
    return user


@router.post(
    "/demo",
    response_model=MeRead,
    status_code=status.HTTP_201_CREATED,
    summary="데모 계정으로 시작",
    responses={
        404: {"description": "데모 모드가 꺼져 있음 (DEMO_MODE_ENABLED=false)"},
        429: {"description": "최근 1시간 동안 데모 계정이 DEMO_MAX_CREATIONS_PER_HOUR개 만들어짐"},
    },
)
def create_demo_account(request: Request, response: Response, db: Session = Depends(get_db)) -> User:
    """이메일·비밀번호 없는 임시 계정을 만들고 예시 일정(대학생 일주일)을 채운 뒤 바로 로그인시킨다. 24시간 뒤 계정과 데이터가
    모두 지워지고, 그때까지는 같은 쿠키로 다시 들어올 수 있다. LLM은 DEMO_LLM_BUDGET_* 한도를 따로 쓴다."""
    if not settings.demo_mode_enabled:
        raise NotFoundError("Not Found")
    user, token = demo_service.create_demo_user(db, accept_language(request.headers.get("accept-language")))
    remaining = (user.demo_expires_at or utc_now_naive()) - utc_now_naive()
    set_session_cookie(response, token, max(int(remaining.total_seconds()), 0))
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="로그아웃")
def logout(response: Response, token: str | None = Depends(session_cookie), db: Session = Depends(get_db)) -> None:
    """이 브라우저의 세션을 서버에서 지우고 쿠키를 없앤다. 로그인하지 않았어도 204."""
    if token:
        auth_service.end_session(db, token)
    clear_session_cookie(response)


@router.get("/me", response_model=MeRead, summary="내 계정", responses={401: {"description": "로그인하지 않음"}})
def get_me(user: User = Depends(get_current_user)) -> User:
    """로그인한 계정. 웹 화면은 시작할 때 이것으로 로그인 여부를 확인하고, 401이면 로그인 화면을 보여준다."""
    return user
