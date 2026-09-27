from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import EventInstanceStatus
from app.models.event import Event
from app.models.event_instance import EventInstance
from app.services.draft_rules import build_rrule as build_recurrence_rule
from app.services.draft_rules import occurrences

__all__ = [
    "build_recurrence_rule",
    "create_single_instance",
    "follow_single_instance",
    "generate_event_instances",
    "occurrences",
]


def generate_event_instances(session: Session, event: Event) -> list[EventInstance]:
    """반복 이벤트의 EventInstance를 미리 생성한다.

    event.recurrence_rule은 RFC 5545 RRULE 문자열이다 (예: "FREQ=WEEKLY;BYDAY=MO"는
    "매주 월요일"). event.date_range_id가 가리키는 ImportantDateRange의
    start_date~end_date 구간에서 규칙에 맞는 날짜마다 EventInstance를 만든다.
    이미 그 날짜의 EventInstance가 있으면 건너뛴다 (여러 번 호출해도 안전).
    """
    if not event.is_recurring:
        raise ValueError("event.is_recurring이 False인 이벤트는 인스턴스를 생성할 수 없습니다")
    if not event.recurrence_rule:
        raise ValueError("event.recurrence_rule이 없습니다")

    date_range = event.date_range
    if date_range is None:
        raise ValueError("event.date_range_id가 가리키는 ImportantDateRange가 없습니다")

    # DEADLINE 이벤트는 start_time이 없으므로 마감 시각(end_time)이 기준이다(anchor_time).
    occurrence_dates = set(occurrences(event.recurrence_rule, event.anchor_time, date_range.start_date, date_range.end_date))
    if not occurrence_dates:
        return []

    existing_dates = set(
        session.execute(
            select(EventInstance.date).where(
                EventInstance.event_id == event.id,
                EventInstance.date.in_(occurrence_dates),
            )
        )
        .scalars()
        .all()
    )

    created: list[EventInstance] = []
    for occurrence_date in sorted(occurrence_dates - existing_dates):
        instance = EventInstance(
            event_id=event.id,
            date=occurrence_date,
            status=EventInstanceStatus.PENDING,
        )
        session.add(instance)
        created.append(instance)

    session.flush()
    return created


def create_single_instance(session: Session, event: Event) -> EventInstance:
    """비반복 이벤트의 회차 하나를 일정 날짜(deadline이면 마감 날짜)에 만든다. 이미 있으면 그대로 둔다.

    알림(FR-4)·미준수 사유(FR-6)·체크인(FR-8)·포인트(FR-10)가 모두 EventInstance 기준이라,
    단발 일정도 회차가 있어야 이 기능들에 잡힌다.
    """
    existing = session.execute(select(EventInstance).where(EventInstance.event_id == event.id)).scalars().first()
    if existing is not None:
        return existing
    instance = EventInstance(event_id=event.id, date=event.anchor_time.date(), status=EventInstanceStatus.PENDING)
    session.add(instance)
    session.flush()
    return instance


def follow_single_instance(session: Session, event: Event) -> None:
    """비반복 이벤트는 회차가 하나뿐이라 이벤트의 날짜·시각을 그대로 따른다 (수정 뒤 호출)."""
    if event.is_recurring:
        return
    for instance in session.execute(select(EventInstance).where(EventInstance.event_id == event.id)).scalars():
        instance.date = event.anchor_time.date()
        instance.start_time_override = None
        instance.end_time_override = None
