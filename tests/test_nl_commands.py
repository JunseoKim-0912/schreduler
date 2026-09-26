"""FR-2 v3.6 자연어 일정 관리(삭제·수정). LLM은 fill_event_slots_for_user를 가짜로 바꿔 호출하지 않는다."""

from collections.abc import Callable
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.models import ActionHistory, Base, Event, EventInstance, User
from app.models.enums import EventInstanceStatus
from app.services import event_parse_service
from app.services.llm_client import EventSlotFillResult
from app.services.slot_fill_session import clear_all_sessions

TODAY = date(2026, 9, 26)  # 토요일


@pytest.fixture(autouse=True)
def frozen_today():
    with freeze_time("2026-09-26 09:00:00") as frozen:
        yield frozen


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
def user_id(engine) -> int:
    with Session(engine) as session:
        user = User(name="June", preferred_language="ko")
        session.add(user)
        session.commit()
        return user.id


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """다음 LLM 응답들을 차례로 돌려주도록 설정한다. 남은 응답보다 많이 부르면 실패한다."""
    queue: list[EventSlotFillResult] = []

    def fake(db, user_id, utterance, **kwargs) -> EventSlotFillResult:
        assert queue, f"예상하지 못한 LLM 호출: {utterance!r}"
        return queue.pop(0)

    monkeypatch.setattr(event_parse_service, "fill_event_slots_for_user", fake)

    def push(**fields) -> None:
        queue.append(EventSlotFillResult(**fields))

    return push


