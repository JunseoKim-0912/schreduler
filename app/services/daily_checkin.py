from __future__ import annotations

import logging
from datetime import date as dt_date

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.exceptions import NotFoundError
from app.core.scheduler import scheduler
from app.i18n import render_notification
from app.models.user import User
from app.schemas.daily_checkin import DailyCheckinMessageResponse
from app.schemas.persona import PersonaRead
from app.services.context_builder import build_daily_checkin_summary
from app.services.llm_client import generate_daily_checkin_reply
from app.services.persona_conversation_service import record_turn, resolve_conversation
from app.services.notification import send_push_notification

logger = logging.getLogger(__name__)

DAILY_CHECKIN_JOB_ID = "daily_evening_checkin"
DAILY_CHECKIN_CONTEXT_TYPE = "daily_checkin"
DAILY_CHECKIN_HOUR = 21
DAILY_CHECKIN_MINUTE = 0


def _send_daily_checkin_to_user(user: User) -> None:
    # User에 아직 fcm_token 필드가 없어서(디바이스 등록 전) 항상 None이다.
    # 필드가 추가되면 이 한 줄만 바뀌면 된다.
    device_token = getattr(user, "fcm_token", None)
    if not device_token:
        logger.info("[하루 체크인] device_token 없음 - 발송 생략. user_id=%s", user.id)
        return
    send_push_notification(
        device_token,
        render_notification("daily_checkin.title", user.preferred_language),
        render_notification("daily_checkin.body", user.preferred_language),
    )


def send_daily_checkin_reminders() -> None:
    """FR-8: 매일 밤 9시에 모든 사용자에게 "오늘 하루 어떻게 보냈는지" 체크인
    알림을 보낸다.

    실제 응답(요약/실제 있었던 일)은 클라이언트가 이 알림을 받고 나서
    POST /daily-actual-logs로 기록한다 — 이 함수는 "물어보는" 역할만 한다.
    """
    with SessionLocal() as db:
        users = db.execute(select(User)).scalars().all()
        for user in users:
            _send_daily_checkin_to_user(user)


def handle_daily_checkin_message(
    db: Session,
    user_id: int,
    utterance: str,
    target_date: dt_date | None = None,
    conversation_id: int | None = None,
    *,
    http_client: httpx.Client | None = None,
) -> DailyCheckinMessageResponse:
    """저녁 9시 체크인 대화 한 턴을 처리한다 (FR-8).

    context_builder.build_daily_checkin_summary로 만든 하루 요약(완료는 개수만,
    놓친 일정은 상세히)을 시스템 프롬프트에 넣어서, LLM이 그날 놓친 일정 위주로
    대화하게 한다.
    """
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError(f"user_id {user_id} does not exist")

    conversation = resolve_conversation(db, user, DAILY_CHECKIN_CONTEXT_TYPE, conversation_id)
    summary = build_daily_checkin_summary(db, user_id, target_date or dt_date.today())
    persona = PersonaRead.model_validate(user.selected_persona) if user.selected_persona else None
    reply = generate_daily_checkin_reply(
        summary, utterance, persona=persona, language=user.preferred_language, http_client=http_client
    )
    conversation = record_turn(db, user, DAILY_CHECKIN_CONTEXT_TYPE, utterance, reply, conversation)
    return DailyCheckinMessageResponse(
        reply=reply,
        summary=summary,
        conversation_id=conversation.id if conversation else None,
    )


def register_daily_checkin_job() -> None:
    """앱 시작 시 한 번 호출해서, 매일 저녁 9시 체크인 알림을 스케줄러에 등록한다."""
    scheduler.add_job(
        send_daily_checkin_reminders,
        trigger="cron",
        hour=DAILY_CHECKIN_HOUR,
        minute=DAILY_CHECKIN_MINUTE,
        id=DAILY_CHECKIN_JOB_ID,
        replace_existing=True,
    )
