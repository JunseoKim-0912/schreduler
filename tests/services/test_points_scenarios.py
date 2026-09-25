"""FR-10 포인트를 여러 날에 걸쳐 시뮬레이션하는 시나리오 테스트.

매일 밤 자정 잡이 하는 것처럼 하루씩 일정 결과를 넣고 record_daily_points로 PointsLedger에 기록한 뒤,
날짜별 streak/배율/점수와 누적 합계가 기대대로 쌓이는지 확인한다.
"""

from collections.abc import Iterator
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import Base, Event, EventInstance, EventInstanceStatus, Importance, PointsLedger, User
from app.services.points import calculate_daily_points, get_points_summary, record_daily_points

DONE = EventInstanceStatus.DONE
MISSED = EventInstanceStatus.MISSED
DAY_1 = date(2026, 9, 1)

# 매일 MUST(5점) + LEISURE(1점) 두 개 → 전부 완료하면 기본 6점
DAILY_PLAN = (Importance.MUST, Importance.LEISURE)
PERFECT = (DONE, DONE)
LEISURE_MISSED = (DONE, MISSED)  # MUST만 완료 → 기본 5점, 100% 아님


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


def _day(n: int) -> date:
    return DAY_1 + timedelta(days=n - 1)


def _play_day(db: Session, user_id: int, n: int, statuses: tuple[EventInstanceStatus, ...]) -> PointsLedger:
    """n일차 일정 결과를 넣고, 그날 밤 자정 잡처럼 포인트를 기록한다."""
    day = _day(n)
    for importance, status in zip(DAILY_PLAN, statuses, strict=True):
        event = Event(
            user_id=user_id,
            title=f"{n}일차 {importance.name}",
            start_time=datetime(day.year, day.month, day.day, 9),
            end_time=datetime(day.year, day.month, day.day, 10),
            importance=importance,
        )
        db.add(event)
        db.flush()
        db.add(EventInstance(event_id=event.id, date=day, status=status))
    db.commit()
    return record_daily_points(db, user_id, day)


def _ledger_total(db: Session, user_id: int) -> float:
    return round(db.scalar(select(func.sum(PointsLedger.points_earned)).where(PointsLedger.user_id == user_id)), 2)


# --- 시나리오 1: 연속 완료 ---


def test_consecutive_completion_climbs_every_bonus_tier(db: Session, user_id: int) -> None:
    """15일 연속 100% 완료: 3일째 1.1, 7일째 1.25, 14일째 1.5로 올라간다."""
    expected = {
        1: (1, 1.0, 6),
        2: (2, 1.0, 6),
        3: (3, 1.1, 6.6),
        6: (6, 1.1, 6.6),
        7: (7, 1.25, 7.5),
        13: (13, 1.25, 7.5),
        14: (14, 1.5, 9),
        15: (15, 1.5, 9),
    }

    for n in range(1, 16):
        entry = _play_day(db, user_id, n, PERFECT)
        streak = calculate_daily_points(db, user_id, _day(n)).streak_days
        assert streak == n
        if n in expected:
            assert (streak, entry.streak_multiplier, entry.points_earned) == expected[n], f"{n}일차"

    # 6×2 + 6.6×4 + 7.5×7 + 9×2
    assert _ledger_total(db, user_id) == 108.9
    assert db.scalar(select(func.count(PointsLedger.id))) == 15


def test_consecutive_completion_skips_days_without_events(db: Session, user_id: int) -> None:
    """일정 없는 날(주말 등)이 끼어도 연속 기록은 이어지고, 그날은 0점·보너스 없음으로 기록된다."""
    for n in range(1, 4):
        _play_day(db, user_id, n, PERFECT)
    rest = record_daily_points(db, user_id, _day(4))
    fourth = _play_day(db, user_id, 5, PERFECT)

    # 쉬는 날 시점의 streak는 3(보너스 구간)이지만, 그날 받은 보너스는 없어야 한다
    assert calculate_daily_points(db, user_id, _day(4)).streak_days == 3
    assert (rest.points_earned, rest.streak_multiplier) == (0, 1.0)
    assert calculate_daily_points(db, user_id, _day(5)).streak_days == 4
    assert fourth.streak_multiplier == 1.1


