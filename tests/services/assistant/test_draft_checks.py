"""평가에서 반복된 실패의 안전망 (docs/assistant_design.md §11.11).

- "변경 사항 없음": 지금 값과 같은 수정, 직전 제안과 같은 재제안은 도구 에러로 돌려보내 모델이 다시 생각하게 한다.
- 이름 매칭: 괄호 안 설명·대소문자·공백을 무시해 "Bahen Centre (이동 10분)"도 기존 장소로 잡는다 (기간 이름도 같다).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import Base, Event, ImportantDateRange, Location, PendingProposal, User
from app.schemas.event import NewEvent
from app.services import event_service
from app.services.assistant import agent, prompt
from tests.fake_responses import FakeResponsesClient, ScriptedCall, call, say

NOW = datetime(2026, 9, 27, 14, 0, tzinfo=ZoneInfo("America/Toronto"))


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def user(db: Session) -> User:
    user = User(name="June", preferred_language="ko")
    db.add(user)
    db.flush()
    db.add_all([
        ImportantDateRange(user_id=user.id, name="Lecture Period", start_date=date(2026, 9, 3), end_date=date(2026, 12, 8)),
        Location(user_id=user.id, name="Bahen Centre", default_travel_minutes=10),
    ])
    db.commit()
    return user


@pytest.fixture
def quiz(db: Session, user: User) -> Event:
    period = db.execute(select(ImportantDateRange)).scalar_one()
    start = datetime(2026, 9, 5, 17)
    return event_service.create_event(
        db,
        NewEvent(user_id=user.id, title="물리 퀴즈", start_time=start, end_time=start + timedelta(hours=1), importance=4,
                    is_recurring=True, recurrence_rule="FREQ=WEEKLY;BYDAY=SA", date_range_id=period.id),
    )


def _turn(db: Session, user: User, script: list, message: str, session_id: int | None = None, minutes: int = 0):
    fake = FakeResponsesClient(script)
    result = agent.chat(db, user, message, session_id, client=fake, now=NOW + timedelta(minutes=minutes))
    assert fake.finished
    return result, fake


def _outputs(fake: FakeResponsesClient) -> list[dict]:
    found: dict[str, dict] = {}
    for request in fake.requests:
        found.update(request.tool_outputs())
    return [found[key] for key in sorted(found)]


def _update(event_id: int, **changes) -> ScriptedCall:
    fields = {"title": None, "date": None, "start_time": None, "end_time": None, "importance": None, "location": None}
    return call("propose_update_event", target_ids=[{"event_id": event_id, "instance_id": None}], scope="series",
                changes={**fields, **changes}, inferred_fields=[], draft_id=None)


def _create(**overrides) -> ScriptedCall:
    args = {"event_type": "scheduled", "title": "알고리즘 스터디", "date": "2026-10-05", "start_time": "18:00", "end_time": None,
            "importance": 2, "recurrence": None, "date_range": None, "location": None, "inferred_fields": ["end_time"], "draft_id": None}
    return call("propose_create_event", **{**args, **overrides})


def _codes(output: dict) -> list[str]:
    return [error["code"] for error in output.get("errors", [])]


# --- 변경 사항 없음 ----------------------------------------------------------------------------


def test_update_to_the_current_time_is_a_no_change_error(db: Session, user: User, quiz: Event) -> None:
    # "5시에서 6시 시작으로"를 17:00으로 잘못 읽은 경우: 지금 값과 같아 에러, 모델이 18:00으로 고쳐 다시 부른다.
    result, fake = _turn(db, user, [
        call("search_events", query="물리 퀴즈", date_from=None, date_to=None, weekday=None),
        _update(quiz.id, start_time="17:00", end_time="18:00"),
        _update(quiz.id, start_time="18:00"),
        say("이렇게 바꿀까요?"),
    ], "물리 퀴즈 5시에서 6시 시작으로 바꿔줘")

    first, second = _outputs(fake)[1:3]
    assert _codes(first) == ["no_change"]
    assert "물리 퀴즈 오후 5:00 – 오후 6:00" in first["errors"][0]["message"], "지금 값을 알려 줘야 모델이 고칠 수 있다"
    assert second["ok"] is True
    [card] = result.proposal["items"]
    assert card["changes"]["start_time"] == "18:00"


@pytest.mark.parametrize(
    "changes",
    [
        {"importance": 4},
        {"title": "물리 퀴즈"},
        {"location": {"action": "remove", "name": None, "travel_minutes": None}},
    ],
)
def test_other_fields_equal_to_the_current_value_are_no_change(db: Session, user: User, quiz: Event, changes: dict) -> None:
    _, fake = _turn(db, user, [call("search_events", query="물리 퀴즈", date_from=None, date_to=None, weekday=None),
                               _update(quiz.id, **changes), say("그대로예요")], "물리 퀴즈")

    assert _codes(_outputs(fake)[1]) == ["no_change"]


def test_a_real_change_is_accepted(db: Session, user: User, quiz: Event) -> None:
    _, fake = _turn(db, user, [call("search_events", query="물리 퀴즈", date_from=None, date_to=None, weekday=None),
                               _update(quiz.id, importance=5), say("이렇게 바꿀까요?")], "물리 퀴즈 중요도 5")

    assert _outputs(fake)[1]["ok"] is True


def test_reproposing_the_pending_draft_unchanged_is_a_no_change_error(db: Session, user: User) -> None:
    # "7시로 바꿔줘"에 18:00을 그대로 다시 낸 경우
    first, _ = _turn(db, user, [_create(), say("이렇게 만들까요?")], "10월 5일 저녁 6시 알고리즘 스터디")

    second, fake = _turn(db, user, [_create(), _create(start_time="19:00"), say("7시로 바꿀까요?")], "7시로 바꿔줘",
                         session_id=first.session_id, minutes=1)

    rejected, accepted = _outputs(fake)
    assert _codes(rejected) == ["no_change"] and accepted["ok"] is True
    assert second.proposal["items"][0]["start_time"] == "2026-10-05T19:00:00"
    assert db.scalar(select(func.count()).select_from(PendingProposal).where(PendingProposal.status == "superseded")) == 1


def test_unchanged_item_of_a_multi_item_proposal_is_not_rejected(db: Session, user: User) -> None:
    pair = [_create(), _create(title="리뷰 세션", date="2026-10-06")]
    first, _ = _turn(db, user, [*pair, say("두 개 만들까요?")], "스터디랑 리뷰 세션")

    _, fake = _turn(db, user, [_create(), _create(title="리뷰 세션", date="2026-10-06", start_time="20:00"), say("바꿀까요?")],
                    "리뷰는 8시로", session_id=first.session_id, minutes=1)

    assert all(output["ok"] for output in _outputs(fake))


# --- 이름 매칭 -------------------------------------------------------------------------------------


def test_location_name_with_a_parenthetical_matches_the_existing_location(db: Session, user: User) -> None:
    result, _ = _turn(db, user, [
        _create(title="스터디", date="2026-10-06", start_time="15:00",
                location={"name": " bahen centre (이동 10분) ", "travel_minutes": None}),
        say("이렇게 만들까요?"),
    ], "10월 6일 3시 스터디 Bahen Centre (이동 10분)에서")

    location = result.proposal["items"][0]["location"]
    assert (location["name"], location["is_new"]) == ("Bahen Centre", False)


def test_range_name_with_a_parenthetical_matches_the_existing_range(db: Session, user: User) -> None:
    result, _ = _turn(db, user, [
        _create(date=None, recurrence={"frequency": "WEEKLY", "interval": 1, "by_day": ["MO"], "start_date": None},
                date_range={"name": "lecture period (2026-09-03 ~ 2026-12-08)", "start_date": None, "end_date": None}),
        say("이렇게 만들까요?"),
    ], "매주 월요일 6시 알고리즘 스터디")

    date_range = result.proposal["items"][0]["date_range"]
    assert (date_range["name"], date_range["is_new"]) == ("Lecture Period", False)


def test_prompt_lists_names_separately_from_their_details(db: Session, user: User) -> None:
    block = prompt.ranges_and_locations_block(db, user.id, "ko")

    assert '- name="Bahen Centre", travel_minutes=10' in block
    assert '- name="Lecture Period", start_date=2026-09-03, end_date=2026-12-08, events_using=0' in block
