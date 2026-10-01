from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.child_events.service import create_child_event, get_travel_child_event, list_child_events
from app.core.clock import local_today
from app.core.exceptions import InvalidInputError
from app.models.enums import EventInstanceStatus, EventType
from app.models.event import Event
from app.models.event_instance import EventInstance
from app.models.important_date_range import ImportantDateRange
from app.models.location import Location
from app.models.user import User
from app.schemas.event import EventCreate, EventUpdate, validate_event_times, validate_recurrence
from app.services.common import require
from app.services.notification import sync_notifications
from app.services.recurrence import create_single_instance, follow_single_instance, generate_event_instances

# build_event / apply_event_update / remove_event는 커밋하지 않는다. 자연어 명령·되돌리기
# (app/services/event_command_service.py, action_history_service.py)가 여러 변경과 ActionHistory 기록을
# 한 트랜잭션으로 묶을 수 있게 하기 위해서다. 같은 이름의 create/update/delete_event는 이를 감싸 커밋한다.


def _ensure_references_exist(db: Session, data: EventCreate | EventUpdate) -> None:
    user_id = getattr(data, "user_id", None)
    if user_id is not None:
        require(db, User, user_id, "user_id")
    if data.date_range_id is not None:
        require(db, ImportantDateRange, data.date_range_id, "date_range_id")
    if data.parent_event_id is not None:
        require(db, Event, data.parent_event_id, "parent_event_id")
    if data.location_id is not None:
        require(db, Location, data.location_id, "location_id")


def build_event(db: Session, data: EventCreate, instances_from: date | None = None) -> Event:
    """이벤트와 반복 인스턴스, 이동시간 하위 일정을 만든다 (커밋하지 않음). instances_from이 있으면 반복 회차는
    그 날짜부터 만든다 (자연어 경로: 오늘부터)."""
    _ensure_references_exist(db, data)
    event = Event(**data.model_dump())
    db.add(event)
    db.flush()

    if event.is_recurring and event.recurrence_rule and event.date_range_id is not None:
        generate_event_instances(db, event, not_before=instances_from)
    elif not event.is_recurring:
        create_single_instance(db, event)

    # event 자신이 child가 아니고(parent_event_id 없음) 장소가 있으면, 이동시간
    # TRAVEL child를 자동 생성한다 (FR-5).
    if event.location_id is not None and event.parent_event_id is None:
        create_child_event(db, event)
    return event


def create_event(db: Session, data: EventCreate) -> Event:
    """이벤트와 반복 인스턴스, 이동시간 하위 일정을 한 트랜잭션으로 만든다 — 중간에 실패하면 아무것도 남지 않는다."""
    event = build_event(db, data)
    db.commit()
    db.refresh(event)
    sync_notifications(db, event_ids=[event.id])
    return event


def get_event(db: Session, event_id: int) -> Event | None:
    return db.get(Event, event_id)


def list_events(db: Session, user_id: int | None = None) -> list[Event]:
    stmt = select(Event)
    if user_id is not None:
        stmt = stmt.where(Event.user_id == user_id)
    return list(db.execute(stmt).scalars().all())


@dataclass
class LocationChange:
    """장소를 바꾼 결과: 새로 만든 이동 child, 상태가 바뀐 child 회차, 새로 생긴 child 회차 (되돌리기·알림용)."""

    created_child: Event | None = None
    changed_instances: list[EventInstance] = field(default_factory=list)
    created_instances: list[EventInstance] = field(default_factory=list)


