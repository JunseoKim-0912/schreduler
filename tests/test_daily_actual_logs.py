import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.models import Base, User


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


def _payload(user_id: int, **overrides: object) -> dict[str, object]:
    payload = {
        "user_id": user_id,
        "date": "2026-09-17",
        "summary_text": "오늘 계획한 6개 중 5개 완료",
        "actual_events": [{"title": "아침 운동", "status": "done"}],
    }
    payload.update(overrides)
    return payload


def test_create_daily_actual_log_returns_201(client: TestClient, user_id: int) -> None:
    response = client.post("/daily-actual-logs", json=_payload(user_id))

    assert response.status_code == 201
    body = response.json()
    assert body["user_id"] == user_id
    assert body["date"] == "2026-09-17"
    assert body["summary_text"] == "오늘 계획한 6개 중 5개 완료"
    assert body["actual_events"] == [{"title": "아침 운동", "status": "done"}]
    assert "id" in body


def test_create_daily_actual_log_defaults_actual_events_to_empty_list(
    client: TestClient, user_id: int
) -> None:
    payload = _payload(user_id)
    del payload["actual_events"]

    response = client.post("/daily-actual-logs", json=payload)

    assert response.status_code == 201
    assert response.json()["actual_events"] == []


def test_create_daily_actual_log_with_unknown_user_returns_404(client: TestClient) -> None:
    response = client.post("/daily-actual-logs", json=_payload(user_id=999))

    assert response.status_code == 404


def test_get_daily_actual_log_returns_created_one(client: TestClient, user_id: int) -> None:
    created = client.post("/daily-actual-logs", json=_payload(user_id)).json()

    response = client.get(f"/daily-actual-logs/{created['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_get_daily_actual_log_not_found_returns_404(client: TestClient) -> None:
    response = client.get("/daily-actual-logs/999")

    assert response.status_code == 404


def test_list_daily_actual_logs_filters_by_user(client: TestClient, engine) -> None:
    with Session(engine) as session:
        user1 = User(name="June", preferred_language="ko")
        user2 = User(name="Other", preferred_language="en")
        session.add_all([user1, user2])
        session.commit()
        user1_id, user2_id = user1.id, user2.id

    client.post("/daily-actual-logs", json=_payload(user1_id))
    client.post("/daily-actual-logs", json=_payload(user2_id, date="2026-09-18"))

    response = client.get("/daily-actual-logs", params={"user_id": user1_id})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["user_id"] == user1_id


def test_update_daily_actual_log_applies_partial_changes(client: TestClient, user_id: int) -> None:
    created = client.post("/daily-actual-logs", json=_payload(user_id)).json()

    response = client.put(
        f"/daily-actual-logs/{created['id']}", json={"summary_text": "수정된 요약"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["summary_text"] == "수정된 요약"
    assert body["actual_events"] == created["actual_events"]  # 건드리지 않은 필드는 그대로


def test_update_daily_actual_log_not_found_returns_404(client: TestClient) -> None:
    response = client.put("/daily-actual-logs/999", json={"summary_text": "x"})

    assert response.status_code == 404


def test_delete_daily_actual_log_removes_it(client: TestClient, user_id: int) -> None:
    created = client.post("/daily-actual-logs", json=_payload(user_id)).json()

    delete_response = client.delete(f"/daily-actual-logs/{created['id']}")
    get_response = client.get(f"/daily-actual-logs/{created['id']}")

    assert delete_response.status_code == 204
    assert get_response.status_code == 404


def test_delete_daily_actual_log_not_found_returns_404(client: TestClient) -> None:
    response = client.delete("/daily-actual-logs/999")

    assert response.status_code == 404
