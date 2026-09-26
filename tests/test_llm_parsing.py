import json
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.db import get_db
from app.core.scheduler import scheduler, shutdown_scheduler, start_scheduler
from app.main import app
from app.models import Base, Event, EventInstance, ImportantDateRange, User
from app.services import llm_client as llm_client_module
from app.services.slot_fill_session import clear_all_sessions, get_session


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "llm_api_key", "test-key")


@pytest.fixture(autouse=True)
def reset_sessions():
    clear_all_sessions()
    yield
    clear_all_sessions()


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def client(engine):
    testing_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = testing_session_local()
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


def _chat_response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]},
    )


def _mock_llm(monkeypatch: pytest.MonkeyPatch, handlers: list) -> None:
    """POST /events/parse 안에서 fill_event_slots가 새로 만드는 httpx.Client를
    가로채, 턴마다 handlers 리스트의 다음 응답을 순서대로 돌려준다."""
    real_client_cls = httpx.Client  # 패치 전에 원본을 캡처 (안 하면 아래서 자기 자신을 재귀 호출함)
    calls = {"count": 0}

    def fake_client_factory(*args, **kwargs) -> httpx.Client:
        index = calls["count"]
        calls["count"] += 1
        handler = handlers[index]
        return real_client_cls(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(llm_client_module.httpx, "Client", fake_client_factory)


def test_parse_event_returns_next_question_when_slots_are_missing(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(
            {
                "title": "알고리즘 스터디",
                "frequency": None,
                "by_day": None,
                "start_time": None,
                "end_time": None,
                "importance": None,
                "date_range_id": None,
                "missing_slots": ["frequency", "by_day", "start_time", "end_time"],
                "clarifying_questions": [
                    {"slot": "frequency", "question": "얼마나 자주 반복하나요?"},
                    {"slot": "by_day", "question": "무슨 요일에 하나요?"},
                    {"slot": "start_time", "question": "몇 시에 시작하나요?"},
                    {"slot": "end_time", "question": "몇 시에 끝나나요?"},
                ],
            }
        )

    _mock_llm(monkeypatch, [handler])

    response = client.post(
        "/events/parse", json={"user_id": user_id, "utterance": "알고리즘 스터디 해야 돼"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["is_complete"] is False
    assert body["next_question"] == {"slot": "frequency", "question": "얼마나 자주 반복하나요?"}
    assert set(body["missing_slots"]) == {"frequency", "by_day", "start_time", "end_time"}
    assert body["draft"] is None
    assert "session_id" in body


def test_parse_event_multiturn_completes_with_draft(
    client: TestClient, engine, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Session(engine) as session:
        date_range = ImportantDateRange(
            user_id=user_id,
            name="2026 가을학기",
            start_date=date(2026, 9, 7),  # 월요일
            end_date=date(2026, 12, 20),
        )
        session.add(date_range)
        session.commit()
        date_range_id = date_range.id

    captured_bodies: list[dict] = []

    def turn1(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content))
        return _chat_response(
            {
                "title": "알고리즘 스터디",
                "frequency": None,
                "by_day": None,
                "start_time": None,
                "end_time": None,
                "importance": None,
                "date_range_id": None,
                "missing_slots": ["frequency", "by_day", "start_time", "end_time", "date_range_id"],
                "clarifying_questions": [
                    {"slot": "frequency", "question": "얼마나 자주 반복하나요?"}
                ],
            }
        )

    def turn2(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content))
        return _chat_response(
            {
                "title": "알고리즘 스터디",
                "frequency": "WEEKLY",
                "by_day": ["MO"],
                "start_time": "09:00",
                "end_time": "10:00",
                "importance": None,
                "date_range_id": date_range_id,
                "missing_slots": [],
                "clarifying_questions": [],
            }
        )

    _mock_llm(monkeypatch, [turn1, turn2])

    first = client.post(
        "/events/parse", json={"user_id": user_id, "utterance": "알고리즘 스터디 해야 돼"}
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["is_complete"] is False
    session_id = first_body["session_id"]

    second = client.post(
        "/events/parse",
        json={
            "user_id": user_id,
            "session_id": session_id,
            "utterance": "매주 월요일 오전 9시부터 10시, 2026 가을학기 기준으로",
        },
    )
    assert second.status_code == 200
    second_body = second.json()

    assert second_body["session_id"] == session_id
    assert second_body["is_complete"] is True
    assert second_body["next_question"] is None
    assert second_body["draft"] == {
        "user_id": user_id,
        "title": "알고리즘 스터디",
        "start_time": "2026-09-07T09:00:00",
        "end_time": "2026-09-07T10:00:00",
        "importance": None,
        "is_recurring": True,
        "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO",
        "date_range_id": date_range_id,
        "new_date_range": None,
    }

    # 2턴째 요청에 1턴에서 알아낸 title이 "이미 확정된 슬롯"으로 같이 넘어갔는지 확인
    second_user_message = captured_bodies[1]["messages"][1]["content"]
    assert '"title": "알고리즘 스터디"' in second_user_message


def test_parse_event_with_unknown_session_id_returns_404(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = client.post(
        "/events/parse",
        json={"user_id": user_id, "session_id": "does-not-exist", "utterance": "아무 말"},
    )

    assert response.status_code == 404


def test_parse_event_rejects_session_from_a_different_user(
    client: TestClient, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Session(engine) as session:
        user1 = User(name="June", preferred_language="ko")
        user2 = User(name="Other", preferred_language="en")
        session.add_all([user1, user2])
        session.commit()
        user1_id, user2_id = user1.id, user2.id

    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(
            {
                "title": "이벤트",
                "missing_slots": ["frequency", "by_day", "start_time", "end_time", "importance", "date_range_id"],
                "clarifying_questions": [{"slot": "frequency", "question": "얼마나 자주 반복하나요?"}],
            }
        )

    _mock_llm(monkeypatch, [handler])

    started = client.post("/events/parse", json={"user_id": user1_id, "utterance": "이벤트 하나 만들자"})
    session_id = started.json()["session_id"]

    response = client.post(
        "/events/parse",
        json={"user_id": user2_id, "session_id": session_id, "utterance": "아무 말"},
    )

    assert response.status_code == 404


def test_parse_event_passes_registered_date_ranges_as_candidates(
    client: TestClient, engine, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Session(engine) as session:
        date_range = ImportantDateRange(
            user_id=user_id,
            name="2026 가을학기",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 12, 20),
        )
        session.add(date_range)
        session.commit()
        date_range_id = date_range.id

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _chat_response(
            {
                "title": "헬스",
                "frequency": "WEEKLY",
                "by_day": ["TU"],
                "start_time": "19:00",
                "end_time": "20:00",
                "importance": 1,
                "date_range_id": date_range_id,
                "missing_slots": [],
                "clarifying_questions": [],
            }
        )

    _mock_llm(monkeypatch, [handler])

    response = client.post(
        "/events/parse",
        json={"user_id": user_id, "utterance": "공강까지 매주 화요일 저녁 7시에 헬스"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["draft"]["date_range_id"] == date_range_id
    assert body["draft"]["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=TU"
    # 반복 시작일(date_range.start_date=2026-09-01) 기준 전체 datetime이어야 함
    assert body["draft"]["start_time"] == "2026-09-01T19:00:00"
    assert body["draft"]["end_time"] == "2026-09-01T20:00:00"
    assert body["draft"]["is_recurring"] is True
    assert body["draft"]["user_id"] == user_id

    system_message = captured["messages"][0]["content"]
    assert "2026 가을학기" in system_message


def test_parse_event_without_date_range_uses_today_as_anchor(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """date_range_id가 끝까지 null로 확정되면(등록된 기간 없이 반복) 오늘 날짜를
    반복 시작일로 anchor 삼는다."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(
            {
                "title": "아침 운동",
                "frequency": "DAILY",
                "by_day": None,
                "start_time": "07:00",
                "end_time": "07:30",
                "importance": 1,
                "date_range_id": None,
                "missing_slots": [],
                "clarifying_questions": [],
            }
        )

    _mock_llm(monkeypatch, [handler])

    response = client.post(
        "/events/parse", json={"user_id": user_id, "utterance": "매일 아침 7시부터 7시반까지 운동"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["draft"]["recurrence_rule"] == "FREQ=DAILY"
    assert body["draft"]["date_range_id"] is None
    assert body["draft"]["start_time"] == f"{date.today().isoformat()}T07:00:00"
    assert body["draft"]["end_time"] == f"{date.today().isoformat()}T07:30:00"


def test_parse_event_session_state_is_persisted_between_requests(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(
            {
                "title": "발표 준비",
                "missing_slots": ["frequency", "by_day", "start_time", "end_time", "importance", "date_range_id"],
                "clarifying_questions": [{"slot": "frequency", "question": "얼마나 자주 반복하나요?"}],
            }
        )

    _mock_llm(monkeypatch, [handler])

    response = client.post("/events/parse", json={"user_id": user_id, "utterance": "발표 준비"})
    session_id = response.json()["session_id"]

    stored = get_session(session_id)
    assert stored is not None
    assert stored.title == "발표 준비"
    assert stored.user_id == user_id


def test_parse_event_returns_422_when_llm_response_schema_is_invalid(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "이건 JSON이 아님"}}]},
        )

    _mock_llm(monkeypatch, [handler])

    response = client.post(
        "/events/parse", json={"user_id": user_id, "utterance": "아무 발화"}
    )

    assert response.status_code == 422
    assert "detail" in response.json()


def test_parse_event_returns_502_when_llm_http_call_fails(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream is down")

    _mock_llm(monkeypatch, [handler])

    response = client.post(
        "/events/parse", json={"user_id": user_id, "utterance": "아무 발화"}
    )

    assert response.status_code == 502
    assert "detail" in response.json()


def test_parse_event_returns_500_when_api_key_missing(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "llm_api_key", None)

    response = client.post(
        "/events/parse", json={"user_id": user_id, "utterance": "아무 발화"}
    )

    assert response.status_code == 500
    assert "detail" in response.json()


# --- 되묻는 질문 언어 (FR-2 + FR-11) ---


def _add_user(engine, language: str) -> int:
    with Session(engine) as session:
        user = User(name="Alex", preferred_language=language)
        session.add(user)
        session.commit()
        return user.id


def _question_turn(question: dict, captured: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return _chat_response(
            {
                "title": "Algorithms study",
                "frequency": None,
                "by_day": None,
                "start_time": None,
                "end_time": None,
                "importance": None,
                "date_range_id": None,
                "missing_slots": [question["slot"]],
                "clarifying_questions": [question],
            }
        )

    return handler


def test_english_user_gets_clarifying_question_in_english(
    client: TestClient, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    en_user_id = _add_user(engine, "en")
    captured: list[dict] = []
    english_question = {"slot": "frequency", "question": "How often do you want to repeat it?"}
    _mock_llm(monkeypatch, [_question_turn(english_question, captured)])

    response = client.post("/events/parse", json={"user_id": en_user_id, "utterance": "I need to study algorithms"})

    assert response.status_code == 200
    assert response.json()["next_question"] == english_question

    system_message = captured[0]["messages"][0]["content"]
    assert "[질문 언어]" in system_message
    assert "preferred_language: en" in system_message
    assert "반드시 영어(English)로만 작성하라" in system_message
    assert "Write every clarifying question in English only." in system_message
    assert "한국어" not in system_message  # 지시문 어디에도 언어가 하드코딩돼 있지 않다
    assert "[질문 언어]" not in captured[0]["messages"][1]["content"]


def test_korean_user_gets_korean_question_rule(client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict] = []
    _mock_llm(monkeypatch, [_question_turn({"slot": "frequency", "question": "얼마나 자주 반복하나요?"}, captured)])

    client.post("/events/parse", json={"user_id": user_id, "utterance": "알고리즘 스터디 해야 돼"})

    system_message = captured[0]["messages"][0]["content"]
    assert "preferred_language: ko" in system_message
    assert "되묻는 질문은 반드시 한국어로만 작성하세요." in system_message
    assert "Write every clarifying question in English only." not in system_message


def test_question_language_follows_latest_preference_between_turns(
    client: TestClient, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = _add_user(engine, "ko")
    captured: list[dict] = []
    _mock_llm(
        monkeypatch,
        [
            _question_turn({"slot": "frequency", "question": "얼마나 자주 반복하나요?"}, captured),
            _question_turn({"slot": "frequency", "question": "How often do you want to repeat it?"}, captured),
        ],
    )

    session_id = client.post("/events/parse", json={"user_id": user_id, "utterance": "알고리즘 스터디"}).json()[
        "session_id"
    ]
    with Session(engine) as session:
        session.get(User, user_id).preferred_language = "en"
        session.commit()
    client.post("/events/parse", json={"user_id": user_id, "utterance": "weekly", "session_id": session_id})

    assert "preferred_language: ko" in captured[0]["messages"][0]["content"]
    assert "preferred_language: en" in captured[1]["messages"][0]["content"]


def test_question_language_changes_cache_key_but_not_task_instructions() -> None:
    ko = llm_client_module._build_request_payload("헬스", [], date(2026, 9, 17), language="ko")
    en = llm_client_module._build_request_payload("gym", [], date(2026, 9, 17), language="en")

    assert ko["prompt_cache_key"] != en["prompt_cache_key"]
    ko_blocks = ko["messages"][0]["content"].split("\n\n")
    en_blocks = en["messages"][0]["content"].split("\n\n")
    assert ko_blocks[0] == en_blocks[0]  # 작업 지시문은 언어와 무관하게 동일
    assert len(en_blocks) == 4
    assert en_blocks[1].startswith("[질문 언어]\npreferred_language: en")
    assert en_blocks[2] == "등록된 기간(date_range) 후보: []"
    assert en_blocks[3] == "등록된 일정 제목: []"


def test_parse_event_with_unknown_user_is_404_without_calling_llm(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("없는 사용자에 대해 LLM을 호출하면 안 된다")

    _mock_llm(monkeypatch, [handler])

    response = client.post("/events/parse", json={"user_id": 999, "utterance": "스터디"})

    assert response.status_code == 404
    assert response.json()["detail"] == "user_id 999 does not exist"


# --- 단발 일정 / 오전·오후 (반복 언급이 없으면 단발, 애매한 시각은 한 번만 묻기) ---

MEETING = "UTKESA 썸포차 이벤트 미팅"


def _slots(**overrides) -> dict:
    """LLM 슬롯필링 응답. 기본값은 '단발 일정, 전부 확정'."""
    payload = {
        "title": MEETING,
        "date": "10-01",
        "frequency": None,
        "by_day": None,
        "start_time": "20:00",
        "end_time": "21:00",
        "importance": 2,
        "date_range_id": None,
        "missing_slots": [],
        "clarifying_questions": [],
    }
    payload.update(overrides)
    return payload


def _answer(payload: dict, sink: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if sink is not None:
            sink.append(json.loads(request.content))
        return _chat_response(payload)

    return handler


def _parse(client: TestClient, user_id: int, utterance: str, session_id: str | None = None) -> dict:
    body = {"user_id": user_id, "utterance": utterance}
    if session_id:
        body["session_id"] = session_id
    response = client.post("/events/parse", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@freeze_time("2026-09-26 09:00:00")
def test_one_off_without_recurrence_mention_becomes_one_off_draft_without_asking(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list = []
    _mock_llm(monkeypatch, [_answer(_slots(), sent)])

    body = _parse(client, user_id, f"10월 1일 저녁 8시에 {MEETING} 1시간동안 추가해줘")

    assert body["is_complete"] is True and body["next_question"] is None
    assert body["draft"] == {
        "user_id": user_id,
        "title": MEETING,
        "start_time": "2026-10-01T20:00:00",
        "end_time": "2026-10-01T21:00:00",
        "importance": 2,
        "is_recurring": False,
        "recurrence_rule": None,
        "date_range_id": None,
        "new_date_range": None,
    }
    assert body["command"]["affected"][0]["is_recurring"] is False
    instructions = sent[0]["messages"][0]["content"]
    assert "항상 반복" not in instructions and "단발 일정과 반복 일정을 모두 지원한다" in instructions


@freeze_time("2026-09-26 09:00:00")
def test_one_off_answer_to_recurrence_question_is_handled_as_one_off(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # LLM이 (예전처럼) 반복 주기를 되물은 상황. "반복 없이 한번만"은 LLM을 다시 부르지 않고 단발로 확정한다.
    asks_frequency = _slots(
        missing_slots=["frequency", "by_day", "date_range_id"],
        clarifying_questions=[{"slot": "frequency", "question": "얼마나 자주 반복하나요?"}],
    )
    _mock_llm(monkeypatch, [_answer(asks_frequency)])

    first = _parse(client, user_id, f"10월 1일 저녁 8시에 {MEETING} 1시간동안 추가해줘")
    assert first["next_question"]["slot"] == "frequency"

    second = _parse(client, user_id, "반복 없이 한번만", first["session_id"])

    assert second["is_complete"] is True
    draft = second["draft"]
    assert (draft["is_recurring"], draft["recurrence_rule"], draft["date_range_id"]) == (False, None, None)
    assert (draft["start_time"], draft["end_time"]) == ("2026-10-01T20:00:00", "2026-10-01T21:00:00")


@freeze_time("2026-09-26 09:00:00")
def test_one_off_stays_one_off_even_if_llm_asks_about_recurrence_again(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    asks_frequency = _slots(
        importance=None,
        missing_slots=["frequency", "importance"],
        clarifying_questions=[
            {"slot": "frequency", "question": "얼마나 자주 반복하나요?"},
            {"slot": "importance", "question": "얼마나 중요한가요?"},
        ],
    )
    # 중요도 답을 받은 LLM이 다시 반복을 물어도, 이미 단발이라고 답했으므로 묻지 않는다.
    asks_again = _slots(
        importance=3,
        missing_slots=["frequency"],
        clarifying_questions=[{"slot": "frequency", "question": "이 앱에서는 모든 일정이 반복 일정으로 등록돼요. 얼마나 자주?"}],
    )
    _mock_llm(monkeypatch, [_answer(asks_frequency), _answer(asks_again)])

    first = _parse(client, user_id, f"10월 1일 저녁 8시에 {MEETING} 1시간동안 추가해줘")
    second = _parse(client, user_id, "이번만", first["session_id"])
    assert second["next_question"]["slot"] == "importance"
    third = _parse(client, user_id, "학교 공식 행사야, 중요도 3", first["session_id"])

    assert third["is_complete"] is True
    assert (third["draft"]["is_recurring"], third["draft"]["importance"]) == (False, 3)


@freeze_time("2026-09-26 09:00:00")
def test_weekly_mention_still_goes_through_recurrence_flow(
    client: TestClient, engine, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Session(engine) as session:
        date_range = ImportantDateRange(
            user_id=user_id, name="2026 가을학기", start_date=date(2026, 9, 7), end_date=date(2026, 12, 18)
        )
        session.add(date_range)
        session.commit()
        date_range_id = date_range.id
    weekly = _slots(
        title="알고리즘 스터디", date=None, frequency="WEEKLY", by_day=["MO"], start_time="09:00", end_time="10:00",
        missing_slots=["date_range_id"],
        clarifying_questions=[{"slot": "date_range_id", "question": "언제까지 반복할까요?"}],
    )
    filled = {**weekly, "date_range_id": date_range_id, "missing_slots": [], "clarifying_questions": []}
    _mock_llm(monkeypatch, [_answer(weekly), _answer(filled)])

    first = _parse(client, user_id, "매주 월요일 오전 9시 알고리즘 스터디")
    # 반복 종료는 등록된 기간을 제안하면서 다른 날짜도 말할 수 있게 한 번에 묻는다.
    assert first["next_question"] == {
        "slot": "date_range_id",
        "question": "2026 가을학기(9/7~12/18)까지 반복할까요? 다른 날짜까지라면 말해주세요.",
    }
    assert "date" not in first["missing_slots"], "반복 일정은 날짜를 따로 묻지 않는다"

    second = _parse(client, user_id, "가을학기 동안", first["session_id"])

    draft = second["draft"]
    assert (draft["is_recurring"], draft["recurrence_rule"], draft["date_range_id"]) == (
        True,
        "FREQ=WEEKLY;BYDAY=MO",
        date_range_id,
    )
    assert draft["start_time"] == "2026-09-07T09:00:00"


@freeze_time("2026-09-26 09:00:00")
def test_ambiguous_hour_is_asked_once_and_pm_answer_shifts_both_times(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "8시"를 LLM은 지시대로 오전(08:00)으로 적는다. 오전/오후 답은 LLM 없이 처리한다(handler 하나뿐).
    _mock_llm(monkeypatch, [_answer(_slots(start_time="08:00", end_time="09:00"))])

    first = _parse(client, user_id, f"10월 1일 8시에 {MEETING} 1시간동안 추가해줘")

    assert first["is_complete"] is False
    assert first["next_question"] == {"slot": "start_time", "question": "오전 8시인가요, 오후 8시인가요?"}
    assert first["missing_slots"] == ["start_time"]

    second = _parse(client, user_id, "오후요", first["session_id"])

    assert second["is_complete"] is True
    assert (second["draft"]["start_time"], second["draft"]["end_time"]) == ("2026-10-01T20:00:00", "2026-10-01T21:00:00")


@freeze_time("2026-09-26 09:00:00")
def test_ambiguous_hour_is_not_asked_twice(client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch) -> None:
    ambiguous = _slots(start_time="08:00", end_time="09:00")
    _mock_llm(monkeypatch, [_answer(ambiguous), _answer(ambiguous)])

    first = _parse(client, user_id, f"10월 1일 8시에 {MEETING} 1시간동안 추가해줘")
    second = _parse(client, user_id, "음 8시 그대로", first["session_id"])

    assert second["is_complete"] is True, "한 번 물었으면 다시 묻지 않고 처음 해석(오전)대로 둔다"
    assert second["draft"]["start_time"] == "2026-10-01T08:00:00"


@pytest.mark.parametrize(
    "utterance", ["10월 1일 오후 8시에 미팅", "10월 1일 저녁 8시에 미팅", "10월 1일 20시에 미팅", "10월 1일 12시에 점심 미팅"]
)
@freeze_time("2026-09-26 09:00:00")
def test_clear_hour_is_not_asked(client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch, utterance: str) -> None:
    _mock_llm(monkeypatch, [_answer(_slots())])

    assert _parse(client, user_id, utterance)["is_complete"] is True


@freeze_time("2026-09-26 09:00:00")
def test_date_without_year_is_the_nearest_upcoming_date(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_llm(monkeypatch, [_answer(_slots(date="09-01")), _answer(_slots(date="10-01"))])

    past_month = _parse(client, user_id, "9월 1일 저녁 8시 미팅")
    upcoming = _parse(client, user_id, "10월 1일 저녁 8시 미팅")

    assert past_month["draft"]["start_time"] == "2027-09-01T20:00:00", "올해 9월 1일은 지났으므로 내년"
    assert upcoming["draft"]["start_time"] == "2026-10-01T20:00:00"


@freeze_time("2026-09-26 09:00:00")
def test_one_off_without_date_asks_for_the_date(client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_llm(monkeypatch, [_answer(_slots(date=None)), _answer(_slots(date="10-02"))])

    first = _parse(client, user_id, "저녁 8시에 미팅 1시간")
    assert first["next_question"] == {"slot": "date", "question": "몇 월 며칠 일정인가요?"}

    second = _parse(client, user_id, "10월 2일", first["session_id"])
    assert second["draft"]["start_time"] == "2026-10-02T20:00:00"


@freeze_time("2026-09-26 09:00:00")
def test_one_off_draft_creates_one_instance_with_notification_jobs(
    client: TestClient, engine, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_llm(monkeypatch, [_answer(_slots())])
    draft = _parse(client, user_id, f"10월 1일 저녁 8시에 {MEETING} 1시간동안 추가해줘")["draft"]

    start_scheduler()
    scheduler.pause()  # 등록만 확인하고 실행은 하지 않는다
    try:
        response = client.post("/events", json=draft)
        assert response.status_code == 201
        with Session(engine) as session:
            event = session.get(Event, response.json()["id"])
            [instance] = session.execute(select(EventInstance).where(EventInstance.event_id == event.id)).scalars().all()
            assert (event.is_recurring, instance.date) == (False, date(2026, 10, 1))
        assert {job.id for job in scheduler.get_jobs()} == {
            f"event_instance_{instance.id}_start",
            f"event_instance_{instance.id}_end",
        }
    finally:
        scheduler.remove_all_jobs()
        shutdown_scheduler()
