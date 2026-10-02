from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.clock import app_timezone

logger = logging.getLogger(__name__)

# 앱 전체에서 공유하는 단일 스케줄러 인스턴스. 알림 등 시각 기반 job은 이 객체에
# add_job으로 등록한다 (예: app/services/notification.py).
# cron job의 "자정", "21시"는 APP_TIMEZONE 기준이다. 시간대를 안 주면 서버(컨테이너는 보통 UTC) 시각으로 돈다.
scheduler = BackgroundScheduler(timezone=app_timezone())


def start_scheduler() -> None:
    if not scheduler.running:
        scheduler.start()
        logger.info("APScheduler started")


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler shut down")
