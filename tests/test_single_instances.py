"""단발(비반복) 일정의 회차 하나: 생성·알림 job·완료/놓침·수정·되돌리기. 자연어 변경은 가짜 LLM 대본으로 어시스턴트를 거친다."""

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.core.scheduler import scheduler, shutdown_scheduler, start_scheduler
from app.main import app
from app.models import Base, Event, EventInstance, Location, User
from app.models.enums import EventInstanceStatus
from app.services import notification
from app.services.points import recalculate_points_since
from tests.assistant_flow import create, find, only_action, propose, run, update

TODAY = date(2026, 9, 26)


@pytest.fixture(autouse=True)
def frozen_today():
    with freeze_time("2026-09-26 13:00:00"):  # 토론토 09:00
        yield


@pytest.fixture(autouse=True)
def paused_scheduler():
    # 알림 job은 스케줄러가 돌고 있을 때만 등록된다. 실행은 되지 않게 멈춰 두고 등록 결과만 본다.
    start_scheduler()
    scheduler.pause()
    yield
    scheduler.remove_all_jobs()
    shutdown_scheduler()


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
def user_id(engine) -> int:
    with Session(engine) as session:
        user = User(name="June", preferred_language="ko")
        session.add(user)
        session.commit()
        return user.id


def _headers(user_id: int) -> dict[str, str]:
    return {"X-User-Id": str(user_id)}


def _create(client: TestClient, user_id: int, title: str, start: str, end: str, **extra) -> int:
    response = client.post("/events", json={"user_id": user_id, "title": title, "start_time": start, "end_time": end, **extra})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _instances(engine, event_id: int) -> list[EventInstance]:
    with Session(engine) as session:
        return list(session.execute(select(EventInstance).where(EventInstance.event_id == event_id)).scalars())


def _jobs(instance_id: int) -> dict[str, datetime]:
    prefix = f"event_instance_{instance_id}_"
    return {
        job.id.removeprefix(prefix): job.trigger.run_date.replace(tzinfo=None)
        for job in scheduler.get_jobs()
        if job.id.startswith(prefix)
    }


def test_one_off_event_gets_one_instance_and_notification_jobs(client, engine, user_id):
    event_id = _create(client, user_id, "치과", "2026-09-28T10:00:00", "2026-09-28T11:00:00", importance=3)

    [instance] = _instances(engine, event_id)

    assert (instance.date, instance.status) == (date(2026, 9, 28), EventInstanceStatus.PENDING)
    assert _jobs(instance.id) == {"start": datetime(2026, 9, 28, 10), "end": datetime(2026, 9, 28, 11)}


def test_only_future_notifications_are_registered(client, engine, user_id):
    event_id = _create(client, user_id, "지금 하는 중", "2026-09-26T08:30:00", "2026-09-26T10:00:00")

    [instance] = _instances(engine, event_id)

    assert _jobs(instance.id) == {"end": datetime(2026, 9, 26, 10)}, "이미 지난 시작 알림은 등록하지 않는다"


def test_recurring_event_is_not_given_an_extra_instance(client, engine, user_id):
    date_range = client.post(
        "/date-ranges", json={"user_id": user_id, "name": "학기", "start_date": "2026-09-28", "end_date": "2026-10-11"}
    ).json()["id"]
    event_id = _create(
        client, user_id, "강의", "2026-09-28T09:00:00", "2026-09-28T10:00:00",
        is_recurring=True, recurrence_rule="FREQ=WEEKLY;BYDAY=MO", date_range_id=date_range,
    )

    instances = _instances(engine, event_id)

    assert [i.date for i in instances] == [date(2026, 9, 28), date(2026, 10, 5)]
    assert all(_jobs(i.id).keys() == {"start", "end"} for i in instances), "반복 일정도 같은 경로로 알림 job이 생긴다"


def test_one_off_task_has_exactly_one_instance(client, engine, user_id):
    task = client.post("/tasks", json={"title": "과제", "end_time": "2026-09-30T23:59:00"}, headers=_headers(user_id)).json()

    instances = _instances(engine, task["event_id"])

    assert [i.id for i in instances] == [task["event_instance_id"]]
    assert _jobs(task["event_instance_id"]) == {
        "deadline_reminder": datetime(2026, 9, 29, 23, 59),
        "end": datetime(2026, 9, 30, 23, 59),
    }


