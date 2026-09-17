from __future__ import annotations

import logging

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.scheduler import scheduler
from app.models.user import User
from app.services.notification import send_push_notification

logger = logging.getLogger(__name__)

SLEEP_CHECKIN_JOB_ID = "daily_sleep_checkin"
SLEEP_CHECKIN_HOUR = 8
SLEEP_CHECKIN_MINUTE = 0

SLEEP_CHECKIN_TITLE = "[Schreduler] 수면 체크인"
SLEEP_CHECKIN_BODY = "어제 몇 시에 주무셨고 오늘 몇 시에 일어나셨나요?"


def _send_sleep_checkin_to_user(user: User) -> None:
    # User에 아직 fcm_token 필드가 없어서(디바이스 등록 전) 항상 None이다.
    # 필드가 추가되면 이 한 줄만 바뀌면 된다.
    device_token = getattr(user, "fcm_token", None)
    if not device_token:
        logger.info("[수면 체크인] device_token 없음 - 발송 생략. user_id=%s", user.id)
        return
    send_push_notification(device_token, SLEEP_CHECKIN_TITLE, SLEEP_CHECKIN_BODY)


def send_daily_sleep_checkin_reminders() -> None:
    """FR-7: 매일 아침 정해진 시각에 모든 사용자에게 수면 체크인 알림을 보낸다.

    실제 응답(취침/기상 시각 입력)은 클라이언트가 이 알림을 받고 나서
    POST /sleep-logs로 기록한다 — 이 함수는 "물어보는" 역할만 한다.
    """
    with SessionLocal() as db:
        users = db.execute(select(User)).scalars().all()
        for user in users:
            _send_sleep_checkin_to_user(user)


def register_sleep_checkin_job() -> None:
    """앱 시작 시 한 번 호출해서, 매일 정해진 시각에 수면 체크인 알림을 스케줄러에 등록한다."""
    scheduler.add_job(
        send_daily_sleep_checkin_reminders,
        trigger="cron",
        hour=SLEEP_CHECKIN_HOUR,
        minute=SLEEP_CHECKIN_MINUTE,
        id=SLEEP_CHECKIN_JOB_ID,
        replace_existing=True,
    )
