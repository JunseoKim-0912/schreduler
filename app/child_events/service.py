from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import ChildEventKind, Importance
from app.models.event import Event

TRAVEL_CHILD_TITLE_PREFIX = "이동"


def list_child_events(session: Session, parent_event_id: int) -> list[Event]:
    """parent_event_id에 딸린 모든 Child 이벤트를 종류(TRAVEL/CUSTOM) 상관없이 반환한다."""
    stmt = select(Event).where(Event.parent_event_id == parent_event_id)
    return list(session.execute(stmt).scalars().all())


def get_travel_child_event(session: Session, parent_event_id: int) -> Event | None:
    """TRAVEL Child만 조회한다. CUSTOM Child는 이동시간 재배치 대상이 아니므로 제외한다."""
    stmt = select(Event).where(
        Event.parent_event_id == parent_event_id,
        Event.child_kind == ChildEventKind.TRAVEL,
    )
    return session.execute(stmt).scalars().first()


def find_next_chained_event(session: Session, event: Event) -> Event | None:
    """event와 같은 장소에서 열리는, event 종료 이후의 다음 이벤트를 찾는다.

    같은 장소로 이어지는 연속된 이벤트들 중 맨 앞 이벤트에만 이동시간 Child가 붙으므로
    (뒤 이벤트는 이미 그 장소에 있다고 가정), Child 이벤트 자신은 후보에서 제외한다.
    """
    if event.location_id is None:
        return None

    stmt = (
        select(Event)
        .where(
            Event.user_id == event.user_id,
            Event.location_id == event.location_id,
            Event.parent_event_id.is_(None),
            Event.id != event.id,
            Event.start_time >= event.end_time,
        )
        .order_by(Event.start_time)
    )
    return session.execute(stmt).scalars().first()


def create_child_event(session: Session, parent_event: Event) -> Event | None:
    """parent_event 시작 전 이동시간만큼의 TRAVEL Child 이벤트를 생성한다 (FR-5)."""
    location = parent_event.location
    if location is None:
        return None

    child = Event(
        user_id=parent_event.user_id,
        title=f"{TRAVEL_CHILD_TITLE_PREFIX}: {parent_event.title}",
        start_time=parent_event.start_time - timedelta(minutes=location.default_travel_minutes),
        end_time=parent_event.start_time,
        importance=parent_event.importance,
        parent_event_id=parent_event.id,
        child_kind=ChildEventKind.TRAVEL,
        location_id=parent_event.location_id,
    )
    session.add(child)
    session.flush()
    return child


def create_custom_child_event(
    session: Session,
    parent_event: Event,
    title: str,
    start_time: datetime,
    end_time: datetime,
    importance: Importance | None = None,
) -> Event:
    """사용자가 직접 정의하는 준비 Child 이벤트를 생성한다 (예: 면접 전 "준비" 30분).

    TRAVEL Child와 달리 Location 없이도 만들 수 있고, 이동시간 체인 재배치(다음 이벤트로
    옮겨붙는 것) 대상은 아니다. 다만 부모가 missed되면 reconcile_missed_event에 의해
    TRAVEL Child와 마찬가지로 삭제된다 — 부모가 없어졌는데 그 준비만 남을 이유는 없다.
    """
    child = Event(
        user_id=parent_event.user_id,
        title=title,
        start_time=start_time,
        end_time=end_time,
        importance=importance if importance is not None else parent_event.importance,
        parent_event_id=parent_event.id,
        child_kind=ChildEventKind.CUSTOM,
    )
    session.add(child)
    session.flush()
    return child


def reconcile_missed_event(session: Session, missed_event: Event) -> Event | None:
    """missed_event가 미준수 처리됐을 때 딸린 Child 이벤트들을 정리한다 (FR-5).

    - CUSTOM Child(사용자 지정 준비 등)는 부모가 없어졌으므로 전부 삭제한다.
    - TRAVEL Child는 더 이상 필요 없으므로 제거하되, 같은 장소로 이어지는 다음 이벤트가
      아직 남아있으며 자신의 TRAVEL Child가 없다면 그 이벤트 앞으로 새로 생성한다.
    """
    for child in list_child_events(session, missed_event.id):
        if child.child_kind == ChildEventKind.CUSTOM:
            session.delete(child)

    travel_child = get_travel_child_event(session, missed_event.id)
    if travel_child is None:
        session.flush()
        return None

    session.delete(travel_child)
    session.flush()

    next_event = find_next_chained_event(session, missed_event)
    if next_event is None or get_travel_child_event(session, next_event.id) is not None:
        return None

    return create_child_event(session, next_event)
