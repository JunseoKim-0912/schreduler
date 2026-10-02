"""One-click demo accounts (POST /auth/demo): sample data, isolation, expiry cleanup, separate LLM caps, the creation
limit and the jobs that skip demo accounts."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import SESSION_COOKIE
from app.core.clock import utc_now_naive
from app.core.config import settings
from app.core.db import get_db
from app.core.scheduler import scheduler, shutdown_scheduler, start_scheduler
from app.main import app
from app.models import (
    ActionHistory,
    ActionSource,
    ActionType,
    AssistantMessage,
    AssistantSession,
    AssistantTurnLog,
    Base,
    ComplianceReport,
    DailyActualLog,
    EngagementScope,
    EngagementState,
    Event,
    EventInstance,
    EventInstanceStatus,
    EventType,
    LlmUsageLog,
    NonComplianceCategory,
    PendingProposal,
    Persona,
    PersonaConversation,
    PointsLedger,
    SleepLog,
    User,
    UserSession,
)
from app.services import auth_service, daily_checkin, demo_service, llm_usage, notification, sleep_checkin
from app.services import points as points_module
from app.services.assistant import agent
from app.services.auth_service import ForbiddenError
from app.services.demo_seed import seed_demo_week
from tests.auth_helpers import as_user
from tests.fake_responses import FakeResponsesClient, say

NOW_UTC = "2026-10-01 16:00:00"  # Thursday, 12:00 in Toronto
TODAY = date(2026, 10, 1)


@pytest.fixture(autouse=True)
def frozen() -> Iterator[None]:
    with freeze_time(NOW_UTC):
        yield


@pytest.fixture(autouse=True)
def demo_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "demo_mode_enabled", True)
    monkeypatch.setattr(settings, "demo_max_creations_per_hour", 30)
    monkeypatch.setattr(settings, "demo_llm_budget_per_user_usd", 0.05)
    monkeypatch.setattr(settings, "demo_llm_budget_total_usd", 2.0)
    monkeypatch.setattr(settings, "llm_daily_budget_per_user_usd", 1.0)
    monkeypatch.setattr(settings, "llm_daily_budget_total_usd", 5.0)
    monkeypatch.setattr(settings, "llm_daily_budget_admin_usd", None)
    monkeypatch.setattr(settings, "signup_mode", "closed")
    monkeypatch.setattr(settings, "app_timezone", "America/Toronto")
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "allowed_origins", ())


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for module in (notification, points_module, demo_service, daily_checkin, sleep_checkin):
        monkeypatch.setattr(module, "SessionLocal", local)
    return engine


@pytest.fixture
def client(engine) -> Iterator[TestClient]:
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
def me(engine) -> int:
    """The owner's real account, with its own copy of the sample week so there is plenty to accidentally delete."""
    with Session(engine) as session:
        user = User(name="June", email="june@example.com", preferred_language="en")
        session.add(user)
        session.flush()
        seed_demo_week(session, user, TODAY)
        return user.id


@pytest.fixture
def running_scheduler() -> Iterator[None]:
    start_scheduler()
    yield
    scheduler.remove_all_jobs()
    shutdown_scheduler()


def _start_demo(client: TestClient) -> tuple[int, dict[str, str]]:
    """POST /auth/demo on a fresh cookie jar; returns the demo user id and a Cookie header for it."""
    response = client.post("/auth/demo", headers={"Accept-Language": "en"})
    assert response.status_code == 201, response.text
    token = response.cookies[SESSION_COOKIE]
    client.cookies.clear()
    return response.json()["id"], {"Cookie": f"{SESSION_COOKIE}={token}"}


def _spend(engine, user_id: int | None, cost: float, *, demo: bool) -> None:
    with Session(engine) as session:
        session.add(
            LlmUsageLog(
                user_id=user_id, is_demo=demo, feature="assistant", model="gpt-5.6-luna", input_tokens=0, cached_tokens=0,
                output_tokens=0, reasoning_tokens=0, cost_usd=cost, created_at=utc_now_naive(),
            )
        )
        session.commit()


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch):
    def use(*steps) -> FakeResponsesClient:
        fake = FakeResponsesClient(list(steps))
        monkeypatch.setattr(agent, "ResponsesClient", lambda: fake)
        return fake

    return use


# --- switch and sign-in -------------------------------------------------------------------------


