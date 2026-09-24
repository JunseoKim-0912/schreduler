from collections.abc import Iterator
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Persona, PersonaConversation, User
from app.services.persona_conversation_service import (
    ConversationNotFoundError,
    get_conversation,
    list_conversations,
    record_turn,
    resolve_conversation,
)


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for name in ("Hana", "Rordon"):
            session.add(
                Persona(
                    name=name,
                    display_name={"ko": name, "en": name},
                    description={"ko": "설명", "en": "description"},
                )
            )
        session.commit()
        yield session


def _user(db: Session, name: str = "June", persona: str | None = "Hana") -> User:
    user = User(name=name, preferred_language="ko", selected_persona_id=persona)
    db.add(user)
    db.commit()
    return user


def test_record_turn_creates_conversation_with_selected_persona(db: Session) -> None:
    user = _user(db)

    conversation = record_turn(db, user, "daily_checkin", "안녕", "반가워요")

    assert conversation is not None
    assert conversation.persona_id == "Hana"
    assert conversation.context_type == "daily_checkin"
    assert [(m["role"], m["content"]) for m in conversation.messages] == [
        ("user", "안녕"),
        ("assistant", "반가워요"),
    ]
    for message in conversation.messages:
        datetime.fromisoformat(message["created_at"])


def test_record_turn_appends_and_persists(db: Session) -> None:
    user = _user(db)
    conversation = record_turn(db, user, "daily_checkin", "1", "a")
    record_turn(db, user, "daily_checkin", "2", "b", conversation)

    db.expire_all()
    stored = db.get(PersonaConversation, conversation.id)
    assert [m["content"] for m in stored.messages] == ["1", "a", "2", "b"]
    assert db.query(PersonaConversation).count() == 1


def test_record_turn_without_persona_saves_nothing(db: Session) -> None:
    user = _user(db, persona=None)

    assert record_turn(db, user, "daily_checkin", "안녕", "반가워요") is None
    assert db.query(PersonaConversation).count() == 0


def test_continued_conversation_keeps_original_persona_after_switch(db: Session) -> None:
    user = _user(db)
    conversation = record_turn(db, user, "daily_checkin", "1", "a")

    user.selected_persona_id = "Rordon"
    db.commit()
    continued = record_turn(db, user, "daily_checkin", "2", "b", conversation)
    fresh = record_turn(db, user, "daily_checkin", "3", "c")

    assert continued.persona_id == "Hana"
    assert fresh.persona_id == "Rordon"
    assert fresh.id != conversation.id


def test_resolve_conversation(db: Session) -> None:
    owner = _user(db)
    other = _user(db, name="Other")
    conversation = record_turn(db, owner, "daily_checkin", "1", "a")

    assert resolve_conversation(db, owner, "daily_checkin", None) is None
    assert resolve_conversation(db, owner, "daily_checkin", conversation.id).id == conversation.id

    with pytest.raises(ConversationNotFoundError):
        resolve_conversation(db, other, "daily_checkin", conversation.id)
    with pytest.raises(ConversationNotFoundError):
        resolve_conversation(db, owner, "sleep_checkin", conversation.id)
    with pytest.raises(ConversationNotFoundError):
        resolve_conversation(db, owner, "daily_checkin", 999)


def test_list_and_get_are_scoped_to_user(db: Session) -> None:
    owner = _user(db)
    other = _user(db, name="Other")
    first = record_turn(db, owner, "daily_checkin", "1", "a")
    second = record_turn(db, owner, "event_start_check", "2", "b")
    others = record_turn(db, other, "daily_checkin", "3", "c")

    assert [c.id for c in list_conversations(db, owner.id)] == [second.id, first.id]
    assert [c.id for c in list_conversations(db, owner.id, "daily_checkin")] == [first.id]
    assert get_conversation(db, owner.id, first.id).id == first.id
    assert get_conversation(db, owner.id, others.id) is None
