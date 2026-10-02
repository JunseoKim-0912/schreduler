"""FR-2 되돌리기. 자연어 변경은 가짜 LLM 대본으로 어시스턴트를 거쳐 만든다 (tests/assistant_flow)."""

from datetime import date
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
from app.services.points import recalculate_points_since
from tests.assistant_flow import create, delete, find, only_action, run, update
from tests.auth_helpers import as_user, sign_in

TODAY = date(2026, 9, 26)  # 토요일
TABLES = ("events", "event_instances", "compliance_reports", "points_ledger")


@pytest.fixture(autouse=True)
def frozen_today():
    with freeze_time("2026-09-26 09:00:00") as frozen:
        yield frozen


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
def user_id(engine, client) -> int:
    with Session(engine) as session:
        user = User(name="June", preferred_language="ko")
        session.add(user)
        session.commit()
        sign_in(client, user.id)
        return user.id


def _dump(engine) -> dict[str, list[dict[str, Any]]]:
    """행 단위 비교용: 테이블별 전체 행을 id 순으로."""
    with engine.connect() as conn:
        return {table: [dict(row._mapping) for row in conn.execute(text(f"SELECT * FROM {table} ORDER BY id"))] for table in TABLES}


def _weekly_quiz(client: TestClient, user_id: int) -> int:
    date_range = client.post(
        "/date-ranges",
        json={"name": "가을학기", "start_date": "2026-09-01", "end_date": "2026-10-31"}, headers=as_user(user_id),
    ).json()["id"]
    response = client.post(
        "/events",
        json={
            "title": "물리 퀴즈",
            "start_time": "2026-09-05T17:00:00",
            "end_time": "2026-09-05T18:00:00",
            "importance": 4,
            "is_recurring": True,
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=SA",
            "date_range_id": date_range,
        }, headers=as_user(user_id),
    )
    assert response.status_code == 201
    return response.json()["id"]


def _one_off(client: TestClient, user_id: int, title: str, day: str = "2026-09-28") -> int:
    response = client.post(
        "/events",
        json={"title": title, "start_time": f"{day}T10:00:00", "end_time": f"{day}T11:00:00"}, headers=as_user(user_id),
    )
    assert response.status_code == 201
    return response.json()["id"]


def _instance_id(engine, event_id: int, day: date) -> int:
    with Session(engine) as session:
        return session.execute(
            select(EventInstance.id).where(EventInstance.event_id == event_id, EventInstance.date == day)
        ).scalar_one()


def _undo(client: TestClient, user_id: int, action_id: int):
    return client.post(f"/actions/{action_id}/undo", headers=as_user(user_id))