def test_demo_endpoint_is_404_when_demo_mode_is_off(client: TestClient, engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "demo_mode_enabled", False)

    response = client.post("/auth/demo")

    assert response.status_code == 404
    assert SESSION_COOKIE not in response.cookies
    with Session(engine) as session:
        assert session.execute(select(User)).first() is None


def test_demo_signs_in_a_new_account_without_email_for_24_hours(client: TestClient, engine) -> None:
    response = client.post("/auth/demo", headers={"Accept-Language": "ko"})

    assert response.status_code == 201
    body = response.json()
    assert body["is_demo"] is True and body["email"] is None and body["is_admin"] is False
    assert body["preferred_language"] == "ko"
    assert body["demo_expires_at"] == "2026-10-02T16:00:00"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Max-Age=86400" in cookie
    assert client.get("/auth/me").json()["id"] == body["id"]
    with Session(engine) as session:
        user = session.get(User, body["id"])
        assert user.password_hash is None and user.email is None
        assert session.execute(select(UserSession.expires_at)).scalar_one() == datetime(2026, 10, 2, 16, 0)


def test_the_same_cookie_works_until_the_demo_expires_then_stops(client: TestClient) -> None:
    _, cookie = _start_demo(client)

    with freeze_time("2026-10-02 15:59:00"):
        assert client.get("/auth/me", headers=cookie).status_code == 200
    with freeze_time("2026-10-02 16:00:01"):
        assert client.get("/auth/me", headers=cookie).status_code == 401


def test_expired_demo_cannot_get_in_even_before_the_cleanup_runs(engine) -> None:
    with Session(engine) as session:
        user, token = demo_service.create_demo_user(session, "en")
        user.demo_expires_at = utc_now_naive() - timedelta(minutes=1)
        session.commit()
        assert auth_service.user_for_token(session, token) is None


# --- sample data ----------------------------------------------------------------------------------


def test_demo_gets_a_student_week_relative_to_today(client: TestClient) -> None:
    _, cookie = _start_demo(client)

    events = client.get("/events", headers=cookie).json()
    top = [e for e in events if e["parent_event_id"] is None]
    recurring = {e["title"]: e["recurrence_rule"] for e in top if e["is_recurring"]}
    lectures = [title for title in recurring if title.endswith("Lecture")]
    assert 2 <= len(lectures) <= 3
    assert recurring["ECE244 Lab"] == "FREQ=WEEKLY;INTERVAL=2;BYDAY=WE"
    assert "MAT235 Tutorial" in recurring
    deadlines = [e for e in top if e["event_type"] == "deadline"]
    assert 2 <= len([d for d in deadlines if d["end_time"] >= f"{TODAY}"]) <= 3
    assert any(e["child_kind"] == "travel" for e in events)

    ranges = client.get("/date-ranges", headers=cookie).json()
    assert [r["name"] for r in ranges] == ["Fall term"]
    assert ranges[0]["start_date"] < str(TODAY) < ranges[0]["end_date"]
    assert len(client.get("/locations", headers=cookie).json()) == 2

    points = client.get("/points/summary", headers=cookie).json()
    assert points["total_points"] > 0
    assert points["current_streak_days"] >= 3


def test_sample_dates_follow_the_creation_day(engine) -> None:
    with freeze_time("2026-11-16 15:00:00"), Session(engine) as session:
        user, _ = demo_service.create_demo_user(session, "en")
        dates = session.execute(
            select(EventInstance.date).join(Event).where(Event.user_id == user.id, Event.event_type == EventType.DEADLINE)
        ).scalars().all()
        assert sorted(dates) == [date(2026, 11, 14), date(2026, 11, 18), date(2026, 11, 20)]
        past = session.execute(
            select(EventInstance.status).join(Event).where(Event.user_id == user.id, EventInstance.date < date(2026, 11, 16))
        ).scalars().all()
        assert EventInstanceStatus.PENDING not in past and EventInstanceStatus.DONE in past


# --- isolation ------------------------------------------------------------------------------------


