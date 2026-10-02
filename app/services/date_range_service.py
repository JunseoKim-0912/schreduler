from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.event import Event
from app.models.important_date_range import ImportantDateRange
from app.models.user import User
from app.schemas.date_range import DateRangeUsageRead
from app.services.common import require_owned

# 생성·수정·삭제는 되돌리기 기록과 회차 맞추기가 함께 필요해 app/services/date_range_command_service.py에 있다.


def get_date_range(db: Session, user: User, date_range_id: int) -> ImportantDateRange:
    return require_owned(db, ImportantDateRange, date_range_id, user.id, "date range")


def list_date_ranges_with_usage(db: Session, user: User) -> list[DateRangeUsageRead]:
    date_ranges = db.execute(
        select(ImportantDateRange)
        .where(ImportantDateRange.user_id == user.id)
        .order_by(ImportantDateRange.start_date, ImportantDateRange.id)
    ).scalars().all()
    counts = dict(
        db.execute(
            select(Event.date_range_id, func.count())
            .where(Event.date_range_id.in_([r.id for r in date_ranges]), Event.parent_event_id.is_(None))
            .group_by(Event.date_range_id)
        ).all()
    )
    return [
        DateRangeUsageRead(
            id=r.id, user_id=r.user_id, name=r.name, start_date=r.start_date, end_date=r.end_date, event_count=counts.get(r.id, 0)
        )
        for r in date_ranges
    ]
