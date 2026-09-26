"""FR-2 v3.6 되돌리기. LLM은 fill_event_slots_for_user를 가짜로 바꿔 호출하지 않는다."""

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.models import Base, ComplianceReport, EventInstance, User
from app.models.enums import EventInstanceStatus, NonComplianceCategory
from app.services import event_parse_service
from app.services.llm_client import EventSlotFillResult
from app.services.points import recalculate_points_since
from app.services.slot_fill_session import clear_all_sessions

TODAY = date(2026, 9, 26)  # 토요일
TABLES = ("events", "event_instances", "compliance_reports", "points_ledger")


@pytest.fixture(autouse=True)
def frozen_today():
    with freeze_time("2026-09-26 09:00:00") as frozen:
        yield frozen


@pytest.fixture(autouse=True)
def reset_sessions():
    clear_all_sessions()
    yield
    clear_all_sessions()


@pytest.fixture
def engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def client(engine):
    local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = local()
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


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    queue: list[EventSlotFillResult] = []

    def fake(db, user_id, utterance, **kwargs) -> EventSlotFillResult:
        assert queue, f"예상하지 못한 LLM 호출: {utterance!r}"
        return queue.pop(0)

    monkeypatch.setattr(event_parse_service, "fill_event_slots_for_user", fake)
    return lambda **fields: queue.append(EventSlotFillResult(**fields))


def _dump(engine) -> dict[str, list[dict[str, Any]]]:
    """행 단위 비교용: 테이블별 전체 행을 id 순으로."""
    with engine.connect() as conn:
        return {table: [dict(row._mapping) for row in conn.execute(text(f"SELECT * FROM {table} ORDER BY id"))] for table in TABLES}


