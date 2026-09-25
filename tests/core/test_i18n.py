import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import get_current_user
from app.core.db import get_db
from app.core.i18n import get_response_language
from app.i18n import Language
from app.models import Base, User

test_app = FastAPI()


@test_app.get("/lang")
def read_language(language: Language = Depends(get_response_language)) -> dict[str, str]:
    return {"language": language}


@test_app.get("/lang-and-user")
def read_language_and_user(
    language: Language = Depends(get_response_language), user: User = Depends(get_current_user)
) -> dict[str, object]:
    return {"language": language, "user_id": user.id}


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
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

    test_app.dependency_overrides[get_db] = override_get_db
    yield TestClient(test_app)
    test_app.dependency_overrides.clear()


def _add_user(engine, language: str) -> int:
    with Session(engine) as session:
        user = User(name="June", preferred_language=language)
        session.add(user)
        session.commit()
        return user.id


@pytest.mark.parametrize("language", ["ko", "en"])
def test_language_follows_preferred_language(client: TestClient, engine, language: str) -> None:
    user_id = _add_user(engine, language)

    response = client.get("/lang", headers={"X-User-Id": str(user_id)})

    assert response.json() == {"language": language}


def test_language_changes_when_user_updates_preference(client: TestClient, engine) -> None:
    user_id = _add_user(engine, "ko")
    with Session(engine) as session:
        session.get(User, user_id).preferred_language = "en"
        session.commit()

    assert client.get("/lang", headers={"X-User-Id": str(user_id)}).json() == {"language": "en"}


def test_user_is_loaded_once_when_combined_with_get_current_user(client: TestClient, engine) -> None:
    user_id = _add_user(engine, "en")
    user_selects: list[str] = []

    def count_user_selects(conn, cursor, statement, *args) -> None:
        if statement.lstrip().upper().startswith("SELECT") and "FROM users" in statement:
            user_selects.append(statement)

    event.listen(engine, "before_cursor_execute", count_user_selects)
    try:
        response = client.get("/lang-and-user", headers={"X-User-Id": str(user_id)})
    finally:
        event.remove(engine, "before_cursor_execute", count_user_selects)

    assert response.json() == {"language": "en", "user_id": user_id}
    assert len(user_selects) == 1


@pytest.mark.parametrize(("headers", "status_code"), [({}, 401), ({"X-User-Id": "999"}, 404)])
def test_requires_current_user(client: TestClient, headers: dict, status_code: int) -> None:
    assert client.get("/lang", headers=headers).status_code == status_code