def test_travel_child_of_one_off_event_gets_instance_and_jobs(client, engine, user_id):
    with Session(engine) as session:
        location = Location(user_id=user_id, name="학교", default_travel_minutes=30)
        session.add(location)
        session.commit()
        location_id = location.id
    event_id = _create(client, user_id, "수업", "2026-09-28T10:00:00", "2026-09-28T11:00:00", location_id=location_id)
    with Session(engine) as session:
        child_id = session.execute(select(Event.id).where(Event.parent_event_id == event_id)).scalar_one()

    [child_instance] = _instances(engine, child_id)

    assert child_instance.date == date(2026, 9, 28)
    assert _jobs(child_instance.id) == {"start": datetime(2026, 9, 28, 9, 30), "end": datetime(2026, 9, 28, 10)}


def test_completing_one_off_instance_counts_toward_points_and_drops_its_jobs(client, engine, user_id):
    event_id = _create(client, user_id, "운동", "2026-09-26T18:00:00", "2026-09-26T19:00:00", importance=4)
    [instance] = _instances(engine, event_id)

    response = client.put(f"/event-instances/{instance.id}/complete", headers=_headers(user_id))

    assert response.status_code == 200
    today = client.get("/points/summary", headers=_headers(user_id)).json()["today"]
    assert (today["done_instances"], today["total_instances"], today["points_earned"]) == (1, 1, 4)
    assert _jobs(instance.id) == {}, "완료한 회차의 남은 알림은 지운다"


def test_missed_one_off_instance_resets_streak(client, engine, user_id):
    ids = [
        _create(client, user_id, f"공부 {day}", f"2026-09-{day}T10:00:00", f"2026-09-{day}T11:00:00", importance=3)
        for day in (23, 24, 25)
    ]
    for event_id in ids:
        [instance] = _instances(engine, event_id)
        assert client.put(f"/event-instances/{instance.id}/complete", headers=_headers(user_id)).status_code == 200
    assert client.get("/points/summary", headers=_headers(user_id)).json()["current_streak_days"] == 3

    with Session(engine) as session:
        session.execute(select(EventInstance).where(EventInstance.event_id == ids[-1])).scalar_one().status = EventInstanceStatus.MISSED
        session.commit()
        recalculate_points_since(session, user_id, date(2026, 9, 25))
        session.commit()

    assert client.get("/points/summary", headers=_headers(user_id)).json()["current_streak_days"] == 0


def test_put_update_moves_the_instance_and_its_jobs(client, engine, user_id):
    event_id = _create(client, user_id, "치과", "2026-09-28T10:00:00", "2026-09-28T11:00:00")
    [before] = _instances(engine, event_id)

    response = client.put(f"/events/{event_id}", json={"start_time": "2026-09-29T15:00:00", "end_time": "2026-09-29T16:30:00"})

    assert response.status_code == 200
    [after] = _instances(engine, event_id)
    assert after.id == before.id and after.date == date(2026, 9, 29)
    items = client.get("/event-instances", params={"start": "2026-09-28", "end": "2026-09-29"}, headers=_headers(user_id)).json()
    assert [(i["date"], i["start_time"], i["end_time"]) for i in items] == [("2026-09-29", "2026-09-29T15:00:00", "2026-09-29T16:30:00")]
    assert _jobs(after.id) == {"start": datetime(2026, 9, 29, 15), "end": datetime(2026, 9, 29, 16, 30)}


def test_nl_update_of_one_off_event_changes_the_event_and_instance_follows(client, engine, user_id, monkeypatch):
    event_id = _create(client, user_id, "스터디", "2026-09-26T17:00:00", "2026-09-26T18:00:00")

    run(client, monkeypatch, user_id, find(query="스터디"), update([(event_id, None)], scope="instance", start_time="18:00"))

    with Session(engine) as session:
        event = session.get(Event, event_id)
        assert (event.start_time, event.end_time) == (datetime(2026, 9, 26, 18), datetime(2026, 9, 26, 19))
    [instance] = _instances(engine, event_id)
    assert (instance.start_time_override, instance.end_time_override) == (None, None)
    assert _jobs(instance.id) == {"start": datetime(2026, 9, 26, 18), "end": datetime(2026, 9, 26, 19)}


