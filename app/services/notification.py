from __future__ import annotations

import logging
from datetime import datetime

import firebase_admin
from firebase_admin import credentials, messaging

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.scheduler import scheduler
from app.models.event_instance import EventInstance

logger = logging.getLogger(__name__)


def _get_firebase_app() -> firebase_admin.App | None:
    """이미 초기화된 기본 Firebase 앱이 있으면 재사용하고, 없으면 자격증명으로
    새로 초기화한다. 자격증명이 없거나 초기화에 실패하면 None을 반환한다 —
    호출부는 이 경우 실제 발송 없이 로그만 남기고 넘어가야 한다.
    """
    try:
        return firebase_admin.get_app()
    except ValueError:
        pass  # 아직 초기화 안 됨

    if not settings.firebase_credentials_path:
        logger.warning(
            "FIREBASE_CREDENTIALS_PATH가 설정되지 않아 FCM 발송을 건너뜁니다 (.env 확인)"
        )
        return None

    try:
        cred = credentials.Certificate(settings.firebase_credentials_path)
        return firebase_admin.initialize_app(cred)
    except Exception:
        logger.exception("Firebase Admin SDK 초기화에 실패했습니다")
        return None


def send_push_notification(device_token: str, title: str, body: str) -> None:
    """FCM으로 푸시 알림을 보낸다 (FR-4).

    Firebase 자격증명이 없거나, 토큰이 유효하지 않거나, 네트워크 문제가 있어도
    이 함수는 예외를 올리지 않는다 — 알림 발송 실패가 API 요청이나 스케줄러
    스레드를 죽이면 안 되기 때문이다. 대신 무슨 일이 있었는지 로그로 남긴다.
    """
    app = _get_firebase_app()
    if app is None:
        logger.info(
            "[FCM 발송 생략] token=%s title=%r body=%r (Firebase 미설정)",
            device_token,
            title,
            body,
        )
        return

    message = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        fid=device_token,
    )
    try:
        message_id = messaging.send(message, app=app)
    except Exception as exc:  # firebase_admin.exceptions.FirebaseError 등
        logger.warning("[FCM 발송 실패] token=%s error=%s", device_token, exc)
        return

    logger.info("[FCM 발송 성공] token=%s message_id=%s", device_token, message_id)


def _send_notification(event_instance_id: int, kind: str) -> None:
    """스케줄된 job이 실제로 실행될 때 호출된다.

    job이 실행되는 시점에 DB에서 다시 조회한다 — 스케줄을 등록한 시점과 실제
    발송 시점 사이에 이벤트가 수정/삭제됐을 수 있기 때문이다.
    """
    with SessionLocal() as db:
        instance = db.get(EventInstance, event_instance_id)
        if instance is None:
            logger.warning(
                "알림 대상 EventInstance(id=%s)를 찾을 수 없어 건너뜁니다", event_instance_id
            )
            return

        event = instance.event
        title = f"[Schreduler] {event.title}"
        body = "지금 시작할 시간이에요." if kind == "start" else "종료 시각이에요, 완료 체크 해주세요."

        # User에 아직 fcm_token 필드가 없어서(디바이스 등록 전) 항상 None이다.
        # 필드가 추가되면 이 한 줄만 바뀌면 된다.
        device_token = getattr(event.user, "fcm_token", None)
        if not device_token:
            logger.info(
                "[알림][%s] device_token 없음 - 발송 생략. user_id=%s event=%r date=%s",
                kind,
                event.user_id,
                event.title,
                instance.date,
            )
            return

        send_push_notification(device_token, title, body)


def schedule_event_instance_notifications(instance: EventInstance) -> None:
    """EventInstance의 시작 시각과 종료 시각에 각각 알림 job을 등록한다 (FR-4).

    instance.event.start_time/end_time의 시각(time-of-day)을 instance.date와
    합쳐 실제 발송 시각을 계산한다. 같은 instance로 다시 호출해도 job id가
    같아서(replace_existing=True) 중복 등록되지 않는다.
    """
    event = instance.event
    start_at = datetime.combine(instance.date, event.start_time.time())
    end_at = datetime.combine(instance.date, event.end_time.time())

    scheduler.add_job(
        _send_notification,
        trigger="date",
        run_date=start_at,
        args=[instance.id, "start"],
        id=f"event_instance_{instance.id}_start",
        replace_existing=True,
    )
    scheduler.add_job(
        _send_notification,
        trigger="date",
        run_date=end_at,
        args=[instance.id, "end"],
        id=f"event_instance_{instance.id}_end",
        replace_existing=True,
    )