def set_event_location(db: Session, event: Event, location: Location | None, today: date | None = None) -> LocationChange:
    """이벤트의 장소를 바꾸고 이동시간 하위 일정(FR-5)을 맞춘다 (커밋하지 않음). 장소는 반복 시리즈 단위 값이다.

    - 연결: 이동 child가 없으면 새로 만든다(반복이면 같은 규칙·기간으로 회차도).
    - 변경: 이동 child의 시각을 새 장소의 이동 시간으로 다시 계산하고, 앞으로의 회차 중 부모 회차가 살아 있는데
      취소돼 있던 것(장소를 뺐다가 다시 넣은 경우)은 되살린다.
    - 삭제: 연결을 끊고 앞으로 남은 이동 child 회차만 취소한다. 지난 기록은 그대로 둔다.
    """
    today = today or local_today()
    result = LocationChange()
    event.location = location
    child = get_travel_child_event(db, event.id)
    if location is None or event.event_type == EventType.DEADLINE:
        if child is not None:
            child.location = None
            for instance in child.instances:
                if instance.date >= today and instance.status == EventInstanceStatus.PENDING:
                    instance.status = EventInstanceStatus.CANCELLED
                    result.changed_instances.append(instance)
        return result

    db.flush()
    if child is None:
        child = create_child_event(db, event)
        result.created_child = child
        return result

    child.location = location
    child.start_time = event.start_time - timedelta(minutes=location.default_travel_minutes)
    child.end_time = event.start_time
    parent_active = {i.date for i in event.instances if i.status != EventInstanceStatus.CANCELLED}
    for instance in child.instances:
        if instance.date >= today and instance.status == EventInstanceStatus.CANCELLED and instance.date in parent_active:
            instance.status = EventInstanceStatus.PENDING
            result.changed_instances.append(instance)
    if child.is_recurring and child.recurrence_rule and child.date_range_id is not None:
        result.created_instances = generate_event_instances(db, child)
    return result


def apply_event_update(db: Session, event: Event, changes: dict[str, Any]) -> None:
    """검증 후 변경을 적용한다 (커밋하지 않음). 시작 시각이 바뀌면 하위 일정도 같은 만큼 옮긴다."""
    if changes.get("parent_event_id") == event.id:
        raise InvalidInputError("an event cannot be its own parent")
    # 요청 값만으로는 알 수 없는 규칙은 기존 값과 합친 최종 상태로 검사한다.
    validate_event_times(
        changes.get("event_type", event.event_type),
        changes.get("start_time", event.start_time),
        changes.get("end_time", event.end_time),
    )
    validate_recurrence(
        changes.get("is_recurring", event.is_recurring),
        changes.get("recurrence_rule", event.recurrence_rule),
    )

    changes = dict(changes)
    location_given = "location_id" in changes
    location_id = changes.pop("location_id", None)
    old_start = event.start_time
    for name, value in changes.items():
        setattr(event, name, value)

    # 이동시간·준비 하위 일정은 부모 시작 시각에 붙어 있으므로 같은 만큼 민다 (FR-5).
    children = list_child_events(db, event.id)
    if old_start is not None and event.start_time is not None and event.start_time != old_start:
        delta = event.start_time - old_start
        for child in children:
            if child.start_time is not None:
                child.start_time += delta
            child.end_time += delta
    for item in [event, *children]:
        follow_single_instance(db, item)
    if location_given and location_id != event.location_id and event.parent_event_id is None:
        set_event_location(db, event, db.get(Location, location_id) if location_id is not None else None)


def update_event(db: Session, event_id: int, data: EventUpdate) -> Event | None:
    event = db.get(Event, event_id)
    if event is None:
        return None

    _ensure_references_exist(db, data)
    apply_event_update(db, event, data.model_dump(exclude_unset=True))
    db.commit()
    db.refresh(event)
    sync_notifications(db, event_ids=[event.id])
    return event


def remove_event(db: Session, event: Event) -> None:
    """이벤트와 그 인스턴스, 하위 일정(이동시간·준비)까지 삭제한다 (커밋하지 않음).

    인스턴스의 미준수 사유(ComplianceReport)도 cascade로 함께 지워진다. 되돌리기를 위해 이 함수를 부르기
    전에 action_history_service가 이 행들을 모두 스냅샷으로 남긴다.
    """
    for child in list_child_events(db, event.id):
        db.delete(child)
    db.delete(event)


def delete_event(db: Session, event_id: int) -> bool:
    """이벤트와 그 인스턴스, 하위 일정(이동시간·준비)까지 삭제한다. 부모 없이 하위 일정만 남을 이유가 없다."""
    event = db.get(Event, event_id)
    if event is None:
        return False

    instance_ids = [i.id for item in [event, *list_child_events(db, event.id)] for i in item.instances]
    remove_event(db, event)
    db.commit()
    sync_notifications(db, instance_ids=instance_ids)
    return True
