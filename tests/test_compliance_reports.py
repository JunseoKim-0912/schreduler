import json
from datetime import date, datetime, timedelta

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


_REAL_HTTPX_CLIENT = httpx.Client  # 몽키패치 전에 원본을 캡처 (안 하면 자기 자신을 재귀 호출함)


def _patch_llm_http(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    monkeypatch.setattr(
        llm_client_module.httpx,
        "Client",
        lambda *a, **kw: _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler)),
    )


def _mock_llm_text_response(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": text}}]}
        )

    _patch_llm_http(monkeypatch, handler)


def _mock_llm_error_response(monkeypatch: pytest.MonkeyPatch, status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="upstream error")

    _patch_llm_http(monkeypatch, handler)


def _fail_if_llm_called(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("LLM이 호출되면 안 되는 상황에서 호출됨")

    _patch_llm_http(monkeypatch, handler)


def test_category_only_skips_llm_and_saves_directly(
    client: TestClient, event_instance_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_if_llm_called(monkeypatch)

    response = client.post(
        "/compliance-reports",
        json={"event_instance_id": event_instance_id, "reason_category": "overslept"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["llm_triggered"] is False
    assert body["llm_feedback"] is None
    assert body["reason_category"] == "overslept"


def test_category_other_triggers_llm_and_returns_feedback(
    client: TestClient, event_instance_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_llm_text_response(monkeypatch, "괜찮아요, 다음엔 더 잘할 수 있을 거예요!")

    response = client.post(
        "/compliance-reports",
        json={
            "event_instance_id": event_instance_id,
            "reason_category": "other",
            "reason_text": "갑자기 급한 일이 생겼어요",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["llm_triggered"] is True
    assert body["llm_feedback"] == "괜찮아요, 다음엔 더 잘할 수 있을 거예요!"


def test_category_with_free_text_triggers_llm_even_when_not_other(
    client: TestClient, event_instance_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_llm_text_response(monkeypatch, "피곤하셨군요, 푹 쉬세요.")

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
    assert body["llm_feedback"] == "피곤하셨군요, 푹 쉬세요."


def test_category_with_blank_reason_text_does_not_trigger_llm(
    client: TestClient, event_instance_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_if_llm_called(monkeypatch)

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


def test_unknown_event_instance_returns_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_if_llm_called(monkeypatch)

    response = client.post(
        "/compliance-reports",
        json={"event_instance_id": 999, "reason_category": "overslept"},
    )

    assert response.status_code == 404


def test_reason_text_over_150_chars_returns_422(
    client: TestClient, event_instance_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_if_llm_called(monkeypatch)

    response = client.post(
        "/compliance-reports",
        json={
            "event_instance_id": event_instance_id,
            "reason_category": "other",
            "reason_text": "가" * 151,
        },
    )

    assert response.status_code == 422


def test_invalid_category_returns_422(
    client: TestClient, event_instance_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_if_llm_called(monkeypatch)

    response = client.post(
        "/compliance-reports",
        json={"event_instance_id": event_instance_id, "reason_category": "not_a_real_category"},
    )

    assert response.status_code == 422


def test_llm_failure_returns_502(
    client: TestClient, event_instance_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_llm_error_response(monkeypatch, 500)

    response = client.post(
        "/compliance-reports",
        json={"event_instance_id": event_instance_id, "reason_category": "other"},
    )

    assert response.status_code == 502
    assert "detail" in response.json()


def _make_event_instance_for_user(session: Session, user: User) -> EventInstance:
    event = Event(
        user_id=user.id,
        title="이벤트",
        start_time=datetime(2026, 9, 17, 9, 0),
        end_time=datetime(2026, 9, 17, 10, 0),
    )
    session.add(event)
    session.flush()

    instance = EventInstance(
        event_id=event.id, date=date(2026, 9, 17), status=EventInstanceStatus.MISSED
    )
    session.add(instance)
    session.flush()
    return instance


def _make_report(
    session: Session, event_instance_id: int, category: str, created_at: datetime
) -> None:
    from app.models.compliance_report import ComplianceReport
    from app.models.enums import NonComplianceCategory

    report = ComplianceReport(
        event_instance_id=event_instance_id,
        reason_category=NonComplianceCategory(category),
        llm_triggered=False,
        created_at=created_at,
    )
    session.add(report)
    session.commit()


def test_stats_counts_recent_categories_and_zero_fills_the_rest(
    client: TestClient, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_if_llm_called(monkeypatch)
    now = datetime.utcnow()

    with Session(engine) as session:
        user = User(name="June", preferred_language="ko")
        session.add(user)
        session.flush()
        instance = _make_event_instance_for_user(session, user)

        _make_report(session, instance.id, "overslept", now - timedelta(days=5))
        _make_report(session, instance.id, "overslept", now - timedelta(days=10))
        _make_report(session, instance.id, "fatigue", now - timedelta(days=1))
        _make_report(session, instance.id, "overslept", now - timedelta(days=40))  # 범위 밖

    response = client.get("/compliance-reports/stats")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    counts = {item["reason_category"]: item["count"] for item in body["by_category"]}
    assert counts["overslept"] == 2
    assert counts["fatigue"] == 1
    assert counts["forgot"] == 0  # 없는 카테고리도 0으로 항상 포함
    assert set(counts.keys()) == {
        "overslept",
        "fatigue",
        "priority_shift",
        "schedule_conflict",
        "forgot",
        "transit_issue",
        "other",
    }


def test_stats_respects_custom_days_window(
    client: TestClient, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_if_llm_called(monkeypatch)
    now = datetime.utcnow()

    with Session(engine) as session:
        user = User(name="June", preferred_language="ko")
        session.add(user)
        session.flush()
        instance = _make_event_instance_for_user(session, user)
        _make_report(session, instance.id, "forgot", now - timedelta(days=10))

    response = client.get("/compliance-reports/stats", params={"days": 1})

    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_stats_filters_by_user_id(
    client: TestClient, engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_if_llm_called(monkeypatch)
    now = datetime.utcnow()

    with Session(engine) as session:
        user1 = User(name="June", preferred_language="ko")
        user2 = User(name="Other", preferred_language="en")
        session.add_all([user1, user2])
        session.flush()

        instance1 = _make_event_instance_for_user(session, user1)
        instance2 = _make_event_instance_for_user(session, user2)
        _make_report(session, instance1.id, "overslept", now)
        _make_report(session, instance2.id, "fatigue", now)
        user1_id = user1.id

    response = client.get("/compliance-reports/stats", params={"user_id": user1_id})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    counts = {item["reason_category"]: item["count"] for item in body["by_category"]}
    assert counts["overslept"] == 1
    assert counts["fatigue"] == 0
