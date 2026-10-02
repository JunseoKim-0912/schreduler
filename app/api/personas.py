from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_admin
from app.core.clock import local_today
from app.core.db import get_db
from app.core.exceptions import NotFoundError
from app.core.openapi import CONFLICT, CURRENT_USER, NOT_FOUND
from app.models.persona import Persona
from app.models.persona_conversation import PersonaConversation
from app.models.user import User
from app.schemas.persona import PersonaConversationRead, PersonaCreate, PersonaRead, PersonaUpdate
from app.services import persona_conversation_service, persona_service
from app.services.daily_checkin import DAILY_CHECKIN_CONTEXT_TYPE

router = APIRouter(prefix="/personas", tags=["personas"])

# 대화 상황 (쿼리 값 → 저장되는 context_type)
_CONTEXTS = {"checkin": DAILY_CHECKIN_CONTEXT_TYPE}
ConversationContext = Literal["checkin"]


def _persona_or_404(db: Session, persona_id: str) -> Persona:
    persona = persona_service.get_persona(db, persona_id)
    if persona is None:
        raise NotFoundError(f"persona {persona_id} does not exist")
    return persona


@router.get(
    "/{persona_id}/conversations/current",
    response_model=PersonaConversationRead | None,
    summary="이 페르소나와의 오늘 대화",
    responses=CURRENT_USER,
)
def get_current_conversation(
    persona_id: str,
    context: ConversationContext = Query(default="checkin", description="대화 상황"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PersonaConversation | None:
    """(현재 사용자, 이 페르소나, 상황, 오늘)의 대화. 그날 [새 대화]를 눌렀으면 가장 최근 대화. 아직 없으면 `null`.

    탭을 옮겼다 오거나 페르소나를 바꿨다 돌아와도 이 대화를 불러와 이어 간다. 날짜가 바뀌면 새 대화다.
    """
    _persona_or_404(db, persona_id)
    return persona_conversation_service.current_conversation(db, user.id, persona_id, _CONTEXTS[context], local_today())


@router.post(
    "/{persona_id}/conversations",
    response_model=PersonaConversationRead,
    status_code=status.HTTP_201_CREATED,
    summary="이 페르소나와 오늘 새 대화 시작",
    responses=CURRENT_USER,
)
def start_conversation(
    persona_id: str,
    context: ConversationContext = Query(default="checkin", description="대화 상황"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PersonaConversation:
    """[새 대화]: 오늘의 새 대화를 시작한다. 이전 대화는 지우지 않고 `/users/me/persona-conversations`에 남는다."""
    _persona_or_404(db, persona_id)
    return persona_conversation_service.start_conversation(db, user, persona_id, _CONTEXTS[context], local_today())


ADMIN_ONLY: dict[int | str, dict[str, str]] = {403: {"description": "관리자(`is_admin`)만 가능"}}


@router.post("", response_model=PersonaRead, status_code=status.HTTP_201_CREATED, summary="페르소나 생성 (관리자)", responses={**CONFLICT, **ADMIN_ONLY})
def create_persona(data: PersonaCreate, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> Persona:
    """코드 수정 없이 페르소나를 추가한다 (FR-9). `display_name`·`description`은 ko/en이 모두 필요하다."""
    return persona_service.create_persona(db, data)


@router.get("", response_model=list[PersonaRead], summary="페르소나 목록")
def list_personas(db: Session = Depends(get_db)) -> list[Persona]:
    """모든 페르소나를 name 순으로 돌려준다."""
    return persona_service.list_personas(db)


@router.get("/{name}", response_model=PersonaRead, summary="페르소나 조회", responses=NOT_FOUND)
def get_persona(name: str, db: Session = Depends(get_db)) -> Persona:
    """페르소나 하나를 조회한다."""
    persona = persona_service.get_persona(db, name)
    if persona is None:
        raise NotFoundError("Persona not found")
    return persona


@router.put("/{name}", response_model=PersonaRead, summary="페르소나 수정 (관리자)", responses={**NOT_FOUND, **ADMIN_ONLY})
def update_persona(name: str, data: PersonaUpdate, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> Persona:
    """보낸 필드만 수정한다. `display_name`·`description`은 null로 지울 수 없다."""
    persona = persona_service.update_persona(db, name, data)
    if persona is None:
        raise NotFoundError("Persona not found")
    return persona


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT, summary="페르소나 삭제 (관리자)", responses={**NOT_FOUND, **CONFLICT, **ADMIN_ONLY})
def delete_persona(name: str, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> None:
    """선택한 사용자나 대화 기록이 있으면 409로 거부한다."""
    deleted = persona_service.delete_persona(db, name)
    if not deleted:
        raise NotFoundError("Persona not found")
