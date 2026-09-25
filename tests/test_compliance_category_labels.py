from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.models import Base, ComplianceReport, Event, EventInstance, EventInstanceStatus, NonComplianceCategory, User


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

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _user_with_instance(engine, language: str) -> tuple[int, int]:
    with Session(engine) as session:
        user = User(name="June", preferred_language=language)
        session.add(user)
        session.flush()
        event = Event(
            user_id=user.id, title="아침 운동", start_time=datetime(2026, 9, 17, 7), end_time=datetime(2026, 9, 17, 8)
        )
        session.add(event)
        session.flush()
        instance = EventInstance(event_id=event.id, date=date(2026, 9, 17), status=EventInstanceStatus.MISSED)
        session.add(instance)
        session.commit()
        return user.id, instance.id


# --- GET /compliance-reports/categories ---


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            "ko",
            ["늦잠/기상 실패", "피로/무기력", "우선순위 변경", "일정 충돌", "깜빡함", "이동/교통 문제", "기타"],
        ),
        (
            "en",
            ["Overslept", "Fatigue / low energy", "Priorities changed", "Schedule conflict", "Forgot", "Transit issue", "Other"],
        ),
    ],
)
def test_categories_are_labelled_in_users_language(
    client: TestClient, engine, language: str, expected: list[str]
) -> None:
    user_id, _ = _user_with_instance(engine, language)

    body = client.get("/compliance-reports/categories", headers={"X-User-Id": str(user_id)}).json()

    assert [item["code"] for item in body] == [
        "overslept", "fatigue", "priority_shift", "schedule_conflict", "forgot", "transit_issue", "other"
    ]
    assert [item["label"] for item in body] == expected


def test_categories_require_user(client: TestClient) -> None:
    assert client.get("/compliance-reports/categories").status_code == 401


# --- POST /compliance-reports 응답 라벨 ---


@pytest.mark.parametrize(("language", "label"), [("ko", "늦잠/기상 실패"), ("en", "Overslept")])
def test_created_report_has_label_in_owner_language(client: TestClient, engine, language: str, label: str) -> None:
    _, instance_id = _user_with_instance(engine, language)

    body = client.post(
        "/compliance-reports", json={"event_instance_id": instance_id, "reason_category": "overslept"}
    ).json()

    assert body["reason_category"] == "overslept"  # 코드값은 언어와 무관하게 그대로
    assert body["reason_category_label"] == label


def test_code_value_is_stored_unchanged(client: TestClient, engine) -> None:
    _, instance_id = _user_with_instance(engine, "en")

    report_id = client.post(
        "/compliance-reports", json={"event_instance_id": instance_id, "reason_category": "transit_issue"}
    ).json()["id"]

    with Session(engine) as session:
        assert session.get(ComplianceReport, report_id).reason_category == NonComplianceCategory.TRANSIT_ISSUE


# --- GET /compliance-reports/stats 라벨 ---


def test_stats_labels_follow_filtered_users_language(client: TestClient, engine) -> None:
    en_user_id, instance_id = _user_with_instance(engine, "en")
    client.post("/compliance-reports", json={"event_instance_id": instance_id, "reason_category": "forgot"})

    body = client.get("/compliance-reports/stats", params={"user_id": en_user_id}).json()

    stats = {item["reason_category"]: item for item in body["by_category"]}
    assert (stats["forgot"]["label"], stats["forgot"]["count"]) == ("Forgot", 1)
    assert stats["overslept"]["label"] == "Overslept"


def test_stats_without_user_use_default_korean(client: TestClient, engine) -> None:
    _user_with_instance(engine, "en")

    body = client.get("/compliance-reports/stats").json()

    labels = {item["reason_category"]: item["label"] for item in body["by_category"]}
    assert labels["forgot"] == "깜빡함"
