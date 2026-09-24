from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models.user import User


def get_current_user(
    x_user_id: int | None = Header(default=None), db: Session = Depends(get_db)
) -> User:
    # 인증이 아직 없어서 X-User-Id 헤더를 현재 사용자로 신뢰한다. 인증 도입 시 이 함수만 교체한다.
    if x_user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="X-User-Id header is required")
    user = db.get(User, x_user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"user_id {x_user_id} does not exist")
    return user
