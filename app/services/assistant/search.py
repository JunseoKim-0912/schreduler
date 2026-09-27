"""search_events: 기존 매칭(match_events/similar_events)과 회차 조회(list_instances_in_range)를 감싼다.
돌려준 ID는 세션에 기억해 두고, 제안 도구는 그 ID만 받는다."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.core.exceptions import InvalidInputError
from app.models.enums import EventInstanceStatus
from app.models.event import Event
from app.services import date_range_command_service as ranges
from app.services import draft_rules as rules
from app.services.assistant.context import TurnContext
from app.services.event_command_service import match_events, similar_events
from app.services.event_instance_service import MAX_RANGE_DAYS, list_instances_in_range
from app.schemas.event_instance import EventInstanceRead

MAX_RESULTS = 20
DEFAULT_WINDOW_DAYS = 14


def _clock_text(event: Event) -> tuple[str | None, str]:
    start = event.start_time.strftime("%H:%M") if event.start_time else None
    return start, event.end_time.strftime("%H:%M")


def _weekdays(event: Event) -> set[str]:
    rule = event.recurrence_rule or ""
    for part in rule.split(";"):
        if part.startswith("BYDAY="):
            return set(part.removeprefix("BYDAY=").split(","))
    return {rules.WEEKDAY_CODES[event.anchor_time.weekday()]}


def _event_row(ctx: TurnContext, event: Event) -> dict[str, Any]:
    active = sorted(i.date for i in event.instances if i.status != EventInstanceStatus.CANCELLED)
    upcoming = [d for d in active if d >= ctx.today]
    start, end = _clock_text(event)
    return {
        "event_id": event.id,
        "instance_id": None,
        "title": event.title,
        "event_type": event.event_type.value,
        "recurring": event.is_recurring or len(active) > 1,
        "recurrence_rule": event.recurrence_rule,
        "date_range": event.date_range.name if event.date_range else None,
        "first_date": active[0].isoformat() if active else event.anchor_time.date().isoformat(),
        "next_date": upcoming[0].isoformat() if upcoming else None,
        "start": start,
        "end": end,
        "time_display": rules.format_time_range(event.start_time, event.end_time, ctx.language),
        "importance": event.importance.value if event.importance is not None else None,
        "location": event.location.name if event.location else None,
    }


def _instance_row(ctx: TurnContext, item: EventInstanceRead) -> dict[str, Any]:
    return {
        "event_id": item.event_id,
        "instance_id": item.event_instance_id,
        "title": item.title,
        "event_type": item.event_type.value,
        "date": item.date.isoformat(),
        "weekday": rules.WEEKDAY_CODES[item.date.weekday()],
        "recurring": item.is_recurring,
        "start": item.start_time.strftime("%H:%M") if item.start_time else None,
        "end": item.end_time.strftime("%H:%M"),
        "time_display": rules.format_time_range(item.start_time, item.end_time, ctx.language),
        "location": item.location_name,
    }


def _day(value: str | None, ctx: TurnContext) -> date | None:
    return ranges.parse_day(value, ctx.today.year) if value else None


def search_events(ctx: TurnContext, args: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip() or None
    weekday = args.get("weekday")
    if not any((query, args.get("date_from"), args.get("date_to"), weekday)):
        return {"errors": [{"code": "empty_search", "message": "give query, date_from/date_to or weekday"}], "results": []}
    try:
        date_from, date_to = _day(args.get("date_from"), ctx), _day(args.get("date_to"), ctx)
    except InvalidInputError as exc:
        return {"errors": [{"code": "invalid_date", "message": str(exc)}], "results": []}

    matched = match_events(ctx.db, ctx.user.id, query) if query else None
    similar = similar_events(ctx.db, ctx.user.id, query) if query and not matched else []
    by_dates = date_from is not None or date_to is not None or (weekday and not query)

    if by_dates:
        start = date_from or ctx.today
        end = date_to or (start if date_from and not weekday else start + timedelta(days=DEFAULT_WINDOW_DAYS - 1))
        if end < start or (end - start).days > MAX_RANGE_DAYS:
            return {"errors": [{"code": "invalid_range", "message": f"date_to must be within {MAX_RANGE_DAYS} days after date_from"}], "results": []}
        allowed = {e.id for e in matched} if matched is not None else None
        rows = [
            item
            for item in list_instances_in_range(ctx.db, ctx.user.id, start, end)
            if item.parent_event_id is None
            and (allowed is None or item.event_id in allowed)
            and (not weekday or rules.WEEKDAY_CODES[item.date.weekday()] == weekday)
        ]
        results = [_instance_row(ctx, item) for item in rows]
    else:
        events = [e for e in matched or [] if not weekday or weekday in _weekdays(e)]
        results = [_event_row(ctx, e) for e in events]

    truncated = len(results) > MAX_RESULTS
    results = results[:MAX_RESULTS]
    similar_rows = [_event_row(ctx, e) for e in similar]
    ctx.remember_ids(
        [r["event_id"] for r in [*results, *similar_rows]],
        [r["instance_id"] for r in results if r["instance_id"] is not None],
    )
    response: dict[str, Any] = {"results": results, "count": len(results), "truncated": truncated}
    if similar_rows:
        response["similar"] = similar_rows
    if not results:
        response["note"] = "No match. Check spelling with the user or look at `similar`."
    return response
