from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, Event, EventInstance, ImportantDateRange, User
from app.models.enums import EventInstanceStatus, EventType
from app.scripts import backfill_single_instances as script

TODAY = date(2026, 9, 26)


@pytest.fixture
def engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def ids(engine) -> dict[str, int]:
    """회차가 없는 옛 단발 일정들과, 채우면 안 되는 일정들."""
    with Session(engine) as db:
        user = User(name="June", preferred_language="ko")
        db.add(user)
        db.flush()
        date_range = ImportantDateRange(user_id=user.id, name="학기", start_date=date(2026, 9, 1), end_date=date(2026, 12, 31))
        db.add(date_range)
        db.flush()

        def event(title: str, start: datetime | None, end: datetime, **extra) -> Event:
            item = Event(user_id=user.id, title=title, start_time=start, end_time=end, **extra)
            db.add(item)
            db.flush()
            return item

        events = {
            "past": event("지난 일정", datetime(2026, 9, 20, 10), datetime(2026, 9, 20, 11)),
            "yesterday_deadline": event("어제 마감", None, datetime(2026, 9, 25, 23, 59), event_type=EventType.DEADLINE),
            "today": event("오늘 일정", datetime(2026, 9, 26, 8), datetime(2026, 9, 26, 9)),
            "future_deadline": event("다음 주 마감", None, datetime(2026, 10, 2, 18), event_type=EventType.DEADLINE),
            "already_has": event("이미 회차 있음", datetime(2026, 9, 30, 10), datetime(2026, 9, 30, 11)),
            "recurring": event(
                "반복", datetime(2026, 9, 28, 9), datetime(2026, 9, 28, 10),
                is_recurring=True, recurrence_rule="FREQ=WEEKLY;BYDAY=MO", date_range_id=date_range.id,
            ),
        }
        db.add(EventInstance(event_id=events["already_has"].id, date=date(2026, 9, 30), status=EventInstanceStatus.PENDING))
        db.commit()
        return {name: item.id for name, item in events.items()}


def _instance_dates(engine) -> dict[int, list[date]]:
    with Session(engine) as db:
        rows = db.execute(select(EventInstance.event_id, EventInstance.date)).all()
    result: dict[int, list[date]] = {}
    for event_id, day in rows:
        result.setdefault(event_id, []).append(day)
    return result


def test_candidates_split_into_today_or_later_and_past(engine, ids):
    with Session(engine) as db:
        upcoming, past = script.find_candidates(db, TODAY)

        assert [e.id for e in upcoming] == [ids["today"], ids["future_deadline"]]
        assert [e.id for e in past] == [ids["past"], ids["yesterday_deadline"]]


def test_backfill_fills_only_today_or_later_and_leaves_past_untouched(engine, ids):
    before = _instance_dates(engine)

    with Session(engine) as db:
        created = script.backfill(db, TODAY)

    after = _instance_dates(engine)
    assert len(created) == 2
    assert after[ids["today"]] == [date(2026, 9, 26)]
    assert after[ids["future_deadline"]] == [date(2026, 10, 2)]
    assert ids["past"] not in after and ids["yesterday_deadline"] not in after, "지난 streak·포인트를 바꾸지 않는다"
    assert after[ids["already_has"]] == before[ids["already_has"]]
    assert ids["recurring"] not in after, "반복 일정은 대상이 아니다"


def test_running_twice_does_not_duplicate(engine, ids):
    with Session(engine) as db:
        script.backfill(db, TODAY)
        assert script.backfill(db, TODAY) == []

    assert len(_instance_dates(engine)[ids["today"]]) == 1


def test_main_lists_targets_without_changing_the_db_unless_apply(engine, ids, monkeypatch, capsys):
    monkeypatch.setattr(script, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(script, "date", type("FrozenDate", (date,), {"today": classmethod(lambda cls: TODAY)}))

    script.main([])
    out = capsys.readouterr().out
    assert "회차를 채울 단발 일정 2개" in out and "오늘 일정" in out and "다음 주 마감" in out
    assert "건드리지 않는 단발 일정 2개" in out and "지난 일정" in out
    assert ids["today"] not in _instance_dates(engine)

    script.main(["--apply"])
    assert "회차 2개를 만들었습니다" in capsys.readouterr().out
    assert ids["today"] in _instance_dates(engine)
