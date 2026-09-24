from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.db import get_db
from app.models.persona_conversation import PersonaConversation
from app.models.user import User
from app.schemas.persona import PersonaConversationRead, PersonaRead, UserPersonaRead, UserPersonaSelect
from app.services import persona_conversation_service, persona_service

router = APIRouter(prefix="/users/me", tags=["users"])


def _to_user_persona_read(user: User) -> UserPersonaRead:
    selected = PersonaRead.model_validate(user.selected_persona) if user.selected_persona else None
    return UserPersonaRead(user_id=user.id, selected_persona=selected)


@router.get("/persona", response_model=UserPersonaRead)
def get_my_persona(user: User = Depends(get_current_user)) -> UserPersonaRead:
    return _to_user_persona_read(user)


@router.put("/persona", response_model=UserPersonaRead)
def select_my_persona(
    data: UserPersonaSelect,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserPersonaRead:
    try:
        user = persona_service.select_persona_for_user(db, user, data.persona_name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _to_user_persona_read(user)


@router.get("/persona-conversations", response_model=list[PersonaConversationRead])
def list_my_persona_conversations(
    context_type: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[PersonaConversation]:
    return persona_conversation_service.list_conversations(db, user.id, context_type)


@router.get("/persona-conversations/{conversation_id}", response_model=PersonaConversationRead)
def get_my_persona_conversation(
    conversation_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PersonaConversation:
    conversation = persona_conversation_service.get_conversation(db, user.id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return conversation
