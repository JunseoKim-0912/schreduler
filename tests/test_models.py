from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Event, Importance, User


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def user(session: Session) -> User:
    user = User(name="June", preferred_language="ko")
    session.add(user)
    session.flush()
    return user


def _event_kwargs(user: User, **overrides: object) -> dict[str, object]:
    kwargs = {
        "user_id": user.id,
        "title": "이벤트",
        "start_time": datetime(2026, 9, 17, 9, 0),
        "end_time": datetime(2026, 9, 17, 10, 0),
    }
    kwargs.update(overrides)
    return kwargs


def test_event_accepts_none_importance_for_sleep(session: Session, user: User) -> None:
    event = Event(**_event_kwargs(user, importance=None))

    assert event.importance is None


@pytest.mark.parametrize("value", [Importance.LEISURE, Importance.MUST, Importance.MAX, 1, 6])
def test_event_accepts_valid_importance_values(session: Session, user: User, value: object) -> None:
    event = Event(**_event_kwargs(user, importance=value))

    assert event.importance == Importance(value)


@pytest.mark.parametrize("value", [0, 7, -1, 100])
def test_event_rejects_out_of_range_importance(session: Session, user: User, value: int) -> None:
    with pytest.raises(ValueError):
        Event(**_event_kwargs(user, importance=value))


def test_event_rejects_out_of_range_importance_on_assignment(session: Session, user: User) -> None:
    event = Event(**_event_kwargs(user, importance=Importance.MUST))

    with pytest.raises(ValueError):
        event.importance = 99  # type: ignore[assignment]
