import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.db import get_db
from app.main import app
from app.models import Base, PersonaConversation, User
from app.services import llm_client as llm_client_module

_REAL_HTTPX_CLIENT = httpx.Client  # 몽키패치 전에 원본을 캡처 (안 하면 자기 자신을 재귀 호출함)


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


def _persona_payload(name: str = "Hana", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": name,
        "display_name": {"ko": "하나", "en": "Hana"},
        "description": {"ko": "상냥한 대학생", "en": "Kind college student"},
    }
    payload.update(overrides)
    return payload


def _mock_llm(monkeypatch: pytest.MonkeyPatch, reply_text: str) -> list[httpx.Request]:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": reply_text}}]})

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(
        llm_client_module.httpx,
        "Client",
        lambda *a, **kw: _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler)),
    )
    return calls


def test_persona_crud(client: TestClient) -> None:
    response = client.post("/personas", json=_persona_payload())
    assert response.status_code == 201
    assert response.json()["example_lines"] is None

    assert client.post("/personas", json=_persona_payload()).status_code == 409
    assert [p["name"] for p in client.get("/personas").json()] == ["Hana"]
    assert client.get("/personas/Hana").json()["display_name"]["ko"] == "하나"
    assert client.get("/personas/Nobody").status_code == 404

    backstory = {"ko": "과거", "en": "past"}
    response = client.put("/personas/Hana", json={"backstory": backstory})
    assert response.status_code == 200
    assert response.json()["backstory"] == backstory
    assert response.json()["description"]["ko"] == "상냥한 대학생"

    assert client.put("/personas/Hana", json={"display_name": None}).status_code == 422
    assert client.put("/personas/Nobody", json={"backstory": backstory}).status_code == 404

    assert client.delete("/personas/Hana").status_code == 204
    assert client.get("/personas/Hana").status_code == 404
    assert client.delete("/personas/Hana").status_code == 404


def test_create_persona_rejects_incomplete_localization(client: TestClient) -> None:
    response = client.post("/personas", json=_persona_payload(display_name={"ko": "하나"}))
    assert response.status_code == 422


def test_select_my_persona(client: TestClient, user_id: int) -> None:
    client.post("/personas", json=_persona_payload())
    headers = {"X-User-Id": str(user_id)}

    assert client.get("/users/me/persona", headers=headers).json()["selected_persona"] is None

    response = client.put("/users/me/persona", json={"persona_name": "Hana"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["selected_persona"]["name"] == "Hana"

    response = client.put("/users/me/persona", json={"persona_name": "Nobody"}, headers=headers)
    assert response.status_code == 404

    response = client.put("/users/me/persona", json={"persona_name": None}, headers=headers)
    assert response.json()["selected_persona"] is None


def test_me_requires_user_header(client: TestClient) -> None:
    assert client.put("/users/me/persona", json={"persona_name": None}).status_code == 401
    assert client.get("/users/me/persona", headers={"X-User-Id": "999"}).status_code == 404


def test_delete_persona_in_use_is_rejected(client: TestClient, user_id: int) -> None:
    client.post("/personas", json=_persona_payload())
    client.put("/users/me/persona", json={"persona_name": "Hana"}, headers={"X-User-Id": str(user_id)})

    assert client.delete("/personas/Hana").status_code == 409


def test_checkin_saves_conversation_turns(
    client: TestClient, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_llm(monkeypatch, "오늘 수고했어요!")
    client.post("/personas", json=_persona_payload())
    headers = {"X-User-Id": str(user_id)}
    client.put("/users/me/persona", json={"persona_name": "Hana"}, headers=headers)

    first = client.post(
        "/daily-actual-logs/checkin", json={"user_id": user_id, "utterance": "오늘 좀 힘들었어"}
    ).json()
    conversation_id = first["conversation_id"]
    assert conversation_id is not None

    second = client.post(
        "/daily-actual-logs/checkin",
        json={"user_id": user_id, "utterance": "내일은 잘할게", "conversation_id": conversation_id},
    ).json()
    assert second["conversation_id"] == conversation_id

    conversation = client.get(f"/users/me/persona-conversations/{conversation_id}", headers=headers).json()
    assert conversation["persona_id"] == "Hana"
    assert conversation["context_type"] == "daily_checkin"
    assert [(m["role"], m["content"]) for m in conversation["messages"]] == [
        ("user", "오늘 좀 힘들었어"),
        ("assistant", "오늘 수고했어요!"),
        ("user", "내일은 잘할게"),
        ("assistant", "오늘 수고했어요!"),
    ]

    listed = client.get("/users/me/persona-conversations?context_type=daily_checkin", headers=headers).json()
    assert [c["id"] for c in listed] == [conversation_id]
    assert client.delete("/personas/Hana").status_code == 409


def test_checkin_without_persona_is_not_saved(
    client: TestClient, engine, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_llm(monkeypatch, "좋아요")
    body = client.post("/daily-actual-logs/checkin", json={"user_id": user_id, "utterance": "안녕"}).json()

    assert body["conversation_id"] is None
    with Session(engine) as session:
        assert session.query(PersonaConversation).count() == 0


def test_checkin_with_foreign_conversation_skips_llm(
    client: TestClient, engine, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _mock_llm(monkeypatch, "좋아요")
    client.post("/personas", json=_persona_payload())
    with Session(engine) as session:
        other = User(name="Other", preferred_language="ko", selected_persona_id="Hana")
        session.add(other)
        session.flush()
        conversation = PersonaConversation(
            user_id=other.id, persona_id="Hana", context_type="daily_checkin", messages=[]
        )
        session.add(conversation)
        session.commit()
        conversation_id = conversation.id

    response = client.post(
        "/daily-actual-logs/checkin",
        json={"user_id": user_id, "utterance": "안녕", "conversation_id": conversation_id},
    )
    assert response.status_code == 404
    assert calls == []
    assert client.get(
        f"/users/me/persona-conversations/{conversation_id}", headers={"X-User-Id": str(user_id)}
    ).status_code == 404
