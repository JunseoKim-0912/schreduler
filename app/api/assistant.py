from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.db import get_db
from app.core.openapi import CONFLICT, CURRENT_USER, EXPIRED, LLM_ERRORS
from app.models.assistant import AssistantSession
from app.models.user import User
from app.schemas.assistant import (
    AssistantChatRequest,
    AssistantChatResponse,
    AssistantConfirmResponse,
    AssistantSessionRead,
    AssistantTokenRequest,
)
from app.services.assistant import agent

router = APIRouter(prefix="/assistant", tags=["assistant"])


def _session_read(db: Session, session: AssistantSession | None) -> AssistantSessionRead:
    if session is None:
        return AssistantSessionRead(session_id=None)
    proposal = agent.active_proposal(db, session)
    db.commit()
    return AssistantSessionRead(
        session_id=session.id,
        messages=agent.display_messages(db, session),
        proposal=agent.proposal_view(proposal) if proposal is not None else None,
    )


@router.post("/chat", response_model=AssistantChatResponse, summary="어시스턴트와 대화 한 턴", responses={**CURRENT_USER, **LLM_ERRORS})
def chat(data: AssistantChatRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> AssistantChatResponse:
    """자연어 한 턴. `session_id`를 생략하면 새 대화를 시작한다 (다른 사용자의 세션이면 404).

    - 어시스턴트는 되묻기보다 추론해서 **초안**을 만든다. 초안은 저장되지 않고 `proposal`(확인 카드)로 돌아온다.
      `items[].inferred_fields`는 사용자가 말하지 않아 추정한 항목, `warnings`는 자정 넘김·12시간 초과·지난 날짜·
      여러 개 한꺼번에 변경 등 확인이 필요한 점이다. 반복 일정 카드에는 처음 3회차 `preview_dates`가 있다.
    - 새 제안은 같은 대화의 이전 대기 제안을 대체한다. 제안은 30분 뒤 만료된다.
    - 사용자가 채팅으로 승인하면("좋아") 이전 턴에 보여준 제안이 실행되고 `executed[].action_id`가 돌아온다.
    - 한 턴은 LLM 호출 최대 6회, 20초. 넘으면 지금까지 만든 초안을 보여주거나 짧게 되묻는다.

    카드 `kind`: `create_event`(title, event_type, date, start_time, end_time, time_display, importance, recurring,
    recurrence_rule, date_range, location, preview_dates) / `update_event`·`delete_event`(scope, targets[], changes) /
    `create_range`·`update_range`·`delete_range`(name, start_date, end_date, events_using, mode).
    """
    result = agent.chat(db, user, data.message, data.session_id)
    return AssistantChatResponse(session_id=result.session_id, reply=result.reply, proposal=result.proposal, executed=result.executed)


@router.post(
    "/confirm",
    response_model=AssistantConfirmResponse,
    summary="제안 확정 (버튼)",
    responses={**CURRENT_USER, **CONFLICT, **EXPIRED},
)
def confirm_proposal(data: AssistantTokenRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> AssistantConfirmResponse:
    """확인 카드의 [만들기] 버튼. 제안의 모든 초안을 **한 트랜잭션**으로 실행한다 (하나라도 실패하면 아무것도 저장되지 않음).

    `executed[].action_id`로 `POST /actions/{id}/undo`를 부르면 되돌릴 수 있다. 만료(30분)면 410, 이미 확정·취소·대체된
    제안이거나 그사이 대상이 바뀌었으면 409, 이 세션의 토큰이 아니면 404.
    """
    result = agent.confirm(db, user, data.session_id, data.token)
    return AssistantConfirmResponse(session_id=result.session_id, reply=result.reply, executed=result.executed)


@router.post("/cancel", response_model=AssistantConfirmResponse, summary="제안 취소 (버튼)", responses={**CURRENT_USER, **CONFLICT, **EXPIRED})
def cancel_proposal(data: AssistantTokenRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> AssistantConfirmResponse:
    """확인 카드의 [취소] 버튼. 아무것도 저장하지 않고 제안을 닫는다."""
    result = agent.cancel(db, user, data.session_id, data.token)
    return AssistantConfirmResponse(session_id=result.session_id, reply=result.reply)


@router.get("/sessions/current", response_model=AssistantSessionRead, summary="오늘의 대화 이어보기", responses=CURRENT_USER)
def get_current_session(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> AssistantSessionRead:
    """오늘(앱 시간대) 마지막으로 쓴 대화와 그 기록(사용자 말·어시스턴트 답), 아직 대기 중인 제안. 없으면 `session_id: null`."""
    return _session_read(db, agent.current_session(db, user))


@router.post("/sessions", response_model=AssistantSessionRead, status_code=status.HTTP_201_CREATED, summary="새 대화", responses=CURRENT_USER)
def create_session(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> AssistantSessionRead:
    """빈 대화를 새로 시작한다. 이후 `POST /assistant/chat`에 이 `session_id`를 보낸다."""
    session = agent.create_session(db, user)
    db.commit()
    return _session_read(db, session)
