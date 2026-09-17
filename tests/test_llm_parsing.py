import json
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.db import get_db
from app.main import app
from app.models import Base, ImportantDateRange, User
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
                "day_of_week": None,
                "start_time": None,
                "end_time": None,
                "importance": None,
                "date_range_id": None,
                "missing_slots": ["day_of_week", "start_time", "end_time"],
                "clarifying_questions": [
                    {"slot": "day_of_week", "question": "무슨 요일에 하나요?"},
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
    assert body["next_question"] == {"slot": "day_of_week", "question": "무슨 요일에 하나요?"}
    assert set(body["missing_slots"]) == {"day_of_week", "start_time", "end_time"}
    assert body["draft"] is None
    assert "session_id" in body


def test_parse_event_multiturn_completes_with_draft(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_bodies: list[dict] = []

    def turn1(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content))
        return _chat_response(
            {
                "title": "알고리즘 스터디",
                "day_of_week": None,
                "start_time": None,
                "end_time": None,
                "importance": None,
                "date_range_id": None,
                "missing_slots": ["day_of_week", "start_time", "end_time"],
                "clarifying_questions": [
                    {"slot": "day_of_week", "question": "무슨 요일에 하나요?"}
                ],
            }
        )

    def turn2(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content))
        return _chat_response(
            {
                "title": "알고리즘 스터디",
                "day_of_week": "MO",
                "start_time": "09:00",
                "end_time": "10:00",
                "importance": None,
                "date_range_id": None,
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
            "utterance": "월요일 9시부터 10시",
        },
    )
    assert second.status_code == 200
    second_body = second.json()

    assert second_body["session_id"] == session_id
    assert second_body["is_complete"] is True
    assert second_body["draft"] == {
        "title": "알고리즘 스터디",
        "day_of_week": "MO",
        "start_time": "09:00",
        "end_time": "10:00",
        "importance": None,
        "date_range_id": None,
    }
    assert second_body["next_question"] is None

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
                "missing_slots": ["day_of_week", "start_time", "end_time", "importance", "date_range_id"],
                "clarifying_questions": [{"slot": "day_of_week", "question": "요일은요?"}],
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
                "day_of_week": "TU",
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

    user_message = captured["messages"][1]["content"]
    assert "2026 가을학기" in user_message


def test_parse_event_session_state_is_persisted_between_requests(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(
            {
                "title": "발표 준비",
                "missing_slots": ["day_of_week", "start_time", "end_time", "importance", "date_range_id"],
                "clarifying_questions": [{"slot": "day_of_week", "question": "요일은요?"}],
            }
        )

    _mock_llm(monkeypatch, [handler])

    response = client.post("/events/parse", json={"user_id": user_id, "utterance": "발표 준비"})
    session_id = response.json()["session_id"]

    stored = get_session(session_id)
    assert stored is not None
    assert stored.title == "발표 준비"
    assert stored.user_id == user_id