def test_demo_accounts_are_isolated_from_each_other_and_from_my_account(client: TestClient, me: int) -> None:
    first, first_cookie = _start_demo(client)
    second, second_cookie = _start_demo(client)
    assert first != second

    first_events = {e["id"] for e in client.get("/events", headers=first_cookie).json()}
    second_events = {e["id"] for e in client.get("/events", headers=second_cookie).json()}
    my_events = {e["id"] for e in client.get("/events", headers=as_user(me)).json()}
    assert first_events and second_events and my_events
    assert not (first_events & second_events) and not (first_events & my_events) and not (second_events & my_events)

    assert client.get(f"/events/{min(second_events)}", headers=first_cookie).status_code == 404
    assert client.get(f"/events/{min(my_events)}", headers=first_cookie).status_code == 404
    assert client.delete(f"/events/{min(my_events)}", headers=first_cookie).status_code == 404
    assert client.get(f"/events/{min(first_events)}", headers=as_user(me)).status_code == 404


def test_demo_cannot_manage_personas_or_set_a_password(client: TestClient, engine) -> None:
    demo_id, cookie = _start_demo(client)
    persona = {"name": "mine", "display_name": {"en": "Mine", "ko": "내 것"}, "description": {"en": "x", "ko": "x"}}

    assert client.post("/personas", json=persona, headers=cookie).status_code == 403
    assert client.put("/users/me/language", json={"language": "ko"}, headers=cookie).status_code == 200
    with Session(engine) as session, pytest.raises(ForbiddenError):
        auth_service.set_password(session, session.get(User, demo_id), "correct horse battery")


# --- cleanup ----------------------------------------------------------------------------------------


def _add_everything_else(session: Session, user_id: int) -> None:
    """One row in every other table a user can own, so the cleanup has all of them to get right."""
    if session.get(Persona, "coach") is None:
        session.add(Persona(name="coach", display_name={"en": "Coach"}, description={"en": "Coach"}))
    instance = session.execute(select(EventInstance).join(Event).where(Event.user_id == user_id)).scalars().first()
    session.add(ComplianceReport(event_instance_id=instance.id, reason_category=NonComplianceCategory.FATIGUE))
    session.add(EngagementState(user_id=user_id, scope=EngagementScope.EVENT, ref_event_id=instance.event_id))
    chat = AssistantSession(user_id=user_id, created_at=utc_now_naive(), updated_at=utc_now_naive())
    session.add(chat)
    session.flush()
    session.add_all(
        [
            AssistantMessage(session_id=chat.id, role="user", content={"text": "hi"}, created_at=utc_now_naive()),
            PendingProposal(
                session_id=chat.id, token=f"t{user_id}", proposals=[], shown_at=utc_now_naive(), expires_at=utc_now_naive()
            ),
            AssistantTurnLog(session_id=chat.id, model="m", reasoning_effort="low", created_at=utc_now_naive()),
            ActionHistory(
                user_id=user_id, action_type=ActionType.CREATE, source=ActionSource.UI, summary_text="x", snapshot_before={},
                affected_ids={},
            ),
            DailyActualLog(user_id=user_id, date=TODAY, summary_text="ok", actual_events=[]),
            SleepLog(user_id=user_id, date=TODAY, actual_bedtime=datetime(2026, 9, 30, 23), actual_wake_time=datetime(2026, 10, 1, 7)),
            PersonaConversation(user_id=user_id, persona_id="coach", context_type="daily_checkin", messages=[]),
            LlmUsageLog(
                user_id=user_id, feature="assistant", model="m", cost_usd=0.01, created_at=utc_now_naive(),
                is_demo=session.get(User, user_id).is_demo,
            ),
        ]
    )
    auth_service.start_session(session, session.get(User, user_id))
    session.commit()


def _snapshot(engine) -> dict[str, dict[tuple[Any, ...], dict[str, Any]]]:
    tables: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}
    with engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            keys = [column.name for column in table.primary_key.columns]
            tables[table.name] = {
                tuple(row[k] for k in keys): dict(row) for row in (r._mapping for r in connection.execute(table.select()))
            }
    return tables


def _owner(tables: dict[str, dict[tuple[Any, ...], dict[str, Any]]], table: str, row: dict[str, Any]) -> int | None:
    """Which user a row belongs to, directly or through its event / instance / assistant session."""
    if table == "users":
        return row["id"]
    if "user_id" in row:
        return row["user_id"]
    if table == "event_instances":
        return tables["events"][(row["event_id"],)]["user_id"]
    if table == "compliance_reports":
        return _owner(tables, "event_instances", tables["event_instances"][(row["event_instance_id"],)])
    if "session_id" in row:
        return tables["assistant_sessions"][(row["session_id"],)]["user_id"]
    return None


