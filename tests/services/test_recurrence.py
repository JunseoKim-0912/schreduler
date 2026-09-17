from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Event, ImportantDateRange, User
from app.services.recurrence import generate_event_instances


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _make_user(session: Session) -> User:
    user = User(name="June", preferred_language="ko")
    session.add(user)
    session.flush()
    return user


def test_generate_event_instances_creates_one_per_matching_weekday(session: Session) -> None:
    user = _make_user(session)
    date_range = ImportantDateRange(
        user_id=user.id,
        name="2026 가을학기",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
    )
    session.add(date_range)
    session.flush()

    event = Event(
        user_id=user.id,
        title="월요일 수업",
        start_time=datetime(2026, 9, 1, 9, 0),
        end_time=datetime(2026, 9, 1, 10, 0),
        is_recurring=True,
        recurrence_rule="FREQ=WEEKLY;BYDAY=MO",  # 매주 월요일
        date_range_id=date_range.id,
    )
    session.add(event)
    session.flush()

    created = generate_event_instances(session, event)

    assert [instance.date.weekday() for instance in created] == [0] * len(created)
    assert all(date_range.start_date <= instance.date <= date_range.end_date for instance in created)
    assert len(created) == 4  # 2026년 9월의 월요일: 7, 14, 21, 28


def test_generate_event_instances_is_idempotent(session: Session) -> None:
    user = _make_user(session)
    date_range = ImportantDateRange(
        user_id=user.id,
        name="2026 가을학기",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
    )
    session.add(date_range)
    session.flush()

    event = Event(
        user_id=user.id,
        title="월요일 수업",
        start_time=datetime(2026, 9, 1, 9, 0),
        end_time=datetime(2026, 9, 1, 10, 0),
        is_recurring=True,
        recurrence_rule="FREQ=WEEKLY;BYDAY=MO",
        date_range_id=date_range.id,
    )
    session.add(event)
    session.flush()

    first_call = generate_event_instances(session, event)
    second_call = generate_event_instances(session, event)

    assert len(first_call) == 4
    assert second_call == []


def test_generate_event_instances_requires_recurring_flag(session: Session) -> None:
    user = _make_user(session)
    event = Event(
        user_id=user.id,
        title="단발 이벤트",
        start_time=datetime(2026, 9, 1, 9, 0),
        end_time=datetime(2026, 9, 1, 10, 0),
        is_recurring=False,
    )
    session.add(event)
    session.flush()

    with pytest.raises(ValueError):
        generate_event_instances(session, event)


def test_generate_event_instances_requires_date_range(session: Session) -> None:
    user = _make_user(session)
    event = Event(
        user_id=user.id,
        title="월요일 수업",
        start_time=datetime(2026, 9, 1, 9, 0),
        end_time=datetime(2026, 9, 1, 10, 0),
        is_recurring=True,
        recurrence_rule="FREQ=WEEKLY;BYDAY=MO",
    )
    session.add(event)
    session.flush()

    with pytest.raises(ValueError):
        generate_event_instances(session, event)
