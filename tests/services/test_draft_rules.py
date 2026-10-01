from datetime import date, datetime, time, timedelta

import pytest

from app.services import draft_rules as rules
from app.services.draft_rules import DraftRuleError, DraftSpec, validate_draft

NOW = datetime(2026, 9, 27, 14, 0)  # Sunday


# --- dates ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2027-01-05", date(2027, 1, 5)),
        ("10-01", date(2026, 10, 1)),
        ("9-27", date(2026, 9, 27)),  # today still counts
        ("09-26", date(2027, 9, 26)),  # already past this year -> next year
        ("02-29", date(2028, 2, 29)),  # waits for the next leap year
    ],
)
def test_resolve_event_date(value: str, expected: date) -> None:
    assert rules.resolve_event_date(value, NOW.date()) == expected


@pytest.mark.parametrize("value", ["tomorrow", "13-01", "2026-02-30", ""])
def test_resolve_event_date_rejects_garbage(value: str) -> None:
    with pytest.raises(DraftRuleError):
        rules.resolve_event_date(value, NOW.date())


def test_parse_hhmm() -> None:
    assert rules.parse_hhmm("09:05") == time(9, 5)
    with pytest.raises(DraftRuleError):
        rules.parse_hhmm("9pm")


# --- times ---------------------------------------------------------------------------


def test_combine_times_rolls_past_midnight_to_next_day() -> None:
    day = date(2026, 10, 1)
    assert rules.combine_times(day, time(11), time(13)) == (datetime(2026, 10, 1, 11), datetime(2026, 10, 1, 13))
    assert rules.combine_times(day, time(23), time(0, 30)) == (datetime(2026, 10, 1, 23), datetime(2026, 10, 2, 0, 30))
    assert rules.combine_times(day, None, time(23, 59)) == (None, datetime(2026, 10, 1, 23, 59))


def _spec(start: datetime | None, end: datetime, **extra: object) -> DraftSpec:
    return DraftSpec(start=start, end=end, **extra)


def test_eleven_to_one_is_a_clean_daytime_draft() -> None:
    day = date(2026, 10, 1)
    start, end = rules.combine_times(day, time(11), time(13))

    check = validate_draft(_spec(start, end), NOW)

    assert (start, end) == (datetime(2026, 10, 1, 11), datetime(2026, 10, 1, 13))
    assert check.ok
    assert check.errors == [] and check.warnings == []


def test_eleven_am_to_one_am_warns() -> None:
    check = validate_draft(_spec(datetime(2026, 10, 1, 11), datetime(2026, 10, 2, 1)), NOW)

    assert check.ok
    assert check.warnings == ["crosses_midnight", "over_12_hours"]


def test_short_overnight_event_only_warns_about_midnight() -> None:
    check = validate_draft(_spec(datetime(2026, 10, 1, 23), datetime(2026, 10, 2, 0, 30)), NOW)
    assert check.warnings == ["crosses_midnight"]


@pytest.mark.parametrize("end", [datetime(2026, 10, 1, 11), datetime(2026, 10, 1, 9)])
def test_end_not_after_start_is_an_error(end: datetime) -> None:
    check = validate_draft(_spec(datetime(2026, 10, 1, 11), end), NOW)

    assert not check.ok
    assert check.errors == ["end_not_after_start"]


def test_scheduled_without_start_is_an_error() -> None:
    assert validate_draft(_spec(None, datetime(2026, 10, 1, 11)), NOW).errors == ["end_not_after_start"]


def test_deadline_rules() -> None:
    due = datetime(2026, 9, 29, 23, 30)
    assert validate_draft(_spec(None, due, event_type="deadline"), NOW).ok
    check = validate_draft(_spec(datetime(2026, 9, 29, 22), due, event_type="deadline"), NOW)
    assert check.errors == ["deadline_has_start_time"]


@pytest.mark.parametrize(
    ("frequency", "by_day", "interval"),
    [("HOURLY", None, None), ("WEEKLY", ["TUE"], None), ("WEEKLY", ["TU"], 0)],
)
def test_unbuildable_recurrence_is_an_error(frequency: str, by_day: list[str] | None, interval: int | None) -> None:
    spec = _spec(datetime(2026, 9, 29, 9), datetime(2026, 9, 29, 12), frequency=frequency, by_day=by_day, interval=interval)

    check = validate_draft(spec, NOW)

    assert check.errors == ["invalid_recurrence"]
    assert check.recurrence_rule is None


def test_valid_recurrence_returns_rule() -> None:
    spec = _spec(
        datetime(2026, 9, 29, 9), datetime(2026, 9, 29, 12), frequency="WEEKLY", by_day=["TU"], interval=2,
        recurrence_start=date(2026, 9, 29),
    )

    check = validate_draft(spec, NOW)

    assert check.ok and check.warnings == []
    assert check.recurrence_rule == "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU"


def test_start_date_on_wrong_weekday_warns() -> None:
    spec = _spec(
        datetime(2026, 9, 30, 9), datetime(2026, 9, 30, 12), frequency="WEEKLY", by_day=["TU"],
        recurrence_start=date(2026, 9, 30),
    )
    assert validate_draft(spec, NOW).warnings == ["start_weekday_mismatch"]


@pytest.mark.parametrize(
    ("start", "event_type", "warned"),
    [
        (NOW - timedelta(days=1), "scheduled", True),
        (NOW - timedelta(hours=2), "scheduled", True),  # earlier today already passed
        (NOW + timedelta(hours=1), "scheduled", False),
        (NOW - timedelta(days=1), "deadline", True),
    ],
)
def test_past_date_warns(start: datetime, event_type: str, warned: bool) -> None:
    if event_type == "deadline":
        spec = _spec(None, start, event_type="deadline")
    else:
        spec = _spec(start, start + timedelta(hours=1))

    assert ("past_date" in validate_draft(spec, NOW).warnings) is warned


def test_aware_now_is_compared_as_wall_clock() -> None:
    from zoneinfo import ZoneInfo

    now = NOW.replace(tzinfo=ZoneInfo("America/Toronto"))
    assert validate_draft(_spec(NOW + timedelta(hours=1), NOW + timedelta(hours=2)), now).warnings == []
