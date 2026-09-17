from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.scheduler import scheduler
from app.models.engagement_state import EngagementState
from app.models.enums import EngagementScope, EscalationStage
from app.services.telegram_bot import send_telegram_message

ESCALATION_JOB_ID = "engagement_escalation_check"

# FR-4-1: 1주 -> 텔레그램 알림, 2주 -> 모든 알림 중단, 3주 -> 최종 메시지 후 완전 중단.
_STAGE_THRESHOLDS: list[tuple[timedelta, EscalationStage]] = [
    (timedelta(weeks=3), EscalationStage.WEEK_3_FINAL),
    (timedelta(weeks=2), EscalationStage.WEEK_2_MUTED),
    (timedelta(weeks=1), EscalationStage.WEEK_1_TELEGRAM),
]


def _target_stage_for_elapsed(elapsed: timedelta) -> EscalationStage:
    for threshold, stage in _STAGE_THRESHOLDS:
        if elapsed >= threshold:
            return stage
    return EscalationStage.NORMAL


def _describe_state(state: EngagementState) -> str:
    if state.scope == EngagementScope.EVENT and state.ref_event is not None:
        return f"'{state.ref_event.title}' 일정"
    return "앱 사용"


def get_or_create_engagement_state(
    db: Session,
    user_id: int,
    scope: EngagementScope,
    ref_event_id: int | None = None,
) -> EngagementState:
    """(user_id, scope, ref_event_id) 조합의 EngagementState를 찾거나 새로 만든다.

    새로 만들 때는 지금 이 순간까지는 정상이었다고 보고 last_response_at을
    현재 시각으로, escalation_stage를 NORMAL로 초기화한다.
    """
    stmt = select(EngagementState).where(
        EngagementState.user_id == user_id,
        EngagementState.scope == scope,
        EngagementState.ref_event_id == ref_event_id,
    )
    state = db.execute(stmt).scalars().first()
    if state is not None:
        return state

    now = datetime.utcnow()
    state = EngagementState(
        user_id=user_id,
        scope=scope,
        ref_event_id=ref_event_id,
        last_response_at=now,
        escalation_stage=EscalationStage.NORMAL,
        stage_updated_at=now,
    )
    db.add(state)
    db.commit()
    db.refresh(state)
    return state


def record_response(db: Session, state: EngagementState) -> None:
    """사용자가 응답했을 때 즉시 NORMAL로 리셋한다 (FR-4-1: "응답 시 즉시 리셋")."""
    now = datetime.utcnow()
    state.last_response_at = now
    state.escalation_stage = EscalationStage.NORMAL
    state.stage_updated_at = now
    db.commit()


def evaluate_escalation(
    db: Session, state: EngagementState, now: datetime | None = None
) -> EscalationStage:
    """state의 무응답 경과 시간에 따라 에스컬레이션 단계를 갱신한다 (FR-4-1).

    단계가 실제로 올라갈 때만 그 단계에 맞는 메시지를 보낸다 (같은 단계에
    머물러 있으면 매번 다시 보내지 않는다). WEEK_2_MUTED는 모든 알림을
    중단하는 단계라 보낼 메시지가 없고, WEEK_3_FINAL에 이미 도달했다면 최종
    메시지를 보낸 뒤로는 응답해서 리셋되기 전까지 더 이상 아무것도 하지 않는다
    (완전 중단).
    """
    now = now or datetime.utcnow()
    if state.last_response_at is None:
        return state.escalation_stage

    if state.escalation_stage == EscalationStage.WEEK_3_FINAL:
        return state.escalation_stage  # 이미 완전 중단 상태

    elapsed = now - state.last_response_at
    target_stage = _target_stage_for_elapsed(elapsed)

    if target_stage == state.escalation_stage:
        return state.escalation_stage

    state.escalation_stage = target_stage
    state.stage_updated_at = now
    db.commit()

    if target_stage == EscalationStage.WEEK_1_TELEGRAM:
        send_telegram_message(
            state.user,
            f"[Schreduler] {_describe_state(state)}에 대해 1주째 응답이 없어요. "
            "잘 지내고 계신가요?",
        )
    elif target_stage == EscalationStage.WEEK_3_FINAL:
        send_telegram_message(
            state.user,
            f"[Schreduler] {_describe_state(state)}에 대해 3주째 응답이 없어 "
            "더 이상 알림을 보내지 않습니다. 다시 시작하고 싶으면 앱에서 아무 "
            "일정이나 완료 체크해주세요.",
        )
    # WEEK_2_MUTED: 모든 알림 중단 -> 보낼 메시지 없음.

    return target_stage


def run_escalation_check() -> None:
    """등록된 모든 EngagementState를 한 번씩 평가한다. 스케줄러가 주기적으로 호출한다."""
    with SessionLocal() as db:
        states = db.execute(select(EngagementState)).scalars().all()
        now = datetime.utcnow()
        for state in states:
            evaluate_escalation(db, state, now=now)


def register_escalation_job() -> None:
    """앱 시작 시 한 번 호출해서, 에스컬레이션 체크를 주기적으로 스케줄러에 등록한다."""
    scheduler.add_job(
        run_escalation_check,
        trigger="interval",
        days=1,
        id=ESCALATION_JOB_ID,
        replace_existing=True,
    )
