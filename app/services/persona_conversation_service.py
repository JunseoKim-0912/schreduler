from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import local_today, local_wall_now
from app.core.exceptions import NotFoundError
from app.models.persona_conversation import PersonaConversation
from app.models.user import User
from app.schemas.persona import ConversationMessage


class ConversationNotFoundError(NotFoundError):
    pass


def resolve_conversation(
    db: Session, user: User, context_type: str, conversation_id: int | None
) -> PersonaConversation | None:
    """이어갈 대화를 찾는다. LLM 호출 전에 불러서 잘못된 conversation_id로 비용을 쓰지 않게 한다."""
    if conversation_id is None:
        return None
    conversation = db.get(PersonaConversation, conversation_id)
    if conversation is None or conversation.user_id != user.id or conversation.context_type != context_type:
        raise ConversationNotFoundError(
            f"conversation_id {conversation_id} does not exist for this user/context"
        )
    return conversation


def current_conversation(
    db: Session, user_id: int, persona_id: str, context_type: str, day: date
) -> PersonaConversation | None:
    """(사용자, 페르소나, 상황, 날짜)의 지금 대화 — 그날 가장 최근에 시작한 대화. 날짜가 바뀌면 없으므로 새 대화가 된다."""
    return db.execute(
        select(PersonaConversation)
        .where(
            PersonaConversation.user_id == user_id,
            PersonaConversation.persona_id == persona_id,
            PersonaConversation.context_type == context_type,
            PersonaConversation.conversation_date == day,
        )
        .order_by(PersonaConversation.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def start_conversation(db: Session, user: User, persona_id: str, context_type: str, day: date) -> PersonaConversation:
    """[새 대화]: 그날의 새 대화를 시작한다. 이전 대화는 지우지 않고 남긴다."""
    conversation = PersonaConversation(
        user_id=user.id, persona_id=persona_id, context_type=context_type, messages=[], conversation_date=day
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def record_turn(
    db: Session,
    user: User,
    context_type: str,
    user_message: str,
    assistant_message: str,
    conversation: PersonaConversation | None = None,
    *,
    day: date | None = None,
    llm_skipped: bool = False,
    filter_reason: str | None = None,
) -> PersonaConversation | None:
    """사용자 발화와 페르소나 응답 한 쌍을 대화에 추가한다.

    conversation이 없으면 새 대화를 만든다. 사용자가 페르소나를 선택하지 않았으면
    persona_id를 채울 수 없으므로 저장하지 않고 None을 반환한다.
    """
    if conversation is None:
        if user.selected_persona_id is None:
            return None
        conversation = PersonaConversation(
            user_id=user.id,
            persona_id=user.selected_persona_id,
            context_type=context_type,
            messages=[],
            conversation_date=day or local_today(),
        )
        db.add(conversation)

    now = local_wall_now()
    new_messages = [
        ConversationMessage(role="user", content=user_message, created_at=now),
        ConversationMessage(
            role="assistant", content=assistant_message, created_at=now, llm_skipped=llm_skipped, filter_reason=filter_reason
        ),
    ]
    # JSON 컬럼은 in-place 변경을 감지하지 못하므로 새 리스트를 할당해야 저장된다.
    conversation.messages = [*conversation.messages, *(m.model_dump(mode="json") for m in new_messages)]

    db.commit()
    db.refresh(conversation)
    return conversation


def list_conversations(
    db: Session, user_id: int, context_type: str | None = None
) -> list[PersonaConversation]:
    stmt = select(PersonaConversation).where(PersonaConversation.user_id == user_id)
    if context_type is not None:
        stmt = stmt.where(PersonaConversation.context_type == context_type)
    return list(db.execute(stmt.order_by(PersonaConversation.id.desc())).scalars().all())


def get_conversation(db: Session, user_id: int, conversation_id: int) -> PersonaConversation | None:
    conversation = db.get(PersonaConversation, conversation_id)
    if conversation is None or conversation.user_id != user_id:
        return None
    return conversation
