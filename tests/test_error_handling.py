import logging
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.db import get_db
from app.core.exceptions import ConflictError, NotFoundError, register_exception_handlers
from app.main import app
from app.models import Base, Event, EventInstance, User
from app.services import llm_client as llm_client_module
from app.services.llm_client import LLMConfigError, LLMRequestError, LLMResponseParsingError

_REAL_HTTPX_CLIENT = httpx.Client


@pytest.fixture(autouse=True)
def forbid_real_llm_calls(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """이 파일의 어떤 테스트도 실제 LLM API를 부르지 않게 한다. 호출되면 기록만 하고 실패시킨다."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("LLM이 호출되면 안 된다")

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(
        llm_client_module.httpx, "Client", lambda *a, **kw: _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler))
    )
    return calls


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(name="June", preferred_language="ko"))
        session.commit()
    return engine


@pytest.fixture
def client(engine) -> Iterator[TestClient]:
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


def _count(engine, model) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model))


def _date_range(client: TestClient) -> int:
    return client.post(
        "/date-ranges", json={"user_id": 1, "name": "학기", "start_date": "2026-09-01", "end_date": "2026-12-20"}
    ).json()["id"]


def _location(client: TestClient) -> int:
    return client.post("/locations", json={"user_id": 1, "name": "학교", "default_travel_minutes": 30}).json()["id"]


BASE_EVENT = {"user_id": 1, "title": "스터디", "start_time": "2026-09-01T19:00:00", "end_time": "2026-09-01T20:00:00"}


# --- 잘못된 반복 규칙 ---


@pytest.mark.parametrize("rule", ["FREQ=NOPE", "garbage", "FREQ=WEEKLY;BYDAY=XX"])
def test_invalid_rrule_on_create_is_422_and_nothing_is_saved(client: TestClient, engine, rule: str) -> None:
    date_range_id = _date_range(client)

    response = client.post(
        "/events", json={**BASE_EVENT, "is_recurring": True, "recurrence_rule": rule, "date_range_id": date_range_id}
    )

    assert response.status_code == 422
    assert "invalid recurrence_rule" in response.text
    assert _count(engine, Event) == 0
    assert _count(engine, EventInstance) == 0


def test_invalid_rrule_on_update_is_rejected(client: TestClient) -> None:
    event_id = client.post(
        "/events", json={**BASE_EVENT, "is_recurring": True, "recurrence_rule": "FREQ=DAILY"}
    ).json()["id"]

    response = client.put(f"/events/{event_id}", json={"recurrence_rule": "garbage"})

    assert response.status_code == 422
    assert client.get(f"/events/{event_id}").json()["recurrence_rule"] == "FREQ=DAILY"


def test_invalid_rrule_on_task_is_422(client: TestClient) -> None:
    response = client.post(
        "/tasks",
        headers={"X-User-Id": "1"},
        json={"title": "리포트", "end_time": "2026-09-04T18:00:00", "recurrence_rule": "BAD", "date_range_id": _date_range(client)},
    )

    assert response.status_code == 422


# --- is_recurring / recurrence_rule 짝 ---


@pytest.mark.parametrize(
    "fields", [{"is_recurring": True}, {"is_recurring": False, "recurrence_rule": "FREQ=DAILY"}, {"recurrence_rule": "FREQ=DAILY"}]
)
def test_recurrence_flag_and_rule_must_match_on_create(client: TestClient, fields: dict) -> None:
    assert client.post("/events", json={**BASE_EVENT, **fields}).status_code == 422


def test_recurrence_flag_and_rule_must_match_after_update(client: TestClient) -> None:
    event_id = client.post(
        "/events", json={**BASE_EVENT, "is_recurring": True, "recurrence_rule": "FREQ=DAILY"}
    ).json()["id"]

    assert client.put(f"/events/{event_id}", json={"is_recurring": False}).status_code == 422
    response = client.put(f"/events/{event_id}", json={"is_recurring": False, "recurrence_rule": None})
    assert response.status_code == 200
    assert (response.json()["is_recurring"], response.json()["recurrence_rule"]) == (False, None)


def test_event_cannot_be_its_own_parent(client: TestClient) -> None:
    event_id = client.post("/events", json=BASE_EVENT).json()["id"]

    response = client.put(f"/events/{event_id}", json={"parent_event_id": event_id})

    assert response.status_code == 422
    assert "own parent" in response.json()["detail"]


# --- 부분 수정 시 기존 값과 합친 최종 상태 검사 ---


def test_date_range_partial_update_cannot_end_before_existing_start(client: TestClient) -> None:
    date_range_id = _date_range(client)

    response = client.put(f"/date-ranges/{date_range_id}", json={"end_date": "2026-08-01"})

    assert response.status_code == 422
    assert client.get(f"/date-ranges/{date_range_id}").json()["end_date"] == "2026-12-20"


def test_sleep_log_partial_update_cannot_wake_before_existing_bedtime(client: TestClient) -> None:
    sleep_id = client.post(
        "/sleep-logs",
        json={"user_id": 1, "date": "2026-09-24", "actual_bedtime": "2026-09-23T23:00:00", "actual_wake_time": "2026-09-24T07:00:00"},
    ).json()["id"]

    response = client.put(f"/sleep-logs/{sleep_id}", json={"actual_wake_time": "2026-09-23T22:00:00"})

    assert response.status_code == 422
    assert client.get(f"/sleep-logs/{sleep_id}").json()["actual_wake_time"] == "2026-09-24T07:00:00"


@pytest.mark.parametrize(
    ("create_path", "create_body", "field"),
    [
        ("/locations", {"user_id": 1, "name": "학교", "default_travel_minutes": 30}, "name"),
        ("/locations", {"user_id": 1, "name": "학교", "default_travel_minutes": 30}, "default_travel_minutes"),
        ("/date-ranges", {"user_id": 1, "name": "학기", "start_date": "2026-09-01", "end_date": "2026-12-20"}, "start_date"),
        ("/events", BASE_EVENT, "title"),
        ("/events", BASE_EVENT, "end_time"),
        ("/events", BASE_EVENT, "is_recurring"),
        ("/daily-actual-logs", {"user_id": 1, "date": "2026-09-24", "summary_text": "요약"}, "actual_events"),
    ],
)
def test_explicit_null_for_required_field_is_422_not_db_error(
    client: TestClient, create_path: str, create_body: dict, field: str
) -> None:
    resource_id = client.post(create_path, json=create_body).json()["id"]

    response = client.put(f"{create_path}/{resource_id}", json={field: None})

    assert response.status_code == 422
    assert f"{field} cannot be null" in response.text


# --- 빈 문자열 ---


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/events", {**BASE_EVENT, "title": "   "}),
        ("/locations", {"user_id": 1, "name": "", "default_travel_minutes": 5}),
        ("/date-ranges", {"user_id": 1, "name": " ", "start_date": "2026-09-01", "end_date": "2026-09-02"}),
        ("/daily-actual-logs", {"user_id": 1, "date": "2026-09-24", "summary_text": ""}),
        ("/events/parse", {"user_id": 1, "utterance": "  "}),
        ("/daily-actual-logs/checkin", {"user_id": 1, "utterance": ""}),
    ],
)
def test_blank_strings_are_rejected_before_any_llm_call(
    client: TestClient, forbid_real_llm_calls: list, path: str, body: dict
) -> None:
    assert client.post(path, json=body).status_code == 422
    assert forbid_real_llm_calls == []


def test_titles_are_trimmed(client: TestClient) -> None:
    assert client.post("/events", json={**BASE_EVENT, "title": "  스터디  "}).json()["title"] == "스터디"


# --- 참조 중인 리소스 삭제 ---


def test_date_range_in_use_cannot_be_deleted(client: TestClient) -> None:
    date_range_id = _date_range(client)
    client.post("/events", json={**BASE_EVENT, "is_recurring": True, "recurrence_rule": "FREQ=DAILY", "date_range_id": date_range_id})

    response = client.delete(f"/date-ranges/{date_range_id}")

    assert response.status_code == 409
    assert "used by 1 events" in response.json()["detail"]
    assert client.get(f"/date-ranges/{date_range_id}").status_code == 200


def test_location_in_use_cannot_be_deleted_but_unused_can(client: TestClient) -> None:
    used = _location(client)
    unused = _location(client)
    client.post("/events", json={**BASE_EVENT, "location_id": used})

    assert client.delete(f"/locations/{used}").status_code == 409
    assert client.delete(f"/locations/{unused}").status_code == 204


def test_deleting_event_also_deletes_its_travel_child(client: TestClient, engine) -> None:
    event_id = client.post("/events", json={**BASE_EVENT, "location_id": _location(client)}).json()["id"]
    assert _count(engine, Event) == 2  # 본 일정 + 이동시간 하위 일정

    assert client.delete(f"/events/{event_id}").status_code == 204

    assert _count(engine, Event) == 0


# --- 전역 예외 처리기 ---


@pytest.fixture
def handler_client() -> TestClient:
    test_app = FastAPI()
    register_exception_handlers(test_app)

    @test_app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internal detail")

    @test_app.get("/integrity")
    def integrity() -> None:
        raise IntegrityError("INSERT INTO users ...", {}, Exception("UNIQUE constraint failed"))

    @test_app.get("/not-found")
    def not_found() -> None:
        raise NotFoundError("widget 7 does not exist")

    @test_app.get("/conflict")
    def conflict() -> None:
        raise ConflictError("already exists")

    return TestClient(test_app, raise_server_exceptions=False)


def test_unexpected_error_is_json_500_and_logged_without_leaking(handler_client: TestClient, caplog) -> None:
    with caplog.at_level(logging.ERROR, logger="app.core.exceptions"):
        response = handler_client.get("/boom")

    assert response.status_code == 500
    assert response.json() == {"detail": "서버 내부 오류가 발생했습니다"}
    assert "secret internal detail" not in response.text
    assert "GET /boom -> 500" in caplog.text
    assert "secret internal detail" in caplog.text  # 원인은 로그에만 남긴다


def test_integrity_error_is_409_without_sql(handler_client: TestClient, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="app.core.exceptions"):
        response = handler_client.get("/integrity")

    assert response.status_code == 409
    assert "INSERT" not in response.text
    assert "UNIQUE constraint failed" in caplog.text


@pytest.mark.parametrize(("path", "status_code"), [("/not-found", 404), ("/conflict", 409)])
def test_app_errors_map_to_status_and_are_logged(handler_client: TestClient, caplog, path: str, status_code: int) -> None:
    with caplog.at_level(logging.INFO, logger="app.core.exceptions"):
        response = handler_client.get(path)

    assert response.status_code == status_code
    assert f"GET {path} -> {status_code}" in caplog.text


def test_llm_errors_keep_their_status_codes() -> None:
    assert (LLMConfigError.status_code, LLMRequestError.status_code, LLMResponseParsingError.status_code) == (500, 502, 422)
    assert LLMConfigError.log_level == logging.ERROR


def test_app_logger_is_configured() -> None:
    logger = logging.getLogger("app")

    assert logger.handlers, "setup_logging이 app 로거에 핸들러를 붙여야 uvicorn에서 INFO 로그가 보인다"
    assert logger.level == logging.getLevelName(settings.log_level)
    assert logger.propagate is True
