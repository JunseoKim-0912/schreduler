from __future__ import annotations

from datetime import datetime

from dateutil.rrule import rrulestr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import EventInstanceStatus
from app.models.event import Event
from app.models.event_instance import EventInstance


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

    # DEADLINE 이벤트는 start_time이 없으므로 마감 시각(end_time)을 기준으로 반복 날짜를 계산한다.
    anchor = event.anchor_time.time()
    dtstart = datetime.combine(date_range.start_date, anchor)
    until = datetime.combine(date_range.end_date, anchor)

    rule = rrulestr(event.recurrence_rule, dtstart=dtstart)
    occurrence_dates = {occurrence.date() for occurrence in rule.between(dtstart, until, inc=True)}
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


def build_recurrence_rule(frequency: str, by_day: list[str] | None) -> str:
    """frequency("WEEKLY" 등)와 by_day(["MO"] 등)로 RRULE 문자열을 조립한다.

    예: build_recurrence_rule("WEEKLY", ["MO"]) -> "FREQ=WEEKLY;BYDAY=MO".
    generate_event_instances가 파싱하는 포맷과 그대로 짝을 이룬다.
    """
    parts = [f"FREQ={frequency}"]
    if by_day:
        parts.append(f"BYDAY={','.join(by_day)}")
    return ";".join(parts)
