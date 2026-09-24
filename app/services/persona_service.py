from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.persona import Persona
from app.models.persona_conversation import PersonaConversation
from app.models.user import User
from app.schemas.persona import PersonaCreate, PersonaUpdate


class PersonaAlreadyExistsError(Exception):
    pass


class PersonaInUseError(Exception):
    pass


def create_persona(db: Session, data: PersonaCreate) -> Persona:
    if db.get(Persona, data.name) is not None:
        raise PersonaAlreadyExistsError(f"persona '{data.name}' already exists")

    persona = Persona(**data.model_dump())
    db.add(persona)
    db.commit()
    db.refresh(persona)
    return persona


def get_persona(db: Session, name: str) -> Persona | None:
    return db.get(Persona, name)


def list_personas(db: Session) -> list[Persona]:
    return list(db.execute(select(Persona).order_by(Persona.name)).scalars().all())


def update_persona(db: Session, name: str, data: PersonaUpdate) -> Persona | None:
    persona = db.get(Persona, name)
    if persona is None:
        return None

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(persona, field, value)

    db.commit()
    db.refresh(persona)
    return persona


def delete_persona(db: Session, name: str) -> bool:
    persona = db.get(Persona, name)
    if persona is None:
        return False

    user_count = db.scalar(select(func.count()).select_from(User).where(User.selected_persona_id == name))
    conversation_count = db.scalar(
        select(func.count()).select_from(PersonaConversation).where(PersonaConversation.persona_id == name)
    )
    if user_count or conversation_count:
        raise PersonaInUseError(
            f"persona '{name}' is in use (selected by {user_count} users, "
            f"{conversation_count} conversations)"
        )

    db.delete(persona)
    db.commit()
    return True


def select_persona_for_user(db: Session, user: User, persona_name: str | None) -> User:
    if persona_name is not None and db.get(Persona, persona_name) is None:
        raise ValueError(f"persona '{persona_name}' does not exist")

    user.selected_persona_id = persona_name
    db.commit()
    db.refresh(user)
    return user
