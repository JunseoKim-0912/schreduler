from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

# 앱 전체에서 공유하는 단일 스케줄러 인스턴스. 알림 등 시각 기반 job은 이 객체에
# add_job으로 등록한다 (예: app/services/notification.py).
scheduler = BackgroundScheduler()


def start_scheduler() -> None:
    if not scheduler.running:
        scheduler.start()
        logger.info("APScheduler started")


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler shut down")
