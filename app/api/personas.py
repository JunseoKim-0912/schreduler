from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.exceptions import NotFoundError
from app.core.openapi import CONFLICT, NOT_FOUND
from app.models.persona import Persona
from app.schemas.persona import PersonaCreate, PersonaRead, PersonaUpdate
from app.services import persona_service

router = APIRouter(prefix="/personas", tags=["personas"])


@router.post("", response_model=PersonaRead, status_code=status.HTTP_201_CREATED, summary="페르소나 생성", responses=CONFLICT)
def create_persona(data: PersonaCreate, db: Session = Depends(get_db)) -> Persona:
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


@router.put("/{name}", response_model=PersonaRead, summary="페르소나 수정", responses=NOT_FOUND)
def update_persona(name: str, data: PersonaUpdate, db: Session = Depends(get_db)) -> Persona:
    """보낸 필드만 수정한다. `display_name`·`description`은 null로 지울 수 없다."""
    persona = persona_service.update_persona(db, name, data)
    if persona is None:
        raise NotFoundError("Persona not found")
    return persona


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT, summary="페르소나 삭제", responses={**NOT_FOUND, **CONFLICT})
def delete_persona(name: str, db: Session = Depends(get_db)) -> None:
    """선택한 사용자나 대화 기록이 있으면 409로 거부한다."""
    deleted = persona_service.delete_persona(db, name)
    if not deleted:
        raise NotFoundError("Persona not found")
