from datetime import datetime, timezone

import pytest

from app.core import clock
from app.core.config import settings


def test_default_timezone_is_toronto() -> None:
    assert settings.app_timezone == "America/Toronto"


def test_current_time_block_converts_to_app_timezone() -> None:
    now = datetime(2026, 9, 27, 18, 5, tzinfo=timezone.utc)

    assert clock.current_time_block(now) == (
        "[현재 시각]\n"
        "오늘: 2026-09-27 (Sunday, SU)\n"
        "현재 시각: 14:05\n"
        "시간대: America/Toronto (UTC-04:00)"
    )


def test_current_time_block_crosses_date_line_and_winter_offset() -> None:
    now = datetime(2026, 12, 1, 3, 30, tzinfo=timezone.utc)

    block = clock.current_time_block(now)

    assert "오늘: 2026-11-30 (Monday, MO)" in block
    assert "현재 시각: 22:30" in block
    assert "UTC-05:00" in block


def test_naive_now_is_taken_as_app_local_time() -> None:
    assert "현재 시각: 09:00" in clock.current_time_block(datetime(2026, 9, 28, 9, 0))


def test_timezone_setting_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_timezone", "Asia/Seoul")

    block = clock.current_time_block(datetime(2026, 9, 27, 18, 5, tzinfo=timezone.utc))

    assert "오늘: 2026-09-28 (Monday, MO)" in block
    assert "시간대: Asia/Seoul (UTC+09:00)" in block
    assert clock.local_now().utcoffset().total_seconds() == 9 * 3600
