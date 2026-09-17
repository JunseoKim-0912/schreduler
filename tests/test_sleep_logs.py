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
        "actual_bedtime": "2026-09-16T23:30:00",
        "actual_wake_time": "2026-09-17T07:00:00",
    }
    payload.update(overrides)
    return payload


def test_create_sleep_log_returns_201(client: TestClient, user_id: int) -> None:
    response = client.post("/sleep-logs", json=_payload(user_id))

    assert response.status_code == 201
    body = response.json()
    assert body["user_id"] == user_id
    assert body["date"] == "2026-09-17"
    assert body["actual_bedtime"] == "2026-09-16T23:30:00"
    assert body["actual_wake_time"] == "2026-09-17T07:00:00"
    assert "id" in body


def test_create_sleep_log_with_unknown_user_returns_404(client: TestClient) -> None:
    response = client.post("/sleep-logs", json=_payload(user_id=999))

    assert response.status_code == 404


def test_create_sleep_log_with_wake_before_bedtime_returns_422(
    client: TestClient, user_id: int
) -> None:
    response = client.post(
        "/sleep-logs",
        json=_payload(
            user_id,
            actual_bedtime="2026-09-17T07:00:00",
            actual_wake_time="2026-09-16T23:30:00",
        ),
    )

    assert response.status_code == 422


def test_get_sleep_log_returns_created_one(client: TestClient, user_id: int) -> None:
    created = client.post("/sleep-logs", json=_payload(user_id)).json()

    response = client.get(f"/sleep-logs/{created['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_get_sleep_log_not_found_returns_404(client: TestClient) -> None:
    response = client.get("/sleep-logs/999")

    assert response.status_code == 404


def test_list_sleep_logs_filters_by_user(client: TestClient, engine) -> None:
    with Session(engine) as session:
        user1 = User(name="June", preferred_language="ko")
        user2 = User(name="Other", preferred_language="en")
        session.add_all([user1, user2])
        session.commit()
        user1_id, user2_id = user1.id, user2.id

    client.post("/sleep-logs", json=_payload(user1_id))
    client.post("/sleep-logs", json=_payload(user2_id, date="2026-09-18"))

    response = client.get("/sleep-logs", params={"user_id": user1_id})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["user_id"] == user1_id


def test_update_sleep_log_applies_partial_changes(client: TestClient, user_id: int) -> None:
    created = client.post("/sleep-logs", json=_payload(user_id)).json()

    response = client.put(
        f"/sleep-logs/{created['id']}", json={"actual_wake_time": "2026-09-17T08:00:00"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["actual_wake_time"] == "2026-09-17T08:00:00"
    assert body["actual_bedtime"] == created["actual_bedtime"]  # 건드리지 않은 필드는 그대로


def test_update_sleep_log_not_found_returns_404(client: TestClient) -> None:
    response = client.put("/sleep-logs/999", json={"date": "2026-09-18"})

    assert response.status_code == 404


def test_delete_sleep_log_removes_it(client: TestClient, user_id: int) -> None:
    created = client.post("/sleep-logs", json=_payload(user_id)).json()

    delete_response = client.delete(f"/sleep-logs/{created['id']}")
    get_response = client.get(f"/sleep-logs/{created['id']}")

    assert delete_response.status_code == 204
    assert get_response.status_code == 404


def test_delete_sleep_log_not_found_returns_404(client: TestClient) -> None:
    response = client.delete("/sleep-logs/999")

    assert response.status_code == 404
