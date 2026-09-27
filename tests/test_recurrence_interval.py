"""v3.8 반복 간격(INTERVAL)과 반복 시작일. LLM은 가짜로 바꿔 호출하지 않는다."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.models import Base, Event, EventInstance, ImportantDateRange, Location, User
from app.models.enums import EventInstanceStatus
from app.services import event_parse_service
from app.services.llm_client import _EVENT_SLOT_INSTRUCTIONS, DraftEditResult, EventSlotFillResult
from app.services.recurrence import build_recurrence_rule
from app.services.slot_fill_session import clear_all_sessions

LAB = dict(title="ECE360 Lab", frequency="WEEKLY", by_day=["TU"], interval=2, recurrence_start="09-22",
           start_time="09:00", end_time="12:00", importance=4)


@pytest.fixture(autouse=True)
def frozen_today():
    with freeze_time("2026-09-26 09:00:00"):
        yield


@pytest.fixture(autouse=True)
def reset_sessions():
    clear_all_sessions()
    yield
    clear_all_sessions()


@pytest.fixture
def engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def client(engine):
    local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def ids(engine) -> dict[str, int]:
    with Session(engine) as session:
        user = User(name="June", preferred_language="ko")
        session.add(user)
        session.flush()
        period = ImportantDateRange(user_id=user.id, name="Lecture period", start_date=date(2026, 9, 8), end_date=date(2026, 12, 8))
        session.add(period)
        session.commit()
        return {"user": user.id, "period": period.id}


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch):
    slots: list[EventSlotFillResult] = []
    edits: list[DraftEditResult] = []

    def fill_slots(db, user_id, utterance, **kwargs):
        assert slots, f"예상하지 못한 슬롯필링 호출: {utterance!r}"
        return slots.pop(0)

    def fill_edit(db, user_id, utterance, **kwargs):
        assert edits, f"예상하지 못한 초안 수정 호출: {utterance!r}"
        return edits.pop(0)

    monkeypatch.setattr(event_parse_service, "fill_event_slots_for_user", fill_slots)
    monkeypatch.setattr(event_parse_service, "fill_draft_edit_for_user", fill_edit)
    return {"slot": lambda **f: slots.append(EventSlotFillResult(**f)), "edit": lambda **f: edits.append(DraftEditResult(**f))}


def _parse(client: TestClient, user_id: int, utterance: str, session_id: str | None = None) -> dict:
    body = {"user_id": user_id, "utterance": utterance}
    if session_id:
        body["session_id"] = session_id
    response = client.post("/events/parse", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _dates(engine, event_id: int) -> list[date]:
    with Session(engine) as session:
        return list(
            session.execute(
                select(EventInstance.date)
                .where(EventInstance.event_id == event_id, EventInstance.status != EventInstanceStatus.CANCELLED)
                .order_by(EventInstance.date)
            ).scalars()
        )


BIWEEKLY_FROM_922 = [date(2026, 9, 22), date(2026, 10, 6), date(2026, 10, 20), date(2026, 11, 3), date(2026, 11, 17), date(2026, 12, 1)]


def test_biweekly_lab_from_922_is_created_without_asking(client, engine, ids, llm):
    llm["slot"](**LAB, date_range_id=ids["period"])

    body = _parse(client, ids["user"], "ECE360 Lab 매주 화요일 오전 9-12시 Lecture period 동안 9/22일부터 2주마다 중요도 4")

    assert body["is_complete"] is True, "매주 화요일 + 2주마다는 격주 화요일이라 되묻지 않는다"
    draft = body["draft"]
    assert draft["recurrence_rule"] == "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU"
    assert (draft["start_time"], draft["end_time"]) == ("2026-09-22T09:00:00", "2026-09-22T12:00:00")
    assert draft["preview_dates"] == ["2026-10-06", "2026-10-20", "2026-11-03"], "미리보기는 오늘(9/26) 이후 회차"
    assert (draft["date_range_name"], draft["date_range_end"]) == ("Lecture period", "2026-12-08")

    confirmed = client.post("/events/commands/confirm", json={"user_id": ids["user"], "token": body["command"]["confirmation_token"]}).json()

    [event_id] = [t["event_id"] for t in confirmed["command"]["affected"]]
    assert _dates(engine, event_id) == BIWEEKLY_FROM_922[1:], "9/22 기준 격주 리듬, 회차는 오늘(9/26) 이후만"


def test_prompt_explains_intervals_and_not_asking():
    for phrase in ("'2주마다', '격주', '한 주 걸러'는 WEEKLY에 interval 2", "'3주마다'는 3", "'격주 화요일'", "되묻지 않는다"):
        assert phrase in _EVENT_SLOT_INSTRUCTIONS


@pytest.mark.parametrize(
    ("interval", "rule", "expected"),
    [
        (2, "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU", BIWEEKLY_FROM_922[1:4]),
        (3, "FREQ=WEEKLY;INTERVAL=3;BYDAY=TU", [date(2026, 10, 13), date(2026, 11, 3), date(2026, 11, 24)]),
        (1, "FREQ=WEEKLY;BYDAY=TU", [date(2026, 9, 29), date(2026, 10, 6), date(2026, 10, 13)]),
    ],
)
def test_interval_goes_into_rrule_and_preview(client, engine, ids, llm, interval, rule, expected):
    llm["slot"](**{**LAB, "interval": interval}, date_range_id=ids["period"])

    draft = _parse(client, ids["user"], "ECE360 Lab")["draft"]

    assert draft["recurrence_rule"] == rule
    assert draft["preview_dates"] == [d.isoformat() for d in expected]


def test_without_start_date_first_matching_weekday_after_period_start(client, engine, ids, llm):
    llm["slot"](**{**LAB, "recurrence_start": None}, date_range_id=ids["period"])

    draft = _parse(client, ids["user"], "격주 화요일 오전 9-12시 ECE360 Lab")["draft"]

    assert draft["start_time"] == "2026-09-08T09:00:00", "기간 시작일(9/8)이 화요일이라 그날부터"
    assert draft["preview_dates"] == ["2026-10-06", "2026-10-20", "2026-11-03"], "리듬은 9/8 기준(9/22·10/6…), 미리보기는 오늘(9/26) 이후"


def test_start_date_on_the_wrong_weekday_is_asked_once(client, engine, ids, llm):
    llm["slot"](**{**LAB, "recurrence_start": "09-23"}, date_range_id=ids["period"])

    asked = _parse(client, ids["user"], "9/23부터 격주 화요일 ECE360 Lab")

    assert asked["next_question"] == {
        "slot": "recurrence_start",
        "question": "9/23은(는) 수요일이에요. 화요일 반복이라면 첫 회차를 언제로 할까요?",
    }
    llm["slot"](**LAB, date_range_id=ids["period"])  # "9/22부터"

    body = _parse(client, ids["user"], "그럼 9/22부터", asked["session_id"])

    assert body["draft"]["start_time"] == "2026-09-22T09:00:00"


def test_wrong_weekday_answer_is_not_asked_twice(client, engine, ids, llm):
    llm["slot"](**{**LAB, "recurrence_start": "09-23"}, date_range_id=ids["period"])
    asked = _parse(client, ids["user"], "9/23부터 격주 화요일 ECE360 Lab")
    llm["slot"](**{**LAB, "recurrence_start": "09-23"}, date_range_id=ids["period"])

    body = _parse(client, ids["user"], "그냥 9/23부터 해", asked["session_id"])

    assert body["is_complete"] is True
    assert body["draft"]["preview_dates"][0] == "2026-10-06", "9/23이 속한 주 기준 격주의 첫 화요일"


def _post_event(client: TestClient, user_id: int, range_id: int, **extra) -> int:
    body = {
        "user_id": user_id, "title": "ECE360 Lab", "start_time": "2026-09-22T09:00:00", "end_time": "2026-09-22T12:00:00",
        "is_recurring": True, "recurrence_rule": "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU", "date_range_id": range_id, **extra,
    }
    response = client.post("/events", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_extending_the_period_keeps_the_biweekly_rhythm(client, engine, ids):
    short = client.post(
        "/date-ranges", json={"user_id": ids["user"], "name": "짧은 기간", "start_date": "2026-09-08", "end_date": "2026-10-25"}
    ).json()["id"]
    event_id = _post_event(client, ids["user"], short)
    assert _dates(engine, event_id) == BIWEEKLY_FROM_922[:3]

    client.put(f"/date-ranges/{short}", json={"start_date": "2026-09-01", "end_date": "2026-12-08"})

    assert _dates(engine, event_id) == BIWEEKLY_FROM_922, "늘린 뒤에도 10/27·11/10이 아니라 11/3·11/17 리듬, 9/22 이전 회차 없음"


def test_extending_the_period_backwards_does_not_create_past_instances(client, engine, ids):
    event_id = _post_event(client, ids["user"], ids["period"], start_time="2026-09-01T09:00:00", end_time="2026-09-01T12:00:00")
    before = _dates(engine, event_id)
    assert before[0] == date(2026, 9, 15), "POST /events는 기존대로 기간 안의 회차를 모두 만든다"

    client.put(f"/date-ranges/{ids['period']}", json={"start_date": "2026-09-01"})

    assert _dates(engine, event_id) == before, "기간을 늘려도 오늘(9/26) 이전 회차(9/1)는 새로 만들지 않는다"


def test_travel_child_is_biweekly_too(client, engine, ids):
    with Session(engine) as session:
        location = Location(user_id=ids["user"], name="Bahen", default_travel_minutes=20)
        session.add(location)
        session.commit()
        location_id = location.id

    event_id = _post_event(client, ids["user"], ids["period"], location_id=location_id)

    with Session(engine) as session:
        child = session.execute(select(Event).where(Event.parent_event_id == event_id)).scalar_one()
        assert child.recurrence_rule == "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU"
    assert _dates(engine, child.id) == BIWEEKLY_FROM_922


def test_recurring_deadline_uses_the_same_rhythm(client, engine, ids):
    task = client.post(
        "/tasks",
        json={"title": "랩 리포트", "end_time": "2026-09-22T23:59:00", "recurrence_rule": "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU",
              "date_range_id": ids["period"]},
        headers={"X-User-Id": str(ids["user"])},
    ).json()

    assert _dates(engine, task["event_id"]) == BIWEEKLY_FROM_922


def test_existing_weekly_event_keeps_its_instances(client, engine, ids):
    """예전 방식(기간 시작일 기준)으로 만들어진 매주 일정: 기간을 바꿔도 이미 있는 회차는 그대로 두고 매주로 이어진다."""
    event_id = _post_event(client, ids["user"], ids["period"], recurrence_rule="FREQ=WEEKLY;BYDAY=TU")
    with Session(engine) as session:
        for legacy in (date(2026, 9, 8), date(2026, 9, 15)):  # 예전 생성 방식이 만든 시작일 이전 회차
            session.add(EventInstance(event_id=event_id, date=legacy, status=EventInstanceStatus.PENDING))
        session.commit()
    before = _dates(engine, event_id)

    client.put(f"/date-ranges/{ids['period']}", json={"end_date": "2026-12-15"})

    after = _dates(engine, event_id)
    assert after[: len(before)] == before and after[-1] == date(2026, 12, 15)
    assert all((b - a).days == 7 for a, b in zip(after[1:], after[2:])), "매주 간격 유지"


def test_change_to_weekly_in_the_confirmation_card(client, engine, ids, llm):
    llm["slot"](**LAB, date_range_id=ids["period"])
    first = _parse(client, ids["user"], "ECE360 Lab 격주")
    llm["edit"](interval=1)

    body = _parse(client, ids["user"], "매주로 바꿔줘", first["session_id"])

    assert body["draft"]["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=TU"
    assert body["draft"]["preview_dates"] == ["2026-09-29", "2026-10-06", "2026-10-13"]
    assert "recurrence" in body["draft_changes"]


def test_build_recurrence_rule():
    assert build_recurrence_rule("WEEKLY", ["TU"], 2) == "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU"
    assert build_recurrence_rule("WEEKLY", ["TU"], 1) == "FREQ=WEEKLY;BYDAY=TU"
    assert build_recurrence_rule("DAILY", None, None) == "FREQ=DAILY"
