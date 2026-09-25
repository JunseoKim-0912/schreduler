from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.child_events.service import create_child_event, list_child_events
from app.core.exceptions import InvalidInputError
from app.models.event import Event
from app.models.important_date_range import ImportantDateRange
from app.models.location import Location
from app.models.user import User
from app.schemas.event import EventCreate, EventUpdate, validate_event_times, validate_recurrence
from app.services.common import require
from app.services.recurrence import generate_event_instances


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


def create_event(db: Session, data: EventCreate) -> Event:
    """이벤트와 반복 인스턴스, 이동시간 하위 일정을 한 트랜잭션으로 만든다 — 중간에 실패하면 아무것도 남지 않는다."""
    _ensure_references_exist(db, data)
    event = Event(**data.model_dump())
    db.add(event)
    db.flush()

    if event.is_recurring and event.recurrence_rule and event.date_range_id is not None:
        generate_event_instances(db, event)

    # event 자신이 child가 아니고(parent_event_id 없음) 장소가 있으면, 이동시간
    # TRAVEL child를 자동 생성한다 (FR-5).
    if event.location_id is not None and event.parent_event_id is None:
        create_child_event(db, event)

    db.commit()
    db.refresh(event)
    return event


def get_event(db: Session, event_id: int) -> Event | None:
    return db.get(Event, event_id)


def list_events(db: Session, user_id: int | None = None) -> list[Event]:
    stmt = select(Event)
    if user_id is not None:
        stmt = stmt.where(Event.user_id == user_id)
    return list(db.execute(stmt).scalars().all())


def update_event(db: Session, event_id: int, data: EventUpdate) -> Event | None:
    event = db.get(Event, event_id)
    if event is None:
        return None

    _ensure_references_exist(db, data)
    changes = data.model_dump(exclude_unset=True)
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
    for field, value in changes.items():
        setattr(event, field, value)

    db.commit()
    db.refresh(event)
    return event


def delete_event(db: Session, event_id: int) -> bool:
    """이벤트와 그 인스턴스, 하위 일정(이동시간·준비)까지 삭제한다. 부모 없이 하위 일정만 남을 이유가 없다."""
    event = db.get(Event, event_id)
    if event is None:
        return False

    for child in list_child_events(db, event.id):
        db.delete(child)
    db.delete(event)
    db.commit()
    return True
