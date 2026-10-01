from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.config import settings

_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_WEEKDAY_KO = ("월", "화", "수", "목", "금", "토", "일")
_WEEK_LABELS = ("이번 주", "다음 주", "다다음 주")
CALENDAR_DAYS = 14


def app_timezone() -> ZoneInfo:
    return ZoneInfo(settings.app_timezone)


def local_now() -> datetime:
    """Timezone-aware current time in the app timezone."""
    return datetime.now(app_timezone())


def local_today() -> date:
    return local_now().date()


def local_wall_now() -> datetime:
    """Naive wall-clock time in the app timezone — the format every stored event, instance and action time uses."""
    return local_now().replace(tzinfo=None)


def utc_now_naive() -> datetime:
    """Naive UTC — the stored format of compliance_reports.created_at and engagement_states times."""
    return datetime.now(UTC).replace(tzinfo=None)


def week_label(day: date, today: date) -> str:
    """Weeks run Monday to Sunday: '이번 주' is the week holding today, '다음 주' the one after."""
    weeks_ahead = (day - timedelta(days=day.weekday()) - (today - timedelta(days=today.weekday()))).days // 7
    return _WEEK_LABELS[weeks_ahead] if 0 <= weeks_ahead < len(_WEEK_LABELS) else f"{weeks_ahead}주 뒤"


def calendar_table(today: date, days: int = CALENDAR_DAYS) -> list[str]:
    """One line per day from today, so the model picks '다음 주 화요일' from the table instead of computing it."""
    lines = []
    for offset in range(days):
        day = today + timedelta(days=offset)
        weekday = day.weekday()
        marker = " (오늘)" if offset == 0 else " (내일)" if offset == 1 else ""
        lines.append(
            f"{day.isoformat()} {_WEEKDAY_KO[weekday]} {_WEEKDAY_NAMES[weekday][:3]} {_WEEKDAY_CODES[weekday]}"
            f" | {week_label(day, today)}{marker}"
        )
    return lines


def current_time_block(now: datetime | None = None) -> str:
    """Prompt block that pins 'today', the weekday, the clock and a 14-day calendar so relative dates are looked
    up, not guessed. Changes every call, so it belongs after the cacheable prefix."""
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
            f"날짜표 (오늘부터 {CALENDAR_DAYS}일, 한 주는 월~일):",
            *calendar_table(current.date()),
        ]
    )
