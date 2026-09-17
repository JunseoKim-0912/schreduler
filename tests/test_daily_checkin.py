"""SleepLog API, DailyActualLog API, context_builder 요약 함수에 대한 테스트.

가상의 하루(완료/미완료가 섞인)를 만들어서 build_daily_checkin_summary가
기대한 형태의 요약 텍스트를 만드는지 집중적으로 확인한다.
"""

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import app
from app.models import (
    Base,
    ComplianceReport,
    Event,
    EventInstance,
    EventInstanceStatus,
    NonComplianceCategory,
    User,
)
from app.services.context_builder import build_daily_checkin_summary


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


# ---------------------------------------------------------------------------
# SleepLog API
# ---------------------------------------------------------------------------


def test_create_and_get_sleep_log(client: TestClient, user_id: int) -> None:
    response = client.post(
        "/sleep-logs",
        json={
            "user_id": user_id,
            "date": "2026-09-17",
            "actual_bedtime": "2026-09-16T23:30:00",
            "actual_wake_time": "2026-09-17T07:00:00",
        },
    )
    assert response.status_code == 201
    created = response.json()
    assert created["actual_bedtime"] == "2026-09-16T23:30:00"
    assert created["actual_wake_time"] == "2026-09-17T07:00:00"

    fetched = client.get(f"/sleep-logs/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created


def test_update_sleep_log_wake_time(client: TestClient, user_id: int) -> None:
    created = client.post(
        "/sleep-logs",
        json={
            "user_id": user_id,
            "date": "2026-09-17",
            "actual_bedtime": "2026-09-16T23:30:00",
            "actual_wake_time": "2026-09-17T07:00:00",
        },
    ).json()

    response = client.put(
        f"/sleep-logs/{created['id']}", json={"actual_wake_time": "2026-09-17T06:30:00"}
    )

    assert response.status_code == 200
    assert response.json()["actual_wake_time"] == "2026-09-17T06:30:00"


def test_sleep_log_rejects_wake_time_before_bedtime(client: TestClient, user_id: int) -> None:
    response = client.post(
        "/sleep-logs",
        json={
            "user_id": user_id,
            "date": "2026-09-17",
            "actual_bedtime": "2026-09-17T07:00:00",
            "actual_wake_time": "2026-09-16T23:30:00",
        },
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# DailyActualLog API
# ---------------------------------------------------------------------------


def test_create_and_get_daily_actual_log(client: TestClient, user_id: int) -> None:
    response = client.post(
        "/daily-actual-logs",
        json={
            "user_id": user_id,
            "date": "2026-09-17",
            "summary_text": "오늘 계획한 3개 중 2개 완료.",
            "actual_events": [{"title": "아침 운동", "status": "done"}],
        },
    )
    assert response.status_code == 201
    created = response.json()
    assert created["summary_text"] == "오늘 계획한 3개 중 2개 완료."

    fetched = client.get(f"/daily-actual-logs/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created


def test_update_daily_actual_log_summary(client: TestClient, user_id: int) -> None:
    created = client.post(
        "/daily-actual-logs",
        json={
            "user_id": user_id,
            "date": "2026-09-17",
            "summary_text": "초안",
            "actual_events": [],
        },
    ).json()

    response = client.put(
        f"/daily-actual-logs/{created['id']}", json={"summary_text": "최종 요약"}
    )

    assert response.status_code == 200
    assert response.json()["summary_text"] == "최종 요약"


# ---------------------------------------------------------------------------
# context_builder: 완료/미완료가 섞인 가상의 하루
# ---------------------------------------------------------------------------


@pytest.fixture
def session(engine):
    with Session(engine) as session:
        yield session


def _make_instance(
    session: Session,
    user: User,
    *,
    title: str,
    start: datetime,
    end: datetime,
    status: EventInstanceStatus,
) -> EventInstance:
    event = Event(user_id=user.id, title=title, start_time=start, end_time=end)
    session.add(event)
    session.flush()

    instance = EventInstance(event_id=event.id, date=start.date(), status=status)
    session.add(instance)
    session.flush()
    return instance


def test_mixed_day_summary_has_expected_shape(session: Session) -> None:
    """오늘 하루: 3개 계획 중 2개 완료(상세 내용 없음), 1개는 미완료(제목/시간/
    사유가 상세히 담겨야 함)."""
    user = User(name="June", preferred_language="ko")
    session.add(user)
    session.flush()

    _make_instance(
        session,
        user,
        title="아침 운동",
        start=datetime(2026, 9, 17, 7, 0),
        end=datetime(2026, 9, 17, 7, 30),
        status=EventInstanceStatus.DONE,
    )
    _make_instance(
        session,
        user,
        title="점심 약속",
        start=datetime(2026, 9, 17, 12, 0),
        end=datetime(2026, 9, 17, 13, 0),
        status=EventInstanceStatus.DONE,
    )
    missed = _make_instance(
        session,
        user,
        title="알고리즘 스터디",
        start=datetime(2026, 9, 17, 21, 0),
        end=datetime(2026, 9, 17, 22, 0),
        status=EventInstanceStatus.MISSED,
    )
    session.add(
        ComplianceReport(
            event_instance_id=missed.id,
            reason_category=NonComplianceCategory.FATIGUE,
            reason_text="너무 피곤했어요",
            llm_triggered=True,
        )
    )
    session.commit()

    summary = build_daily_checkin_summary(session, user.id, date(2026, 9, 17))

    expected = (
        "오늘 계획한 3개 중 2개 완료.\n"
        "놓친 일정:\n"
        "- 알고리즘 스터디 (21:00~22:00) - 사유: 피로/무기력 (너무 피곤했어요)"
    )
    assert summary == expected
    assert "아침 운동" not in summary
    assert "점심 약속" not in summary


def test_mixed_day_with_multiple_missed_events_lists_each_with_its_own_reason(
    session: Session,
) -> None:
    user = User(name="June", preferred_language="ko")
    session.add(user)
    session.flush()

    _make_instance(
        session,
        user,
        title="독서",
        start=datetime(2026, 9, 17, 6, 0),
        end=datetime(2026, 9, 17, 6, 30),
        status=EventInstanceStatus.DONE,
    )
    missed1 = _make_instance(
        session,
        user,
        title="헬스장",
        start=datetime(2026, 9, 17, 18, 0),
        end=datetime(2026, 9, 17, 19, 0),
        status=EventInstanceStatus.MISSED,
    )
    missed2 = _make_instance(
        session,
        user,
        title="발표 준비",
        start=datetime(2026, 9, 17, 20, 0),
        end=datetime(2026, 9, 17, 21, 0),
        status=EventInstanceStatus.MISSED,
    )
    session.add(
        ComplianceReport(
            event_instance_id=missed1.id,
            reason_category=NonComplianceCategory.SCHEDULE_CONFLICT,
            llm_triggered=False,
        )
    )
    # missed2는 사유를 아예 기록 안 한 케이스
    session.commit()

    summary = build_daily_checkin_summary(session, user.id, date(2026, 9, 17))

    expected = (
        "오늘 계획한 3개 중 1개 완료.\n"
        "놓친 일정:\n"
        "- 헬스장 (18:00~19:00) - 사유: 일정 충돌\n"
        "- 발표 준비 (20:00~21:00) - 사유 미기록"
    )
    assert summary == expected


def test_all_done_day_has_no_missed_section(session: Session) -> None:
    user = User(name="June", preferred_language="ko")
    session.add(user)
    session.flush()

    _make_instance(
        session,
        user,
        title="아침 운동",
        start=datetime(2026, 9, 17, 7, 0),
        end=datetime(2026, 9, 17, 7, 30),
        status=EventInstanceStatus.DONE,
    )

    summary = build_daily_checkin_summary(session, user.id, date(2026, 9, 17))

    assert summary == "오늘 계획한 1개 중 1개 완료."
    assert "놓친 일정" not in summary
