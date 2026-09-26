from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.db import get_db
from app.core.exceptions import NotFoundError
from app.core.openapi import CONFLICT, CURRENT_USER
from app.models.event_instance import EventInstance
from app.models.user import User
from app.schemas.event_instance import EventInstanceRead
from app.services import event_command_service, event_instance_service

router = APIRouter(prefix="/event-instances", tags=["event-instances"])


def _own_instance(db: Session, user: User, event_instance_id: int) -> EventInstance:
    instance = db.get(EventInstance, event_instance_id)
    if instance is None or instance.event.user_id != user.id:
        raise NotFoundError(f"event instance {event_instance_id} not found")
    return instance


@router.get("", response_model=list[EventInstanceRead], summary="기간의 일정 회차 (캘린더)", responses=CURRENT_USER)
def list_event_instances(
    start: date = Query(description="시작 날짜 (포함)", examples=["2026-09-21"]),
    end: date = Query(description="끝 날짜 (포함). start부터 최대 62일", examples=["2026-09-27"]),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[EventInstanceRead]:
    """start~end 날짜의 일정 회차를 이벤트 정보(제목·종류·중요도·반복 여부)와 함께 시간순으로 돌려준다.

    - 취소된(`cancelled`) 회차는 빠진다.
    - `start_time`/`end_time`은 회차별 시간 변경을 반영한 실제 시각이다(타임존 없는 로컬 시각). `deadline`은 `start_time`이 null.
    - 회차가 없는 단발성 일정(`POST /events`로 만든 비반복 일정)은 `event_instance_id: null`로 포함된다.
    """
    return event_instance_service.list_instances_in_range(db, user.id, start, end)


@router.put(
    "/{event_instance_id}/complete",
    response_model=EventInstanceRead,
    summary="일정 회차 완료 처리",
    responses={**CURRENT_USER, **CONFLICT},
)
def complete_event_instance(
    event_instance_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> EventInstanceRead:
    """`scheduled`·`deadline` 모두 가능(`PUT /tasks/{id}/complete`와 같은 처리). 다시 호출해도 결과가 같고,
    지난 날짜면 포인트를 다시 계산한다. 취소된 회차는 409."""
    instance = event_instance_service.complete_event_instance(db, _own_instance(db, user, event_instance_id))
    return event_instance_service.to_event_instance_read(instance)


@router.delete(
    "/{event_instance_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="일정 회차 하나 삭제",
    responses={**CURRENT_USER, **CONFLICT},
)
def delete_event_instance(
    event_instance_id: int, response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    """반복 일정의 이 회차만 취소(`cancelled`)한다. 같은 날짜의 하위 일정(이동시간·준비)도 함께 취소된다.

    변경 기록(source=ui)이 남아 `POST /actions/{id}/undo`로 되돌릴 수 있고, 기록 id는 응답 헤더 `X-Action-Id`로 준다.
    일정 전체를 지우려면 `DELETE /events/{event_id}`.
    """
    action = event_command_service.delete_instance_from_ui(db, _own_instance(db, user, event_instance_id))
    response.headers["X-Action-Id"] = str(action.id)
