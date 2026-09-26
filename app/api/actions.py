from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.db import get_db
from app.core.openapi import CONFLICT, CURRENT_USER
from app.models.action_history import ActionHistory
from app.models.user import User
from app.schemas.action import ActionRead
from app.services import action_history_service

router = APIRouter(prefix="/actions", tags=["actions"])


@router.get("", response_model=list[ActionRead], summary="최근 변경 기록", responses=CURRENT_USER)
def list_actions(
    limit: int = Query(default=10, ge=1, le=100, description="가져올 기록 수 (최신순)"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ActionHistory]:
    """자연어로 실행한 생성·삭제·수정과 목록의 삭제 버튼 기록을 최신순으로 돌려준다. `undone`이 true면 이미 되돌린 기록."""
    return action_history_service.list_actions(db, user.id, limit)


@router.post("/{action_id}/undo", response_model=ActionRead, summary="변경 되돌리기", responses={**CURRENT_USER, **CONFLICT})
def undo_action(action_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> ActionHistory:
    """기록된 변경을 되돌린다.

    - 삭제는 원래 ID 그대로 다시 넣고(회차·하위 일정·미준수 사유 포함), 수정은 이전 값으로, 생성은 만든 일정을 지운다.
    - 과거 날짜 회차가 영향을 받았으면 그날부터 어제까지 포인트 원장을 다시 계산한다.
    - 이미 되돌린 기록이거나, 같은 일정을 건드린 더 최근 기록이 아직 남아 있으면 409. 되돌리기 자체는 다시 되돌릴 수 없다.
    """
    return action_history_service.undo_action(db, user, action_id)
