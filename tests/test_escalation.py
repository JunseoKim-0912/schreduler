from datetime import timedelta

import pytest
from freezegun import freeze_time
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, EngagementScope, EngagementState, EscalationStage, User
from app.services import engagement_service
from app.services.engagement_service import (
    evaluate_escalation,
    get_or_create_engagement_state,
    record_response,
    run_escalation_check,
)


@pytest.fixture
def engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session(engine):
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


def _make_user(session: Session) -> User:
    user = User(
        name="June", preferred_language="ko", telegram_opt_in=True, telegram_chat_id="12345"
    )
    session.add(user)
    session.flush()
    return user


def test_escalation_progresses_through_stages_as_time_passes(
    session: Session, no_real_telegram_calls: list
) -> None:
    """freezegun으로 실제 시간을 흐르게 하면서 now=를 넘기지 않는 실제 운영
    경로(evaluate_escalation이 내부에서 datetime.utcnow()를 쓰는 경로)를 검증한다.
    """
    with freeze_time("2026-01-01 09:00:00") as frozen_time:
        user = _make_user(session)
        state = get_or_create_engagement_state(session, user.id, EngagementScope.GLOBAL)
        assert state.escalation_stage == EscalationStage.NORMAL

        frozen_time.tick(timedelta(days=6))
        evaluate_escalation(session, state)
        assert state.escalation_stage == EscalationStage.NORMAL
        assert no_real_telegram_calls == []

        frozen_time.tick(timedelta(days=1))  # 총 1주 경과
        evaluate_escalation(session, state)
        assert state.escalation_stage == EscalationStage.WEEK_1_TELEGRAM
        assert len(no_real_telegram_calls) == 1

        frozen_time.tick(timedelta(days=7))  # 총 2주 경과
        evaluate_escalation(session, state)
        assert state.escalation_stage == EscalationStage.WEEK_2_MUTED
        assert len(no_real_telegram_calls) == 1  # 2주차는 전면 중단이라 발송 없음

        frozen_time.tick(timedelta(days=7))  # 총 3주 경과
        evaluate_escalation(session, state)
        assert state.escalation_stage == EscalationStage.WEEK_3_FINAL
        assert len(no_real_telegram_calls) == 2  # 최종 메시지

        frozen_time.tick(timedelta(weeks=10))  # 훨씬 더 지나도
        evaluate_escalation(session, state)
        assert state.escalation_stage == EscalationStage.WEEK_3_FINAL
        assert len(no_real_telegram_calls) == 2  # 완전 침묵, 추가 발송 없음


def test_responding_resets_and_escalation_restarts_from_zero(
    session: Session, no_real_telegram_calls: list
) -> None:
    with freeze_time("2026-01-01 09:00:00") as frozen_time:
        user = _make_user(session)
        state = get_or_create_engagement_state(session, user.id, EngagementScope.GLOBAL)

        frozen_time.tick(timedelta(weeks=1))
        evaluate_escalation(session, state)
        assert state.escalation_stage == EscalationStage.WEEK_1_TELEGRAM
        assert len(no_real_telegram_calls) == 1

        record_response(session, state)  # "응답 시 즉시 리셋"
        assert state.escalation_stage == EscalationStage.NORMAL

        frozen_time.tick(timedelta(days=6))
        evaluate_escalation(session, state)
        assert state.escalation_stage == EscalationStage.NORMAL  # 리셋 후 아직 1주 안 지남

        frozen_time.tick(timedelta(days=1))  # 리셋 이후로 다시 1주 경과
        evaluate_escalation(session, state)
        assert state.escalation_stage == EscalationStage.WEEK_1_TELEGRAM
        assert len(no_real_telegram_calls) == 2


def test_run_escalation_check_escalates_multiple_users_after_one_week(
    engine, monkeypatch: pytest.MonkeyPatch, no_real_telegram_calls: list
) -> None:
    """실제 스케줄러가 호출하는 진입점(run_escalation_check)까지 포함해서,
    1주가 지난 뒤 여러 사용자의 상태가 한 번에 올바르게 갱신되는지 확인한다.
    """
    testing_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(engagement_service, "SessionLocal", testing_session_local)

    with freeze_time("2026-01-01 09:00:00") as frozen_time:
        with Session(engine) as setup_session:
            user1 = _make_user(setup_session)
            user2 = _make_user(setup_session)
            state1 = get_or_create_engagement_state(setup_session, user1.id, EngagementScope.GLOBAL)
            state2 = get_or_create_engagement_state(setup_session, user2.id, EngagementScope.GLOBAL)
            state1_id, state2_id = state1.id, state2.id

        frozen_time.tick(timedelta(weeks=1))
        run_escalation_check()

        with Session(engine) as verify_session:
            refreshed1 = verify_session.get(EngagementState, state1_id)
            refreshed2 = verify_session.get(EngagementState, state2_id)
            assert refreshed1.escalation_stage == EscalationStage.WEEK_1_TELEGRAM
            assert refreshed2.escalation_stage == EscalationStage.WEEK_1_TELEGRAM

        assert len(no_real_telegram_calls) == 2
