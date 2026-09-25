from __future__ import annotations

from fastapi import Depends

from app.core.auth import get_current_user
from app.i18n import Language, to_language
from app.models.user import User


def get_response_language(user: User = Depends(get_current_user)) -> Language:
    """현재 사용자의 응답 언어 (FR-11). User.preferred_language를 따르고, 지원하지 않는 값이면 ko.

    같은 요청에서 get_current_user를 함께 써도 FastAPI가 의존성 결과를 캐시하므로 사용자 조회는 한 번이다.
    """
    return to_language(user.preferred_language)
