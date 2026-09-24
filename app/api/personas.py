from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models.persona import Persona
from app.schemas.persona import PersonaCreate, PersonaRead, PersonaUpdate
from app.services import persona_service
from app.services.persona_service import PersonaAlreadyExistsError, PersonaInUseError

router = APIRouter(prefix="/personas", tags=["personas"])


@router.post("", response_model=PersonaRead, status_code=status.HTTP_201_CREATED)
def create_persona(data: PersonaCreate, db: Session = Depends(get_db)) -> Persona:
    try:
        return persona_service.create_persona(db, data)
    except PersonaAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("", response_model=list[PersonaRead])
def list_personas(db: Session = Depends(get_db)) -> list[Persona]:
    return persona_service.list_personas(db)


@router.get("/{name}", response_model=PersonaRead)
def get_persona(name: str, db: Session = Depends(get_db)) -> Persona:
    persona = persona_service.get_persona(db, name)
    if persona is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Persona not found")
    return persona


@router.put("/{name}", response_model=PersonaRead)
def update_persona(name: str, data: PersonaUpdate, db: Session = Depends(get_db)) -> Persona:
    persona = persona_service.update_persona(db, name, data)
    if persona is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Persona not found")
    return persona


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_persona(name: str, db: Session = Depends(get_db)) -> None:
    try:
        deleted = persona_service.delete_persona(db, name)
    except PersonaInUseError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Persona not found")
