"""회차(EventInstance)가 없는 단발 일정에 회차 하나를 채워 넣는다.

단발 일정은 예전에는 회차 없이 만들어졌다. 이제 POST /events가 회차를 만들므로, 그 전에 만든 일정 중
날짜가 오늘 이후(오늘 포함)인 것만 채운다. 지난 날짜에 회차를 만들면 이미 확정된 streak와 포인트가
바뀌므로 건드리지 않는다.

실행 (프로젝트 루트에서):
    python -m app.scripts.backfill_single_instances          # 대상 목록만 보여준다 (DB는 바꾸지 않음)
    python -m app.scripts.backfill_single_instances --apply  # 실제로 채운다

알림 job은 서버가 시작할 때 DB에서 다시 등록하므로, 실행 중인 서버가 있으면 적용 후 재시작한다.
"""

from __future__ import annotations

import sys
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models.event import Event
from app.models.event_instance import EventInstance
from app.services.recurrence import create_single_instance


def find_candidates(db: Session, today: date) -> tuple[list[Event], list[Event]]:
    """회차가 없는 단발 일정을 (오늘 이후 → 채울 대상, 지난 날짜 → 건드리지 않음)으로 나눠 날짜순으로 돌려준다."""
    has_instances = select(EventInstance.id).where(EventInstance.event_id == Event.id).exists()
    events = db.execute(select(Event).where(Event.is_recurring.is_(False), ~has_instances)).scalars().all()
    events = sorted(events, key=lambda e: (e.anchor_time, e.id))
    upcoming = [e for e in events if e.anchor_time.date() >= today]
    past = [e for e in events if e.anchor_time.date() < today]
    return upcoming, past


def backfill(db: Session, today: date) -> list[EventInstance]:
    upcoming, _ = find_candidates(db, today)
    created = [create_single_instance(db, event) for event in upcoming]
    db.commit()
    return created


def _describe(event: Event) -> str:
    when = event.anchor_time.strftime("%Y-%m-%d %H:%M")
    kind = "마감" if event.start_time is None else "일정"
    child = f" (하위 일정, 부모 #{event.parent_event_id})" if event.parent_event_id else ""
    return f"  #{event.id:<4} user {event.user_id:<3} {when} [{kind}] {event.title}{child}"


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    today = date.today()
    with SessionLocal() as db:
        upcoming, past = find_candidates(db, today)
        print(f"기준 날짜: {today} (이 날짜 이후 일정만 채움)")
        print(f"\n회차를 채울 단발 일정 {len(upcoming)}개:")
        print("\n".join(_describe(e) for e in upcoming) or "  (없음)")
        print(f"\n지난 날짜라 건드리지 않는 단발 일정 {len(past)}개:")
        print("\n".join(_describe(e) for e in past) or "  (없음)")

        if not apply:
            print("\n목록만 확인했습니다. 실제로 채우려면 --apply를 붙여 다시 실행하세요.")
            return 0
        created = backfill(db, today)
        print(f"\n회차 {len(created)}개를 만들었습니다. 실행 중인 서버가 있으면 재시작해야 알림 job이 등록됩니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
