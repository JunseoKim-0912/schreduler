from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.child_events.service import create_child_event
from app.models import Base, Event, Importance, Location, User


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _make_user_and_location(session: Session, travel_minutes: int) -> tuple[User, Location]:
    user = User(name="June", preferred_language="ko")
    session.add(user)
    session.flush()

    location = Location(user_id=user.id, name="학교", default_travel_minutes=travel_minutes)
    session.add(location)
    session.flush()
    return user, location


def test_child_event_inherits_parent_importance(session: Session) -> None:
    user, location = _make_user_and_location(session, travel_minutes=30)
    parent = Event(
        user_id=user.id,
        title="퀴즈",
        start_time=datetime(2026, 9, 17, 9, 0),
        end_time=datetime(2026, 9, 17, 10, 0),
        location_id=location.id,
        importance=Importance.OFFICIAL,
    )
    session.add(parent)
    session.flush()

    child = create_child_event(session, parent)

    assert child is not None
    assert child.importance == parent.importance == Importance.OFFICIAL


def test_child_event_starts_exactly_default_travel_minutes_before_parent(
    session: Session,
) -> None:
    user, location = _make_user_and_location(session, travel_minutes=25)
    parent = Event(
        user_id=user.id,
        title="수업",
        start_time=datetime(2026, 9, 17, 9, 0),
        end_time=datetime(2026, 9, 17, 10, 0),
        location_id=location.id,
    )
    session.add(parent)
    session.flush()

    child = create_child_event(session, parent)

    assert child is not None
    assert child.end_time == parent.start_time
    assert child.start_time == parent.start_time - timedelta(minutes=location.default_travel_minutes)
    assert child.start_time == datetime(2026, 9, 17, 8, 35)
