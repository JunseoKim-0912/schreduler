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
    payload = {"user_id": user_id, "name": "학교", "default_travel_minutes": 25}
    payload.update(overrides)
    return payload


def test_create_location_returns_201(client: TestClient, user_id: int) -> None:
    response = client.post("/locations", json=_payload(user_id))

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "학교"
    assert body["default_travel_minutes"] == 25
    assert body["user_id"] == user_id


def test_create_location_with_unknown_user_returns_404(client: TestClient) -> None:
    response = client.post("/locations", json=_payload(user_id=999))

    assert response.status_code == 404


def test_create_location_with_non_positive_travel_minutes_returns_422(
    client: TestClient, user_id: int
) -> None:
    response = client.post("/locations", json=_payload(user_id, default_travel_minutes=0))

    assert response.status_code == 422


def test_get_location_returns_created_one(client: TestClient, user_id: int) -> None:
    created = client.post("/locations", json=_payload(user_id)).json()

    response = client.get(f"/locations/{created['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_get_location_not_found_returns_404(client: TestClient) -> None:
    response = client.get("/locations/999")

    assert response.status_code == 404


def test_list_locations_filters_by_user(client: TestClient, engine) -> None:
    with Session(engine) as session:
        user1 = User(name="June", preferred_language="ko")
        user2 = User(name="Other", preferred_language="en")
        session.add_all([user1, user2])
        session.commit()
        user1_id, user2_id = user1.id, user2.id

    client.post("/locations", json=_payload(user1_id, name="장소1"))
    client.post("/locations", json=_payload(user2_id, name="장소2"))

    response = client.get("/locations", params={"user_id": user1_id})

    assert response.status_code == 200
    names = [item["name"] for item in response.json()]
    assert names == ["장소1"]


def test_update_location_applies_partial_changes(client: TestClient, user_id: int) -> None:
    created = client.post("/locations", json=_payload(user_id)).json()

    response = client.put(f"/locations/{created['id']}", json={"default_travel_minutes": 40})

    assert response.status_code == 200
    body = response.json()
    assert body["default_travel_minutes"] == 40
    assert body["name"] == created["name"]


def test_update_location_not_found_returns_404(client: TestClient) -> None:
    response = client.put("/locations/999", json={"name": "x"})

    assert response.status_code == 404


def test_delete_location_removes_it(client: TestClient, user_id: int) -> None:
    created = client.post("/locations", json=_payload(user_id)).json()

    delete_response = client.delete(f"/locations/{created['id']}")
    get_response = client.get(f"/locations/{created['id']}")

    assert delete_response.status_code == 204
    assert get_response.status_code == 404


def test_delete_location_not_found_returns_404(client: TestClient) -> None:
    response = client.delete("/locations/999")

    assert response.status_code == 404
