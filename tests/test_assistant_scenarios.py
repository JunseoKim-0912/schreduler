"""어시스턴트로 만든 변경의 실행 결과: 반복 기간, 장소(이동 일정), 회차·시리즈 수정과 되돌리기.

옛 /events/parse 테스트(test_date_range_nl, test_event_location, test_nl_commands)에서 옮긴 시나리오다. LLM은 가짜 대본이고,
채팅 → [만들기]는 앱과 같은 API 경로를 지난다 (tests/assistant_flow). 오늘은 2026-09-26(토).
"""

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.models import Base, Event, EventInstance, ImportantDateRange, Location, User
from app.models.enums import EventInstanceStatus
from app.services.assistant import agent
from tests.assistant_flow import confirm, create, date_range, find, only_action, propose, run, undo, update
from tests.auth_helpers import as_user, sign_in

TODAY = date(2026, 9, 26)
PENDING, DONE, MISSED, CANCELLED = (
    EventInstanceStatus.PENDING, EventInstanceStatus.DONE, EventInstanceStatus.MISSED, EventInstanceStatus.CANCELLED,
)
WEEKLY_MO_WE = {"frequency": "WEEKLY", "interval": 1, "by_day": ["MO", "WE"], "start_date": None}


@pytest.fixture(autouse=True)
def frozen_today():
    with freeze_time("2026-09-26 13:00:00"):  # 토론토 09:00
        yield


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
def user_id(engine, client) -> int:
    with Session(engine) as session:
        user = User(name="June", preferred_language="ko")
        session.add(user)
        session.flush()
        session.add(ImportantDateRange(user_id=user.id, name="2026-2학기", start_date=date(2026, 9, 1), end_date=date(2026, 12, 20)))
        session.commit()
        sign_in(client, user.id)
        return user.id


def _ranges(engine) -> dict[str, tuple[date, date]]:
    with Session(engine) as session:
        return {r.name: (r.start_date, r.end_date) for r in session.execute(select(ImportantDateRange)).scalars()}


def _instances(engine, event_id: int) -> dict[date, EventInstanceStatus]:
    with Session(engine) as session:
        rows = session.execute(
            select(EventInstance.date, EventInstance.status).where(EventInstance.event_id == event_id).order_by(EventInstance.date)
        ).all()
        return {day: status for day, status in rows}


def _set_status(engine, event_id: int, day: date, status: EventInstanceStatus) -> None:
    with Session(engine) as session:
        session.execute(select(EventInstance).where(EventInstance.event_id == event_id, EventInstance.date == day)).scalar_one().status = status
        session.commit()


def _event(engine, **where) -> Event | None:
    with Session(engine) as session:
        event = session.execute(select(Event).filter_by(**where)).scalar_one_or_none()
        if event is not None:
            session.expunge(event)
        return event


