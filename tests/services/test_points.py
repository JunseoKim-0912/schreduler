from collections.abc import Iterator
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Event, EventInstance, EventInstanceStatus, Importance, PointsLedger, User
from app.services.points import (
    calculate_daily_points,
    calculate_streak_days,
    importance_weight,
    record_daily_points,
    streak_multiplier,
)

DONE = EventInstanceStatus.DONE
MISSED = EventInstanceStatus.MISSED
PENDING = EventInstanceStatus.PENDING
START = date(2026, 9, 1)


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def user_id(db: Session) -> int:
    user = User(name="June", preferred_language="ko")
    db.add(user)
    db.commit()
    return user.id


def _add(db: Session, user_id: int, day: date, status: EventInstanceStatus, importance: Importance | None) -> None:
    event = Event(
        user_id=user_id,
        title="일정",
        start_time=datetime.combine(day, datetime.min.time()).replace(hour=9),
        end_time=datetime.combine(day, datetime.min.time()).replace(hour=10),
        importance=importance,
    )
    db.add(event)
    db.flush()
    db.add(EventInstance(event_id=event.id, date=day, status=status))
    db.commit()


def _perfect_days(db: Session, user_id: int, count: int, start: date = START) -> date:
    for offset in range(count):
        _add(db, user_id, start + timedelta(days=offset), DONE, Importance.LEISURE)
    return start + timedelta(days=count - 1)


@pytest.mark.parametrize(
    ("importance", "weight"),
    [
        (None, 0),
        (Importance.LEISURE, 1),
        (Importance.SOCIAL, 2),
        (Importance.OBLIGATION_NO_CHECK, 3),
        (Importance.OFFICIAL, 4),
        (Importance.MUST, 5),
        (Importance.MAX, 10),
    ],
)
def test_importance_weight(importance: Importance | None, weight: int) -> None:
    assert importance_weight(importance) == weight


@pytest.mark.parametrize(
    ("days", "multiplier"),
    [(0, 1.0), (1, 1.0), (2, 1.0), (3, 1.1), (6, 1.1), (7, 1.25), (13, 1.25), (14, 1.5), (100, 1.5)],
)
def test_streak_multiplier_thresholds(days: int, multiplier: float) -> None:
    assert streak_multiplier(days) == multiplier


def test_base_points_count_only_done_instances(db: Session, user_id: int) -> None:
    _add(db, user_id, START, DONE, Importance.MAX)
    _add(db, user_id, START, DONE, Importance.OFFICIAL)
    _add(db, user_id, START, DONE, None)
    _add(db, user_id, START, MISSED, Importance.MUST)

    result = calculate_daily_points(db, user_id, START)

    assert result.base_points == 14  # 10 + 4 + 0, missed MUST는 0점
    assert (result.total_instances, result.done_instances) == (4, 3)
    assert result.is_perfect_day is False
    assert result.streak_days == 0
    assert result.points_earned == 14


def test_streak_bonus_applies_from_third_perfect_day(db: Session, user_id: int) -> None:
    day2 = _perfect_days(db, user_id, 2)
    assert calculate_daily_points(db, user_id, day2).streak_multiplier == 1.0

    day3 = day2 + timedelta(days=1)
    _add(db, user_id, day3, DONE, Importance.MUST)
    _add(db, user_id, day3, DONE, Importance.SOCIAL)

    result = calculate_daily_points(db, user_id, day3)
    assert result.streak_days == 3
    assert result.streak_multiplier == 1.1
    assert result.points_earned == 7.7  # (5 + 2) × 1.1, 부동소수 오차 없이 반올림


@pytest.mark.parametrize(("days", "multiplier"), [(7, 1.25), (14, 1.5), (20, 1.5)])
def test_longer_streaks(db: Session, user_id: int, days: int, multiplier: float) -> None:
    last = _perfect_days(db, user_id, days)

    result = calculate_daily_points(db, user_id, last)

    assert result.streak_days == days
    assert result.streak_multiplier == multiplier


def test_one_missed_instance_resets_streak(db: Session, user_id: int) -> None:
    last = _perfect_days(db, user_id, 5)
    miss_day = last + timedelta(days=1)
    _add(db, user_id, miss_day, DONE, Importance.MUST)
    _add(db, user_id, miss_day, MISSED, Importance.LEISURE)

    assert calculate_streak_days(db, user_id, miss_day) == 0
    assert calculate_daily_points(db, user_id, miss_day).points_earned == 5

    next_day = miss_day + timedelta(days=1)
    _add(db, user_id, next_day, DONE, Importance.LEISURE)
    assert calculate_streak_days(db, user_id, next_day) == 1