def _date_range(client: TestClient, user_id: int) -> int:
    response = client.post(
        "/date-ranges",
        json={"user_id": user_id, "name": "가을학기", "start_date": "2026-09-01", "end_date": "2026-10-31"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _weekly_quiz(client: TestClient, user_id: int, title: str = "물리 퀴즈") -> int:
    response = client.post(
        "/events",
        json={
            "user_id": user_id,
            "title": title,
            "start_time": "2026-09-05T17:00:00",
            "end_time": "2026-09-05T18:00:00",
            "importance": 4,
            "is_recurring": True,
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=SA",
            "date_range_id": _date_range(client, user_id),
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def _one_off(client: TestClient, user_id: int, title: str, day: str = "2026-09-28") -> int:
    response = client.post(
        "/events",
        json={"user_id": user_id, "title": title, "start_time": f"{day}T10:00:00", "end_time": f"{day}T11:00:00"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _parse(client: TestClient, user_id: int, utterance: str, session_id: str | None = None) -> dict:
    body: dict = {"user_id": user_id, "utterance": utterance}
    if session_id:
        body["session_id"] = session_id
    response = client.post("/events/parse", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _events(engine) -> list[Event]:
    with Session(engine) as session:
        return list(session.execute(select(Event)).scalars())


def _instance(engine, event_id: int, day: date) -> EventInstance:
    with Session(engine) as session:
        return session.execute(
            select(EventInstance).where(EventInstance.event_id == event_id, EventInstance.date == day)
        ).scalar_one()


def _actions(engine) -> list[ActionHistory]:
    with Session(engine) as session:
        return list(session.execute(select(ActionHistory)).scalars())


def test_single_event_is_deleted_immediately_and_recorded(client, engine, user_id, llm):
    event_id = _one_off(client, user_id, "치과 예약")
    _one_off(client, user_id, "스터디")
    llm(intent="delete", target_title="치과")

    body = _parse(client, user_id, "치과 예약 지워줘")

    assert body["intent"] == "delete"
    command = body["command"]
    assert command["status"] == "executed"
    assert [t["event_id"] for t in command["affected"]] == [event_id]
    assert [e.title for e in _events(engine)] == ["스터디"]
    [action] = _actions(engine)
    assert command["action_id"] == action.id
    assert action.source.value == "nl" and action.action_type.value == "delete"
    assert "치과 예약" in body["message"]


def test_delete_all_needs_confirmation_then_runs(client, engine, user_id, llm):
    first = _one_off(client, user_id, "물리 과제 1", "2026-09-28")
    second = _one_off(client, user_id, "물리 과제 2", "2026-09-29")
    llm(intent="delete", target_title="물리 과제", target_all=True)

    body = _parse(client, user_id, "물리 과제 전부 없애줘")

    command = body["command"]
    assert command["status"] == "needs_confirmation"
    assert command["affected_count"] == 2
    assert {t["event_id"] for t in command["affected"]} == {first, second}
    assert command["confirmation_token"]
    assert len(_events(engine)) == 2, "확인 전에는 아무것도 지우지 않는다"
    assert _actions(engine) == []

    response = client.post("/events/commands/confirm", json={"user_id": user_id, "token": command["confirmation_token"]})

    assert response.status_code == 200, response.text
    assert response.json()["command"]["status"] == "executed"
    assert _events(engine) == []
    assert len(_actions(engine)) == 1, "여러 개를 지워도 변경 기록은 하나"

    again = client.post("/events/commands/confirm", json={"user_id": user_id, "token": command["confirmation_token"]})
    assert again.status_code == 404, "토큰은 한 번만 쓸 수 있다"


def test_moving_todays_instance_keeps_duration_and_leaves_others(client, engine, user_id, llm):
    event_id = _weekly_quiz(client, user_id)
    llm(intent="update", target_title="물리 퀴즈", target_date=TODAY, new_start_time="18:00")

    body = _parse(client, user_id, "오늘 물리 퀴즈 5시에서 6시로 옮겨줘")

    command = body["command"]
    assert command["status"] == "executed" and command["scope"] == "instance"
    today = _instance(engine, event_id, TODAY)
    assert today.start_time_override == datetime(2026, 9, 26, 18, 0)
    assert today.end_time_override == datetime(2026, 9, 26, 19, 0)
    next_week = _instance(engine, event_id, TODAY + timedelta(days=7))
    assert next_week.start_time_override is None and next_week.end_time_override is None
    [event] = _events(engine)
    assert (event.start_time.hour, event.end_time.hour) == (17, 18)
    assert "17:00" in body["message"] and "18:00" in body["message"]


def test_series_update_changes_the_event_itself(client, engine, user_id, llm):
    event_id = _weekly_quiz(client, user_id)
    llm(intent="update", target_title="물리 퀴즈", target_scope="series", new_start_time="16:00", new_end_time="17:30")

    body = _parse(client, user_id, "물리 퀴즈 반복 전체를 4시부터 5시 반으로 바꿔줘")

    assert body["command"]["status"] == "executed" and body["command"]["scope"] == "series"
    [event] = _events(engine)
    assert event.id == event_id
    assert (event.start_time.time().isoformat(), event.end_time.time().isoformat()) == ("16:00:00", "17:30:00")
    assert _instance(engine, event_id, TODAY).start_time_override is None


def test_recurring_without_date_or_all_asks_for_scope(client, engine, user_id, llm):
    event_id = _weekly_quiz(client, user_id)
    llm(intent="delete", target_title="물리 퀴즈")
    llm(intent="delete", target_scope="instance", target_date=TODAY)

    first = _parse(client, user_id, "물리 퀴즈 삭제해줘")

    assert first["command"]["status"] == "needs_clarification"
    assert first["message"]
    assert len(_events(engine)) == 1

    second = _parse(client, user_id, "오늘 것만", session_id=first["session_id"])

    assert second["command"]["status"] == "executed"
    assert _instance(engine, event_id, TODAY).status == EventInstanceStatus.CANCELLED
    assert _instance(engine, event_id, TODAY + timedelta(days=7)).status == EventInstanceStatus.PENDING


def test_not_found(client, engine, user_id, llm):
    _one_off(client, user_id, "스터디")
    llm(intent="delete", target_title="수영 강습")

    body = _parse(client, user_id, "수영 강습 지워줘")

    assert body["command"]["status"] == "not_found"
    assert "찾지 못했어요" in body["message"]
    assert len(_events(engine)) == 1
    assert _actions(engine) == []


def test_multiple_candidates_asks_back_with_candidates(client, engine, user_id, llm):
    first = _one_off(client, user_id, "물리 과제 1")
    second = _one_off(client, user_id, "물리 과제 2")
    llm(intent="delete", target_title="물리 과제")

    body = _parse(client, user_id, "물리 과제 지워줘")

    command = body["command"]
    assert command["status"] == "needs_clarification"
    assert {c["event_id"] for c in command["candidates"]} == {first, second}
    assert "물리 과제 1" in body["message"] and "물리 과제 2" in body["message"]
    assert len(_events(engine)) == 2


def test_unknown_intent_returns_guidance(client, engine, user_id, llm):
    llm(intent="unknown")

    body = _parse(client, user_id, "오늘 날씨 어때?")

    assert body["intent"] == "unknown"
    assert "일정 추가·삭제·수정만 도와드릴 수 있어요" in body["message"]
    assert body["command"] is None
    assert _events(engine) == []


def test_expired_token_is_rejected(client, engine, user_id, llm, frozen_today):
    _one_off(client, user_id, "물리 과제 1")
    _one_off(client, user_id, "물리 과제 2")
    llm(intent="delete", target_title="물리 과제", target_all=True)
    token = _parse(client, user_id, "물리 과제 전부 지워줘")["command"]["confirmation_token"]

    frozen_today.tick(timedelta(minutes=10, seconds=1))
    response = client.post("/events/commands/confirm", json={"user_id": user_id, "token": token})

    assert response.status_code == 410
    assert len(_events(engine)) == 2


def test_token_of_another_user_is_not_found(client, engine, user_id, llm):
    _one_off(client, user_id, "물리 과제 1")
    _one_off(client, user_id, "물리 과제 2")
    with Session(engine) as session:
        other = User(name="Other", preferred_language="ko")
        session.add(other)
        session.commit()
        other_id = other.id
    llm(intent="delete", target_title="물리 과제", target_all=True)
    token = _parse(client, user_id, "물리 과제 전부 지워줘")["command"]["confirmation_token"]

    response = client.post("/events/commands/confirm", json={"user_id": other_id, "token": token})

    assert response.status_code == 404
    assert len(_events(engine)) == 2


def test_completed_draft_is_created_through_confirmation(client, engine, user_id, llm):
    date_range_id = _date_range(client, user_id)
    llm(title="치과 교정", frequency="WEEKLY", by_day=["MO"], start_time="10:00", end_time="11:00", importance=2, date_range_id=date_range_id)

    body = _parse(client, user_id, "학기 동안 매주 월요일 10시에 치과 교정")

    assert body["is_complete"] is True and body["draft"]["title"] == "치과 교정"
    command = body["command"]
    assert command["action"] == "create" and command["status"] == "needs_confirmation"
    assert _events(engine) == []

    response = client.post("/events/commands/confirm", json={"user_id": user_id, "token": command["confirmation_token"]})

    assert response.status_code == 200, response.text
    [event] = _events(engine)
    assert event.title == "치과 교정"
    with Session(engine) as session:
        assert session.execute(select(EventInstance).where(EventInstance.event_id == event.id)).first() is not None
    [action] = _actions(engine)
    assert action.action_type.value == "create" and action.source.value == "nl"
    assert response.json()["command"]["action_id"] == action.id