def test_cleanup_deletes_only_expired_demo_accounts_and_everything_they_own(client: TestClient, engine, me: int) -> None:
    expired, _ = _start_demo(client)
    with freeze_time("2026-10-01 20:00:00"):
        active, active_cookie = _start_demo(client)
    for user_id in (me, expired, active):
        with Session(engine) as session:
            _add_everything_else(session, user_id)
    before = _snapshot(engine)
    owned_before = {name: {_owner(before, name, row) for row in rows.values()} for name, rows in before.items()}
    assert all(expired in owned_before[name] for name in owned_before if name not in ("personas", "login_failures"))

    with freeze_time("2026-10-02 17:00:00"), Session(engine) as session:
        assert demo_service.purge_expired_demo_users(session) == 1

    after = _snapshot(engine)
    for name, rows in before.items():
        for key, row in rows.items():
            owner = _owner(before, name, row)
            if name == "llm_usage_logs" and owner == expired:
                # Kept for the day's demo total and the cost reports, but no longer tied to anyone.
                assert after[name][key] == {**row, "user_id": None, "is_demo": True}
            elif owner == expired:
                assert key not in after[name], f"{name} row {key} of the expired demo survived"
            else:
                assert after[name].get(key) == row, f"{name} row {key} (owner {owner}) was changed or deleted"
        assert set(after[name]) <= set(rows)

    with freeze_time("2026-10-02 17:00:00"):
        assert client.get("/auth/me", headers=active_cookie).status_code == 200
        assert client.get("/events", headers=as_user(me)).json()


def test_cleanup_never_deletes_a_real_account_even_if_asked(engine, me: int) -> None:
    with Session(engine) as session:
        demo_service.purge_demo_users(session, [me])
        session.commit()
        assert session.get(User, me) is not None
        assert session.execute(select(Event).where(Event.user_id == me)).first() is not None


def test_cleanup_job_runs_hourly(running_scheduler, engine) -> None:
    demo_service.register_demo_cleanup_job()
    job = scheduler.get_job(demo_service.CLEANUP_JOB_ID)
    assert job is not None and job.trigger.interval == timedelta(hours=1)

    with Session(engine) as session:
        demo_service.create_demo_user(session, "en")
    with freeze_time("2026-10-02 16:30:00"):
        demo_service.run_demo_cleanup_job()
    with Session(engine) as session:
        assert session.execute(select(User)).first() is None


# --- LLM caps -----------------------------------------------------------------------------------------


def test_demo_has_its_own_per_user_cap_and_message(client: TestClient, engine, me: int, script) -> None:
    demo_id, cookie = _start_demo(client)
    _spend(engine, demo_id, 0.05, demo=True)
    script(say("Hi"))

    usage = client.get("/usage/today", headers=cookie).json()
    assert usage["is_demo"] is True and usage["limit_usd"] == 0.05
    blocked = client.post("/assistant/chat", json={"message": "hello"}, headers=cookie)
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "You've reached the demo's AI limit. You can keep trying everything else."
    assert client.post("/assistant/chat", json={"message": "hello"}, headers=as_user(me)).status_code == 200


def test_demo_total_cap_blocks_only_demos(client: TestClient, engine, me: int, script) -> None:
    _spend(engine, None, 2.0, demo=True)  # earlier demo accounts, already cleaned up
    _, cookie = _start_demo(client)
    script(say("Hi"))

    demo_response = client.post("/assistant/chat", json={"message": "hello"}, headers=cookie)
    assert demo_response.status_code == 429 and demo_response.json()["reason"] == "total_limit"
    with Session(engine) as session:
        session.add(User(name="ko demo", is_demo=True, preferred_language="ko", demo_expires_at=utc_now_naive() + timedelta(hours=1)))
        session.commit()
        korean_demo = session.execute(select(User.id).where(User.name == "ko demo")).scalar_one()
    korean = client.post("/assistant/chat", json={"message": "안녕"}, headers=as_user(korean_demo))
    assert korean.json()["detail"] == "데모 AI 한도에 도달했어요. 다른 기능은 계속 써볼 수 있어요."

    assert client.get("/usage/today", headers=as_user(me)).json()["total_blocked"] is False
    assert client.post("/assistant/chat", json={"message": "hello"}, headers=as_user(me)).status_code == 200