def _weekly(client: TestClient, user_id: int, range_id: int, title: str = "물리 강의", byday: str = "MO", start: str = "2026-09-07T10:00:00",
            end: str = "2026-09-07T11:00:00") -> int:
    response = client.post(
        "/events",
        json={"title": title, "start_time": start, "end_time": end, "is_recurring": True,
              "recurrence_rule": f"FREQ=WEEKLY;BYDAY={byday}", "date_range_id": range_id}, headers=as_user(user_id),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _range(client: TestClient, user_id: int, name: str = "Lecture End Date", start: str = "2026-09-01", end: str = "2026-10-31") -> int:
    response = client.post("/date-ranges", json={"name": name, "start_date": start, "end_date": end}, headers=as_user(user_id))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _lecture_with_new_range(**range_spec) -> object:
    spec = {"name": "Lecture End Date", "start_date": "2026-09-01", "end_date": "2026-12-08", **range_spec}
    return create(title="물리 강의", start_time="10:00", end_time="11:00", importance=4, recurrence=WEEKLY_MO_WE, date_range=spec)


# --- 일정을 만들면서 새 기간 ---------------------------------------------------------------


def test_undoing_event_with_new_range_removes_both(client, engine, user_id, monkeypatch):
    action_id = only_action(run(client, monkeypatch, user_id, _lecture_with_new_range()))
    assert _ranges(engine)["Lecture End Date"] == (date(2026, 9, 1), date(2026, 12, 8))

    assert undo(client, user_id, action_id).status_code == 200

    assert "Lecture End Date" not in _ranges(engine)
    assert _event(engine, title="물리 강의") is None


def test_undoing_event_keeps_new_range_if_another_event_uses_it(client, engine, user_id, monkeypatch):
    action_id = only_action(run(client, monkeypatch, user_id, _lecture_with_new_range()))
    with Session(engine) as session:
        range_id = session.execute(select(ImportantDateRange.id).where(ImportantDateRange.name == "Lecture End Date")).scalar_one()
    _weekly(client, user_id, range_id, title="세미나", byday="FR", start="2026-09-04T15:00:00", end="2026-09-04T16:00:00")

    assert undo(client, user_id, action_id).status_code == 200

    assert "Lecture End Date" in _ranges(engine)


def test_existing_range_with_the_same_name_is_reused(client, engine, user_id, monkeypatch):
    _range(client, user_id, end="2026-12-08")

    chat = propose(client, monkeypatch, user_id, _lecture_with_new_range(name="lecture end  date", start_date=None, end_date=None))

    assert chat["proposal"]["items"][0]["date_range"]["is_new"] is False
    confirm(client, user_id, chat)
    assert len(_ranges(engine)) == 2, "같은 이름이면 새로 만들지 않는다"


def test_range_with_the_same_name_created_before_confirmation_is_reused(client, engine, user_id, monkeypatch):
    chat = propose(client, monkeypatch, user_id, _lecture_with_new_range())
    _range(client, user_id, end="2026-12-08")

    confirm(client, user_id, chat)

    assert len(_ranges(engine)) == 2


# --- 기간 자체 -----------------------------------------------------------------------------


def test_create_range_and_undo(client, engine, user_id, monkeypatch):
    action_id = only_action(run(client, monkeypatch, user_id, date_range("create", "Lecture End Date", start_date="09-01", end_date="12-08")))

    assert _ranges(engine)["Lecture End Date"] == (date(2026, 9, 1), date(2026, 12, 8)), "연도 없는 날짜는 올해 기준"
    assert undo(client, user_id, action_id).status_code == 200
    assert "Lecture End Date" not in _ranges(engine)


def test_extending_range_adds_instances_and_undo_removes_them(client, engine, user_id, monkeypatch):
    event_id = _weekly(client, user_id, _range(client, user_id))
    before = _instances(engine, event_id)
    assert max(before) == date(2026, 10, 26)

    confirmed = run(client, monkeypatch, user_id, date_range("update", "Lecture End Date", end_date="12-10"))

    assert _ranges(engine)["Lecture End Date"] == (date(2026, 9, 1), date(2026, 12, 10))
    after = _instances(engine, event_id)
    assert max(after) == date(2026, 12, 7) and after[date(2026, 12, 7)] == PENDING
    assert len(after) == len(before) + 6
    assert "회차 6개 추가·0개 취소" in confirmed["executed"][0]["summary"]

    assert undo(client, user_id, only_action(confirmed)).status_code == 200
    assert _instances(engine, event_id) == before
    assert _ranges(engine)["Lecture End Date"] == (date(2026, 9, 1), date(2026, 10, 31))


def test_shrinking_range_cancels_pending_instances_but_keeps_past_records(client, engine, user_id, monkeypatch):
    event_id = _weekly(client, user_id, _range(client, user_id))
    _set_status(engine, event_id, date(2026, 9, 7), DONE)
    _set_status(engine, event_id, date(2026, 9, 14), MISSED)
    before = _instances(engine, event_id)

    action_id = only_action(run(client, monkeypatch, user_id, date_range("update", "Lecture End Date", start_date="09-15", end_date="10-05")))

    statuses = _instances(engine, event_id)
    assert statuses[date(2026, 9, 7)] == DONE, "범위 밖이어도 완료 기록은 그대로"
    assert statuses[date(2026, 9, 14)] == MISSED, "놓침 기록도 그대로"
    assert [d for d, s in statuses.items() if s == PENDING] == [date(2026, 9, 21), date(2026, 9, 28), date(2026, 10, 5)]
    assert all(statuses[d] == CANCELLED for d in (date(2026, 10, 12), date(2026, 10, 26)))

    assert undo(client, user_id, action_id).status_code == 200
    assert _instances(engine, event_id) == before


def test_deleting_an_unused_range(client, engine, user_id, monkeypatch):
    _range(client, user_id)

    chat = propose(client, monkeypatch, user_id, date_range("delete", "Lecture End Date"))

    assert chat["proposal"]["items"][0]["warnings"] == []
    confirm(client, user_id, chat)
    assert "Lecture End Date" not in _ranges(engine)


def test_deleting_a_range_in_use_warns_and_keeps_events_by_default(client, engine, user_id, monkeypatch):
    range_id = _range(client, user_id)
    event_id = _weekly(client, user_id, range_id)
    before = _instances(engine, event_id)

    chat = propose(client, monkeypatch, user_id, date_range("delete", "Lecture End Date"))

    [card] = chat["proposal"]["items"]
    assert (card["events_using"], card["mode"]) == (1, "range_only")
    assert [w["code"] for w in card["warnings"]] == ["range_in_use"] and "mode" in card["inferred_fields"]
    assert "Lecture End Date" in _ranges(engine), "확인 전에는 지우지 않는다"

    action_id = only_action(confirm(client, user_id, chat))

    assert "Lecture End Date" not in _ranges(engine)
    assert _event(engine, id=event_id).date_range_id is None
    assert _instances(engine, event_id) == before, "일정은 이미 만들어진 마지막 회차에서 끝난다"
    assert undo(client, user_id, action_id).status_code == 200
    assert _event(engine, id=event_id).date_range_id == range_id


def test_deleting_a_range_with_its_events_and_undo(client, engine, user_id, monkeypatch):
    event_id = _weekly(client, user_id, _range(client, user_id))
    before = _instances(engine, event_id)

    action_id = only_action(run(client, monkeypatch, user_id, date_range("delete", "Lecture End Date", mode="with_events")))

    assert "Lecture End Date" not in _ranges(engine)
    assert _event(engine, id=event_id) is None
    assert undo(client, user_id, action_id).status_code == 200
    assert "Lecture End Date" in _ranges(engine)
    assert _instances(engine, event_id) == before


def test_ui_update_and_delete_are_recorded_and_list_has_usage(client, engine, user_id):
    """화면(API)에서 바꾼 기간도 같은 기록을 남긴다 — 어시스턴트와 같은 서비스(date_range_command_service)를 쓴다."""
    range_id = _range(client, user_id)
    _weekly(client, user_id, range_id)

    listed = client.get("/date-ranges", headers=as_user(user_id)).json()
    assert [(r["name"], r["event_count"]) for r in listed] == [("2026-2학기", 0), ("Lecture End Date", 1)]

    updated = client.put(f"/date-ranges/{range_id}", json={"end_date": "2026-12-10"})
    assert updated.status_code == 200 and updated.headers["X-Action-Id"]

    assert client.delete(f"/date-ranges/{range_id}").status_code == 409, "사용 중이면 처리 방법(mode)이 필요하다"
    deleted = client.delete(f"/date-ranges/{range_id}", params={"mode": "range_only"})
    assert deleted.status_code == 204 and deleted.headers["X-Action-Id"]
    actions = client.get("/actions", headers=as_user(user_id)).json()
    assert [a["summary_text"] for a in actions[:2]] == [
        "반복 기간 'Lecture End Date' 삭제",
        "반복 기간 'Lecture End Date': 기간 9/1~10/31→9/1~12/10, 회차 6개 추가·0개 취소",
    ]


# --- 장소와 이동 일정 ------------------------------------------------------------------------


@pytest.fixture
def lecture(client, engine, user_id) -> dict[str, int]:
    with Session(engine) as session:
        period = ImportantDateRange(user_id=user_id, name="Lecture period", start_date=date(2026, 9, 8), end_date=date(2026, 10, 31))
        bahen = Location(user_id=user_id, name="Bahen", default_travel_minutes=20)
        myhal = Location(user_id=user_id, name="Myhal", default_travel_minutes=30)
        session.add_all([period, bahen, myhal])
        session.commit()
        ids = {"period": period.id, "bahen": bahen.id}
    ids["event"] = _weekly(client, user_id, ids["period"], title="ESC360 Lecture", start="2026-09-14T11:00:00", end="2026-09-14T12:00:00")
    return ids


def _set_location(name: str, travel_minutes: int | None = None) -> dict:
    return {"action": "set", "name": name, "travel_minutes": travel_minutes}


def _child(engine, event_id: int) -> Event | None:
    return _event(engine, parent_event_id=event_id)


def test_linking_a_registered_location_creates_travel_child_and_instances(client, engine, user_id, lecture, monkeypatch):
    run(client, monkeypatch, user_id, find(query="ESC360 Lecture"), update([(lecture["event"], None)], location=_set_location("bahen")))

    assert _event(engine, id=lecture["event"]).location_id == lecture["bahen"]
    child = _child(engine, lecture["event"])
    assert (child.start_time.hour, child.start_time.minute) == (10, 40)
    assert set(_instances(engine, child.id)) == set(_instances(engine, lecture["event"])), "부모 회차마다 이동 회차"


def test_new_location_is_registered_with_its_travel_time(client, engine, user_id, lecture, monkeypatch):
    run(client, monkeypatch, user_id, find(query="ESC360 Lecture"), update([(lecture["event"], None)], location=_set_location("Galbraith 304", 15)))

    with Session(engine) as session:
        assert session.execute(select(Location.default_travel_minutes).where(Location.name == "Galbraith 304")).scalar_one() == 15
    child = _child(engine, lecture["event"])
    assert (child.start_time.hour, child.start_time.minute) == (10, 45)


def test_removing_location_cancels_future_travel_instances_but_keeps_past(client, engine, user_id, lecture, monkeypatch):
    client.put(f"/events/{lecture['event']}", json={"location_id": lecture["bahen"]})
    child_id = _child(engine, lecture["event"]).id
    _set_status(engine, child_id, date(2026, 9, 21), DONE)

    action_id = only_action(run(
        client, monkeypatch, user_id, find(query="ESC360 Lecture"), update([(lecture["event"], None)], location={"action": "remove"})
    ))

    assert _event(engine, id=lecture["event"]).location_id is None
    statuses = _instances(engine, child_id)
    assert statuses[date(2026, 9, 14)] == PENDING and statuses[date(2026, 9, 21)] == DONE, "지난 기록은 그대로"
    assert all(status == CANCELLED for day, status in statuses.items() if day >= TODAY)

    assert undo(client, user_id, action_id).status_code == 200
    assert _event(engine, id=lecture["event"]).location_id == lecture["bahen"]
    assert all(status == PENDING for day, status in _instances(engine, child_id).items() if day >= TODAY)


def test_changing_location_recalculates_travel_child_times(client, engine, user_id, lecture, monkeypatch):
    client.put(f"/events/{lecture['event']}", json={"location_id": lecture["bahen"]})
    child_id = _child(engine, lecture["event"]).id

    confirmed = run(client, monkeypatch, user_id, find(query="ESC360 Lecture"), update([(lecture["event"], None)], location=_set_location("Myhal")))

    assert "장소 Bahen→Myhal" in confirmed["executed"][0]["summary"]
    child = _child(engine, lecture["event"])
    assert child.id == child_id, "이동 일정을 새로 만들지 않고 시간만 다시 맞춘다"
    assert (child.start_time.hour, child.start_time.minute) == (10, 30)


def test_undoing_location_link_removes_travel_child_and_new_location(client, engine, user_id, lecture, monkeypatch):
    action_id = only_action(run(
        client, monkeypatch, user_id, find(query="ESC360 Lecture"), update([(lecture["event"], None)], location=_set_location("Galbraith 304", 10))
    ))
    assert _child(engine, lecture["event"]) is not None

    assert undo(client, user_id, action_id).status_code == 200

    assert _event(engine, id=lecture["event"]).location_id is None
    assert _child(engine, lecture["event"]) is None, "이동 일정과 그 회차도 사라진다"
    with Session(engine) as session:
        assert session.execute(select(Location).where(Location.name == "Galbraith 304")).first() is None


# --- 회차·시리즈 수정 -------------------------------------------------------------------------


@pytest.fixture
def quiz(client, user_id) -> int:
    range_id = _range(client, user_id, name="가을학기")
    return _weekly(client, user_id, range_id, title="물리 퀴즈", byday="SA", start="2026-09-05T17:00:00", end="2026-09-05T18:00:00")


def test_moving_todays_instance_keeps_duration_and_leaves_others(client, engine, user_id, quiz, monkeypatch):
    with Session(engine) as session:
        today_id = session.execute(select(EventInstance.id).where(EventInstance.event_id == quiz, EventInstance.date == TODAY)).scalar_one()

    run(client, monkeypatch, user_id, find(query="물리 퀴즈", date_from="2026-09-26", date_to="2026-09-26"),
        update([(quiz, today_id)], scope="instance", start_time="18:00"))

    with Session(engine) as session:
        today = session.get(EventInstance, today_id)
        assert (today.start_time_override, today.end_time_override) == (datetime(2026, 9, 26, 18), datetime(2026, 9, 26, 19))
        next_week = session.execute(
            select(EventInstance).where(EventInstance.event_id == quiz, EventInstance.date == date(2026, 10, 3))
        ).scalar_one()
        assert (next_week.start_time_override, next_week.end_time_override) == (None, None)
    event = _event(engine, id=quiz)
    assert (event.start_time.hour, event.end_time.hour) == (17, 18)


def test_series_update_changes_the_event_itself(client, engine, user_id, quiz, monkeypatch):
    run(client, monkeypatch, user_id, find(query="물리 퀴즈"), update([(quiz, None)], start_time="16:00", end_time="17:30"))

    event = _event(engine, id=quiz)
    assert (event.start_time.time().isoformat(), event.end_time.time().isoformat()) == ("16:00:00", "17:30:00")
    with Session(engine) as session:
        overrides = session.execute(select(EventInstance.start_time_override).where(EventInstance.event_id == quiz)).scalars().all()
        assert set(overrides) == {None}


# --- 반복 시작일 ---------------------------------------------------------------------------


def test_start_date_on_the_wrong_weekday_is_warned_on_the_card(client, engine, user_id, monkeypatch):
    _range(client, user_id, name="Lecture period", start="2026-09-08", end="2026-12-08")

    chat = propose(client, monkeypatch, user_id, create(
        title="ECE360 Lab", start_time="09:00", end_time="12:00",
        recurrence={"frequency": "WEEKLY", "interval": 2, "by_day": ["TU"], "start_date": "2026-09-23"},
        date_range={"name": "Lecture period", "start_date": None, "end_date": None},
    ))

    [card] = chat["proposal"]["items"]
    assert "start_weekday_mismatch" in [w["code"] for w in card["warnings"]]
    assert card["preview_dates"][0] == "2026-10-06", "9/23이 속한 주 기준 격주의 첫 화요일"


# --- 한 회차만 제목·중요도·장소 바꾸기 → 그 회차를 떼어낸다 -------------------------------------------------


def _instance_on(engine, event_id: int, day: date) -> EventInstance:
    with Session(engine) as session:
        instance = session.execute(select(EventInstance).where(EventInstance.event_id == event_id, EventInstance.date == day)).scalar_one()
        session.expunge(instance)
        return instance


def _titles_on(engine, user_id: int) -> dict[date, list[str]]:
    with Session(engine) as session:
        rows = session.execute(
            select(EventInstance.date, Event.title)
            .join(Event, EventInstance.event_id == Event.id)
            .where(Event.user_id == user_id, Event.parent_event_id.is_(None), EventInstance.status != CANCELLED)
        ).all()
    found: dict[date, list[str]] = {}
    for day, title in rows:
        found.setdefault(day, []).append(title)
    return found


NEXT_WEEK = date(2026, 10, 3)


def _next_week_only(quiz: int, engine, **changes):
    instance = _instance_on(engine, quiz, NEXT_WEEK)
    return (
        find(query="물리 퀴즈", date_from=NEXT_WEEK.isoformat(), date_to=NEXT_WEEK.isoformat()),
        update([(quiz, instance.id)], scope="instance", **changes),
    )


def test_renaming_one_occurrence_detaches_it_and_leaves_other_weeks(client, engine, user_id, quiz, monkeypatch):
    chat = propose(client, monkeypatch, user_id, *_next_week_only(quiz, engine, title="물리 쪽지시험"), message="다음 주에만 이름 바꿔줘")

    [card] = chat["proposal"]["items"]
    assert (card["scope"], card["affected_count"], card["detaches"]) == ("instance", 1, True)
    confirm(client, user_id, chat)

    titles = _titles_on(engine, user_id)
    assert titles[NEXT_WEEK] == ["물리 쪽지시험"]
    assert titles[date(2026, 9, 26)] == ["물리 퀴즈"] and titles[date(2026, 10, 10)] == ["물리 퀴즈"]
    assert _instance_on(engine, quiz, NEXT_WEEK).status == CANCELLED
    detached = _event(engine, title="물리 쪽지시험")
    assert (detached.is_recurring, detached.start_time, detached.end_time, detached.importance) == (
        False, datetime(2026, 10, 3, 17), datetime(2026, 10, 3, 18), _event(engine, id=quiz).importance
    ), "바꾸지 않은 시간·중요도는 시리즈 값을 그대로"
    assert _event(engine, id=quiz).title == "물리 퀴즈"


def test_changing_importance_of_one_occurrence_detaches_only_it(client, engine, user_id, quiz, monkeypatch):
    run(client, monkeypatch, user_id, *_next_week_only(quiz, engine, importance=6))

    with Session(engine) as session:
        importance = dict(session.execute(
            select(Event.id, Event.importance).where(Event.user_id == user_id, Event.title == "물리 퀴즈")
        ).all())
    assert len(importance) == 2, "시리즈 + 떼어낸 단발 하나"
    assert importance[quiz] is None, "시리즈 중요도는 그대로"
    assert [int(v) for k, v in importance.items() if k != quiz] == [6]


def test_changing_only_the_time_of_one_occurrence_still_uses_an_override(client, engine, user_id, quiz, monkeypatch):
    run(client, monkeypatch, user_id, *_next_week_only(quiz, engine, start_time="19:00"))

    instance = _instance_on(engine, quiz, NEXT_WEEK)
    assert instance.status == PENDING and instance.start_time_override == datetime(2026, 10, 3, 19)
    with Session(engine) as session:
        assert session.execute(select(func.count()).select_from(Event).where(Event.title == "물리 퀴즈")).scalar_one() == 1


def test_series_rename_changes_every_week(client, engine, user_id, quiz, monkeypatch):
    chat = propose(client, monkeypatch, user_id, find(query="물리 퀴즈"), update([(quiz, None)], title="물리 쪽지시험"))

    upcoming = len([d for d in _instances(engine, quiz) if d >= TODAY])
    assert chat["proposal"]["items"][0]["affected_count"] == upcoming > 1
    confirm(client, user_id, chat)
    assert {t for titles in _titles_on(engine, user_id).values() for t in titles} == {"물리 쪽지시험"}


def test_undoing_a_detach_restores_the_occurrence_and_removes_the_new_event(client, engine, user_id, quiz, monkeypatch):
    before = _instances(engine, quiz)
    action_id = only_action(run(client, monkeypatch, user_id, *_next_week_only(quiz, engine, title="물리 쪽지시험")))

    assert undo(client, user_id, action_id).status_code == 200

    assert _instances(engine, quiz) == before
    assert _event(engine, title="물리 쪽지시험") is None


def test_detaching_with_a_location_gives_the_new_event_its_travel_child(client, engine, user_id, quiz, monkeypatch):
    with Session(engine) as session:
        session.add(Location(user_id=user_id, name="Bahen", default_travel_minutes=20))
        session.commit()

    run(client, monkeypatch, user_id, *_next_week_only(quiz, engine, location={"action": "set", "name": "Bahen", "travel_minutes": None}))

    detached = _event(engine, title="물리 퀴즈", is_recurring=False)
    child = _child(engine, detached.id)
    assert (child.start_time, child.end_time) == (datetime(2026, 10, 3, 16, 40), datetime(2026, 10, 3, 17))
    assert _event(engine, id=quiz).location_id is None, "시리즈 장소는 그대로"


def test_instance_scope_that_would_change_two_occurrences_is_refused(client, engine, user_id, quiz, monkeypatch):
    first, second = (_instance_on(engine, quiz, day) for day in (NEXT_WEEK, date(2026, 10, 10)))

    chat = propose(
        client, monkeypatch, user_id,
        find(query="물리 퀴즈", date_from="2026-10-03", date_to="2026-10-10"),
        update([(quiz, first.id), (quiz, second.id)], scope="instance", title="물리 쪽지시험"),
    )

    assert chat["proposal"] is None
    fake = agent.ResponsesClient()  # propose()가 바꿔 둔 가짜 클라이언트
    errors = fake.requests[-1].tool_outputs()
    assert any(e["code"] == "scope_mismatch" for output in errors.values() for e in output.get("errors", []))
