"""Task list = event_type이 DEADLINE인 Event/EventInstance를 보여주는 편의 계층. 별도 테이블은 없다."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.exceptions import NotFoundError
from app.models.enums import EventInstanceStatus, EventType
from app.models.event import Event
from app.models.event_instance import EventInstance
from app.models.user import User
from app.schemas.event import EventCreate
from app.schemas.task import TaskCreate, TaskRead
from app.services import event_service
from app.services.event_instance_service import complete_event_instance


class TaskNotFoundError(NotFoundError):
    pass


def _due_at(event: Event, instance: EventInstance | None) -> datetime:
    if instance is None:
        return event.end_time
    return instance.effective_end


def current_instance(event: Event) -> EventInstance | None:
    """Task 목록에 보여줄 인스턴스: 아직 완료 안 된 것 중 가장 이른 것(밀린 마감이 먼저 보이게),
    전부 완료했다면 가장 최근 것. 취소된 회차는 없는 것으로 본다."""
    instances = sorted((i for i in event.instances if i.status != EventInstanceStatus.CANCELLED), key=lambda i: i.date)
    if not instances:
        return None
    pending = [i for i in instances if i.status != EventInstanceStatus.DONE]
    return pending[0] if pending else instances[-1]


def to_task_read(event: Event, instance: EventInstance | None, now: datetime) -> TaskRead:
    due_at = _due_at(event, instance)
    completed = instance is not None and instance.status == EventInstanceStatus.DONE
    return TaskRead(
        event_id=event.id,
        event_instance_id=instance.id if instance else None,
        title=event.title,
        importance=event.importance,
        due_at=due_at,
        status=instance.status if instance else None,
        completed=completed,
        overdue=not completed and due_at < now,
        is_recurring=event.is_recurring,
        recurrence_rule=event.recurrence_rule,
        date_range_id=event.date_range_id,
    )


def list_tasks(db: Session, user_id: int, now: datetime | None = None) -> list[TaskRead]:
    now = now or datetime.now()
    events = db.execute(
        select(Event)
        .where(Event.user_id == user_id, Event.event_type == EventType.DEADLINE)
        .options(selectinload(Event.instances))
    ).scalars().all()
    tasks = [
        to_task_read(event, current_instance(event), now)
        for event in events
        # 회차가 있는데 전부 취소됐으면 목록에서 뺀다 (회차가 원래 없는 옛 deadline은 그대로 보여준다).
        if not event.instances or current_instance(event) is not None
    ]
    return sorted(tasks, key=lambda t: (t.due_at, t.event_id))


def create_task(db: Session, user: User, data: TaskCreate, now: datetime | None = None) -> TaskRead:
    event = event_service.create_event(
        db,
        EventCreate(
            user_id=user.id,
            title=data.title,
            event_type=EventType.DEADLINE,
            start_time=None,
            end_time=data.end_time,
            importance=data.importance,
            is_recurring=data.recurrence_rule is not None,
            recurrence_rule=data.recurrence_rule,
            date_range_id=data.date_range_id,
        ),
    )
    # 단발성 task의 회차(마감일 하나)는 create_event가 다른 단발 일정과 같은 방식으로 만든다.
    return to_task_read(event, current_instance(event), now or datetime.now())


def complete_task(db: Session, user: User, event_instance_id: int, now: datetime | None = None) -> TaskRead:
    instance = db.get(EventInstance, event_instance_id)
    if (
        instance is None
        or instance.event.user_id != user.id
        or instance.event.event_type != EventType.DEADLINE
    ):
        raise TaskNotFoundError(f"task instance {event_instance_id} not found")

    instance = complete_event_instance(db, instance)
    return to_task_read(instance.event, instance, now or datetime.now())
