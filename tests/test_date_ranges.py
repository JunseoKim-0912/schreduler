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
        "name": "2026 가을학기",
        "start_date": "2026-09-01",
        "end_date": "2026-12-20",
    }
    payload.update(overrides)
    return payload


def test_create_date_range_returns_201(client: TestClient, user_id: int) -> None:
    response = client.post("/date-ranges", json=_payload(user_id))

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "2026 가을학기"
    assert body["user_id"] == user_id
    assert "id" in body


def test_create_date_range_with_unknown_user_returns_404(client: TestClient) -> None:
    response = client.post("/date-ranges", json=_payload(user_id=999))

    assert response.status_code == 404


def test_create_date_range_with_end_before_start_returns_422(
    client: TestClient, user_id: int
) -> None:
    response = client.post(
        "/date-ranges",
        json=_payload(user_id, start_date="2026-09-30", end_date="2026-09-01"),
    )

    assert response.status_code == 422


def test_get_date_range_returns_created_one(client: TestClient, user_id: int) -> None:
    created = client.post("/date-ranges", json=_payload(user_id)).json()

    response = client.get(f"/date-ranges/{created['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_get_date_range_not_found_returns_404(client: TestClient) -> None:
    response = client.get("/date-ranges/999")

    assert response.status_code == 404


def test_list_date_ranges_filters_by_user(client: TestClient, engine) -> None:
    with Session(engine) as session:
        user1 = User(name="June", preferred_language="ko")
        user2 = User(name="Other", preferred_language="en")
        session.add_all([user1, user2])
        session.commit()
        user1_id, user2_id = user1.id, user2.id

    client.post("/date-ranges", json=_payload(user1_id, name="학기1"))
    client.post("/date-ranges", json=_payload(user2_id, name="학기2"))

    response = client.get("/date-ranges", params={"user_id": user1_id})

    assert response.status_code == 200
    names = [item["name"] for item in response.json()]
    assert names == ["학기1"]


def test_update_date_range_applies_partial_changes(client: TestClient, user_id: int) -> None:
    created = client.post("/date-ranges", json=_payload(user_id)).json()

    response = client.put(f"/date-ranges/{created['id']}", json={"name": "변경된 이름"})

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "변경된 이름"
    assert body["start_date"] == created["start_date"]  # 건드리지 않은 필드는 그대로


def test_update_date_range_not_found_returns_404(client: TestClient) -> None:
    response = client.put("/date-ranges/999", json={"name": "x"})

    assert response.status_code == 404


def test_delete_date_range_removes_it(client: TestClient, user_id: int) -> None:
    created = client.post("/date-ranges", json=_payload(user_id)).json()

    delete_response = client.delete(f"/date-ranges/{created['id']}")
    get_response = client.get(f"/date-ranges/{created['id']}")

    assert delete_response.status_code == 204
    assert get_response.status_code == 404


def test_delete_date_range_not_found_returns_404(client: TestClient) -> None:
    response = client.delete("/date-ranges/999")

    assert response.status_code == 404
