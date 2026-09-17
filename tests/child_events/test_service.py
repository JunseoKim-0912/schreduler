from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.child_events.service import (
    create_child_event,
    create_custom_child_event,
    get_travel_child_event,
    list_child_events,
    reconcile_missed_event,
)
from app.models import Base, ChildEventKind, Event, EventInstance, ImportantDateRange, Importance, Location, User


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _make_user_and_location(session: Session, travel_minutes: int = 40) -> tuple[User, Location]:
    user = User(name="June", preferred_language="ko")
    session.add(user)
    session.flush()

    location = Location(user_id=user.id, name="학교", default_travel_minutes=travel_minutes)
    session.add(location)
    session.flush()
    return user, location


def test_reconcile_moves_child_event_to_next_same_location_event(session: Session) -> None:
    user, location = _make_user_and_location(session, travel_minutes=40)

    event1 = Event(
        user_id=user.id,
        title="이벤트1",
        start_time=datetime(2026, 9, 17, 9, 0),
        end_time=datetime(2026, 9, 17, 10, 0),
        location_id=location.id,
    )
    event2 = Event(
        user_id=user.id,
        title="이벤트2",
        start_time=datetime(2026, 9, 17, 10, 0),
        end_time=datetime(2026, 9, 17, 11, 0),
        location_id=location.id,
    )
    session.add_all([event1, event2])
    session.flush()

    original_child = create_child_event(session, event1)
    assert original_child is not None
    assert original_child.start_time == datetime(2026, 9, 17, 8, 20)
    assert original_child.end_time == datetime(2026, 9, 17, 9, 0)

    new_child = reconcile_missed_event(session, event1)

    assert get_travel_child_event(session, event1.id) is None  # 기존 child는 제거됨
    assert new_child is not None
    assert new_child.parent_event_id == event2.id
    assert new_child.start_time == datetime(2026, 9, 17, 9, 20)
    assert new_child.end_time == datetime(2026, 9, 17, 10, 0)


def test_reconcile_does_nothing_without_a_next_same_location_event(session: Session) -> None:
    user, location = _make_user_and_location(session)

    event1 = Event(
        user_id=user.id,
        title="이벤트1",
        start_time=datetime(2026, 9, 17, 9, 0),
        end_time=datetime(2026, 9, 17, 10, 0),
        location_id=location.id,
    )
    session.add(event1)
    session.flush()
    create_child_event(session, event1)

    result = reconcile_missed_event(session, event1)

    assert result is None
    assert get_travel_child_event(session, event1.id) is None


def test_reconcile_skips_events_that_already_have_their_own_child(session: Session) -> None:
    user, location = _make_user_and_location(session)

    event1 = Event(
        user_id=user.id,
        title="이벤트1",
        start_time=datetime(2026, 9, 17, 9, 0),
        end_time=datetime(2026, 9, 17, 10, 0),
        location_id=location.id,
    )
    event2 = Event(
        user_id=user.id,
        title="이벤트2",
        start_time=datetime(2026, 9, 17, 10, 0),
        end_time=datetime(2026, 9, 17, 11, 0),
        location_id=location.id,
    )
    session.add_all([event1, event2])
    session.flush()
    create_child_event(session, event1)
    create_child_event(session, event2)

    result = reconcile_missed_event(session, event1)

    assert result is None
    assert get_travel_child_event(session, event1.id) is None
    assert get_travel_child_event(session, event2.id) is not None


def test_reconcile_returns_none_when_missed_event_had_no_child(session: Session) -> None:
    user, location = _make_user_and_location(session)
    event = Event(
        user_id=user.id,
        title="이벤트",
        start_time=datetime(2026, 9, 17, 9, 0),
        end_time=datetime(2026, 9, 17, 10, 0),
        location_id=location.id,
    )
    session.add(event)
    session.flush()

    assert reconcile_missed_event(session, event) is None


