from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.db import get_db
from app.core.exceptions import NotFoundError
from app.core.openapi import CURRENT_USER
from app.models.persona_conversation import PersonaConversation
from app.models.user import User
from app.schemas.persona import PersonaConversationRead, PersonaRead, UserPersonaRead, UserPersonaSelect
from app.services import persona_conversation_service, persona_service

router = APIRouter(prefix="/users/me", tags=["users"])


def _to_user_persona_read(user: User) -> UserPersonaRead:
    selected = PersonaRead.model_validate(user.selected_persona) if user.selected_persona else None
    return UserPersonaRead(user_id=user.id, selected_persona=selected)


@router.get("/persona", response_model=UserPersonaRead, summary="내 페르소나 조회", responses=CURRENT_USER)
def get_my_persona(user: User = Depends(get_current_user)) -> UserPersonaRead:
    """현재 선택한 페르소나. 선택하지 않았으면 `selected_persona`가 null."""
    return _to_user_persona_read(user)


@router.put("/persona", response_model=UserPersonaRead, summary="내 페르소나 선택/해제", responses=CURRENT_USER)
def select_my_persona(
    data: UserPersonaSelect,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserPersonaRead:
    """`persona_name`에 null을 보내면 선택을 해제한다. 선택한 페르소나는 체크인·미준수 피드백 LLM 응답의 말투에 쓰인다."""
    user = persona_service.select_persona_for_user(db, user, data.persona_name)
    return _to_user_persona_read(user)


@router.get("/persona-conversations", response_model=list[PersonaConversationRead], summary="내 페르소나 대화 목록", responses=CURRENT_USER)
def list_my_persona_conversations(
    context_type: str | None = Query(default=None, description="대화 종류로 거르기 (예: daily_checkin)"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[PersonaConversation]:
    """최신순. `context_type`(예: `daily_checkin`)으로 거를 수 있다."""
    return persona_conversation_service.list_conversations(db, user.id, context_type)


@router.get("/persona-conversations/{conversation_id}", response_model=PersonaConversationRead, summary="내 페르소나 대화 조회", responses=CURRENT_USER)
def get_my_persona_conversation(
    conversation_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PersonaConversation:
    """대화 하나의 전체 메시지. 다른 사용자의 대화는 404."""
    conversation = persona_conversation_service.get_conversation(db, user.id, conversation_id)
    if conversation is None:
        raise NotFoundError("Conversation not found")
    return conversation
