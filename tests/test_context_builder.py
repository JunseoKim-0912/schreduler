from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    Base,
    ComplianceReport,
    Event,
    EventInstance,
    EventInstanceStatus,
    EventType,
    NonComplianceCategory,
    User,
)
from app.services.context_builder import build_daily_checkin_summary


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _make_user(session: Session) -> User:
    user = User(name="June", preferred_language="ko")
    session.add(user)
    session.flush()
    return user


def _make_instance(
    session: Session,
    user: User,
    *,
    title: str,
    start: datetime,
    end: datetime,
    status: EventInstanceStatus,
    instance_date: date,
) -> EventInstance:
    event = Event(user_id=user.id, title=title, start_time=start, end_time=end)
    session.add(event)
    session.flush()

    instance = EventInstance(event_id=event.id, date=instance_date, status=status)
    session.add(instance)
    session.flush()
    return instance


def test_build_daily_checkin_summary_with_no_instances(session: Session) -> None:
    user = _make_user(session)

    summary = build_daily_checkin_summary(session, user.id, date(2026, 9, 17))

    assert summary == "오늘 계획한 0개 중 0개 완료."


def test_build_daily_checkin_summary_counts_done_without_detail(session: Session) -> None:
    user = _make_user(session)
    for i in range(3):
        _make_instance(
            session,
            user,
            title=f"일정{i}",
            start=datetime(2026, 9, 17, 9 + i, 0),
            end=datetime(2026, 9, 17, 10 + i, 0),
            status=EventInstanceStatus.DONE,
            instance_date=date(2026, 9, 17),
        )

    summary = build_daily_checkin_summary(session, user.id, date(2026, 9, 17))

    assert summary == "오늘 계획한 3개 중 3개 완료."
    assert "일정" not in summary  # done인 이벤트의 제목은 요약에 안 들어감


def test_build_daily_checkin_summary_includes_missed_event_detail_with_reason(
    session: Session,
) -> None:
    user = _make_user(session)
    _make_instance(
        session,
        user,
        title="아침 운동",
        start=datetime(2026, 9, 17, 7, 0),
        end=datetime(2026, 9, 17, 7, 30),
        status=EventInstanceStatus.DONE,
        instance_date=date(2026, 9, 17),
    )
    missed = _make_instance(
        session,
        user,
        title="알고리즘 스터디",
        start=datetime(2026, 9, 17, 21, 0),
        end=datetime(2026, 9, 17, 22, 0),
        status=EventInstanceStatus.MISSED,
        instance_date=date(2026, 9, 17),
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

    assert "오늘 계획한 2개 중 1개 완료." in summary
    assert "놓친 일정:" in summary
    assert "알고리즘 스터디 (21:00~22:00) - 사유: 피로/무기력 (너무 피곤했어요)" in summary
    assert "아침 운동" not in summary  # done은 상세 내용이 안 들어감


def test_build_daily_checkin_summary_marks_missed_event_without_reason(
    session: Session,
) -> None:
    user = _make_user(session)
    _make_instance(
        session,
        user,
        title="발표 준비",
        start=datetime(2026, 9, 17, 14, 0),
        end=datetime(2026, 9, 17, 15, 0),
        status=EventInstanceStatus.MISSED,
        instance_date=date(2026, 9, 17),
    )

    summary = build_daily_checkin_summary(session, user.id, date(2026, 9, 17))

    assert "발표 준비 (14:00~15:00) - 사유 미기록" in summary


def test_build_daily_checkin_summary_only_includes_that_date(session: Session) -> None:
    user = _make_user(session)
    _make_instance(
        session,
        user,
        title="어제 일정",
        start=datetime(2026, 9, 16, 9, 0),
        end=datetime(2026, 9, 16, 10, 0),
        status=EventInstanceStatus.MISSED,
        instance_date=date(2026, 9, 16),
    )
    _make_instance(
        session,
        user,
        title="오늘 일정",
        start=datetime(2026, 9, 17, 9, 0),
        end=datetime(2026, 9, 17, 10, 0),
        status=EventInstanceStatus.DONE,
        instance_date=date(2026, 9, 17),
    )

    summary = build_daily_checkin_summary(session, user.id, date(2026, 9, 17))

    assert "오늘 계획한 1개 중 1개 완료." in summary
    assert "어제 일정" not in summary


def test_build_daily_checkin_summary_only_includes_that_users_instances(
    session: Session,
) -> None:
    user1 = _make_user(session)
    user2 = _make_user(session)
    _make_instance(
        session,
        user2,
        title="다른 사람 일정",
        start=datetime(2026, 9, 17, 9, 0),
        end=datetime(2026, 9, 17, 10, 0),
        status=EventInstanceStatus.MISSED,
        instance_date=date(2026, 9, 17),
    )

    summary = build_daily_checkin_summary(session, user1.id, date(2026, 9, 17))

    assert summary == "오늘 계획한 0개 중 0개 완료."


def test_missed_deadline_is_described_by_due_time_and_sorted_with_scheduled(session: Session) -> None:
    user = _make_user(session)
    day = date(2026, 9, 17)
    deadline = Event(
        user_id=user.id, title="과제 제출", event_type=EventType.DEADLINE, start_time=None, end_time=datetime(2026, 9, 17, 8, 0)
    )
    session.add(deadline)
    session.flush()
    session.add(EventInstance(event_id=deadline.id, date=day, status=EventInstanceStatus.MISSED))
    _make_instance(
        session, user, title="수업", start=datetime(2026, 9, 17, 9, 0), end=datetime(2026, 9, 17, 10, 0),
        status=EventInstanceStatus.MISSED, instance_date=day,
    )
    session.flush()

    summary = build_daily_checkin_summary(session, user.id, day)

    assert summary.splitlines()[2:] == [
        "- 과제 제출 (08:00 마감) - 사유 미기록",
        "- 수업 (09:00~10:00) - 사유 미기록",
    ]
