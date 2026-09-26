"""FR-2 v3.7 대화 중 반복 기간 만들기·관리. LLM은 fill_event_slots_for_user를 가짜로 바꿔 호출하지 않는다."""

from collections.abc import Callable
from datetime import date

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.models import Base, Event, EventInstance, ImportantDateRange, User
from app.models.enums import EventInstanceStatus
from app.services import event_parse_service
from app.services.llm_client import EventSlotFillResult, NewDateRangeSlot
from app.services.slot_fill_session import clear_all_sessions

SEMESTER_QUESTION = "2026-2학기(9/1~12/20)까지 반복할까요? 다른 날짜까지라면 말해주세요."


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


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch):
    """다음 LLM 응답들을 차례로 돌려준다. 호출 인자(최근 대화 등)는 calls에 남는다."""
    queue: list[EventSlotFillResult] = []
    calls: list[dict] = []

    def fake(db, user_id, utterance, **kwargs) -> EventSlotFillResult:
        assert queue, f"예상하지 못한 LLM 호출: {utterance!r}"
        calls.append({"utterance": utterance, **kwargs})
        return queue.pop(0)

    monkeypatch.setattr(event_parse_service, "fill_event_slots_for_user", fake)

    def push(**fields) -> None:
        queue.append(EventSlotFillResult(**fields))

    push.calls = calls
    return push


LECTURE = dict(title="물리 강의", frequency="WEEKLY", by_day=["MO", "WE"], start_time="10:00", end_time="11:00", importance=4)


