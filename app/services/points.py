"""FR-10 규칙성 기반 포인트.

이벤트 점수 = 중요도 가중치 × 완료 여부. 하루 점수(base_points)는 그날 EventInstance 점수의 합이고,
연속 100% 완료 일수(streak)에 따라 배율을 곱한다 (3일 ×1.1, 7일 ×1.25, 14일 ×1.5).
"""

from __future__ import annotations

import logging
from datetime import date as dt_date
from datetime import timedelta

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session, contains_eager

from app.core.db import SessionLocal
from app.core.scheduler import scheduler
from app.models.enums import EventInstanceStatus, Importance
from app.models.event import Event
from app.models.event_instance import EventInstance
from app.models.points_ledger import PointsLedger
from app.models.user import User
from app.schemas.points import DailyPointsResult, PointsSummaryRead

logger = logging.getLogger(__name__)

DAILY_POINTS_JOB_ID = "daily_points_calculation"
DAILY_POINTS_HOUR = 0
DAILY_POINTS_MINUTE = 0
# 서버 재시작 등으로 자정 실행이 늦어져도 이 시간 안이면 건너뛰지 않고 실행한다
# (record_daily_points가 같은 날짜를 갱신하므로 늦게/두 번 돌아도 안전하다).
DAILY_POINTS_MISFIRE_GRACE_SECONDS = 6 * 60 * 60

NO_IMPORTANCE_WEIGHT = 0
MAX_IMPORTANCE_WEIGHT = 10

# (최소 연속 일수, 배율) — 큰 기준부터 검사한다.
STREAK_MULTIPLIERS: tuple[tuple[int, float], ...] = ((14, 1.5), (7, 1.25), (3, 1.1))


def importance_weight(importance: Importance | None) -> int:
    """없음(수면)=0, 1~5는 그 값, MAX=10."""
    if importance is None:
        return NO_IMPORTANCE_WEIGHT
    if importance == Importance.MAX:
        return MAX_IMPORTANCE_WEIGHT
    return int(importance)


def streak_multiplier(streak_days: int) -> float:
    for min_days, multiplier in STREAK_MULTIPLIERS:
        if streak_days >= min_days:
            return multiplier
    return 1.0


def _instances_on(db: Session, user_id: int, target_date: dt_date) -> list[EventInstance]:
    stmt = (
        select(EventInstance)
        .join(Event, EventInstance.event_id == Event.id)
        .options(contains_eager(EventInstance.event))
        .where(Event.user_id == user_id, EventInstance.date == target_date)
    )
    return list(db.execute(stmt).scalars().all())


def calculate_streak_days(db: Session, user_id: int, target_date: dt_date) -> int:
    """target_date까지(당일 포함) 이어진 100% 완료 일수를 센다.

    일정이 하나도 없는 날은 streak를 끊지도 늘리지도 않는다(그런 날은 조회 결과에 아예 없다).
    PENDING이 남은 날도 미완료로 본다 — 하루라도 미완료가 있으면 그 날에서 streak가 0으로 끊긴다.
    """
    user_instances_until_target = (Event.user_id == user_id, EventInstance.date <= target_date)

    last_incomplete_date = db.scalar(
        select(func.max(EventInstance.date))
        .join(Event, EventInstance.event_id == Event.id)
        .where(*user_instances_until_target, EventInstance.status != EventInstanceStatus.DONE)
    )

    # 마지막 미완료 날짜 이후(없으면 전체 기간)의 "일정이 있는 날" 수가 곧 streak다.
    stmt = (
        select(func.count(distinct(EventInstance.date)))
        .join(Event, EventInstance.event_id == Event.id)
        .where(*user_instances_until_target)
    )
    if last_incomplete_date is not None:
        stmt = stmt.where(EventInstance.date > last_incomplete_date)
    return db.scalar(stmt) or 0


def calculate_daily_points(db: Session, user_id: int, target_date: dt_date) -> DailyPointsResult:
    instances = _instances_on(db, user_id, target_date)
    done = [i for i in instances if i.status == EventInstanceStatus.DONE]
    base_points = float(sum(importance_weight(i.event.importance) for i in done))
    is_perfect_day = bool(instances) and len(done) == len(instances)

    streak_days = calculate_streak_days(db, user_id, target_date)
    # 일정이 없는 날은 streak가 이어지긴 하지만(streak_days는 유지) 그날 받은 보너스는 없다.
    multiplier = streak_multiplier(streak_days) if is_perfect_day else 1.0

    return DailyPointsResult(
        user_id=user_id,
        date=target_date,
        total_instances=len(instances),
        done_instances=len(done),
        is_perfect_day=is_perfect_day,
        base_points=base_points,
        streak_days=streak_days,
        streak_multiplier=multiplier,
        points_earned=round(base_points * multiplier, 2),
    )


def record_daily_points(db: Session, user_id: int, target_date: dt_date) -> PointsLedger:
    """하루 점수를 계산해 PointsLedger에 저장한다. 같은 날짜를 다시 계산하면 기존 행을 갱신한다."""
    result = calculate_daily_points(db, user_id, target_date)

    entry = db.execute(
        select(PointsLedger).where(PointsLedger.user_id == user_id, PointsLedger.date == target_date)
    ).scalars().first()
    if entry is None:
        entry = PointsLedger(user_id=user_id, date=target_date)
        db.add(entry)

    entry.base_points = result.base_points
    entry.streak_multiplier = result.streak_multiplier
    entry.points_earned = result.points_earned
    db.commit()
    db.refresh(entry)
    return entry


