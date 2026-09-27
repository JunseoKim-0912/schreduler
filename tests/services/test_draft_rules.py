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


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (time(11), time(1), time(13)),  # "11:00-1:00" is 11 AM to 1 PM by default
        (time(12), time(1), time(13)),
        (time(9), time(12), time(12)),
        (time(22), time(1), time(1)),  # 1 PM would be before 10 PM, so it really crosses midnight
        (time(23), time(0, 30), time(0, 30)),
        (time(13), time(15), time(15)),
    ],
)
def test_infer_end_time(start: time, end: time, expected: time) -> None:
    assert rules.infer_end_time(start, end) == expected


def test_combine_times_rolls_past_midnight_to_next_day() -> None:
    day = date(2026, 10, 1)
    assert rules.combine_times(day, time(11), time(13)) == (datetime(2026, 10, 1, 11), datetime(2026, 10, 1, 13))
    assert rules.combine_times(day, time(23), time(0, 30)) == (datetime(2026, 10, 1, 23), datetime(2026, 10, 2, 0, 30))
    assert rules.combine_times(day, None, time(23, 59)) == (None, datetime(2026, 10, 1, 23, 59))


def test_minutes_helpers() -> None:
    assert rules.add_minutes("23:30", 45) == "00:15"
    assert rules.minutes_between("09:00", "10:30") == 90
    assert rules.minutes_between("23:00", "01:00") == 120


def test_normalize_deadline_times() -> None:
    assert rules.normalize_deadline_times("23:30", None) == (None, "23:30")
    assert rules.normalize_deadline_times("10:00", "23:59") == (None, "23:59")
    assert rules.normalize_deadline_times(None, None) == (None, None)


# --- recurrence ----------------------------------------------------------------------


def test_build_rrule_with_interval() -> None:
    assert rules.build_rrule("WEEKLY", ["TU"], 2) == "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU"
    assert rules.build_rrule("WEEKLY", ["MO", "WE"], 1) == "FREQ=WEEKLY;BYDAY=MO,WE"
    assert rules.build_rrule("DAILY", None, None) == "FREQ=DAILY"


@pytest.mark.parametrize(("frequency", "by_day"), [("HOURLY", None), ("WEEKLY", ["TUE"])])
def test_build_rrule_rejects_unknown_values(frequency: str, by_day: list[str] | None) -> None:
    with pytest.raises(DraftRuleError):
        rules.build_rrule(frequency, by_day)


def test_first_occurrence_and_preview_keep_biweekly_rhythm_from_dtstart() -> None:
    rule = rules.build_rrule("WEEKLY", ["TU"], 2)
    range_start, range_end = date(2026, 9, 3), date(2026, 12, 8)

    # no start date given: the first Tuesday after the range start (Thu 9/3) is the first occurrence
    first = rules.first_occurrence(rule, time(9), range_start, range_end)
    assert first == date(2026, 9, 8)
    assert rules.preview_dates(rule, datetime.combine(first, time(9)), range_start, range_end) == [
        date(2026, 9, 8),
        date(2026, 9, 22),
        date(2026, 10, 6),
    ]
    dtstart = datetime(2026, 9, 22, 9)  # "9/22부터 격주"
    assert rules.preview_dates(rule, dtstart, range_start, range_end) == [
        date(2026, 9, 22),
        date(2026, 10, 6),
        date(2026, 10, 20),
    ]


def test_first_occurrence_falls_back_to_range_start_when_no_date_matches() -> None:
    rule = rules.build_rrule("WEEKLY", ["SA"])
    assert rules.first_occurrence(rule, time(9), date(2026, 9, 28), date(2026, 9, 30)) == date(2026, 9, 28)


def test_first_matching_day_and_weekday_mismatch() -> None:
    wednesday = date(2026, 9, 23)
    assert rules.first_matching_day(wednesday, ["TU"], "WEEKLY") == date(2026, 9, 29)
    assert rules.first_matching_day(wednesday, None, "DAILY") == wednesday
    assert rules.start_weekday_mismatch(wednesday, "WEEKLY", ["TU"]) is True
    assert rules.start_weekday_mismatch(date(2026, 9, 22), "WEEKLY", ["TU"]) is False
    assert rules.start_weekday_mismatch(wednesday, "DAILY", None) is False


# --- display -------------------------------------------------------------------------


def test_format_time_range_korean() -> None:
    assert rules.format_time_range(datetime(2026, 10, 1, 11), datetime(2026, 10, 1, 13), "ko") == "오전 11:00 – 오후 1:00 (2시간)"
    assert (
        rules.format_time_range(datetime(2026, 10, 1, 23), datetime(2026, 10, 2, 1, 30), "ko")
        == "오후 11:00 – 오전 1:30 (다음 날) (2시간 30분)"
    )
    assert rules.format_time_range(datetime(2026, 10, 1, 12), datetime(2026, 10, 1, 12, 45), "ko") == "오후 12:00 – 오후 12:45 (45분)"
    assert rules.format_time_range(None, datetime(2026, 9, 29, 23, 30), "ko") == "오후 11:30 마감"


def test_format_time_range_english() -> None:
    assert rules.format_time_range(datetime(2026, 10, 1, 0, 15), datetime(2026, 10, 1, 1, 15), "en") == "12:15 AM – 1:15 AM (1h)"
    assert (
        rules.format_time_range(datetime(2026, 10, 1, 23), datetime(2026, 10, 2, 1), "en")
        == "11:00 PM – 1:00 AM (next day) (2h)"
    )
    assert rules.format_time_range(None, datetime(2026, 9, 29, 23, 59), "en") == "Due 11:59 PM"


# --- validate_draft ------------------------------------------------------------------


def _spec(start: datetime | None, end: datetime, **extra: object) -> DraftSpec:
    return DraftSpec(start=start, end=end, **extra)


def test_eleven_to_one_is_a_clean_daytime_draft() -> None:
    day = date(2026, 10, 1)
    start, end = rules.combine_times(day, time(11), rules.infer_end_time(time(11), time(1)))

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
