import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.db import get_db
from app.main import app
from app.models import Base, User
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


def _add_user(engine, name: str = "June", language: str = "ko") -> int:
    with Session(engine) as session:
        user = User(name=name, preferred_language=language)
        session.add(user)
        session.commit()
        return user.id


@pytest.fixture
def user_id(engine) -> int:
    return _add_user(engine)


@pytest.fixture
def personas(client: TestClient) -> None:
    for name, ko, en in (("Hana", "하나", "Hana"), ("Rordon", "로든 갬지", "Rordon Gamsay")):
        response = client.post(
            "/personas",
            json={
                "name": name,
                "display_name": {"ko": ko, "en": en},
                "description": {"ko": f"{ko} 설명", "en": f"{en} description"},
            },
        )
        assert response.status_code == 201


@pytest.fixture
def llm_requests(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": f"답장 {len(captured)}"}}]})

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(
        llm_client_module.httpx,
        "Client",
        lambda *a, **kw: _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler)),
    )
    return captured


def _headers(user_id: int) -> dict[str, str]:
    return {"X-User-Id": str(user_id)}


def _select(client: TestClient, user_id: int, persona_name: str | None) -> httpx.Response:
    return client.put("/users/me/persona", json={"persona_name": persona_name}, headers=_headers(user_id))


