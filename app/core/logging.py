"""앱 로깅 설정. uvicorn은 자기 로거만 설정하므로, app.* 로거는 여기서 따로 핸들러를 붙여야 INFO 로그가 보인다.

propagate는 켜 둔다 — 루트 로거에는 핸들러가 없어 중복 출력되지 않고, pytest caplog(루트에 붙음)가 계속 로그를 받는다.
"""

from __future__ import annotations

import logging.config

from app.core.config import settings

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging() -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"default": {"format": LOG_FORMAT}},
            "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "default"}},
            "loggers": {
                "app": {"handlers": ["console"], "level": settings.log_level},
                "apscheduler": {"handlers": ["console"], "level": "WARNING"},
            },
        }
    )
