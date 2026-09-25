from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.db import get_db
from app.core.openapi import CURRENT_USER
from app.models.user import User
from app.schemas.task import TaskCreate, TaskRead
from app.services import task_service

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskRead], summary="할 일 목록", responses=CURRENT_USER)
def list_tasks(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[TaskRead]:
    """내 `deadline` 일정을 마감이 빠른 순으로 돌려준다. 반복 할 일은 완료 안 된 가장 이른 회차를 보여준다.

    `overdue`는 마감 시각이 지났는데 아직 완료하지 않은 경우 true.
    """
    return task_service.list_tasks(db, user.id)


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED, summary="할 일(마감) 생성", responses=CURRENT_USER)
def create_task(
    data: TaskCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> TaskRead:
    """`event_type=deadline` 이벤트를 만드는 축약형. 반복이면 `recurrence_rule`과 `date_range_id`가 모두 필요하다. 다른 Event 필드를 보내면 422."""
    return task_service.create_task(db, user, data)


@router.put("/{event_instance_id}/complete", response_model=TaskRead, summary="할 일 완료 처리", responses=CURRENT_USER)
def complete_task(
    event_instance_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> TaskRead:
    """해당 회차(EventInstance)를 완료 처리한다. 다시 호출해도 결과가 같다. 포인트에는 즉시 반영된다."""
    return task_service.complete_task(db, user, event_instance_id)
