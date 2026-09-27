"""'이 내용으로 만들까요?' 확인 카드가 떠 있을 때 채팅으로 초안 고치기. LLM은 가짜로 바꿔 호출하지 않는다."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.models import Base, Event, ImportantDateRange, Location, User
from app.services import event_parse_service
from app.services.llm_client import DraftEditResult, EventSlotFillResult
from app.services.slot_fill_session import clear_all_sessions

MEETING = dict(title="미팅", date="10-01", start_time="20:00", end_time="21:00", importance=2)


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
def user_id(engine) -> int:
    with Session(engine) as session:
        user = User(name="June", preferred_language="ko")
        session.add(user)
        session.flush()
        session.add(ImportantDateRange(user_id=user.id, name="2026-2학기", start_date=date(2026, 9, 1), end_date=date(2026, 12, 20)))
        session.commit()
        return user.id


class FakeLLM:
    """슬롯필링(slots)과 초안 수정(edits) 응답을 차례로 돌려준다. 준비하지 않은 호출은 실패한다."""

    def __init__(self) -> None:
        self.slots: list[EventSlotFillResult] = []
        self.edits: list[DraftEditResult] = []
        self.edit_calls: list[dict] = []

    def slot(self, **fields) -> None:
        self.slots.append(EventSlotFillResult(**fields))

    def edit(self, **fields) -> None:
        self.edits.append(DraftEditResult(**fields))


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    fake = FakeLLM()

    def fill_slots(db, user_id, utterance, **kwargs):
        assert fake.slots, f"예상하지 못한 슬롯필링 호출: {utterance!r}"
        return fake.slots.pop(0)

    def fill_edit(db, user_id, utterance, **kwargs):
        assert fake.edits, f"예상하지 못한 초안 수정 호출: {utterance!r}"
        fake.edit_calls.append({"utterance": utterance, **kwargs})
        return fake.edits.pop(0)

    monkeypatch.setattr(event_parse_service, "fill_event_slots_for_user", fill_slots)
    monkeypatch.setattr(event_parse_service, "fill_draft_edit_for_user", fill_edit)
    return fake


def _parse(client: TestClient, user_id: int, utterance: str, session_id: str | None = None) -> dict:
    body = {"user_id": user_id, "utterance": utterance}
    if session_id:
        body["session_id"] = session_id
    response = client.post("/events/parse", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _start_meeting(client: TestClient, user_id: int, llm: FakeLLM, **overrides) -> dict:
    llm.slot(**{**MEETING, **overrides})
    body = _parse(client, user_id, "10월 1일 오후 8시 미팅 1시간")
    assert body["is_complete"] is True and body["draft"]
    return body


def _events(engine) -> list[Event]:
    with Session(engine) as session:
        return list(session.execute(select(Event).order_by(Event.id)).scalars())


def _times(body: dict) -> tuple[str | None, str]:
    return body["draft"]["start_time"], body["draft"]["end_time"]


# --- 시간·중요도·제목 ---------------------------------------------------------------


def test_start_change_keeps_duration_and_replaces_the_confirmation_token(client, engine, user_id, llm):
    first = _start_meeting(client, user_id, llm)
    llm.edit(start_time="19:00")

    body = _parse(client, user_id, "7시로 바꿔줘", first["session_id"])

    assert _times(body) == ("2026-10-01T19:00:00", "2026-10-01T20:00:00")
    assert body["draft_changes"] == ["time"]
    assert body["message"] == "초안을 고쳤어요. 이 내용으로 만들까요?"
    old_token, new_token = first["command"]["confirmation_token"], body["command"]["confirmation_token"]
    assert new_token != old_token
    assert client.post("/events/commands/confirm", json={"user_id": user_id, "token": old_token}).status_code == 404, "옛 초안은 만들 수 없다"
    assert client.post("/events/commands/confirm", json={"user_id": user_id, "token": new_token}).status_code == 200
    [event] = _events(engine)
    assert event.start_time.hour == 19


def test_duration_change(client, engine, user_id, llm):
    first = _start_meeting(client, user_id, llm)
    llm.edit(duration_minutes=120)

    body = _parse(client, user_id, "2시간으로 해줘", first["session_id"])

    assert _times(body) == ("2026-10-01T20:00:00", "2026-10-01T22:00:00")


def test_time_and_importance_together_and_history_is_passed(client, engine, user_id, llm):
    first = _start_meeting(client, user_id, llm)
    llm.edit(start_time="19:00", importance=3)

    body = _parse(client, user_id, "7시로 바꾸고 중요도 3", first["session_id"])

    assert _times(body)[0] == "2026-10-01T19:00:00"
    assert body["draft"]["importance"] == 3
    assert set(body["draft_changes"]) == {"time", "importance"}
    call = llm.edit_calls[0]
    assert call["draft"]["start_time"] == "2026-10-01T20:00:00", "LLM에는 지금 보고 있는 초안이 간다"
    assert call["history"][-1] == ("assistant", "이 내용으로 만들까요?"), "초안 수정도 최근 대화를 함께 넘긴다"


def test_title_change(client, engine, user_id, llm):
    first = _start_meeting(client, user_id, llm)
    llm.edit(title="UTKESA 썸포차 미팅")

    body = _parse(client, user_id, "제목을 UTKESA 썸포차 미팅으로", first["session_id"])

    assert body["draft"]["title"] == "UTKESA 썸포차 미팅"
    assert body["draft_changes"] == ["title"]


def test_end_before_start_asks_again(client, engine, user_id, llm):
    first = _start_meeting(client, user_id, llm)
    llm.edit(end_time="19:00")

    asked = _parse(client, user_id, "끝나는 건 7시로", first["session_id"])

    assert asked["is_complete"] is False and asked["draft"] is None
    assert asked["next_question"] == {"slot": "end_time", "question": "끝나는 시각이 시작(20:00)보다 빨라요. 몇 시에 끝나나요?"}
    llm.slot(**{**MEETING, "end_time": "21:30"})

    body = _parse(client, user_id, "9시 반", first["session_id"])

    assert _times(body) == ("2026-10-01T20:00:00", "2026-10-01T21:30:00")
    assert body["draft_changes"] == ["time"]


def test_same_start_and_end_is_asked_instead_of_a_24_hour_event(client, engine, user_id, llm):
    llm.slot(**{**MEETING, "start_time": "09:00", "end_time": "09:00"})

    body = _parse(client, user_id, "10월 1일 오전 9시 미팅")

    assert body["is_complete"] is False
    assert body["next_question"] == {"slot": "end_time", "question": "끝나는 시각이 시작(09:00)과 같아요. 몇 시에 끝나나요?"}


def test_switch_to_deadline(client, engine, user_id, llm):
    first = _start_meeting(client, user_id, llm)
    llm.edit(event_type="deadline", end_time="23:59")

    body = _parse(client, user_id, "마감 일정으로 바꿔줘, 밤 11시 59분까지", first["session_id"])

    assert body["draft"]["event_type"] == "deadline"
    assert _times(body) == (None, "2026-10-01T23:59:00")
    assert set(body["draft_changes"]) == {"event_type", "time"}


# --- 반복 ---------------------------------------------------------------------------


def test_adding_recurrence_asks_only_for_the_period(client, engine, user_id, llm):
    first = _start_meeting(client, user_id, llm)
    llm.edit(recurrence="set", frequency="WEEKLY", by_day=["WE"])

    asked = _parse(client, user_id, "매주 수요일로 반복해줘", first["session_id"])

    assert asked["next_question"] == {
        "slot": "date_range_id",
        "question": "2026-2학기(9/1~12/20)까지 반복할까요? 다른 날짜까지라면 말해주세요.",
    }
    llm.slot(**MEETING, frequency="WEEKLY", by_day=["WE"], date_range_id=1)

    body = _parse(client, user_id, "응 학기 끝까지", first["session_id"])

    draft = body["draft"]
    assert (draft["is_recurring"], draft["recurrence_rule"], draft["date_range_id"]) == (True, "FREQ=WEEKLY;BYDAY=WE", 1)
    assert "recurrence" in body["draft_changes"]


def test_removing_recurrence_makes_a_one_off_on_the_shown_date(client, engine, user_id, llm):
    llm.slot(title="스터디", frequency="WEEKLY", by_day=["WE"], start_time="19:00", end_time="21:00", importance=3, date_range_id=1)
    first = _parse(client, user_id, "매주 수요일 저녁 7시 스터디, 2학기 동안")
    assert first["draft"]["is_recurring"] is True
    llm.edit(recurrence="remove")

    body = _parse(client, user_id, "반복 빼줘", first["session_id"])

    draft = body["draft"]
    assert (draft["is_recurring"], draft["recurrence_rule"], draft["date_range_id"]) == (False, None, None)
    assert draft["start_time"] == first["draft"]["start_time"]
    assert body["draft_changes"] == ["recurrence"]


# --- 장소 ---------------------------------------------------------------------------


def test_registered_location_is_linked_and_creates_travel_child(client, engine, user_id, llm):
    with Session(engine) as session:
        session.add(Location(user_id=user_id, name="Bahen", default_travel_minutes=20))
        session.commit()
    first = _start_meeting(client, user_id, llm)
    llm.edit(location_name="bahen")

    body = _parse(client, user_id, "장소는 Bahen이야", first["session_id"])

    assert (body["draft"]["location_name"], body["draft"]["new_location"]) == ("Bahen", None)
    assert body["draft_changes"] == ["location"]
    _parse(client, user_id, "좋아", first["session_id"])
    parent, child = _events(engine)
    assert child.parent_event_id == parent.id and child.start_time.hour == 19 and child.start_time.minute == 40


def test_new_location_asks_travel_time_and_is_created_on_confirm(client, engine, user_id, llm):
    first = _start_meeting(client, user_id, llm)
    llm.edit(location_name="Myhal")

    asked = _parse(client, user_id, "장소는 Myhal이야", first["session_id"])

    assert asked["next_question"] == {"slot": "location", "question": "'Myhal'은(는) 처음 보는 장소예요. 이동 시간이 몇 분인가요?"}
    body = _parse(client, user_id, "15분", first["session_id"])  # LLM 없이 처리한다

    assert body["draft"]["new_location"] == {"name": "Myhal", "default_travel_minutes": 15}
    assert body["draft_changes"] == ["location"]
    assert "장소 'Myhal'(이동 15분)도 새로 등록해요" in body["message"]
    with Session(engine) as session:
        assert session.execute(select(Location)).first() is None, "확정 전에는 장소를 만들지 않는다"

    created = _parse(client, user_id, "응 만들어줘", first["session_id"])

    with Session(engine) as session:
        [location] = session.execute(select(Location)).scalars().all()
        assert (location.name, location.default_travel_minutes) == ("Myhal", 15)
    assert len(_events(engine)) == 2, "이동시간 하위 일정도 생긴다"
    assert client.post(f"/actions/{created['command']['action_id']}/undo", headers={"X-User-Id": str(user_id)}).status_code == 200
    with Session(engine) as session:
        assert session.execute(select(Location)).first() is None, "되돌리면 같이 만든 장소도 지운다"


# --- 만들기·취소 ---------------------------------------------------------------------


@pytest.mark.parametrize("reply", ["좋아", "응 만들어줘", "그대로 해", "네!"])
def test_ok_reply_creates_like_the_button(client, engine, user_id, llm, reply):
    first = _start_meeting(client, user_id, llm)

    body = _parse(client, user_id, reply, first["session_id"])  # LLM을 부르지 않는다

    assert body["command"]["status"] == "executed" and body["command"]["action"] == "create"
    assert body["command"]["action_id"] is not None
    assert [e.title for e in _events(engine)] == ["미팅"]
    token = first["command"]["confirmation_token"]
    assert client.post("/events/commands/confirm", json={"user_id": user_id, "token": token}).status_code == 404, "두 번 만들어지지 않는다"


@pytest.mark.parametrize("reply", ["취소", "안 만들래"])
def test_cancel_reply_cancels_like_the_button(client, engine, user_id, llm, reply):
    first = _start_meeting(client, user_id, llm)

    body = _parse(client, user_id, reply, first["session_id"])

    assert body["command"]["status"] == "cancelled"
    assert body["message"] == "취소했어요. 아무것도 만들지 않았어요."
    assert _events(engine) == []
    token = first["command"]["confirmation_token"]
    assert client.post("/events/commands/confirm", json={"user_id": user_id, "token": token}).status_code == 404


def test_llm_can_also_decide_confirm(client, engine, user_id, llm):
    first = _start_meeting(client, user_id, llm)
    llm.edit(decision="confirm")

    body = _parse(client, user_id, "그래 그걸로 등록하자", first["session_id"])

    assert body["command"]["status"] == "executed"
    assert len(_events(engine)) == 1


# --- 기존 명령으로 새지 않기 · 지원하지 않는 요청 ----------------------------------------


def test_change_words_edit_the_draft_not_an_existing_event(client, engine, user_id, llm):
    existing = client.post(
        "/events", json={"user_id": user_id, "title": "미팅", "start_time": "2026-09-30T10:00:00", "end_time": "2026-09-30T11:00:00"}
    ).json()["id"]
    first = _start_meeting(client, user_id, llm)
    llm.edit(start_time="19:00")

    body = _parse(client, user_id, "미팅 7시로 바꿔줘", first["session_id"])  # 슬롯필링(기존 명령 분류)은 부르지 않는다

    assert body["draft"]["start_time"] == "2026-10-01T19:00:00"
    with Session(engine) as session:
        assert session.get(Event, existing).start_time.hour == 10, "기존 일정은 그대로"


def test_clearly_other_event_command_goes_to_existing_flow_and_keeps_the_draft(client, engine, user_id, llm):
    client.post(
        "/events", json={"user_id": user_id, "title": "물리 퀴즈", "start_time": "2026-09-30T10:00:00", "end_time": "2026-09-30T11:00:00"}
    )
    first = _start_meeting(client, user_id, llm)
    llm.edit(decision="other_event_command")
    llm.slot(intent="delete", target_title="물리 퀴즈")

    body = _parse(client, user_id, "기존 물리 퀴즈를 지워줘", first["session_id"])

    assert body["intent"] == "delete" and body["command"]["status"] == "executed"
    assert [e.title for e in _events(engine)] == []
    token = first["command"]["confirmation_token"]
    assert client.post("/events/commands/confirm", json={"user_id": user_id, "token": token}).status_code == 200, "초안은 그대로 남는다"


def test_unsupported_request_is_reported_and_the_draft_is_kept(client, engine, user_id, llm):
    first = _start_meeting(client, user_id, llm)
    llm.edit(unsupported=["메모"])

    body = _parse(client, user_id, "메모 추가해줘: 회의록 챙기기", first["session_id"])

    assert body["message"] == "지금은 메모은(는) 설정할 수 없어요. 초안은 그대로예요. 이 내용으로 만들까요?"
    assert body["draft"] == first["draft"] and body["draft_changes"] == []
    assert body["command"]["confirmation_token"] == first["command"]["confirmation_token"]


def test_unsupported_part_is_reported_while_the_rest_is_applied(client, engine, user_id, llm):
    first = _start_meeting(client, user_id, llm)
    llm.edit(importance=5, unsupported=["알림 시각"])

    body = _parse(client, user_id, "중요도 5로, 알림은 30분 전에", first["session_id"])

    assert body["draft"]["importance"] == 5
    assert body["message"] == "지금은 알림 시각은(는) 설정할 수 없어요. 초안을 고쳤어요. 이 내용으로 만들까요?"
