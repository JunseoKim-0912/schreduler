from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Literal

import firebase_admin
from apscheduler.jobstores.base import JobLookupError
from firebase_admin import credentials, messaging
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.scheduler import scheduler
from app.i18n import render_notification
from app.models.enums import EventInstanceStatus, EventType
from app.models.event import Event
from app.models.event_instance import EventInstance

logger = logging.getLogger(__name__)

NotificationKind = Literal["start", "end", "deadline_reminder"]
NOTIFICATION_KINDS: tuple[NotificationKind, ...] = ("start", "end", "deadline_reminder")

DEADLINE_REMINDER_OFFSET = timedelta(minutes=1440)


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


def _send_notification(event_instance_id: int, kind: NotificationKind) -> None:
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

        if instance.status == EventInstanceStatus.CANCELLED:
            logger.info("[알림][%s] 취소된 회차라 발송 생략. event_instance_id=%s", kind, event_instance_id)
            return

        event = instance.event
        language = event.user.preferred_language
        if kind == "start":
            title = render_notification("event_start.title", language, title=event.title)
            body = render_notification("event_start.body", language)
        elif kind == "deadline_reminder":
            title = render_notification("deadline_reminder.title", language, title=event.title)
            body = render_notification("deadline_reminder.body", language)
        else:
            title = render_notification("event_end.title", language, title=event.title)
            body = render_notification("event_end.body", language)

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
    """EventInstance의 알림 job을 등록한다 (FR-4).

    SCHEDULED는 시작/종료 알림, DEADLINE은 마감 1일 전 리마인더와 마감 시각 완료 확인
    알림을 등록한다. 이벤트의 시각(time-of-day)을 instance.date와 합쳐 실제 발송 시각을
    계산한다. 같은 instance로 다시 호출해도 job id가
    같아서(replace_existing=True) 중복 등록되지 않는다.
    """
    if instance.status == EventInstanceStatus.CANCELLED:
        return
    for kind, run_date in _notification_times(instance):
        _add_notification_job(instance.id, kind, run_date)


def _notification_times(instance: EventInstance) -> list[tuple[NotificationKind, datetime]]:
    end_at = instance.effective_end
    if instance.event.event_type == EventType.DEADLINE:
        # 마감 이벤트는 시작 시각이 없으므로 시작 알림 대신 마감 하루 전 리마인더를 보낸다.
        return [("deadline_reminder", end_at - DEADLINE_REMINDER_OFFSET), ("end", end_at)]
    return [("start", instance.effective_start), ("end", end_at)]


def _job_id(event_instance_id: int, kind: NotificationKind) -> str:
    return f"event_instance_{event_instance_id}_{kind}"


def remove_instance_notifications(event_instance_id: int) -> None:
    for kind in NOTIFICATION_KINDS:
        try:
            scheduler.remove_job(_job_id(event_instance_id, kind))
        except JobLookupError:
            pass


# 알림 job은 DB에서 파생되는 상태다. 메모리 job 저장소라 서버를 재시작하면 사라지므로 시작할 때
# register_upcoming_notifications로 다시 만들고, 일정이 바뀔 때마다(커밋 뒤) sync_*로 DB에 맞춘다.
# 스케줄러가 돌고 있지 않으면(스크립트·테스트) 등록할 곳이 없으므로 아무것도 하지 않는다.
def sync_instance_notifications(instance: EventInstance, now: datetime | None = None) -> None:
    """회차의 알림 job을 지금 상태에 맞춘다: 지우고, 대기 중인 회차면 아직 지나지 않은 알림만 다시 등록한다."""
    if not scheduler.running:
        return
    remove_instance_notifications(instance.id)
    if instance.status != EventInstanceStatus.PENDING:
        return
    now = now or datetime.now()
    for kind, run_date in _notification_times(instance):
        if run_date > now:
            _add_notification_job(instance.id, kind, run_date)


def sync_notifications(
    db: Session, *, event_ids: Iterable[int] = (), instance_ids: Iterable[int] = (), now: datetime | None = None
) -> None:
    """커밋 뒤에 부른다. 이벤트는 하위 일정까지 모든 회차를, instance_ids는 그 회차들을 다시 맞춘다.
    DB에서 사라진 회차(삭제·생성 되돌리기)는 job만 지운다."""
    if not scheduler.running:
        return
    instances: dict[int, EventInstance] = {}
    event_ids = list(event_ids)
    if event_ids:
        for event in db.execute(select(Event).where(Event.id.in_(event_ids))).scalars():
            for item in [event, *event.child_events]:
                instances.update((i.id, i) for i in item.instances)
    missing = set(instance_ids) - instances.keys()
    if missing:
        instances.update((i.id, i) for i in db.execute(select(EventInstance).where(EventInstance.id.in_(missing))).scalars())
    for instance in instances.values():
        sync_instance_notifications(instance, now)
    for instance_id in missing - instances.keys():
        remove_instance_notifications(instance_id)


def register_upcoming_notifications(today: date | None = None) -> int:
    """서버 시작 시 대기 중인 회차들의 알림 job을 다시 등록하고, 등록한 회차 수를 돌려준다.
    어제 날짜부터 본다 — 전날 시작해 자정을 넘기는 일정의 종료 알림이 남아 있을 수 있다."""
    since = (today or date.today()) - timedelta(days=1)
    try:
        with SessionLocal() as db:
            instances = db.execute(
                select(EventInstance).where(EventInstance.status == EventInstanceStatus.PENDING, EventInstance.date >= since)
            ).scalars().all()
            for instance in instances:
                sync_instance_notifications(instance)
    except SQLAlchemyError:
        logger.exception("[알림] 시작 시 알림 job 재등록 실패 — 마이그레이션을 확인하세요")
        return 0
    logger.info("[알림] 대기 중인 회차 %d개의 알림 job을 등록했습니다", len(instances))
    return len(instances)


def _add_notification_job(event_instance_id: int, kind: NotificationKind, run_date: datetime) -> None:
    scheduler.add_job(
        _send_notification,
        trigger="date",
        run_date=run_date,
        args=[event_instance_id, kind],
        id=_job_id(event_instance_id, kind),
        replace_existing=True,
    )
