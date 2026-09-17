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
        "title": "수업",
        "start_time": "2026-09-17T09:00:00",
        "end_time": "2026-09-17T10:00:00",
    }
    payload.update(overrides)
    return payload


def test_create_event_returns_201(client: TestClient, user_id: int) -> None:
    response = client.post("/events", json=_payload(user_id))

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "수업"
    assert body["user_id"] == user_id
    assert body["importance"] is None
    assert "id" in body


def test_create_event_with_unknown_user_returns_404(client: TestClient) -> None:
    response = client.post("/events", json=_payload(user_id=999))

    assert response.status_code == 404


def test_create_event_with_invalid_time_range_returns_422(client: TestClient, user_id: int) -> None:
    response = client.post(
        "/events",
        json=_payload(user_id, start_time="2026-09-17T10:00:00", end_time="2026-09-17T09:00:00"),
    )

    assert response.status_code == 422


def test_get_event_returns_created_event(client: TestClient, user_id: int) -> None:
    created = client.post("/events", json=_payload(user_id)).json()

    response = client.get(f"/events/{created['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_get_event_not_found_returns_404(client: TestClient) -> None:
    response = client.get("/events/999")

    assert response.status_code == 404


def test_list_events_filters_by_user(client: TestClient, engine) -> None:
    with Session(engine) as session:
        user1 = User(name="June", preferred_language="ko")
        user2 = User(name="Other", preferred_language="en")
        session.add_all([user1, user2])
        session.commit()
        user1_id, user2_id = user1.id, user2.id

    client.post("/events", json=_payload(user1_id, title="이벤트1"))
    client.post("/events", json=_payload(user2_id, title="이벤트2"))

    response = client.get("/events", params={"user_id": user1_id})

    assert response.status_code == 200
    titles = [event["title"] for event in response.json()]
    assert titles == ["이벤트1"]


def test_update_event_applies_partial_changes(client: TestClient, user_id: int) -> None:
    created = client.post("/events", json=_payload(user_id)).json()

    response = client.put(f"/events/{created['id']}", json={"title": "변경된 제목"})

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "변경된 제목"
    assert body["start_time"] == created["start_time"]  # 건드리지 않은 필드는 그대로


def test_update_event_not_found_returns_404(client: TestClient) -> None:
    response = client.put("/events/999", json={"title": "x"})

    assert response.status_code == 404


def test_delete_event_removes_it(client: TestClient, user_id: int) -> None:
    created = client.post("/events", json=_payload(user_id)).json()

    delete_response = client.delete(f"/events/{created['id']}")
    get_response = client.get(f"/events/{created['id']}")

    assert delete_response.status_code == 204
    assert get_response.status_code == 404


def test_delete_event_not_found_returns_404(client: TestClient) -> None:
    response = client.delete("/events/999")

    assert response.status_code == 404