def _checkin(client: TestClient, user_id: int, utterance: str, conversation_id: int | None = None) -> dict:
    body: dict[str, object] = {"user_id": user_id, "utterance": utterance}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    response = client.post("/daily-actual-logs/checkin", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# --- 페르소나 선택 API ---


@pytest.mark.usefixtures("personas")
def test_select_returns_full_persona_and_persists(client: TestClient, engine, user_id: int) -> None:
    response = _select(client, user_id, "Rordon")

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == user_id
    assert body["selected_persona"]["display_name"] == {"ko": "로든 갬지", "en": "Rordon Gamsay"}

    with Session(engine) as session:
        assert session.get(User, user_id).selected_persona_id == "Rordon"
    assert client.get("/users/me/persona", headers=_headers(user_id)).json()["selected_persona"]["name"] == "Rordon"


@pytest.mark.usefixtures("personas")
def test_switching_persona_replaces_selection(client: TestClient, user_id: int) -> None:
    _select(client, user_id, "Hana")
    response = _select(client, user_id, "Rordon")

    assert response.json()["selected_persona"]["name"] == "Rordon"


@pytest.mark.usefixtures("personas")
def test_failed_selection_keeps_previous_persona(client: TestClient, user_id: int) -> None:
    _select(client, user_id, "Hana")

    assert _select(client, user_id, "Nobody").status_code == 404
    assert client.get("/users/me/persona", headers=_headers(user_id)).json()["selected_persona"]["name"] == "Hana"


@pytest.mark.usefixtures("personas")
def test_selection_is_per_user(client: TestClient, engine, user_id: int) -> None:
    other_id = _add_user(engine, name="Other")
    _select(client, user_id, "Hana")
    _select(client, other_id, "Rordon")

    assert client.get("/users/me/persona", headers=_headers(user_id)).json()["selected_persona"]["name"] == "Hana"
    assert client.get("/users/me/persona", headers=_headers(other_id)).json()["selected_persona"]["name"] == "Rordon"


@pytest.mark.parametrize(
    ("headers", "status_code"),
    [({}, 401), ({"X-User-Id": "999"}, 404), ({"X-User-Id": "abc"}, 422)],
)
def test_select_requires_valid_user_header(client: TestClient, headers: dict, status_code: int) -> None:
    response = client.put("/users/me/persona", json={"persona_name": None}, headers=headers)
    assert response.status_code == status_code


def test_select_requires_persona_name_field(client: TestClient, user_id: int) -> None:
    assert client.put("/users/me/persona", json={}, headers=_headers(user_id)).status_code == 422


# --- 선택한 페르소나가 LLM 프롬프트에 반영되는지 ---


@pytest.mark.usefixtures("personas")
def test_selected_persona_and_language_reach_llm_prompt(
    client: TestClient, engine, llm_requests: list[dict]
) -> None:
    en_user_id = _add_user(engine, name="Alex", language="en")
    _select(client, en_user_id, "Rordon")

    _checkin(client, en_user_id, "I skipped the gym")

    system_message = llm_requests[0]["messages"][0]["content"]
    assert "Rordon Gamsay" in system_message
    assert "Rordon Gamsay description" in system_message
    assert "영어로 답하라" in system_message
    assert "I skipped the gym" not in system_message


# --- 대화 로그 저장 ---


@pytest.mark.usefixtures("personas")
def test_conversation_log_records_each_turn_in_order(
    client: TestClient, user_id: int, llm_requests: list[dict]
) -> None:
    _select(client, user_id, "Hana")

    conversation_id = _checkin(client, user_id, "첫 번째")["conversation_id"]
    _checkin(client, user_id, "두 번째", conversation_id)
    _checkin(client, user_id, "세 번째", conversation_id)

    conversation = client.get(
        f"/users/me/persona-conversations/{conversation_id}", headers=_headers(user_id)
    ).json()
    assert [(m["role"], m["content"]) for m in conversation["messages"]] == [
        ("user", "첫 번째"),
        ("assistant", "답장 1"),
        ("user", "두 번째"),
        ("assistant", "답장 2"),
        ("user", "세 번째"),
        ("assistant", "답장 3"),
    ]
    assert all(m["created_at"] for m in conversation["messages"])


@pytest.mark.usefixtures("personas")
def test_new_conversation_after_persona_switch_uses_new_persona(
    client: TestClient, user_id: int, llm_requests: list[dict]
) -> None:
    _select(client, user_id, "Hana")
    first_id = _checkin(client, user_id, "안녕")["conversation_id"]

    _select(client, user_id, "Rordon")
    continued_id = _checkin(client, user_id, "계속", first_id)["conversation_id"]
    second_id = _checkin(client, user_id, "새 대화")["conversation_id"]

    assert continued_id == first_id
    assert second_id != first_id
    conversations = client.get("/users/me/persona-conversations", headers=_headers(user_id)).json()
    assert [(c["id"], c["persona_id"]) for c in conversations] == [(second_id, "Rordon"), (first_id, "Hana")]


@pytest.mark.usefixtures("personas")
def test_deselecting_persona_stops_saving(
    client: TestClient, user_id: int, llm_requests: list[dict]
) -> None:
    _select(client, user_id, "Hana")
    _checkin(client, user_id, "저장됨")
    _select(client, user_id, None)

    assert _checkin(client, user_id, "저장 안 됨")["conversation_id"] is None
    conversations = client.get("/users/me/persona-conversations", headers=_headers(user_id)).json()
    assert len(conversations) == 1


@pytest.mark.usefixtures("personas")
def test_conversation_history_is_private(
    client: TestClient, engine, user_id: int, llm_requests: list[dict]
) -> None:
    other_id = _add_user(engine, name="Other")
    _select(client, user_id, "Hana")
    conversation_id = _checkin(client, user_id, "비밀")["conversation_id"]

    assert client.get("/users/me/persona-conversations", headers=_headers(other_id)).json() == []
    response = client.get(f"/users/me/persona-conversations/{conversation_id}", headers=_headers(other_id))
    assert response.status_code == 404


@pytest.mark.usefixtures("personas")
def test_unknown_conversation_id_is_rejected_before_llm(
    client: TestClient, user_id: int, llm_requests: list[dict]
) -> None:
    _select(client, user_id, "Hana")

    response = client.post(
        "/daily-actual-logs/checkin", json={"user_id": user_id, "utterance": "안녕", "conversation_id": 999}
    )

    assert response.status_code == 404
    assert llm_requests == []


def test_llm_failure_saves_no_conversation(
    client: TestClient, user_id: int, personas: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream down")

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(
        llm_client_module.httpx,
        "Client",
        lambda *a, **kw: _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler)),
    )
    _select(client, user_id, "Hana")

    response = client.post("/daily-actual-logs/checkin", json={"user_id": user_id, "utterance": "안녕"})

    assert response.status_code == 502
    assert client.get("/users/me/persona-conversations", headers=_headers(user_id)).json() == []