def test_undoing_nl_create_removes_the_instance_and_its_jobs(client, engine, user_id, monkeypatch):
    client.post("/date-ranges", json={"user_id": user_id, "name": "학기", "start_date": "2026-09-28", "end_date": "2026-10-11"})
    confirmed = run(
        client, monkeypatch, user_id,
        create(title="강의", start_time="09:00", end_time="10:00",
               recurrence={"frequency": "WEEKLY", "interval": 1, "by_day": ["MO"], "start_date": None},
               date_range={"name": "학기", "start_date": None, "end_date": None}),
    )
    with Session(engine) as session:
        event_id = session.execute(select(Event.id).where(Event.title == "강의")).scalar_one()
    instance_ids = [i.id for i in _instances(engine, event_id)]
    assert instance_ids and all(_jobs(i) for i in instance_ids)

    response = client.post(f"/actions/{only_action(confirmed)}/undo", headers=_headers(user_id))

    assert response.status_code == 200
    assert _instances(engine, event_id) == []
    assert all(_jobs(i) == {} for i in instance_ids)


def test_undoing_one_off_create_removes_its_single_instance_and_jobs(client, engine, user_id):
    """자연어 생성은 반복 일정만 만들므로, 단발 일정 생성→되돌리기는 같은 되돌리기 경로(생성 기록)로 확인한다."""
    from app.schemas.event import EventCreate
    from app.services.event_command_service import create_event_from_nl

    local = sessionmaker(bind=engine)
    with local() as db:
        user = db.get(User, user_id)
        result = create_event_from_nl(
            db, user, EventCreate(user_id=user_id, title="치과", start_time=datetime(2026, 9, 28, 10), end_time=datetime(2026, 9, 28, 11))
        )
        event_id = result.affected[0].event_id
        action_id = result.action.id
    [instance] = _instances(engine, event_id)
    assert _jobs(instance.id).keys() == {"start", "end"}

    assert client.post(f"/actions/{action_id}/undo", headers=_headers(user_id)).status_code == 200

    assert _instances(engine, event_id) == []
    assert _jobs(instance.id) == {}


def test_deleting_and_undoing_restores_jobs(client, engine, user_id):
    event_id = _create(client, user_id, "치과", "2026-09-28T10:00:00", "2026-09-28T11:00:00")
    [instance] = _instances(engine, event_id)

    action_id = client.delete(f"/events/{event_id}").headers["X-Action-Id"]
    assert _jobs(instance.id) == {}

    client.post(f"/actions/{action_id}/undo", headers=_headers(user_id))
    assert _jobs(instance.id).keys() == {"start", "end"}


def test_startup_registers_jobs_for_pending_instances_from_yesterday_on(engine, user_id, monkeypatch):
    local = sessionmaker(bind=engine)
    with local() as db:
        for day, status in [(24, EventInstanceStatus.PENDING), (27, EventInstanceStatus.PENDING), (28, EventInstanceStatus.DONE)]:
            event = Event(user_id=user_id, title=f"일정 {day}", start_time=datetime(2026, 9, day, 10), end_time=datetime(2026, 9, day, 11))
            db.add(event)
            db.flush()
            db.add(EventInstance(event_id=event.id, date=date(2026, 9, day), status=status))
        db.commit()
    monkeypatch.setattr(notification, "SessionLocal", local)

    assert notification.register_upcoming_notifications() == 1

    assert sorted({job.id.rsplit("_", 1)[0] for job in scheduler.get_jobs()}) == ["event_instance_2"]


def test_one_off_created_by_the_assistant_gets_one_instance_and_notification_jobs(client, engine, user_id, monkeypatch):
    run(client, monkeypatch, user_id, create(title="UTKESA 미팅", date="2026-10-01", start_time="20:00", end_time="21:00"))

    with Session(engine) as session:
        event = session.execute(select(Event).where(Event.title == "UTKESA 미팅")).scalar_one()
        [instance] = session.execute(select(EventInstance).where(EventInstance.event_id == event.id)).scalars().all()
        assert (event.is_recurring, instance.date) == (False, date(2026, 10, 1))
    assert _jobs(instance.id) == {"start": datetime(2026, 10, 1, 20), "end": datetime(2026, 10, 1, 21)}


# --- 할 일(마감)의 시간을 어시스턴트로 바꾸기 ---------------------------------------------------------


