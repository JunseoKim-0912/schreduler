from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Event, EngagementScope, EngagementState, EscalationStage, User
from app.services import engagement_service
from app.services.engagement_service import (
    evaluate_escalation,
    get_or_create_engagement_state,
    record_response,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(autouse=True)
def no_real_telegram_calls(monkeypatch: pytest.MonkeyPatch):
    """실제 텔레그램 발송 대신 호출 여부/내용만 기록한다."""
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        engagement_service,
        "send_telegram_message",
        lambda user, text: calls.append((user.id, text)),
    )
    return calls


def _make_user(session: Session, *, opt_in: bool = True) -> User:
    user = User(
        name="June",
        preferred_language="ko",
        telegram_opt_in=opt_in,
        telegram_chat_id="12345" if opt_in else None,
    )
    session.add(user)
    session.flush()
    return user


def test_get_or_create_engagement_state_creates_normal_state(session: Session) -> None:
    user = _make_user(session)

    state = get_or_create_engagement_state(session, user.id, EngagementScope.GLOBAL)

    assert state.escalation_stage == EscalationStage.NORMAL
    assert state.last_response_at is not None
    assert state.scope == EngagementScope.GLOBAL
    assert state.ref_event_id is None


def test_get_or_create_engagement_state_returns_existing(session: Session) -> None:
    user = _make_user(session)

    first = get_or_create_engagement_state(session, user.id, EngagementScope.GLOBAL)
    second = get_or_create_engagement_state(session, user.id, EngagementScope.GLOBAL)

    assert first.id == second.id


def test_get_or_create_engagement_state_distinguishes_event_scope(session: Session) -> None:
    user = _make_user(session)
    event = Event(
        user_id=user.id,
        title="이벤트",
        start_time=datetime(2026, 9, 17, 9, 0),
        end_time=datetime(2026, 9, 17, 10, 0),
    )
    session.add(event)
    session.flush()

    global_state = get_or_create_engagement_state(session, user.id, EngagementScope.GLOBAL)
    event_state = get_or_create_engagement_state(
        session, user.id, EngagementScope.EVENT, ref_event_id=event.id
    )

    assert global_state.id != event_state.id
    assert event_state.ref_event_id == event.id


def test_record_response_resets_to_normal(session: Session) -> None:
    user = _make_user(session)
    state = get_or_create_engagement_state(session, user.id, EngagementScope.GLOBAL)
    state.escalation_stage = EscalationStage.WEEK_2_MUTED
    state.last_response_at = datetime.utcnow() - timedelta(weeks=2)
    session.commit()

    record_response(session, state)

    assert state.escalation_stage == EscalationStage.NORMAL
    assert state.last_response_at is not None
    assert datetime.utcnow() - state.last_response_at < timedelta(seconds=5)


@pytest.mark.parametrize(
    ("elapsed_weeks", "expected_stage"),
    [
        (0, EscalationStage.NORMAL),
        (0.9, EscalationStage.NORMAL),
        (1, EscalationStage.WEEK_1_TELEGRAM),
        (1.5, EscalationStage.WEEK_1_TELEGRAM),
        (2, EscalationStage.WEEK_2_MUTED),
        (2.9, EscalationStage.WEEK_2_MUTED),
        (3, EscalationStage.WEEK_3_FINAL),
        (10, EscalationStage.WEEK_3_FINAL),
    ],
)
def test_evaluate_escalation_picks_correct_stage(
    session: Session, elapsed_weeks: float, expected_stage: EscalationStage
) -> None:
    user = _make_user(session)
    state = get_or_create_engagement_state(session, user.id, EngagementScope.GLOBAL)
    state.last_response_at = datetime.utcnow() - timedelta(weeks=elapsed_weeks)
    session.commit()

    result = evaluate_escalation(session, state)

    assert result == expected_stage
    assert state.escalation_stage == expected_stage


def test_evaluate_escalation_sends_telegram_message_only_on_week1_and_week3(
    session: Session, no_real_telegram_calls: list
) -> None:
    user = _make_user(session)
    state = get_or_create_engagement_state(session, user.id, EngagementScope.GLOBAL)

    state.last_response_at = datetime.utcnow() - timedelta(weeks=1)
    session.commit()
    evaluate_escalation(session, state)
    assert len(no_real_telegram_calls) == 1  # week 1 메시지

    state.last_response_at = datetime.utcnow() - timedelta(weeks=2)
    session.commit()
    evaluate_escalation(session, state)
    assert len(no_real_telegram_calls) == 1  # week 2는 mute라 추가 발송 없음

    state.last_response_at = datetime.utcnow() - timedelta(weeks=3)
    session.commit()
    evaluate_escalation(session, state)
    assert len(no_real_telegram_calls) == 2  # week 3 최종 메시지


def test_evaluate_escalation_does_not_resend_for_same_stage(
    session: Session, no_real_telegram_calls: list
) -> None:
    user = _make_user(session)
    state = get_or_create_engagement_state(session, user.id, EngagementScope.GLOBAL)
    state.last_response_at = datetime.utcnow() - timedelta(weeks=1)
    session.commit()

    evaluate_escalation(session, state)
    evaluate_escalation(session, state)
    evaluate_escalation(session, state)

    assert len(no_real_telegram_calls) == 1


def test_evaluate_escalation_stays_silent_forever_after_week3_until_reset(
    session: Session, no_real_telegram_calls: list
) -> None:
    user = _make_user(session)
    state = get_or_create_engagement_state(session, user.id, EngagementScope.GLOBAL)
    state.last_response_at = datetime.utcnow() - timedelta(weeks=5)
    session.commit()

    evaluate_escalation(session, state)
    assert len(no_real_telegram_calls) == 1
    assert state.escalation_stage == EscalationStage.WEEK_3_FINAL

    # 훨씬 더 지나도 (이미 완전 중단 상태) 다시 보내지 않는다.
    later = datetime.utcnow() + timedelta(weeks=10)
    evaluate_escalation(session, state, now=later)
    assert len(no_real_telegram_calls) == 1
    assert state.escalation_stage == EscalationStage.WEEK_3_FINAL

    # 응답하면 즉시 리셋되고, 다시 1주가 지나면 다시 알림이 나간다.
    record_response(session, state)
    assert state.escalation_stage == EscalationStage.NORMAL

    state.last_response_at = datetime.utcnow() - timedelta(weeks=1)
    session.commit()
    evaluate_escalation(session, state)
    assert len(no_real_telegram_calls) == 2
    assert state.escalation_stage == EscalationStage.WEEK_1_TELEGRAM
