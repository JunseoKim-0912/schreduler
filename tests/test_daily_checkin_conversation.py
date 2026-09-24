import json
from datetime import date, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.db import get_db
from app.main import app
from app.models import Base, ComplianceReport, Event, EventInstance, EventInstanceStatus, NonComplianceCategory, User
from app.services import llm_client as llm_client_module

_REAL_HTTPX_CLIENT = httpx.Client  # 몽키패치 전에 원본을 캡처 (안 하면 자기 자신을 재귀 호출함)


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "llm_api_key", "test-key")


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


def _mock_llm(monkeypatch: pytest.MonkeyPatch, reply_text: str) -> dict:
    """LLM 호출을 가로채서, 실제로 어떤 system 프롬프트가 갔는지 캡처한다."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": reply_text}}]}
        )

    monkeypatch.setattr(
        llm_client_module.httpx,
        "Client",
        lambda *a, **kw: _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler)),
    )
    return captured


def test_checkin_message_includes_daily_summary_in_user_message(
    client: TestClient, engine, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Session(engine) as session:
        done_event = Event(
            user_id=user_id,
            title="아침 운동",
            start_time=datetime(2026, 9, 17, 7, 0),
            end_time=datetime(2026, 9, 17, 7, 30),
        )
        missed_event = Event(
            user_id=user_id,
            title="알고리즘 스터디",
            start_time=datetime(2026, 9, 17, 21, 0),
            end_time=datetime(2026, 9, 17, 22, 0),
        )
        session.add_all([done_event, missed_event])
        session.flush()

        done_instance = EventInstance(
            event_id=done_event.id, date=date(2026, 9, 17), status=EventInstanceStatus.DONE
        )
        missed_instance = EventInstance(
            event_id=missed_event.id, date=date(2026, 9, 17), status=EventInstanceStatus.MISSED
        )
        session.add_all([done_instance, missed_instance])
        session.flush()

        session.add(
            ComplianceReport(
                event_instance_id=missed_instance.id,
                reason_category=NonComplianceCategory.FATIGUE,
                reason_text="너무 피곤했어요",
                llm_triggered=True,
            )
        )
        session.commit()

    captured = _mock_llm(monkeypatch, "오늘 스터디를 못 하셨네요. 많이 피곤하셨나 봐요, 내일은 좀 쉬어가면서 해봐요!")

    response = client.post(
        "/daily-actual-logs/checkin",
        json={"user_id": user_id, "utterance": "오늘 하루 어땠는지 알려줘", "date": "2026-09-17"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "오늘 스터디를 못 하셨네요. 많이 피곤하셨나 봐요, 내일은 좀 쉬어가면서 해봐요!"
    assert "오늘 계획한 2개 중 1개 완료." in body["summary"]
    assert "알고리즘 스터디" in body["summary"]
    assert "아침 운동" not in body["summary"]  # done은 상세 내용 없음

    # 요약은 매일 바뀌므로 캐시 프리픽스(system)가 아니라 user 메시지에 발화와 함께 들어간다
    system_message = captured["messages"][0]["content"]
    user_message = captured["messages"][1]["content"]
    assert body["summary"] not in system_message
    assert body["summary"] in user_message
    assert user_message.endswith("사용자 발화: 오늘 하루 어땠는지 알려줘")


def test_checkin_message_defaults_to_today_when_date_omitted(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_llm(monkeypatch, "오늘 하루도 고생 많으셨어요!")

    response = client.post(
        "/daily-actual-logs/checkin",
        json={"user_id": user_id, "utterance": "오늘 어땠어?"},
    )

    assert response.status_code == 200
    assert response.json()["summary"] == "오늘 계획한 0개 중 0개 완료."


def test_checkin_message_with_unknown_user_returns_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("유저가 없으면 LLM을 호출하면 안 됨")

    monkeypatch.setattr(
        llm_client_module.httpx,
        "Client",
        lambda *a, **kw: _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler)),
    )

    response = client.post(
        "/daily-actual-logs/checkin",
        json={"user_id": 999, "utterance": "오늘 어땠어?"},
    )

    assert response.status_code == 404


def test_checkin_message_llm_failure_returns_502(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream error")

    monkeypatch.setattr(
        llm_client_module.httpx,
        "Client",
        lambda *a, **kw: _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler)),
    )

    response = client.post(
        "/daily-actual-logs/checkin",
        json={"user_id": user_id, "utterance": "오늘 어땠어?"},
    )

    assert response.status_code == 502
