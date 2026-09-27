from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.config import settings

_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def app_timezone() -> ZoneInfo:
    return ZoneInfo(settings.app_timezone)


def local_now() -> datetime:
    """Timezone-aware current time in the app timezone."""
    return datetime.now(app_timezone())


def current_time_block(now: datetime | None = None) -> str:
    """Prompt block that pins 'today', the weekday and the clock so relative dates are computed, not guessed.
    Changes every call, so it belongs after the cacheable prefix."""
    zone = app_timezone()
    current = now or local_now()
    current = current.astimezone(zone) if current.tzinfo else current.replace(tzinfo=zone)
    offset = current.strftime("%z")
    weekday = current.weekday()
    return "\n".join(
        [
            "[현재 시각]",
            f"오늘: {current.date().isoformat()} ({_WEEKDAY_NAMES[weekday]}, {_WEEKDAY_CODES[weekday]})",
            f"현재 시각: {current.strftime('%H:%M')}",
            f"시간대: {settings.app_timezone} (UTC{offset[:3]}:{offset[3:]})",
        ]
    )