def _weekly_quiz(client: TestClient, user_id: int) -> int:
    date_range = client.post(
        "/date-ranges",
        json={"user_id": user_id, "name": "가을학기", "start_date": "2026-09-01", "end_date": "2026-10-31"},
    ).json()["id"]
    response = client.post(
        "/events",
        json={
            "user_id": user_id,
            "title": "물리 퀴즈",
            "start_time": "2026-09-05T17:00:00",
            "end_time": "2026-09-05T18:00:00",
            "importance": 4,
            "is_recurring": True,
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=SA",
            "date_range_id": date_range,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def _one_off(client: TestClient, user_id: int, title: str, day: str = "2026-09-28") -> int:
    response = client.post(
        "/events",
        json={"user_id": user_id, "title": title, "start_time": f"{day}T10:00:00", "end_time": f"{day}T11:00:00"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _parse(client: TestClient, user_id: int, utterance: str) -> dict:
    response = client.post("/events/parse", json={"user_id": user_id, "utterance": utterance})
    assert response.status_code == 200, response.text
    return response.json()


def _undo(client: TestClient, user_id: int, action_id: int):
    return client.post(f"/actions/{action_id}/undo", headers={"X-User-Id": str(user_id)})


def _add_prep_child(client: TestClient, user_id: int, parent_id: int) -> int:
    response = client.post(
        "/events",
        json={
            "user_id": user_id,
            "title": "퀴즈 준비",
            "start_time": "2026-09-05T16:30:00",
            "end_time": "2026-09-05T17:00:00",
            "parent_event_id": parent_id,
            "child_kind": "custom",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _set_status(engine, event_id: int, days: list[date], status: EventInstanceStatus) -> None:
    with Session(engine) as session:
        for instance in session.execute(
            select(EventInstance).where(EventInstance.event_id == event_id, EventInstance.date.in_(days))
        ).scalars():
            instance.status = status
        session.commit()


def _add_report(engine, event_id: int, day: date) -> None:
    with Session(engine) as session:
        instance = session.execute(
            select(EventInstance).where(EventInstance.event_id == event_id, EventInstance.date == day)
        ).scalar_one()
        session.add(ComplianceReport(event_instance_id=instance.id, reason_category=NonComplianceCategory.OVERSLEPT))
        session.commit()


def _settle_points(engine, user_id: int) -> None:
    """자정 잡이 지난 날짜 원장을 이미 기록해 둔 상태로 만든다. 없으면 삭제·되돌리기 뒤 재계산이 0점 행을 새로 써서
    '삭제 전'과 행 단위로 비교할 수 없다."""
    with Session(engine) as session:
        recalculate_points_since(session, user_id, date(2026, 9, 1))
        session.commit()


def test_ui_delete_then_undo_restores_every_row_with_same_ids(client, engine, user_id):
    event_id = _weekly_quiz(client, user_id)
    _add_prep_child(client, user_id, event_id)
    _set_status(engine, event_id, [date(2026, 9, 12)], EventInstanceStatus.MISSED)
    _add_report(engine, event_id, date(2026, 9, 12))
    _settle_points(engine, user_id)
    before = _dump(engine)
    assert before["compliance_reports"] and len(before["events"]) == 2

    response = client.delete(f"/events/{event_id}")

    assert response.status_code == 204
    after_delete = _dump(engine)
    assert after_delete["events"] == [] and after_delete["event_instances"] == [] and after_delete["compliance_reports"] == []

    undo = _undo(client, user_id, int(response.headers["X-Action-Id"]))

    assert undo.status_code == 200, undo.text
    assert undo.json()["undone"] is True and undo.json()["source"] == "ui"
    assert _dump(engine) == before


def test_nl_instance_update_then_undo_restores_values(client, engine, user_id, llm):
    event_id = _weekly_quiz(client, user_id)
    before = _dump(engine)
    llm(intent="update", target_title="물리 퀴즈", target_date=TODAY, new_start_time="18:00")

    action_id = _parse(client, user_id, "오늘 물리 퀴즈 6시로 옮겨줘")["command"]["action_id"]
    assert _dump(engine) != before

    assert _undo(client, user_id, action_id).status_code == 200
    assert _dump(engine) == before


def test_nl_series_update_then_undo_restores_event_and_children(client, engine, user_id, llm):
    event_id = _weekly_quiz(client, user_id)
    _add_prep_child(client, user_id, event_id)
    before = _dump(engine)
    llm(intent="update", target_title="물리 퀴즈", target_scope="series", new_start_time="16:00", new_title="물리 쪽지시험")

    action_id = _parse(client, user_id, "물리 퀴즈 전체를 4시로, 이름은 물리 쪽지시험으로")["command"]["action_id"]
    assert _dump(engine)["events"] != before["events"]

    assert _undo(client, user_id, action_id).status_code == 200
    assert _dump(engine) == before


def test_delete_all_is_undone_in_one_step(client, engine, user_id, llm):
    _one_off(client, user_id, "물리 과제 1", "2026-09-28")
    _one_off(client, user_id, "물리 과제 2", "2026-09-29")
    _one_off(client, user_id, "스터디")
    before = _dump(engine)
    llm(intent="delete", target_title="물리 과제", target_all=True)
    token = _parse(client, user_id, "물리 과제 전부 없애줘")["command"]["confirmation_token"]
    confirmed = client.post("/events/commands/confirm", json={"user_id": user_id, "token": token}).json()
    assert len(_dump(engine)["events"]) == 1

    response = _undo(client, user_id, confirmed["command"]["action_id"])

    assert response.status_code == 200
    assert _dump(engine) == before


def test_nl_create_then_undo_removes_the_event(client, engine, user_id, llm):
    date_range = client.post(
        "/date-ranges",
        json={"user_id": user_id, "name": "가을학기", "start_date": "2026-09-01", "end_date": "2026-10-31"},
    ).json()["id"]
    before = _dump(engine)
    llm(title="치과 교정", frequency="WEEKLY", by_day=["MO"], start_time="10:00", end_time="11:00", importance=2, date_range_id=date_range)
    token = _parse(client, user_id, "매주 월요일 10시 치과 교정")["command"]["confirmation_token"]
    confirmed = client.post("/events/commands/confirm", json={"user_id": user_id, "token": token}).json()
    assert len(_dump(engine)["events"]) == 1

    response = _undo(client, user_id, confirmed["command"]["action_id"])

    assert response.status_code == 200
    assert response.json()["action_type"] == "create"
    assert _dump(engine) == before


def test_undo_twice_is_rejected(client, engine, user_id):
    event_id = _one_off(client, user_id, "스터디")
    action_id = int(client.delete(f"/events/{event_id}").headers["X-Action-Id"])
    assert _undo(client, user_id, action_id).status_code == 200

    response = _undo(client, user_id, action_id)

    assert response.status_code == 409
    assert len(_dump(engine)["events"]) == 1


def test_older_action_cannot_be_undone_while_newer_one_on_same_event_remains(client, engine, user_id, llm):
    event_id = _weekly_quiz(client, user_id)
    _settle_points(engine, user_id)
    before = _dump(engine)
    llm(intent="update", target_title="물리 퀴즈", target_date=TODAY, new_start_time="18:00")
    update_id = _parse(client, user_id, "오늘 물리 퀴즈 6시로")["command"]["action_id"]
    delete_id = int(client.delete(f"/events/{event_id}").headers["X-Action-Id"])

    blocked = _undo(client, user_id, update_id)

    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "더 최근 변경을 먼저 되돌려야 해요."
    assert _undo(client, user_id, delete_id).status_code == 200
    assert _undo(client, user_id, update_id).status_code == 200
    assert _dump(engine) == before


def test_undoing_past_instance_delete_restores_points_ledger(client, engine, user_id, llm):
    event_id = _weekly_quiz(client, user_id)
    past_days = [date(2026, 9, 5), date(2026, 9, 12), date(2026, 9, 19)]
    _set_status(engine, event_id, past_days, EventInstanceStatus.DONE)
    _settle_points(engine, user_id)
    before = _dump(engine)
    assert len(before["points_ledger"]) == 3
    llm(intent="delete", target_title="물리 퀴즈", target_date=date(2026, 9, 12))

    action_id = _parse(client, user_id, "9월 12일 물리 퀴즈 지워줘")["command"]["action_id"]

    assert _dump(engine)["points_ledger"] != before["points_ledger"], "지난 회차 삭제는 그날 이후 포인트를 바꾼다"
    assert _undo(client, user_id, action_id).status_code == 200
    assert _dump(engine) == before


def test_list_actions_newest_first_with_undone_flag(client, engine, user_id):
    first = int(client.delete(f"/events/{_one_off(client, user_id, 'A')}").headers["X-Action-Id"])
    second = int(client.delete(f"/events/{_one_off(client, user_id, 'B')}").headers["X-Action-Id"])
    _undo(client, user_id, first)

    response = client.get("/actions", params={"limit": 10}, headers={"X-User-Id": str(user_id)})

    assert response.status_code == 200
    assert [(a["id"], a["undone"]) for a in response.json()] == [(second, False), (first, True)]
    assert client.get("/actions", params={"limit": 1}, headers={"X-User-Id": str(user_id)}).json()[0]["id"] == second


def test_other_users_action_is_not_found(client, engine, user_id):
    action_id = int(client.delete(f"/events/{_one_off(client, user_id, 'A')}").headers["X-Action-Id"])
    with Session(engine) as session:
        other = User(name="Other", preferred_language="ko")
        session.add(other)
        session.commit()
        other_id = other.id

    assert _undo(client, other_id, action_id).status_code == 404
    assert _dump(engine)["events"] == []


def test_deleted_ids_are_not_reused_so_undo_cannot_collide(client, engine, user_id):
    first = _one_off(client, user_id, "A")
    action_id = int(client.delete(f"/events/{first}").headers["X-Action-Id"])
    second = _one_off(client, user_id, "B")

    assert second != first
    assert _undo(client, user_id, action_id).status_code == 200
    assert sorted(e["title"] for e in _dump(engine)["events"]) == ["A", "B"]
