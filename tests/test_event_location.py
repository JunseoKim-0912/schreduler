"""v3.8 기존 일정에 장소 넣기·바꾸기·빼기 (자연어 수정과 PUT /events). LLM은 가짜로 바꿔 호출하지 않는다."""

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
from app.services.llm_client import EventSlotFillResult
from app.services.slot_fill_session import clear_all_sessions

PENDING, DONE, CANCELLED = EventInstanceStatus.PENDING, EventInstanceStatus.DONE, EventInstanceStatus.CANCELLED


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
def ids(engine, client) -> dict[str, int]:
    with Session(engine) as session:
        user = User(name="June", preferred_language="ko")
        session.add(user)
        session.flush()
        period = ImportantDateRange(user_id=user.id, name="Lecture period", start_date=date(2026, 9, 8), end_date=date(2026, 10, 31))
        bahen = Location(user_id=user.id, name="Bahen", default_travel_minutes=20)
        myhal = Location(user_id=user.id, name="Myhal", default_travel_minutes=30)
        session.add_all([period, bahen, myhal])
        session.commit()
        values = {"user": user.id, "period": period.id, "bahen": bahen.id, "myhal": myhal.id}
    values["lecture"] = _lecture(client, values, "MO")
    return values


def _lecture(client: TestClient, ids: dict[str, int], byday: str, title: str = "ESC360 Lecture") -> int:
    response = client.post(
        "/events",
        json={
            "user_id": ids["user"], "title": title, "start_time": "2026-09-14T11:00:00", "end_time": "2026-09-14T12:00:00",
            "is_recurring": True, "recurrence_rule": f"FREQ=WEEKLY;BYDAY={byday}", "date_range_id": ids["period"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch):
    queue: list[EventSlotFillResult] = []

    def fake(db, user_id, utterance, **kwargs):
        assert queue, f"예상하지 못한 LLM 호출: {utterance!r}"
        return queue.pop(0)

    monkeypatch.setattr(event_parse_service, "fill_event_slots_for_user", fake)
    return lambda **fields: queue.append(EventSlotFillResult(**fields))


def _parse(client: TestClient, user_id: int, utterance: str, session_id: str | None = None) -> dict:
    body = {"user_id": user_id, "utterance": utterance}
    if session_id:
        body["session_id"] = session_id
    response = client.post("/events/parse", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _child(engine, event_id: int) -> Event | None:
    with Session(engine) as session:
        return session.execute(select(Event).where(Event.parent_event_id == event_id)).scalar_one_or_none()


def _instances(engine, event_id: int) -> dict[date, EventInstanceStatus]:
    with Session(engine) as session:
        rows = session.execute(select(EventInstance.date, EventInstance.status).where(EventInstance.event_id == event_id)).all()
        return {day: status for day, status in rows}


def _location_of(engine, event_id: int) -> str | None:
    with Session(engine) as session:
        event = session.get(Event, event_id)
        return event.location.name if event.location else None


def _undo(client: TestClient, user_id: int, action_id: int):
    return client.post(f"/actions/{action_id}/undo", headers={"X-User-Id": str(user_id)})


def test_linking_a_registered_location_creates_travel_child_and_instances(client, engine, ids, llm):
    llm(intent="update", target_title="ESC360 Lecture", target_weekday="MO", location_action="set", new_location_name="bahen")

    body = _parse(client, ids["user"], "월요일의 ESC360 Lecture에 장소 넣어줘 Bahen")

    assert body["command"]["status"] == "executed", "한 요일로만 반복되면 묻지 않고 바로 적용"
    assert "장소 없음→Bahen" in body["message"]
    assert _location_of(engine, ids["lecture"]) == "Bahen"
    child = _child(engine, ids["lecture"])
    assert (child.start_time.hour, child.start_time.minute) == (10, 40)
    assert set(_instances(engine, child.id)) == set(_instances(engine, ids["lecture"])), "부모 회차마다 이동 회차"


def test_unknown_location_asks_travel_minutes_and_registers_it(client, engine, ids, llm):
    llm(intent="update", target_title="ESC360 Lecture", location_action="set", new_location_name="Galbraith 304")

    asked = _parse(client, ids["user"], "ESC360 Lecture에 장소 넣어줘 'Galbraith 304'")

    assert asked["command"]["status"] == "needs_clarification"
    assert asked["message"] == "Galbraith 304까지 이동 시간이 몇 분인가요?"
    assert _location_of(engine, ids["lecture"]) is None

    body = _parse(client, ids["user"], "15분", asked["session_id"])  # LLM 없이 처리

    assert body["command"]["status"] == "executed"
    with Session(engine) as session:
        location = session.execute(select(Location).where(Location.name == "Galbraith 304")).scalar_one()
        assert location.default_travel_minutes == 15
    child = _child(engine, ids["lecture"])
    assert (child.start_time.hour, child.start_time.minute) == (10, 45)


def test_removing_location_cancels_future_travel_instances_but_keeps_past(client, engine, ids, llm):
    client.put(f"/events/{ids['lecture']}", json={"location_id": ids["bahen"]})
    child_id = _child(engine, ids["lecture"]).id
    with Session(engine) as session:
        session.execute(
            select(EventInstance).where(EventInstance.event_id == child_id, EventInstance.date == date(2026, 9, 21))
        ).scalar_one().status = DONE
        session.commit()
    llm(intent="update", target_title="ESC360 Lecture", location_action="remove")

    body = _parse(client, ids["user"], "ESC360 Lecture 장소 빼줘")

    assert body["command"]["status"] == "executed"
    assert _location_of(engine, ids["lecture"]) is None
    statuses = _instances(engine, child_id)
    assert statuses[date(2026, 9, 14)] == PENDING and statuses[date(2026, 9, 21)] == DONE, "지난 기록은 그대로"
    assert all(status == CANCELLED for day, status in statuses.items() if day >= date(2026, 9, 26))

    assert _undo(client, ids["user"], body["command"]["action_id"]).status_code == 200
    assert _location_of(engine, ids["lecture"]) == "Bahen"
    assert all(status == PENDING for day, status in _instances(engine, child_id).items() if day >= date(2026, 9, 26))


def test_changing_location_recalculates_travel_child_times(client, engine, ids, llm):
    client.put(f"/events/{ids['lecture']}", json={"location_id": ids["bahen"]})
    child_id = _child(engine, ids["lecture"]).id
    llm(intent="update", target_title="ESC360 Lecture", location_action="set", new_location_name="Myhal")

    body = _parse(client, ids["user"], "ESC360 Lecture 장소 Myhal로 바꿔줘")

    assert "장소 Bahen→Myhal" in body["message"]
    child = _child(engine, ids["lecture"])
    assert child.id == child_id, "이동 일정을 새로 만들지 않고 시간만 다시 맞춘다"
    assert (child.start_time.hour, child.start_time.minute) == (10, 30)


def test_multi_weekday_series_asks_before_applying_to_all(client, engine, ids, llm):
    event_id = _lecture(client, ids, "MO,WE", title="ECE355 Lecture")
    llm(intent="update", target_title="ECE355 Lecture", target_weekday="MO", location_action="set", new_location_name="Bahen")

    body = _parse(client, ids["user"], "월요일의 ECE355 Lecture에 장소 넣어줘 Bahen")

    assert body["command"]["status"] == "needs_confirmation"
    assert body["message"] == "장소는 반복 일정 전체(월·수)에 적용돼요. 진행할까요?"
    assert _location_of(engine, event_id) is None

    response = client.post("/events/commands/confirm", json={"user_id": ids["user"], "token": body["command"]["confirmation_token"]})

    assert response.status_code == 200
    assert _location_of(engine, event_id) == "Bahen"


def test_location_request_over_two_turns(client, engine, ids, llm):
    llm(intent="update", target_title="ESC360 Lecture", location_action="set")
    llm(intent="update", location_action="set", new_location_name="Bahen")

    asked = _parse(client, ids["user"], "ESC360 Lecture에 장소 추가해줘")

    assert asked["message"] == "어느 장소로 할까요?"
    body = _parse(client, ids["user"], "Bahen", asked["session_id"])

    assert body["command"]["status"] == "executed"
    assert _location_of(engine, ids["lecture"]) == "Bahen"


def test_undoing_location_link_removes_travel_child_and_new_location(client, engine, ids, llm):
    llm(intent="update", target_title="ESC360 Lecture", location_action="set", new_location_name="Galbraith 304")
    asked = _parse(client, ids["user"], "ESC360 Lecture 장소 Galbraith 304")
    body = _parse(client, ids["user"], "10분", asked["session_id"])
    assert _child(engine, ids["lecture"]) is not None

    assert _undo(client, ids["user"], body["command"]["action_id"]).status_code == 200

    assert _location_of(engine, ids["lecture"]) is None
    assert _child(engine, ids["lecture"]) is None, "이동 일정과 그 회차도 사라진다"
    with Session(engine) as session:
        assert session.execute(select(Location).where(Location.name == "Galbraith 304")).first() is None


def test_put_events_location_creates_and_detaches_travel_child(client, engine, ids):
    assert client.put(f"/events/{ids['lecture']}", json={"location_id": ids["bahen"]}).status_code == 200
    child = _child(engine, ids["lecture"])
    assert child is not None and len(_instances(engine, child.id)) == len(_instances(engine, ids["lecture"]))

    assert client.put(f"/events/{ids['lecture']}", json={"location_id": None}).status_code == 200

    statuses = _instances(engine, child.id)
    assert statuses[date(2026, 9, 21)] == PENDING and statuses[date(2026, 9, 28)] == CANCELLED


def test_unclear_update_lists_what_can_be_changed(client, engine, ids, llm):
    llm(intent="update", target_title="ESC360 Lecture")

    body = _parse(client, ids["user"], "ESC360 Lecture 좀 바꿔줘")

    assert body["message"] == "무엇을 바꿀지 알려 주세요. 바꿀 수 있는 것: 시간, 제목, 중요도, 장소, 마감일."


def test_calendar_shows_location(client, engine, ids):
    client.put(f"/events/{ids['lecture']}", json={"location_id": ids["bahen"]})

    items = client.get("/event-instances", params={"start": "2026-09-28", "end": "2026-09-28"}, headers={"X-User-Id": str(ids["user"])}).json()

    lecture = next(i for i in items if i["event_id"] == ids["lecture"])
    assert lecture["location_name"] == "Bahen"