def test_demo_and_regular_spend_are_counted_apart(client: TestClient, engine, me: int, script) -> None:
    demo_id, cookie = _start_demo(client)
    _spend(engine, None, 4.99, demo=True)
    _spend(engine, me, 0.10, demo=False)

    with Session(engine) as session:
        regular = llm_usage.budget_status(session, session.get(User, me))
        demo = llm_usage.budget_status(session, session.get(User, demo_id))
    assert regular.total_spent_usd == pytest.approx(0.10) and not regular.total_blocked
    assert demo.total_spent_usd == pytest.approx(4.99) and demo.total_blocked

    _spend(engine, None, 5.0, demo=False)  # the regular pool is now full
    with Session(engine) as session:
        demo_user = session.get(User, demo_id)
        assert llm_usage.budget_status(session, demo_user).total_spent_usd == pytest.approx(4.99)


def test_demo_calls_are_logged_as_demo(client: TestClient, engine, script) -> None:
    demo_id, cookie = _start_demo(client)
    script(say("Hi"))

    assert client.post("/assistant/chat", json={"message": "hello"}, headers=cookie).status_code == 200

    with Session(engine) as session:
        rows = session.execute(select(LlmUsageLog).where(LlmUsageLog.user_id == demo_id)).scalars().all()
        assert rows and all(row.is_demo for row in rows)


# --- creation limit -------------------------------------------------------------------------------------


def test_demo_creation_is_capped_per_hour_across_everyone(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "demo_max_creations_per_hour", 2)
    _start_demo(client)
    with freeze_time("2026-10-01 16:20:00"):
        _start_demo(client)
        refused = client.post("/auth/demo", headers={"Accept-Language": "en"})
        assert refused.status_code == 429
        assert refused.json()["detail"].startswith("Too many demo accounts were started just now. Please try again in")
        assert refused.json()["retry_after_seconds"] == 40 * 60
        assert SESSION_COOKIE not in refused.cookies
        korean = client.post("/auth/demo", headers={"Accept-Language": "ko"})
        assert "잠시 후 다시 시도해 주세요" in korean.json()["detail"]
    with freeze_time("2026-10-01 17:00:01"):
        assert client.post("/auth/demo").status_code == 201


# --- jobs skip demo accounts -----------------------------------------------------------------------------


def test_demo_events_get_no_notification_jobs(running_scheduler, client: TestClient, engine, me: int) -> None:
    demo_id, cookie = _start_demo(client)

    created = client.post(
        "/events", json={"title": "Demo study", "start_time": "2026-10-02T09:00:00", "end_time": "2026-10-02T10:00:00"}, headers=cookie
    )
    assert created.status_code == 201
    assert scheduler.get_jobs() == []

    count = notification.register_upcoming_notifications(TODAY)
    with Session(engine) as session:
        demo_instances = set(
            session.execute(select(EventInstance.id).join(Event).where(Event.user_id == demo_id)).scalars()
        )
    job_instances = {int(job.id.split("_")[2]) for job in scheduler.get_jobs()}
    assert count > 0 and job_instances
    assert not (job_instances & demo_instances)


def test_midnight_points_skip_demo_accounts_and_keep_the_seeded_ledger(client: TestClient, engine, me: int) -> None:
    demo_id, _ = _start_demo(client)
    with Session(engine) as session:
        seeded = {(row.date, row.points_earned) for row in session.execute(select(PointsLedger).where(PointsLedger.user_id == demo_id)).scalars()}
    assert seeded

    with freeze_time("2026-10-02 05:00:00"):
        points_module.run_daily_points_job()

    with Session(engine) as session:
        demo_rows = {(r.date, r.points_earned) for r in session.execute(select(PointsLedger).where(PointsLedger.user_id == demo_id)).scalars()}
        mine = session.execute(select(PointsLedger).where(PointsLedger.user_id == me, PointsLedger.date == TODAY)).scalar_one_or_none()
    assert demo_rows == seeded
    assert mine is not None


def test_check_in_reminders_skip_demo_accounts(client: TestClient, me: int, monkeypatch: pytest.MonkeyPatch) -> None:
    _start_demo(client)
    reached: list[int] = []
    monkeypatch.setattr(daily_checkin, "_send_daily_checkin_to_user", lambda user: reached.append(user.id))
    monkeypatch.setattr(sleep_checkin, "_send_sleep_checkin_to_user", lambda user: reached.append(user.id))

    daily_checkin.send_daily_checkin_reminders()
    sleep_checkin.send_daily_sleep_checkin_reminders()

    assert reached == [me, me]
