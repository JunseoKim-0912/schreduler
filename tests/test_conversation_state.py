"""v3.8 대화 상태 초기화, 마감일 자연어 입력, 너그러운 제목 매칭. LLM은 가짜로 바꿔 호출하지 않는다."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.models import Base, ImportantDateRange, User
from app.services import event_parse_service
from app.services.llm_client import EventSlotFillResult
from app.services.slot_fill_session import clear_all_sessions, get_session

TASK = dict(intent="create", answers_previous_question=False, title="MAT389 과제 제출", event_type="deadline",
            date="09-29", end_time="23:30", importance=4)


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
def user_id(engine, client) -> int:
    with Session(engine) as session:
        user = User(name="June", preferred_language="ko")
        session.add(user)
        session.flush()
        period = ImportantDateRange(user_id=user.id, name="Lecture period", start_date=date(2026, 9, 8), end_date=date(2026, 12, 8))
        session.add(period)
        session.commit()
        user_id, period_id = user.id, period.id
    for title, byday in (("ESC360 Lecture", "MO"), ("ECE360 Lab", "TU")):
        client.post(
            "/events",
            json={"user_id": user_id, "title": title, "start_time": "2026-09-14T11:00:00", "end_time": "2026-09-14T12:00:00",
                  "is_recurring": True, "recurrence_rule": f"FREQ=WEEKLY;BYDAY={byday}", "date_range_id": period_id},
        )
    return user_id


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch):
    queue: list[EventSlotFillResult] = []
    calls: list[dict] = []

    def fake(db, user_id, utterance, **kwargs):
        assert queue, f"예상하지 못한 LLM 호출: {utterance!r}"
        calls.append({"utterance": utterance, **kwargs})
        return queue.pop(0)

    monkeypatch.setattr(event_parse_service, "fill_event_slots_for_user", fake)

    def push(**fields) -> None:
        queue.append(EventSlotFillResult(**fields))

    push.calls = calls
    return push


def _parse(client: TestClient, user_id: int, utterance: str, session_id: str | None = None) -> dict:
    body = {"user_id": user_id, "utterance": utterance}
    if session_id:
        body["session_id"] = session_id
    response = client.post("/events/parse", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# --- 실제 대화 재현: 실패한 수정 뒤의 과제 문장 ---------------------------------------


def test_new_create_after_a_stuck_update_is_a_new_request(client, user_id, llm):
    llm(intent="update", answers_previous_question=False, target_title="ECE360 Lecture", target_weekday="MO")
    llm(intent="update", answers_previous_question=True, target_title="ECE360 Lecture")
    llm(**TASK)

    first = _parse(client, user_id, "월요일의 ECE360 Lecture에 장소 넣어줘 'Galbraith 304'")
    second = _parse(client, user_id, "장소 추가해줘", first["session_id"])
    assert "바꿀 수 있는 것" in second["message"]

    body = _parse(client, user_id, "9/29 화요일 11:30 pm에 MAT389 과제 제출날이야", first["session_id"])

    assert body["intent"] == "create" and body["is_complete"] is True, "이전 수정 요청에 합쳐지지 않는다"
    assert (body["draft"]["event_type"], body["draft"]["start_time"], body["draft"]["end_time"]) == (
        "deadline", None, "2026-09-29T23:30:00",
    )
    assert get_session(first["session_id"]).command is None


def test_answer_to_the_question_still_continues_the_request(client, user_id, llm):
    llm(intent="update", answers_previous_question=False, target_title="ESC360 Lecture", location_action="set")
    llm(intent="update", answers_previous_question=True, location_action="set", new_location_name="Bahen")

    asked = _parse(client, user_id, "ESC360 Lecture에 장소 추가해줘")
    body = _parse(client, user_id, "Galbraith 304", asked["session_id"])

    assert body["command"]["status"] == "needs_clarification"
    assert body["message"] == "Bahen까지 이동 시간이 몇 분인가요?", "앞 문장의 대상(ESC360 Lecture)을 이어받는다"


# --- 명령이 끝나면 상태를 비운다 --------------------------------------------------------


def test_state_is_cleared_after_success(client, user_id, llm):
    llm(intent="update", answers_previous_question=False, target_title="ESC360 Lecture", target_scope="series", new_importance=5)

    body = _parse(client, user_id, "ESC360 Lecture 중요도 5로")

    assert body["command"]["status"] == "executed"
    assert get_session(body["session_id"]).command is None


def test_state_is_cleared_after_not_found(client, user_id, llm):
    llm(intent="delete", answers_previous_question=False, target_title="수영 강습")
    llm(intent="unknown", answers_previous_question=True)

    first = _parse(client, user_id, "수영 강습 지워줘")
    assert first["command"]["status"] == "not_found"
    assert get_session(first["session_id"]).command is None

    body = _parse(client, user_id, "그럼 됐어", first["session_id"])
    assert body["intent"] == "unknown", "끝난 명령에 합쳐지지 않는다"


def test_state_is_cleared_after_draft_cancel(client, user_id, llm):
    llm(**TASK)
    first = _parse(client, user_id, "9/29 11:30 pm MAT389 과제 제출")

    body = _parse(client, user_id, "취소", first["session_id"])

    assert body["command"]["status"] == "cancelled"
    assert get_session(first["session_id"]) is None


def test_create_in_progress_is_restarted_for_a_clearly_new_request(client, user_id, llm):
    llm(intent="create", answers_previous_question=False, title="스터디", missing_slots=["start_time", "end_time"],
        clarifying_questions=[{"slot": "start_time", "question": "몇 시에 시작하나요?"}])
    llm(**TASK)  # 새 요청으로 분류
    llm(**TASK)  # 비운 상태로 다시 해석

    first = _parse(client, user_id, "스터디 추가")
    body = _parse(client, user_id, "9/29 11:30 pm에 MAT389 과제 제출날이야", first["session_id"])

    assert body["draft"]["title"] == "MAT389 과제 제출"
    assert llm.calls[2]["known_slots"] == {}, "모으던 '스터디' 슬롯을 섞지 않는다"


# --- 마감일 -----------------------------------------------------------------------


def test_assignment_due_sentence_becomes_a_deadline(client, user_id, llm):
    llm(**TASK)

    body = _parse(client, user_id, "9/29 화요일 11:30 pm에 MAT389 과제 제출날이야")

    draft = body["draft"]
    assert (draft["event_type"], draft["start_time"], draft["end_time"]) == ("deadline", None, "2026-09-29T23:30:00")
    token = body["command"]["confirmation_token"]
    assert client.post("/events/commands/confirm", json={"user_id": user_id, "token": token}).status_code == 200
    tasks = client.get("/tasks", headers={"X-User-Id": str(user_id)}).json()
    assert [(t["title"], t["due_at"]) for t in tasks] == [("MAT389 과제 제출", "2026-09-29T23:30:00")]


def test_deadline_time_given_as_start_is_moved_to_the_deadline(client, user_id, llm):
    llm(**{**TASK, "end_time": None, "start_time": "23:30"})

    body = _parse(client, user_id, "9/29 11:30 pm MAT389 과제 마감")

    assert (body["draft"]["start_time"], body["draft"]["end_time"]) == (None, "2026-09-29T23:30:00")


def test_claimed_complete_but_missing_end_is_asked_instead_of_422(client, user_id, llm):
    # 실제로 422를 냈던 LLM 출력 모양 (event_type 없이 마감 시각을 start_time에, end_time null, missing_slots 비어 있음)
    llm(intent="create", answers_previous_question=False, title="MAT389 과제 제출날", date="2026-09-29", start_time="23:30", importance=4)

    body = _parse(client, user_id, "9/29 화요일 11:30 pm에 MAT389 과제 제출날이야")

    assert body["is_complete"] is False
    assert body["next_question"] == {"slot": "end_time", "question": "몇 시에 끝나나요?"}


# --- 너그러운 제목 매칭 ---------------------------------------------------------------


@pytest.mark.parametrize("said", ["ECE360", "ece-360 lab", "ECE 360 Lab"])
def test_course_code_or_symbols_still_match(client, user_id, llm, said):
    llm(intent="update", answers_previous_question=False, target_title=said, target_scope="series", new_importance=5)

    body = _parse(client, user_id, f"{said} 중요도 5로")

    assert body["command"]["status"] == "executed"
    assert [t["title"] for t in body["command"]["affected"]] == ["ECE360 Lab"]


def test_generic_words_do_not_pull_in_every_lecture(client, engine, user_id, llm):
    client.post(
        "/events",
        json={"user_id": user_id, "title": "ECE355 Lecture", "start_time": "2026-09-14T17:00:00", "end_time": "2026-09-14T18:00:00"},
    )
    llm(intent="update", answers_previous_question=False, target_title="ECE360 Lecture", location_action="set", new_location_name="Bahen")

    body = _parse(client, user_id, "월요일의 ECE360 Lecture에 장소 넣어줘 Bahen")

    candidates = [c["title"] for c in body["command"]["candidates"]]
    assert body["command"]["status"] == "needs_clarification"
    assert candidates == ["ECE360 Lab", "ESC360 Lecture"], "과목 코드가 맞는 것 + 오타로 보이는 비슷한 제목만 (ECE355 Lecture는 아님)"


def test_no_match_shows_similar_titles_as_choices(client, user_id, llm):
    llm(intent="update", answers_previous_question=False, target_title="ESC361 Lecture", location_action="set", new_location_name="Bahen")

    body = _parse(client, user_id, "ESC361 Lecture 장소 Bahen")

    assert body["command"]["status"] == "needs_clarification"
    assert "찾지 못했어요" in body["message"] and "ESC360 Lecture" in body["message"]
    assert "ESC360 Lecture" in [c["title"] for c in body["command"]["candidates"]]