def _add_prep_child(client: TestClient, user_id: int, parent_id: int) -> int:
    response = client.post(
        "/events",
        json={
            "title": "퀴즈 준비",
            "start_time": "2026-09-05T16:30:00",
            "end_time": "2026-09-05T17:00:00",
            "parent_event_id": parent_id,
            "child_kind": "custom",
        }, headers=as_user(user_id),
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


def test_nl_instance_update_then_undo_restores_values(client, engine, user_id, monkeypatch):
    event_id = _weekly_quiz(client, user_id)
    instance_id = _instance_id(engine, event_id, TODAY)
    before = _dump(engine)

    action_id = only_action(run(
        client, monkeypatch, user_id,
        find(query="물리 퀴즈", date_from=TODAY.isoformat(), date_to=TODAY.isoformat()),
        update([(event_id, instance_id)], scope="instance", start_time="18:00"),
    ))
    assert _dump(engine) != before

    assert _undo(client, user_id, action_id).status_code == 200
    assert _dump(engine) == before


def test_nl_series_update_then_undo_restores_event_and_children(client, engine, user_id, monkeypatch):
    event_id = _weekly_quiz(client, user_id)
    _add_prep_child(client, user_id, event_id)
    before = _dump(engine)

    action_id = only_action(run(
        client, monkeypatch, user_id,
        find(query="물리 퀴즈"),
        update([(event_id, None)], start_time="16:00", title="물리 쪽지시험"),
    ))
    assert _dump(engine)["events"] != before["events"]

    assert _undo(client, user_id, action_id).status_code == 200
    assert _dump(engine) == before


def test_delete_all_is_undone_in_one_step(client, engine, user_id, monkeypatch):
    first = _one_off(client, user_id, "물리 과제 1", "2026-09-28")
    second = _one_off(client, user_id, "물리 과제 2", "2026-09-29")
    _one_off(client, user_id, "스터디")
    before = _dump(engine)
    action_id = only_action(run(client, monkeypatch, user_id, find(query="물리 과제"), delete([(first, None), (second, None)])))
    assert len(_dump(engine)["events"]) == 1

    response = _undo(client, user_id, action_id)

    assert response.status_code == 200
    assert _dump(engine) == before


def test_nl_create_then_undo_removes_the_event(client, engine, user_id, monkeypatch):
    client.post(
        "/date-ranges",
        json={"name": "가을학기", "start_date": "2026-09-01", "end_date": "2026-10-31"}, headers=as_user(user_id),
    )
    before = _dump(engine)
    action_id = only_action(run(
        client, monkeypatch, user_id,
        create(title="치과 교정", start_time="10:00", end_time="11:00", importance=2,
               recurrence={"frequency": "WEEKLY", "interval": 1, "by_day": ["MO"], "start_date": None},
               date_range={"name": "가을학기", "start_date": None, "end_date": None}),
    ))
    assert len(_dump(engine)["events"]) == 1

    response = _undo(client, user_id, action_id)

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


def test_older_action_cannot_be_undone_while_newer_one_on_same_event_remains(client, engine, user_id, monkeypatch):
    event_id = _weekly_quiz(client, user_id)
    _settle_points(engine, user_id)
    instance_id = _instance_id(engine, event_id, TODAY)
    before = _dump(engine)
    update_id = only_action(run(
        client, monkeypatch, user_id,
        find(query="물리 퀴즈", date_from=TODAY.isoformat(), date_to=TODAY.isoformat()),
        update([(event_id, instance_id)], scope="instance", start_time="18:00"),
    ))
    delete_id = int(client.delete(f"/events/{event_id}").headers["X-Action-Id"])

    blocked = _undo(client, user_id, update_id)

    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "더 최근 변경을 먼저 되돌려야 해요."
    assert _undo(client, user_id, delete_id).status_code == 200
    assert _undo(client, user_id, update_id).status_code == 200
    assert _dump(engine) == before


def test_undoing_past_instance_delete_restores_points_ledger(client, engine, user_id, monkeypatch):
    event_id = _weekly_quiz(client, user_id)
    past_days = [date(2026, 9, 5), date(2026, 9, 12), date(2026, 9, 19)]
    _set_status(engine, event_id, past_days, EventInstanceStatus.DONE)
    _settle_points(engine, user_id)
    instance_id = _instance_id(engine, event_id, date(2026, 9, 12))
    before = _dump(engine)
    assert len(before["points_ledger"]) == 3

    action_id = only_action(run(
        client, monkeypatch, user_id,
        find(query="물리 퀴즈", date_from="2026-09-12", date_to="2026-09-12"),
        delete([(event_id, instance_id)], scope="instance"),
    ))

    assert _dump(engine)["points_ledger"] != before["points_ledger"], "지난 회차 삭제는 그날 이후 포인트를 바꾼다"
    assert _undo(client, user_id, action_id).status_code == 200
    assert _dump(engine) == before


def test_list_actions_newest_first_with_undone_flag(client, engine, user_id):
    first = int(client.delete(f"/events/{_one_off(client, user_id, 'A')}").headers["X-Action-Id"])
    second = int(client.delete(f"/events/{_one_off(client, user_id, 'B')}").headers["X-Action-Id"])
    _undo(client, user_id, first)

    response = client.get("/actions", params={"limit": 10}, headers=as_user(user_id))

    assert response.status_code == 200
    assert [(a["id"], a["undone"]) for a in response.json()] == [(second, False), (first, True)]
    assert client.get("/actions", params={"limit": 1}, headers=as_user(user_id)).json()[0]["id"] == second


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
