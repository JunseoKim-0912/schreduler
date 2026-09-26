from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, InvalidInputError
from app.models.enums import CompletionMethod, EventInstanceStatus
from app.models.event import Event
from app.models.event_instance import EventInstance
from app.services.points import recalculate_points_since


def complete_event_instance(
    db: Session, instance: EventInstance, method: CompletionMethod = CompletionMethod.MANUAL
) -> EventInstance:
    """EventInstance를 완료(DONE) 처리한다. 이미 완료된 인스턴스는 그대로 둔다.

    지난 날짜의 인스턴스를 뒤늦게 완료하면 자정 잡이 이미 그날 포인트를 기록했으므로, 그 날짜부터
    어제까지의 PointsLedger를 즉시 다시 계산한다. 오늘 날짜분은 /points/summary가 실시간으로 반영한다.
    """
    if instance.status == EventInstanceStatus.CANCELLED:
        raise ConflictError(f"event instance {instance.id} is cancelled")
    if instance.status != EventInstanceStatus.DONE:
        instance.status = EventInstanceStatus.DONE
        instance.completion_method = method
        db.commit()
        db.refresh(instance)
        recalculate_points_since(db, instance.event.user_id, instance.date)
    return instance


def child_instances_on_same_date(db: Session, instance: EventInstance) -> list[EventInstance]:
    """부모 이벤트의 하위 일정(이동시간·준비)에서 같은 날짜의 회차들."""
    return list(
        db.execute(
            select(EventInstance)
            .join(Event, EventInstance.event_id == Event.id)
            .where(Event.parent_event_id == instance.event_id, EventInstance.date == instance.date)
        ).scalars()
    )


def cancel_instance(db: Session, instance: EventInstance) -> list[EventInstance]:
    """반복 일정의 한 회차만 취소한다 (커밋하지 않음). 같은 날짜의 하위 일정 회차도 함께 취소한다.

    행을 지우지 않고 CANCELLED로 남겨야 반복 회차 생성이 그 날짜를 다시 만들지 않는다.
    """
    affected = [instance, *child_instances_on_same_date(db, instance)]
    for item in affected:
        item.status = EventInstanceStatus.CANCELLED
    return affected


def set_instance_times(db: Session, instance: EventInstance, start: datetime | None, end: datetime) -> list[EventInstance]:
    """한 회차의 시각만 바꾼다 (커밋하지 않음). deadline이면 start는 None이고 end가 마감이다.

    같은 날짜의 하위 일정 회차도 부모 시작 시각이 움직인 만큼 함께 옮긴다.
    """
    if start is not None and end <= start:
        raise InvalidInputError("end time must be after start time")

    old_start = instance.effective_start
    children = child_instances_on_same_date(db, instance)
    instance.start_time_override = start
    instance.end_time_override = end

    if old_start is not None and start is not None and start != old_start:
        delta = start - old_start
        for child in children:
            child_start = child.effective_start
            child.end_time_override = child.effective_end + delta
            child.start_time_override = child_start + delta if child_start is not None else None
    return [instance, *children]