def recalculate_points_since(
    db: Session, user_id: int, start_date: dt_date, today: dt_date | None = None
) -> list[PointsLedger]:
    """start_date부터 어제까지의 PointsLedger를 다시 계산해 upsert한다.

    지난 날짜의 인스턴스 상태가 바뀌면(뒤늦은 완료 등) 그날 점수뿐 아니라 이후 날짜들의 streak 배율도
    달라지므로 어제까지 전부 다시 계산한다. 오늘 이후는 get_points_summary가 실시간으로 계산하고
    자정 잡이 확정하므로 여기서는 쓰지 않는다.
    """
    today = today or dt_date.today()
    # 일정이 없는 날은 점수가 항상 0이고 배율도 붙지 않으므로, 일정 회차가 있거나 이미 원장 행이 있는 날만
    # 다시 계산한다. 몇 년 전 할 일을 완료해도 그 사이의 빈 날짜를 전부 쓰지 않는다.
    in_range = (EventInstance.date >= start_date, EventInstance.date < today)
    instance_dates = db.execute(
        select(EventInstance.date).join(Event, EventInstance.event_id == Event.id).where(Event.user_id == user_id, *in_range)
    ).scalars()
    ledger_dates = db.execute(
        select(PointsLedger.date).where(
            PointsLedger.user_id == user_id, PointsLedger.date >= start_date, PointsLedger.date < today
        )
    ).scalars()

    entries = [record_daily_points(db, user_id, day) for day in sorted(set(instance_dates) | set(ledger_dates))]
    if entries:
        logger.info("[포인트] 재계산 user_id=%s %s 이후 %d일", user_id, start_date, len(entries))
    return entries


def run_daily_points_job(target_date: dt_date | None = None) -> None:
    """모든 사용자의 전날(target_date 기본값) 포인트를 계산해 PointsLedger에 기록한다.

    한 사용자에서 실패해도 나머지 사용자는 계속 처리한다.
    """
    target_date = target_date or dt_date.today() - timedelta(days=1)
    with SessionLocal() as db:
        user_ids = db.execute(select(User.id)).scalars().all()
        for user_id in user_ids:
            try:
                entry = record_daily_points(db, user_id, target_date)
            except Exception:
                db.rollback()
                logger.exception("[포인트] 계산 실패. user_id=%s date=%s", user_id, target_date)
                continue
            logger.info(
                "[포인트] user_id=%s date=%s base=%s x%s = %s",
                user_id,
                target_date,
                entry.base_points,
                entry.streak_multiplier,
                entry.points_earned,
            )


def register_daily_points_job() -> None:
    """앱 시작 시 한 번 호출해서, 매일 자정에 전날 포인트 계산을 스케줄러에 등록한다."""
    scheduler.add_job(
        run_daily_points_job,
        trigger="cron",
        hour=DAILY_POINTS_HOUR,
        minute=DAILY_POINTS_MINUTE,
        id=DAILY_POINTS_JOB_ID,
        replace_existing=True,
        misfire_grace_time=DAILY_POINTS_MISFIRE_GRACE_SECONDS,
        coalesce=True,
    )


def current_streak_days(db: Session, user_id: int, today: dt_date) -> int:
    """진행 중인 streak. 오늘 일정이 아직 PENDING이면 오늘은 세지 않고 어제까지의 streak를 보여주되,
    오늘 이미 MISSED가 나왔다면 끊긴 것으로 본다."""
    instances = _instances_on(db, user_id, today)
    if any(i.status == EventInstanceStatus.MISSED for i in instances):
        return 0
    if instances and all(i.status == EventInstanceStatus.DONE for i in instances):
        return calculate_streak_days(db, user_id, today)
    return calculate_streak_days(db, user_id, today - timedelta(days=1))


def _ledger_sum(db: Session, user_id: int, before: dt_date, since: dt_date | None = None) -> float:
    stmt = select(func.coalesce(func.sum(PointsLedger.points_earned), 0.0)).where(
        PointsLedger.user_id == user_id, PointsLedger.date < before
    )
    if since is not None:
        stmt = stmt.where(PointsLedger.date >= since)
    return float(db.scalar(stmt))


def get_points_summary(db: Session, user_id: int, today: dt_date | None = None) -> PointsSummaryRead:
    """오늘(실시간 계산) / 이번 주(월요일 시작) / 누적 포인트.

    어제까지는 PointsLedger 합계를 쓰고 오늘은 실시간 값을 더한다. 오늘 날짜의 ledger 행이
    있더라도(수동 실행 등) 합계에서 제외해 이중 집계를 막는다.
    """
    today = today or dt_date.today()
    week_start = today - timedelta(days=today.weekday())
    today_result = calculate_daily_points(db, user_id, today)

    return PointsSummaryRead(
        user_id=user_id,
        today=today_result,
        week_start=week_start,
        week_points=round(_ledger_sum(db, user_id, today, since=week_start) + today_result.points_earned, 2),
        total_points=round(_ledger_sum(db, user_id, today) + today_result.points_earned, 2),
        current_streak_days=current_streak_days(db, user_id, today),
    )
