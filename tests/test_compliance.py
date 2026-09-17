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
from app.models import Base, Event, EventInstance, EventInstanceStatus, User
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
def event_instance_id(engine) -> int:
    with Session(engine) as session:
        user = User(name="June", preferred_language="ko")
        session.add(user)
        session.flush()

        event = Event(
            user_id=user.id,
            title="아침 운동",
            start_time=datetime(2026, 9, 17, 7, 0),
            end_time=datetime(2026, 9, 17, 7, 30),
        )
        session.add(event)
        session.flush()

        instance = EventInstance(
            event_id=event.id, date=date(2026, 9, 17), status=EventInstanceStatus.MISSED
        )
        session.add(instance)
        session.commit()
        return instance.id


@pytest.fixture
def llm_call_tracker(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """LLM(chat completions) 호출 여부/횟수를 추적하는 mock. 실제 네트워크를 타지 않는다."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url)})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "괜찮아요, 다음엔 더 잘할 수 있을 거예요!"}}
                ]
            },
        )

    monkeypatch.setattr(
        llm_client_module.httpx,
        "Client",
        lambda *a, **kw: _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler)),
    )
    return calls


def test_category_only_request_does_not_call_llm(
    client: TestClient, event_instance_id: int, llm_call_tracker: list[dict]
) -> None:
    response = client.post(
        "/compliance-reports",
        json={"event_instance_id": event_instance_id, "reason_category": "overslept"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["llm_triggered"] is False
    assert body["llm_feedback"] is None
    assert llm_call_tracker == []  # LLM이 아예 호출 안 됐어야 함


def test_category_with_reason_text_calls_llm_even_when_not_other(
    client: TestClient, event_instance_id: int, llm_call_tracker: list[dict]
) -> None:
    response = client.post(
        "/compliance-reports",
        json={
            "event_instance_id": event_instance_id,
            "reason_category": "fatigue",
            "reason_text": "며칠째 잠을 못 잤어요",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["llm_triggered"] is True
    assert body["llm_feedback"] == "괜찮아요, 다음엔 더 잘할 수 있을 거예요!"
    assert len(llm_call_tracker) == 1  # LLM이 정확히 한 번 호출됐어야 함


def test_category_other_calls_llm_even_without_reason_text(
    client: TestClient, event_instance_id: int, llm_call_tracker: list[dict]
) -> None:
    response = client.post(
        "/compliance-reports",
        json={"event_instance_id": event_instance_id, "reason_category": "other"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["llm_triggered"] is True
    assert len(llm_call_tracker) == 1


def test_category_other_with_reason_text_calls_llm_once(
    client: TestClient, event_instance_id: int, llm_call_tracker: list[dict]
) -> None:
    response = client.post(
        "/compliance-reports",
        json={
            "event_instance_id": event_instance_id,
            "reason_category": "other",
            "reason_text": "설명하기 애매한 사정이 있었어요",
        },
    )

    assert response.status_code == 201
    assert response.json()["llm_triggered"] is True
    assert len(llm_call_tracker) == 1


def test_category_with_blank_reason_text_does_not_call_llm(
    client: TestClient, event_instance_id: int, llm_call_tracker: list[dict]
) -> None:
    """공백만 있는 reason_text는 "채워졌다"로 보지 않는다."""
    response = client.post(
        "/compliance-reports",
        json={
            "event_instance_id": event_instance_id,
            "reason_category": "forgot",
            "reason_text": "   ",
        },
    )

    assert response.status_code == 201
    assert response.json()["llm_triggered"] is False
    assert llm_call_tracker == []
