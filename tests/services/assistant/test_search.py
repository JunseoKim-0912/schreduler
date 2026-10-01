"""search_events의 제목 매칭 (옛 test_conversation_state에서 옮김): 과목 코드·기호 차이, 흔한 단어, 오타 후보."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import Base, Event, ImportantDateRange, User
from app.schemas.event import EventCreate
from app.services import event_service
from app.services.assistant import agent
from app.services.assistant.context import TurnContext
from app.services.assistant.search import search_events

NOW = datetime(2026, 9, 27, 14, 0, tzinfo=ZoneInfo("America/Toronto"))


@pytest.fixture
def ctx():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(name="June", preferred_language="ko")
        db.add(user)
        db.flush()
        period = ImportantDateRange(user_id=user.id, name="Lecture period", start_date=date(2026, 9, 8), end_date=date(2026, 12, 8))
        db.add(period)
        db.commit()
        for title, byday in (("ESC360 Lecture", "MO"), ("ECE360 Lab", "TU")):
            _event(db, user, title, rule=f"FREQ=WEEKLY;BYDAY={byday}", range_id=period.id)
        yield TurnContext(db=db, user=user, session=agent.create_session(db, user, NOW), now=NOW)


def _event(db: Session, user: User, title: str, rule: str | None = None, range_id: int | None = None) -> Event:
    start = datetime(2026, 9, 14, 11)
    return event_service.create_event(
        db,
        EventCreate(user_id=user.id, title=title, start_time=start, end_time=start + timedelta(hours=1), is_recurring=rule is not None,
                    recurrence_rule=rule, date_range_id=range_id),
    )


def _titles(rows: list[dict]) -> list[str]:
    return [row["title"] for row in rows]


@pytest.mark.parametrize("said", ["ECE360", "ece-360 lab", "ECE 360 Lab"])
def test_course_code_or_symbols_still_match(ctx: TurnContext, said: str) -> None:
    assert _titles(search_events(ctx, {"query": said})["results"]) == ["ECE360 Lab"]


def test_generic_words_do_not_pull_in_every_lecture(ctx: TurnContext) -> None:
    _event(ctx.db, ctx.user, "ECE355 Lecture")

    found = search_events(ctx, {"query": "ECE360 Lecture"})

    assert _titles(found["results"]) == ["ECE360 Lab", "ESC360 Lecture"], "과목 코드가 맞는 것 + 오타로 보이는 비슷한 제목만"


def test_no_match_offers_similar_titles_and_remembers_their_ids(ctx: TurnContext) -> None:
    found = search_events(ctx, {"query": "ESC361 Lecture"})

    assert found["results"] == [] and "note" in found
    assert "ESC360 Lecture" in _titles(found["similar"])
    assert all(ctx.seen_event(row["event_id"]) for row in found["similar"]), "비슷한 후보도 다음 도구에서 쓸 수 있다"