def test_pending_counts_as_incomplete(db: Session, user_id: int) -> None:
    last = _perfect_days(db, user_id, 3)
    day = last + timedelta(days=1)
    _add(db, user_id, day, PENDING, Importance.LEISURE)

    assert calculate_streak_days(db, user_id, day) == 0


def test_days_without_events_do_not_break_streak(db: Session, user_id: int) -> None:
    _perfect_days(db, user_id, 2, start=START)
    _perfect_days(db, user_id, 1, start=START + timedelta(days=4))  # 9/3, 9/4는 일정 없음

    assert calculate_streak_days(db, user_id, START + timedelta(days=4)) == 3


def test_streak_ignores_future_days_and_other_users(db: Session, user_id: int) -> None:
    other = User(name="Other", preferred_language="ko")
    db.add(other)
    db.commit()
    _add(db, other.id, START, MISSED, Importance.LEISURE)
    last = _perfect_days(db, user_id, 3)
    _add(db, user_id, last + timedelta(days=1), MISSED, Importance.LEISURE)

    assert calculate_streak_days(db, user_id, last) == 3


def test_record_daily_points_upserts_ledger(db: Session, user_id: int) -> None:
    last = _perfect_days(db, user_id, 3)

    first = record_daily_points(db, user_id, last)
    assert (first.base_points, first.streak_multiplier, first.points_earned) == (1, 1.1, 1.1)

    _add(db, user_id, last, MISSED, Importance.MUST)
    second = record_daily_points(db, user_id, last)

    assert second.id == first.id
    assert (second.base_points, second.streak_multiplier, second.points_earned) == (1, 1.0, 1)
    assert db.query(PointsLedger).count() == 1


# --- 엣지 케이스 ---


def test_no_records_at_all(db: Session, user_id: int) -> None:
    result = calculate_daily_points(db, user_id, START)

    assert calculate_streak_days(db, user_id, START) == 0
    assert (result.is_perfect_day, result.base_points, result.streak_multiplier, result.points_earned) == (
        False,
        0,
        1.0,
        0,
    )


def test_first_ever_day_missed(db: Session, user_id: int) -> None:
    _add(db, user_id, START, MISSED, Importance.MUST)

    result = calculate_daily_points(db, user_id, START)

    assert (result.streak_days, result.streak_multiplier, result.points_earned) == (0, 1.0, 0)


def test_first_ever_day_partially_done_keeps_base_points(db: Session, user_id: int) -> None:
    _add(db, user_id, START, DONE, Importance.MUST)
    _add(db, user_id, START, MISSED, Importance.LEISURE)

    result = calculate_daily_points(db, user_id, START)

    assert (result.streak_days, result.streak_multiplier, result.points_earned) == (0, 1.0, 5)


def test_first_ever_day_perfect_starts_streak_at_one(db: Session, user_id: int) -> None:
    _add(db, user_id, START, DONE, Importance.MUST)

    result = calculate_daily_points(db, user_id, START)

    assert (result.streak_days, result.streak_multiplier) == (1, 1.0)


def test_target_date_before_any_record(db: Session, user_id: int) -> None:
    _perfect_days(db, user_id, 5, start=START + timedelta(days=10))

    assert calculate_streak_days(db, user_id, START) == 0


def test_empty_day_keeps_streak_but_records_no_bonus(db: Session, user_id: int) -> None:
    last = _perfect_days(db, user_id, 3)
    empty_day = last + timedelta(days=1)

    result = calculate_daily_points(db, user_id, empty_day)
    assert result.streak_days == 3
    assert (result.is_perfect_day, result.streak_multiplier, result.points_earned) == (False, 1.0, 0)

    entry = record_daily_points(db, user_id, empty_day)
    assert entry.streak_multiplier == 1.0


def test_missed_day_before_streak_is_not_counted(db: Session, user_id: int) -> None:
    _add(db, user_id, START, MISSED, Importance.LEISURE)
    last = _perfect_days(db, user_id, 7, start=START + timedelta(days=1))

    assert calculate_streak_days(db, user_id, last) == 7
    assert calculate_streak_days(db, user_id, START) == 0


def test_older_failure_does_not_affect_streak_after_recovery(db: Session, user_id: int) -> None:
    _perfect_days(db, user_id, 10)
    _add(db, user_id, START + timedelta(days=10), MISSED, Importance.LEISURE)
    last = _perfect_days(db, user_id, 3, start=START + timedelta(days=11))

    assert calculate_streak_days(db, user_id, last) == 3
    assert calculate_streak_days(db, user_id, START + timedelta(days=9)) == 10  # 과거 날짜 재계산도 그대로