# --- 시나리오 2: 중간에 하루 미완료 → streak 리셋 ---


def test_one_incomplete_day_resets_streak_and_bonus(db: Session, user_id: int) -> None:
    """5일 연속 완료 → 6일차에 하나 놓침 → 7일차부터 다시 1일부터 센다."""
    for n in range(1, 6):
        _play_day(db, user_id, n, PERFECT)
    assert calculate_daily_points(db, user_id, _day(5)).streak_multiplier == 1.1

    miss = _play_day(db, user_id, 6, LEISURE_MISSED)
    assert calculate_daily_points(db, user_id, _day(6)).streak_days == 0
    assert (miss.base_points, miss.streak_multiplier, miss.points_earned) == (5, 1.0, 5)

    after = {n: _play_day(db, user_id, n, PERFECT) for n in range(7, 11)}
    streaks = [calculate_daily_points(db, user_id, _day(n)).streak_days for n in range(7, 11)]
    assert streaks == [1, 2, 3, 4]
    assert [after[n].streak_multiplier for n in range(7, 11)] == [1.0, 1.0, 1.1, 1.1]

    # 6×2 + 6.6×3 (1~5일차) + 5 (6일차) + 6×2 + 6.6×2 (7~10일차)
    assert _ledger_total(db, user_id) == 62.0


def test_reset_from_top_tier_drops_back_to_base(db: Session, user_id: int) -> None:
    """14일 연속으로 1.5배까지 올라간 뒤 하루 놓치면, 다음 날은 1.0배부터 다시 시작한다."""
    for n in range(1, 15):
        _play_day(db, user_id, n, PERFECT)
    assert calculate_daily_points(db, user_id, _day(14)).streak_multiplier == 1.5

    _play_day(db, user_id, 15, LEISURE_MISSED)
    next_day = _play_day(db, user_id, 16, PERFECT)

    assert calculate_daily_points(db, user_id, _day(16)).streak_days == 1
    assert (next_day.streak_multiplier, next_day.points_earned) == (1.0, 6)


def test_fully_missed_day_also_resets(db: Session, user_id: int) -> None:
    """그날 일정을 하나도 완료하지 못한 경우: 0점이고 streak도 끊긴다."""
    for n in range(1, 4):
        _play_day(db, user_id, n, PERFECT)

    miss = _play_day(db, user_id, 4, (MISSED, MISSED))
    after = _play_day(db, user_id, 5, PERFECT)

    assert (miss.base_points, miss.points_earned) == (0, 0)
    assert calculate_daily_points(db, user_id, _day(5)).streak_days == 1
    assert after.streak_multiplier == 1.0


def test_reset_does_not_rewrite_past_days(db: Session, user_id: int) -> None:
    """리셋 이후 과거 날짜를 다시 계산해도, 그날 기준 streak와 보너스는 그대로다."""
    for n in range(1, 4):
        _play_day(db, user_id, n, PERFECT)
    _play_day(db, user_id, 4, LEISURE_MISSED)

    recomputed = record_daily_points(db, user_id, _day(3))

    assert calculate_daily_points(db, user_id, _day(3)).streak_days == 3
    assert (recomputed.streak_multiplier, recomputed.points_earned) == (1.1, 6.6)


def test_summary_after_reset_scenario(db: Session, user_id: int) -> None:
    """리셋 시나리오 뒤 요약 API 값: 누적은 ledger 합계, 진행 중인 streak는 리셋 이후만 센다."""
    for n in range(1, 6):
        _play_day(db, user_id, n, PERFECT)
    _play_day(db, user_id, 6, LEISURE_MISSED)
    _play_day(db, user_id, 7, PERFECT)
    _play_day(db, user_id, 8, PERFECT)

    summary = get_points_summary(db, user_id, today=_day(9))  # 9일차는 아직 일정 없음

    assert summary.current_streak_days == 2
    assert summary.total_points == _ledger_total(db, user_id) == 48.8  # 6×2 + 6.6×3 + 5 + 6×2
