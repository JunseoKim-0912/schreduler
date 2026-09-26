from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.exceptions import NotFoundError
from app.core.openapi import CONFLICT, EXPIRED, LLM_ERRORS, NOT_FOUND
from app.models.event import Event
from app.schemas.event import EventCreate, EventRead, EventUpdate
from app.schemas.event_command import CommandConfirmRequest, CommandConfirmResponse
from app.schemas.event_parse import EventParseRequest, EventParseResponse
from app.models.user import User
from app.services import event_command_service, event_parse_service, event_service
from app.services.common import require
from app.services.slot_fill_session import take_pending_action

router = APIRouter(prefix="/events", tags=["events"])


@router.post("", response_model=EventRead, status_code=status.HTTP_201_CREATED, summary="이벤트 생성", responses=NOT_FOUND)
def create_event(data: EventCreate, db: Session = Depends(get_db)) -> Event:
    """일정을 만든다 (FR-1).

    - `event_type=scheduled`(기본): `start_time`·`end_time` 모두 필요, 종료가 시작보다 늦어야 한다.
    - `event_type=deadline`: `start_time`은 null, `end_time`이 마감 일시다.
    - `is_recurring` + `recurrence_rule`(RFC 5545 RRULE) + `date_range_id`를 주면 기간 안의 반복 인스턴스를 함께 만든다.
    - `location_id`가 있으면 이동시간 하위 일정(FR-5)을 자동으로 붙인다 (deadline 제외).
    """
    return event_service.create_event(db, data)


@router.post("/parse", response_model=EventParseResponse, summary="자연어로 이벤트 초안 만들기", responses={**NOT_FOUND, **LLM_ERRORS})
def parse_event(data: EventParseRequest, db: Session = Depends(get_db)) -> EventParseResponse:
    """FR-2: 자연어 발화 한 턴을 슬롯필링한다. session_id를 생략하면 새 대화를
    시작하고, 이전 응답의 session_id를 그대로 보내면 대화를 이어간다."""
    return event_parse_service.parse_event_utterance(db, data)


@router.post(
    "/commands/confirm",
    response_model=CommandConfirmResponse,
    summary="자연어 요청 확인 후 실행",
    responses={**NOT_FOUND, **CONFLICT, **EXPIRED},
)
def confirm_command(data: CommandConfirmRequest, db: Session = Depends(get_db)) -> CommandConfirmResponse:
    """`POST /events/parse`가 `needs_confirmation`으로 돌려준 요청을 실행한다.

    - 자연어로 만든 일정 초안(create), 또는 2개 이상을 한꺼번에 지우거나 바꾸는 요청(delete/update)이 대상이다.
    - 토큰은 발급 후 10분 동안 한 번만 쓸 수 있다. 지나면 410, 다른 사용자의 토큰이면 404.
    - 실행 결과의 `command.action_id`로 `POST /actions/{id}/undo`를 부르면 되돌릴 수 있다.
    """
    user = require(db, User, data.user_id, "user_id")
    pending = take_pending_action(data.token, user.id)
    result = event_command_service.execute_pending(db, user, pending)
    return CommandConfirmResponse(
        message=result.message,
        command=event_command_service.command_result(
            pending.kind, "executed", affected=result.affected, action_id=result.action.id
        ),
    )


@router.get("", response_model=list[EventRead], summary="이벤트 목록")
def list_events(
    user_id: int | None = Query(default=None, description="이 사용자의 것만 조회"),
    db: Session = Depends(get_db),
) -> list[Event]:
    """이벤트 목록. `user_id`로 특정 사용자의 것만 거를 수 있다."""
    return event_service.list_events(db, user_id=user_id)


@router.get("/{event_id}", response_model=EventRead, summary="이벤트 조회", responses=NOT_FOUND)
def get_event(event_id: int, db: Session = Depends(get_db)) -> Event:
    """이벤트 하나를 조회한다."""
    event = event_service.get_event(db, event_id)
    if event is None:
        raise NotFoundError("Event not found")
    return event


@router.put("/{event_id}", response_model=EventRead, summary="이벤트 수정", responses=NOT_FOUND)
def update_event(event_id: int, data: EventUpdate, db: Session = Depends(get_db)) -> Event:
    """보낸 필드만 수정한다. 기존 값과 합친 최종 상태가 event_type 규칙에 어긋나면 422.

    예: scheduled → deadline으로 바꿀 때는 `{"event_type": "deadline", "start_time": null}`을 함께 보낸다.
    """
    event = event_service.update_event(db, event_id, data)
    if event is None:
        raise NotFoundError("Event not found")
    return event


@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT, summary="이벤트 삭제", responses=NOT_FOUND)
def delete_event(event_id: int, response: Response, db: Session = Depends(get_db)) -> None:
    """이벤트와 그 반복 인스턴스, 하위 일정(이동시간·준비)을 함께 삭제한다.

    삭제 전 상태가 변경 기록(source=ui)으로 남아 `POST /actions/{id}/undo`로 되돌릴 수 있다.
    기록 id는 응답 헤더 `X-Action-Id`로 돌려준다.
    """
    event = event_service.get_event(db, event_id)
    if event is None:
        raise NotFoundError("Event not found")
    action = event_command_service.delete_event_from_ui(db, event)
    response.headers["X-Action-Id"] = str(action.id)