def _task(client: TestClient, user_id: int, title: str, due: str, **extra) -> dict:
    response = client.post("/tasks", json={"title": title, "end_time": due, **extra}, headers=_headers(user_id))
    assert response.status_code == 201, response.text
    return response.json()


def test_changing_a_one_off_deadline_moves_its_single_instance_and_jobs(client, engine, user_id, monkeypatch):
    task = _task(client, user_id, "MAT389 과제", "2026-10-02T23:30:00")
    [before] = _instances(engine, task["event_id"])

    confirmed = run(client, monkeypatch, user_id, find(query="MAT389 과제"), update([(task["event_id"], None)], end_time="23:59"))

    [instance] = _instances(engine, task["event_id"])
    assert instance.id == before.id, "회차를 새로 만들지 않고 그대로 옮긴다"
    with Session(engine) as session:
        assert session.get(Event, task["event_id"]).end_time == datetime(2026, 10, 2, 23, 59)
        assert len(session.execute(select(Event).where(Event.title == "MAT389 과제")).scalars().all()) == 1
    assert _jobs(instance.id) == {"deadline_reminder": datetime(2026, 10, 1, 23, 59), "end": datetime(2026, 10, 2, 23, 59)}

    response = client.post(f"/actions/{only_action(confirmed)}/undo", headers=_headers(user_id))

    assert response.status_code == 200
    [restored] = _instances(engine, task["event_id"])
    assert restored.id == before.id and restored.end_time_override is None
    with Session(engine) as session:
        assert session.get(Event, task["event_id"]).end_time == datetime(2026, 10, 2, 23, 30)
    assert _jobs(restored.id) == {"deadline_reminder": datetime(2026, 10, 1, 23, 30), "end": datetime(2026, 10, 2, 23, 30)}


def test_changing_one_occurrence_of_a_repeating_deadline_uses_an_override(client, engine, user_id, monkeypatch):
    range_id = client.post(
        "/date-ranges", json={"user_id": user_id, "name": "학기", "start_date": "2026-09-28", "end_date": "2026-10-25"}
    ).json()["id"]
    task = _task(client, user_id, "주간 퀴즈", "2026-10-02T23:59:00", recurrence_rule="FREQ=WEEKLY;BYDAY=FR", date_range_id=range_id)
    before = {i.date: i.id for i in _instances(engine, task["event_id"])}
    target = before[date(2026, 10, 9)]

    run(client, monkeypatch, user_id, find(query="주간 퀴즈", date_from="2026-10-09", date_to="2026-10-09"),
        update([(task["event_id"], target)], scope="instance", end_time="20:00"))

    after = {i.date: i for i in _instances(engine, task["event_id"])}
    assert {d: i.id for d, i in after.items()} == before, "회차 수와 id가 그대로"
    assert after[date(2026, 10, 9)].end_time_override == datetime(2026, 10, 9, 20, 0)
    assert all(i.end_time_override is None for d, i in after.items() if d != date(2026, 10, 9))
    assert _jobs(target)["end"] == datetime(2026, 10, 9, 20, 0)


def test_creating_a_task_with_the_same_title_nearby_is_warned(client, engine, user_id, monkeypatch):
    _task(client, user_id, "Project 배포", "2026-10-01T11:30:00")

    chat = propose(client, monkeypatch, user_id, create(title="project  배포", event_type="deadline", date="2026-10-01", end_time="23:30"))

    [card] = chat["proposal"]["items"]
    assert card["warnings"] == [
        {"code": "similar_exists", "message": "비슷한 일정이 이미 있어요: Project 배포 (10/1 11:30). 수정하려던 거라면 알려주세요"}
    ]


@pytest.mark.parametrize(
    ("title", "day"),
    [("Project 배포", "2026-10-09"), ("ECE360 퀴즈", "2026-10-01")],  # 8일 뒤 / 과목 코드만 같은 다른 일정
)
def test_no_similar_warning_far_away_or_for_a_different_title(client, engine, user_id, monkeypatch, title, day):
    _task(client, user_id, "Project 배포", "2026-10-01T11:30:00")
    _create(client, user_id, "ECE360 Lab", "2026-10-01T14:00:00", "2026-10-01T17:00:00")

    chat = propose(client, monkeypatch, user_id, create(title=title, event_type="deadline", date=day, end_time="23:30"))

    assert [w["code"] for w in chat["proposal"]["items"][0]["warnings"]] == []
