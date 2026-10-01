"""Pure rules for event drafts: date/time interpretation, RRULE assembly, previews, display strings and validation.

The assistant tools (app/services/assistant) and the services they call share these functions so each rule
exists exactly once. Nothing here touches the DB or the LLM.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Literal

from dateutil.rrule import rrulestr
from pydantic import BaseModel, Field

from app.i18n import render_message

WEEKDAY_CODES: tuple[str, ...] = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
FREQUENCIES: tuple[str, ...] = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
PREVIEW_COUNT = 3
LONG_EVENT = timedelta(hours=12)

_DATE_VALUE = re.compile(r"(?:(\d{4})-)?(\d{1,2})-(\d{1,2})")


class DraftRuleError(ValueError):
    """A draft value that cannot be interpreted (bad date/time string, unbuildable RRULE)."""


# --- dates and times -----------------------------------------------------------------


def parse_hhmm(value: str) -> time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise DraftRuleError(f"not an HH:MM time: {value!r}") from exc


def resolve_event_date(value: str, today: date) -> date:
    """'YYYY-MM-DD' as is; a year-less 'MM-DD' becomes the nearest such date on or after today."""
    match = _DATE_VALUE.fullmatch(value.strip())
    if match is None:
        raise DraftRuleError(f"not a YYYY-MM-DD/MM-DD date: {value!r}")
    year, month, day = match.groups()
    try:
        if year:
            return date(int(year), int(month), int(day))
        for candidate_year in range(today.year, today.year + 5):  # Feb 29 waits for the next leap year
            try:
                candidate = date(candidate_year, int(month), int(day))
            except ValueError:
                continue
            if candidate >= today:
                return candidate
    except ValueError:
        pass
    raise DraftRuleError(f"not a valid date: {value!r}")


def end_not_after_start(start: time, end: time) -> bool:
    return end <= start


def combine_times(day: date, start: time | None, end: time) -> tuple[datetime | None, datetime]:
    """Start/end datetimes on the given day. An end at or before the start (23:00-00:30) ends the next day."""
    if start is None:
        return None, datetime.combine(day, end)
    start_at, end_at = datetime.combine(day, start), datetime.combine(day, end)
    if end_at <= start_at:
        end_at += timedelta(days=1)
    return start_at, end_at


def normalize_deadline_times(start_time: str | None, end_time: str | None) -> tuple[None, str | None]:
    """A deadline has no start; a time given only as the start is the due time."""
    return None, end_time or start_time


# --- recurrence ----------------------------------------------------------------------


def build_rrule(frequency: str, by_day: list[str] | None, interval: int | None = 1) -> str:
    """e.g. build_rrule("WEEKLY", ["TU"], 2) -> "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU"."""
    if frequency not in FREQUENCIES:
        raise DraftRuleError(f"unknown frequency: {frequency!r}")
    unknown_days = [code for code in by_day or [] if code not in WEEKDAY_CODES]
    if unknown_days:
        raise DraftRuleError(f"unknown weekday codes: {unknown_days!r}")
    parts = [f"FREQ={frequency}"]
    if interval and interval > 1:
        parts.append(f"INTERVAL={interval}")
    if by_day:
        parts.append(f"BYDAY={','.join(by_day)}")
    return ";".join(parts)


def occurrences(rule: str, dtstart: datetime, start: date, end: date) -> list[date]:
    """Occurrence dates within [start, end] and not before dtstart. Every recurrence computation uses this.

    dtstart is the event's own first datetime: the INTERVAL=2 'every other week' rhythm is anchored on dtstart's
    week, so using the range start instead would shift the rhythm whenever the range is stretched or shrunk.
    """
    window_start = max(start, dtstart.date())
    if window_start > end:
        return []
    parsed = rrulestr(rule, dtstart=dtstart)
    found = parsed.between(datetime.combine(window_start, time.min), datetime.combine(end, time.max), inc=True)
    return sorted({occurrence.date() for occurrence in found})


def first_occurrence(rule: str, clock: time, range_start: date, range_end: date) -> date:
    """dtstart when no start date was given: the first matching day inside the range, else the range start.

    INTERVAL is dropped for this search: with the range start as dtstart, an every-other-week rule counts weeks
    from the range start's week, so a range starting Thu 9/3 would skip Tue 9/8 and land on 9/15. The first
    matching day then becomes dtstart, which anchors the rhythm on the first real occurrence.
    """
    every_period = ";".join(part for part in rule.split(";") if not part.startswith("INTERVAL="))
    candidates = occurrences(every_period, datetime.combine(range_start, clock), range_start, range_end)
    return candidates[0] if candidates else range_start


def upcoming_occurrences(rule: str, dtstart: datetime, range_start: date, range_end: date, today: date) -> list[date]:
    """Occurrences that become EventInstances: inside the range and on or after today.

    dtstart only anchors the rhythm (the date the user said, or the first matching day after the range start);
    dates before today are never materialized, so '9/22부터 격주 화요일' said on 9/27 yields 10/6, 10/20, ...
    """
    return occurrences(rule, dtstart, max(range_start, today), range_end)


def preview_dates(rule: str, dtstart: datetime, range_start: date, range_end: date, today: date) -> list[date]:
    return upcoming_occurrences(rule, dtstart, range_start, range_end, today)[:PREVIEW_COUNT]


def first_matching_day(day: date, by_day: list[str] | None, frequency: str | None) -> date:
    """The first day on or after `day` that falls on one of the weekly repeat days."""
    if frequency == "WEEKLY" and by_day:
        while WEEKDAY_CODES[day.weekday()] not in by_day:
            day += timedelta(days=1)
    return day


def start_weekday_mismatch(start: date, frequency: str | None, by_day: list[str] | None) -> bool:
    return frequency == "WEEKLY" and bool(by_day) and WEEKDAY_CODES[start.weekday()] not in by_day


# --- display -------------------------------------------------------------------------


def format_clock(value: time, language: str) -> str:
    hour = value.hour % 12 or 12
    key = "time.clock_pm" if value.hour >= 12 else "time.clock_am"
    return render_message(key, language, hour=hour, minute=f"{value.minute:02d}")


def format_duration(delta: timedelta, language: str) -> str:
    hours, minutes = divmod(int(delta.total_seconds() // 60), 60)
    if hours and minutes:
        return render_message("time.hours_minutes", language, hours=hours, minutes=minutes)
    if hours:
        return render_message("time.hours", language, hours=hours)
    return render_message("time.minutes", language, minutes=minutes)


def format_time_range(start: datetime | None, end: datetime, language: str) -> str:
    """'오전 11:00 – 오후 1:00 (2시간)', '오후 11:00 – 오전 1:00 (다음 날) (2시간)', or '오후 11:59 마감' for a deadline."""
    if start is None:
        return render_message("time.deadline", language, time=format_clock(end.time(), language))
    params = {
        "start": format_clock(start.time(), language),
        "end": format_clock(end.time(), language),
        "duration": format_duration(end - start, language),
    }
    days_later = (end.date() - start.date()).days
    if days_later == 0:
        return render_message("time.range", language, **params)
    if days_later == 1:
        return render_message("time.range_next_day", language, **params)
    return render_message("time.range_days_later", language, days=days_later, **params)


# --- validation ----------------------------------------------------------------------

DraftErrorCode = Literal["end_not_after_start", "deadline_has_start_time", "invalid_recurrence"]
DraftWarningCode = Literal["crosses_midnight", "over_12_hours", "past_date", "start_weekday_mismatch"]


class DraftSpec(BaseModel):
    """What validate_draft needs from any draft. Datetimes are wall-clock in the app timezone."""

    event_type: Literal["scheduled", "deadline"] = "scheduled"
    start: datetime | None
    end: datetime
    frequency: str | None = None
    by_day: list[str] | None = None
    interval: int | None = None
    recurrence_start: date | None = None


class DraftCheck(BaseModel):
    errors: list[DraftErrorCode] = Field(default_factory=list)
    warnings: list[DraftWarningCode] = Field(default_factory=list)
    recurrence_rule: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_draft(spec: DraftSpec, now: datetime) -> DraftCheck:
    """Errors reject the draft; warnings are shown on the confirmation card for the user to accept."""
    check = DraftCheck()
    now = now.replace(tzinfo=None)
    deadline = spec.event_type == "deadline"

    if deadline and spec.start is not None:
        check.errors.append("deadline_has_start_time")
    elif not deadline:
        if spec.start is None or spec.end <= spec.start:
            check.errors.append("end_not_after_start")
        else:
            if spec.end.date() > spec.start.date():
                check.warnings.append("crosses_midnight")
            if spec.end - spec.start > LONG_EVENT:
                check.warnings.append("over_12_hours")

    if spec.frequency is not None:
        try:
            if spec.interval is not None and spec.interval < 1:
                raise DraftRuleError(f"interval must be at least 1: {spec.interval}")
            check.recurrence_rule = build_rrule(spec.frequency, spec.by_day, spec.interval)
        except DraftRuleError:
            check.errors.append("invalid_recurrence")

    anchor = spec.start if spec.start is not None and not deadline else spec.end
    first_day = spec.recurrence_start or anchor.date()
    # A repeating event only gets occurrences from today on (upcoming_occurrences), so a past rhythm start is fine.
    if spec.frequency is None and datetime.combine(first_day, anchor.time()) < now:
        check.warnings.append("past_date")
    if spec.frequency is not None and start_weekday_mismatch(first_day, spec.frequency, spec.by_day):
        check.warnings.append("start_weekday_mismatch")
    return check