def _parse(client: TestClient, user_id: int, utterance: str, session_id: str | None = None) -> dict:
    body = {"user_id": user_id, "utterance": utterance}
    if session_id:
        body["session_id"] = session_id
    response = client.post("/events/parse", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _confirm(client: TestClient, user_id: int, token: str, option: str | None = None):
    body = {"user_id": user_id, "token": token}
    if option:
        body["option"] = option
    return client.post("/events/commands/confirm", json=body)


def _ranges(engine) -> dict[str, tuple[date, date]]:
    with Session(engine) as session:
        return {r.name: (r.start_date, r.end_date) for r in session.execute(select(ImportantDateRange)).scalars()}


def _instances(engine, event_id: int) -> list[tuple[date, EventInstanceStatus]]:
    with Session(engine) as session:
        rows = session.execute(
            select(EventInstance.date, EventInstance.status).where(EventInstance.event_id == event_id).order_by(EventInstance.date)
        ).all()
        return [(day, status) for day, status in rows]


def _undo(client: TestClient, user_id: int, action_id: int):
    return client.post(f"/actions/{action_id}/undo", headers={"X-User-Id": str(user_id)})


# --- 일정을 만들면서 새 기간 ---------------------------------------------------------


def test_conversation_creates_named_range_and_event_without_asking_again(client, engine, user_id, llm):
    llm(**LECTURE, missing_slots=["date_range_id"], clarifying_questions=[{"slot": "date_range_id", "question": "반복 기간을 선택해 주세요."}])
    llm(**LECTURE, new_date_range=NewDateRangeSlot(name="Lecture End Date", start_date="2026-09-01", end_date="12-08"))

    first = _parse(client, user_id, "매주 월수 오전 10시 물리 강의 추가해줘")
    assert first["next_question"] == {"slot": "date_range_id", "question": SEMESTER_QUESTION}

    second = _parse(client, user_id, "Lecture End Date는 12월 8일이니깐 2학기 시작부터 그때까지로 등록해줘", first["session_id"])

    assert second["is_complete"] is True, "분명히 말한 새 기간을 다시 묻지 않는다"
    draft = second["draft"]
    assert draft["date_range_id"] is None
    assert draft["new_date_range"] == {"name": "Lecture End Date", "start_date": "2026-09-01", "end_date": "2026-12-08", "auto_named": False}
    assert "Lecture End Date" in second["message"]
    # 두 번째 LLM 호출에는 앱이 물은 말과 사용자의 첫 요청이 최근 대화로 함께 간다
    assert llm.calls[1]["history"] == [("user", "매주 월수 오전 10시 물리 강의 추가해줘"), ("assistant", SEMESTER_QUESTION)]
    assert _ranges(engine) == {"2026-2학기": (date(2026, 9, 1), date(2026, 12, 20))}, "확정 전에는 기간을 만들지 않는다"

    response = _confirm(client, user_id, second["command"]["confirmation_token"])

    assert response.status_code == 200, response.text
    assert _ranges(engine)["Lecture End Date"] == (date(2026, 9, 1), date(2026, 12, 8))
    [event_id] = [t["event_id"] for t in response.json()["command"]["affected"]]
    with Session(engine) as session:
        event = session.get(Event, event_id)
        assert event.date_range.name == "Lecture End Date"
    dates = [day for day, _ in _instances(engine, event_id)]
    assert dates[0] == date(2026, 9, 2) and dates[-1] == date(2026, 12, 7)
    assert response.json()["message"] == "✔ '물리 강의' 일정 생성 (반복 기간 'Lecture End Date' 새로 만듦)"


def test_end_date_only_gets_an_automatic_name(client, engine, user_id, llm):
    llm(**LECTURE, missing_slots=["date_range_id"])
    llm(**LECTURE, new_date_range=NewDateRangeSlot(name=None, start_date=None, end_date="12-08"))

    first = _parse(client, user_id, "매주 월수 오전 10시 물리 강의")
    second = _parse(client, user_id, "12월 8일까지", first["session_id"])

    assert second["draft"]["new_date_range"] == {
        "name": "2026-2학기 (~12/8)",
        "start_date": "2026-09-01",
        "end_date": "2026-12-08",
        "auto_named": True,
    }
    assert "기간 이름은 '2026-2학기 (~12/8)'(으)로 붙였어요" in second["message"]

    confirmed = _confirm(client, user_id, second["command"]["confirmation_token"]).json()

    assert _ranges(engine)["2026-2학기 (~12/8)"] == (date(2026, 9, 1), date(2026, 12, 8))
    assert "기간 이름은 '2026-2학기 (~12/8)'(으)로 붙였어요" in confirmed["message"]


def test_existing_range_with_same_name_is_reused(client, engine, user_id, llm):
    with Session(engine) as session:
        session.add(ImportantDateRange(user_id=user_id, name="Lecture End Date", start_date=date(2026, 9, 1), end_date=date(2026, 12, 8)))
        session.commit()
    llm(**LECTURE, new_date_range=NewDateRangeSlot(name="lecture end  date", start_date=None, end_date="12-08"))

    body = _parse(client, user_id, "매주 월수 오전 10시 물리 강의, lecture end date까지")

    assert body["draft"]["new_date_range"] is None
    existing_id = body["draft"]["date_range_id"]
    assert existing_id is not None
    _confirm(client, user_id, body["command"]["confirmation_token"])
    assert len(_ranges(engine)) == 2, "같은 이름이면 새로 만들지 않는다"


def test_range_with_same_name_created_before_confirmation_is_reused(client, engine, user_id, llm):
    llm(**LECTURE, new_date_range=NewDateRangeSlot(name="Lecture End Date", start_date="2026-09-01", end_date="12-08"))
    token = _parse(client, user_id, "매주 월수 오전 10시 물리 강의, Lecture End Date(12/8)까지")["command"]["confirmation_token"]
    client.post("/date-ranges", json={"user_id": user_id, "name": "Lecture End Date", "start_date": "2026-09-01", "end_date": "2026-12-08"})

    _confirm(client, user_id, token)

    assert len(_ranges(engine)) == 2


def test_cancelled_conversation_leaves_no_range(client, engine, user_id, llm):
    llm(**LECTURE, new_date_range=NewDateRangeSlot(name="Lecture End Date", start_date="2026-09-01", end_date="12-08"))

    body = _parse(client, user_id, "매주 월수 오전 10시 물리 강의, Lecture End Date(12/8)까지")

    assert body["is_complete"] is True
    # 확인하지 않고(취소하고) 새 대화를 시작해도 기간은 남지 않는다
    assert _ranges(engine) == {"2026-2학기": (date(2026, 9, 1), date(2026, 12, 20))}


def test_undoing_event_with_new_range_removes_both(client, engine, user_id, llm):
    llm(**LECTURE, new_date_range=NewDateRangeSlot(name="Lecture End Date", start_date="2026-09-01", end_date="12-08"))
    token = _parse(client, user_id, "매주 월수 오전 10시 물리 강의, Lecture End Date(12/8)까지")["command"]["confirmation_token"]
    action_id = _confirm(client, user_id, token).json()["command"]["action_id"]

    assert _undo(client, user_id, action_id).status_code == 200

    assert "Lecture End Date" not in _ranges(engine)
    with Session(engine) as session:
        assert session.execute(select(Event)).first() is None


def test_undoing_event_keeps_new_range_if_another_event_uses_it(client, engine, user_id, llm):
    llm(**LECTURE, new_date_range=NewDateRangeSlot(name="Lecture End Date", start_date="2026-09-01", end_date="12-08"))
    token = _parse(client, user_id, "매주 월수 오전 10시 물리 강의, Lecture End Date(12/8)까지")["command"]["confirmation_token"]
    action_id = _confirm(client, user_id, token).json()["command"]["action_id"]
    with Session(engine) as session:
        range_id = session.execute(select(ImportantDateRange.id).where(ImportantDateRange.name == "Lecture End Date")).scalar_one()
    client.post(
        "/events",
        json={
            "user_id": user_id, "title": "세미나", "start_time": "2026-09-04T15:00:00", "end_time": "2026-09-04T16:00:00",
            "is_recurring": True, "recurrence_rule": "FREQ=WEEKLY;BYDAY=FR", "date_range_id": range_id,
        },
    )

    assert _undo(client, user_id, action_id).status_code == 200

    assert "Lecture End Date" in _ranges(engine)


# --- 기간 자체를 자연어로 ---------------------------------------------------------------


def _weekly_on_range(client: TestClient, user_id: int, range_id: int, byday: str = "MO", title: str = "물리 강의") -> int:
    response = client.post(
        "/events",
        json={
            "user_id": user_id, "title": title, "start_time": "2026-09-07T10:00:00", "end_time": "2026-09-07T11:00:00",
            "is_recurring": True, "recurrence_rule": f"FREQ=WEEKLY;BYDAY={byday}", "date_range_id": range_id,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _lecture_range(client: TestClient, user_id: int, start: str = "2026-09-01", end: str = "2026-10-31") -> int:
    return client.post("/date-ranges", json={"user_id": user_id, "name": "Lecture End Date", "start_date": start, "end_date": end}).json()["id"]


def _set_status(engine, event_id: int, day: date, status: EventInstanceStatus) -> None:
    with Session(engine) as session:
        session.execute(select(EventInstance).where(EventInstance.event_id == event_id, EventInstance.date == day)).scalar_one().status = status
        session.commit()


def test_create_range_by_voice_and_undo(client, engine, user_id, llm):
    llm(intent="create", target_kind="date_range", range_name="Lecture End Date", range_start="09-01", range_end="12-08")

    body = _parse(client, user_id, "Lecture End Date 기간 만들어줘, 9월 1일부터 12월 8일까지")

    assert body["command"]["status"] == "executed" and body["command"]["target_kind"] == "date_range"
    assert _ranges(engine)["Lecture End Date"] == (date(2026, 9, 1), date(2026, 12, 8)), "연도 없는 날짜는 올해 기준"
    assert _undo(client, user_id, body["command"]["action_id"]).status_code == 200
    assert "Lecture End Date" not in _ranges(engine)


def test_list_ranges(client, engine, user_id, llm):
    range_id = _lecture_range(client, user_id)
    _weekly_on_range(client, user_id, range_id)
    llm(intent="list", target_kind="date_range")

    body = _parse(client, user_id, "등록된 기간 보여줘")

    assert body["intent"] == "list"
    assert body["message"] == "등록된 반복 기간이에요:\n- 2026-2학기: 9/1~12/20 (일정 0개)\n- Lecture End Date: 9/1~10/31 (일정 1개)"


def test_extending_range_adds_instances_and_undo_removes_them(client, engine, user_id, llm):
    range_id = _lecture_range(client, user_id)
    event_id = _weekly_on_range(client, user_id, range_id)
    before = _instances(engine, event_id)
    assert before[-1][0] == date(2026, 10, 26)
    llm(intent="update", target_kind="date_range", range_name="Lecture End Date", range_end="12-10")

    body = _parse(client, user_id, "Lecture End Date를 12월 10일까지로 바꿔줘")

    assert body["command"]["status"] == "executed"
    assert _ranges(engine)["Lecture End Date"] == (date(2026, 9, 1), date(2026, 12, 10))
    after = _instances(engine, event_id)
    assert after[-1] == (date(2026, 12, 7), EventInstanceStatus.PENDING)
    assert len(after) == len(before) + 6
    assert "회차 6개 추가·0개 취소" in body["message"]

    assert _undo(client, user_id, body["command"]["action_id"]).status_code == 200
    assert _instances(engine, event_id) == before
    assert _ranges(engine)["Lecture End Date"] == (date(2026, 9, 1), date(2026, 10, 31))


def test_shrinking_range_cancels_pending_instances_but_keeps_past_records(client, engine, user_id, llm):
    range_id = _lecture_range(client, user_id)
    event_id = _weekly_on_range(client, user_id, range_id)
    _set_status(engine, event_id, date(2026, 9, 7), EventInstanceStatus.DONE)
    _set_status(engine, event_id, date(2026, 9, 14), EventInstanceStatus.MISSED)
    before = _instances(engine, event_id)
    llm(intent="update", target_kind="date_range", range_name="Lecture End Date", range_start="09-15", range_end="10-05")

    body = _parse(client, user_id, "Lecture End Date를 9월 15일부터 10월 5일까지로 바꿔줘")

    statuses = dict(_instances(engine, event_id))
    assert statuses[date(2026, 9, 7)] == EventInstanceStatus.DONE, "범위 밖이어도 완료 기록은 그대로"
    assert statuses[date(2026, 9, 14)] == EventInstanceStatus.MISSED, "놓침 기록도 그대로"
    assert [d for d, s in statuses.items() if s == EventInstanceStatus.PENDING] == [date(2026, 9, 21), date(2026, 9, 28), date(2026, 10, 5)]
    assert all(statuses[d] == EventInstanceStatus.CANCELLED for d in (date(2026, 10, 12), date(2026, 10, 26)))

    assert _undo(client, user_id, body["command"]["action_id"]).status_code == 200
    assert _instances(engine, event_id) == before


def test_deleting_unused_range_runs_immediately(client, engine, user_id, llm):
    _lecture_range(client, user_id)
    llm(intent="delete", target_kind="date_range", range_name="Lecture End Date")

    body = _parse(client, user_id, "Lecture End Date 기간 지워줘")

    assert body["command"]["status"] == "executed"
    assert "Lecture End Date" not in _ranges(engine)


def test_deleting_range_in_use_asks_first_then_range_only_keeps_events(client, engine, user_id, llm):
    range_id = _lecture_range(client, user_id)
    event_id = _weekly_on_range(client, user_id, range_id)
    before = _instances(engine, event_id)
    llm(intent="delete", target_kind="date_range", range_name="Lecture End Date")

    body = _parse(client, user_id, "Lecture End Date 기간 지워줘")

    command = body["command"]
    assert command["status"] == "needs_confirmation" and command["target_kind"] == "date_range"
    assert command["options"] == ["range_only", "with_events"]
    assert [t["event_id"] for t in command["affected"]] == [event_id]
    assert body["message"] == "'Lecture End Date' 기간을 쓰는 반복 일정이 1개 있어요: 물리 강의. 어떻게 할까요?"
    assert "Lecture End Date" in _ranges(engine), "확인 전에는 지우지 않는다"

    assert _confirm(client, user_id, command["confirmation_token"]).status_code == 422, "선택지 없이 확인할 수 없다"
    response = _confirm(client, user_id, command["confirmation_token"], "range_only")

    assert response.status_code == 200, "선택지가 빠진 요청 뒤에도 토큰은 그대로 쓸 수 있다"
    assert "Lecture End Date" not in _ranges(engine)
    with Session(engine) as session:
        assert session.get(Event, event_id).date_range_id is None
    assert _instances(engine, event_id) == before, "일정은 이미 만들어진 마지막 회차에서 끝난다"

    assert _undo(client, user_id, response.json()["command"]["action_id"]).status_code == 200
    with Session(engine) as session:
        assert session.get(Event, event_id).date_range_id == range_id


def test_deleting_range_with_events_and_undo(client, engine, user_id, llm):
    range_id = _lecture_range(client, user_id)
    event_id = _weekly_on_range(client, user_id, range_id)
    before = _instances(engine, event_id)
    llm(intent="delete", target_kind="date_range", range_name="Lecture End Date")
    token = _parse(client, user_id, "Lecture End Date 기간 지워줘")["command"]["confirmation_token"]

    response = _confirm(client, user_id, token, "with_events")

    assert "Lecture End Date" not in _ranges(engine)
    with Session(engine) as session:
        assert session.get(Event, event_id) is None
    assert _undo(client, user_id, response.json()["command"]["action_id"]).status_code == 200
    assert "Lecture End Date" in _ranges(engine)
    assert _instances(engine, event_id) == before


# --- 화면(API)에서 --------------------------------------------------------------------


def test_ui_update_and_delete_are_recorded_and_list_has_usage(client, engine, user_id):
    range_id = _lecture_range(client, user_id)
    _weekly_on_range(client, user_id, range_id)

    listed = client.get("/date-ranges", params={"user_id": user_id}).json()
    assert [(r["name"], r["event_count"]) for r in listed] == [("2026-2학기", 0), ("Lecture End Date", 1)]

    updated = client.put(f"/date-ranges/{range_id}", json={"end_date": "2026-12-10"})
    assert updated.status_code == 200 and updated.headers["X-Action-Id"]

    assert client.delete(f"/date-ranges/{range_id}").status_code == 409, "사용 중이면 처리 방법(mode)이 필요하다"
    deleted = client.delete(f"/date-ranges/{range_id}", params={"mode": "range_only"})
    assert deleted.status_code == 204 and deleted.headers["X-Action-Id"]
    actions = client.get("/actions", headers={"X-User-Id": str(user_id)}).json()
    assert [a["summary_text"] for a in actions[:2]] == [
        "반복 기간 'Lecture End Date' 삭제",
        "반복 기간 'Lecture End Date': 기간 9/1~10/31→9/1~12/10, 회차 6개 추가·0개 취소",
    ]


def test_recurring_event_does_not_ask_for_a_first_date(client, engine, user_id, llm):
    # 실제 LLM이 반복 일정에도 "첫 강의 날짜는 언제인가요?"를 물었던 경우. 반복 일정의 첫 날짜는 선택이다.
    llm(
        **LECTURE,
        missing_slots=["date", "date_range_id"],
        clarifying_questions=[{"slot": "date", "question": "첫 강의 날짜는 언제인가요?"}],
    )

    body = _parse(client, user_id, "매주 월수 오전 10시부터 11시까지 물리 강의")

    assert body["next_question"] == {"slot": "date_range_id", "question": SEMESTER_QUESTION}
    assert "date" not in body["missing_slots"]