def test_create_custom_child_event_does_not_require_a_location(session: Session) -> None:
    user = User(name="June", preferred_language="ko")
    session.add(user)
    session.flush()

    interview = Event(
        user_id=user.id,
        title="온라인 면접",
        start_time=datetime(2026, 9, 17, 17, 0),
        end_time=datetime(2026, 9, 17, 17, 30),
        importance=Importance.MUST,
    )
    session.add(interview)
    session.flush()

    prep = create_custom_child_event(
        session,
        interview,
        title="면접 준비",
        start_time=datetime(2026, 9, 17, 16, 30),
        end_time=datetime(2026, 9, 17, 17, 0),
    )

    assert prep.parent_event_id == interview.id
    assert prep.child_kind == ChildEventKind.CUSTOM
    assert prep.location_id is None
    assert prep.importance == Importance.MUST  # 지정 안 하면 부모에서 상속
    assert prep in list_child_events(session, interview.id)


def test_reconcile_missed_event_also_deletes_custom_children(session: Session) -> None:
    user, location = _make_user_and_location(session)

    event1 = Event(
        user_id=user.id,
        title="이벤트1",
        start_time=datetime(2026, 9, 17, 9, 0),
        end_time=datetime(2026, 9, 17, 10, 0),
        location_id=location.id,
    )
    session.add(event1)
    session.flush()

    create_child_event(session, event1)
    create_custom_child_event(
        session,
        event1,
        title="사전 자료 검토",
        start_time=datetime(2026, 9, 17, 8, 0),
        end_time=datetime(2026, 9, 17, 8, 20),
    )

    reconcile_missed_event(session, event1)

    assert list_child_events(session, event1.id) == []  # TRAVEL, CUSTOM 모두 제거됨


def test_reconcile_missed_event_deletes_custom_child_even_without_travel_child(
    session: Session,
) -> None:
    user = User(name="June", preferred_language="ko")
    session.add(user)
    session.flush()

    interview = Event(
        user_id=user.id,
        title="온라인 면접",
        start_time=datetime(2026, 9, 17, 17, 0),
        end_time=datetime(2026, 9, 17, 17, 30),
        importance=Importance.MUST,
    )
    session.add(interview)
    session.flush()

    create_custom_child_event(
        session,
        interview,
        title="면접 준비",
        start_time=datetime(2026, 9, 17, 16, 30),
        end_time=datetime(2026, 9, 17, 17, 0),
    )

    result = reconcile_missed_event(session, interview)

    assert result is None
    assert list_child_events(session, interview.id) == []


def test_create_child_event_propagates_recurrence_and_generates_instances(
    session: Session,
) -> None:
    user, location = _make_user_and_location(session, travel_minutes=40)
    date_range = ImportantDateRange(
        user_id=user.id,
        name="2026 가을학기",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
    )
    session.add(date_range)
    session.flush()

    parent = Event(
        user_id=user.id,
        title="월요일 수업",
        start_time=datetime(2026, 9, 7, 9, 0),
        end_time=datetime(2026, 9, 7, 10, 0),
        location_id=location.id,
        is_recurring=True,
        recurrence_rule="FREQ=WEEKLY;BYDAY=MO",  # 매주 월요일
        date_range_id=date_range.id,
    )
    session.add(parent)
    session.flush()

    child = create_child_event(session, parent)

    assert child is not None
    assert child.is_recurring is True
    assert child.recurrence_rule == parent.recurrence_rule
    assert child.date_range_id == parent.date_range_id

    child_instance_dates = sorted(
        session.execute(
            select(EventInstance.date).where(EventInstance.event_id == child.id)
        )
        .scalars()
        .all()
    )
    # 2026년 9월의 월요일: 7, 14, 21, 28 -> 부모와 동일한 날짜에 child 인스턴스도 생겨야 함
    assert child_instance_dates == [date(2026, 9, 7), date(2026, 9, 14), date(2026, 9, 21), date(2026, 9, 28)]


def test_create_child_event_does_not_recur_when_parent_is_not_recurring(
    session: Session,
) -> None:
    user, location = _make_user_and_location(session)
    parent = Event(
        user_id=user.id,
        title="일회성 수업",
        start_time=datetime(2026, 9, 7, 9, 0),
        end_time=datetime(2026, 9, 7, 10, 0),
        location_id=location.id,
    )
    session.add(parent)
    session.flush()

    child = create_child_event(session, parent)

    assert child is not None
    assert child.is_recurring is False
    assert child.recurrence_rule is None
    assert child.date_range_id is None
    assert (
        session.execute(select(EventInstance).where(EventInstance.event_id == child.id))
        .scalars()
        .first()
        is None
    )
