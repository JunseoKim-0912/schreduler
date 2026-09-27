"""제안 도구의 본체: LLM이 준 값을 실제 데이터와 맞추고 draft_rules로 검증·정규화해 초안을 만든다. DB에 쓰지 않는다.

모든 함수는 {"ok", "draft", "errors", "warnings", "preview_dates", "time_display", "inferred_fields"}를 돌려준다.
errors는 LLM이 고쳐서 다시 부르라는 뜻이라 영어 코드·설명이고, warnings는 확인 카드에 그대로 보이므로 사용자 언어다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from pydantic import ValidationError

from app.core.exceptions import InvalidInputError
from app.i18n import render_message
from app.models.enums import EventInstanceStatus, EventType, Importance
from app.models.event import Event
from app.models.event_instance import EventInstance
from app.schemas.event import EventCreate
from app.services import date_range_command_service as ranges
from app.services import draft_rules as rules
from app.services.assistant.context import Draft, DraftKind, TurnContext
from app.services.event_command_service import CommandDescription, Target, find_location, new_times

DEFAULT_DURATION_MINUTES = 60
DEFAULT_DEADLINE = "23:59"
DEFAULT_TRAVEL_MINUTES = 15

ToolResult = dict[str, Any]


class _Problems:
    def __init__(self, ctx: TurnContext) -> None:
        self.ctx = ctx
        self.errors: list[dict[str, str]] = []
        self.warnings: list[dict[str, str]] = []

    def error(self, code: str, message: str) -> None:
        self.errors.append({"code": code, "message": message})

    def warn(self, code: str, **params: object) -> None:
        if all(w["code"] != code for w in self.warnings):
            text = render_message(f"assistant.warning.{code}", self.ctx.language, **params)  # type: ignore[arg-type]
            self.warnings.append({"code": code, "message": text})


def _failed(problems: _Problems, inferred: list[str]) -> ToolResult:
    return {
        "ok": False,
        "draft": None,
        "errors": problems.errors,
        "warnings": problems.warnings,
        "preview_dates": [],
        "time_display": None,
        "inferred_fields": inferred,
        "hint": "Fix the errors and call the tool again. Nothing was saved.",
    }


def _accept(
    ctx: TurnContext,
    kind: DraftKind,
    card: dict[str, Any],
    payload: dict[str, Any],
    problems: _Problems,
    inferred: list[str],
    replaces: str | None,
) -> ToolResult:
    """성공한 초안을 이번 턴 목록에 넣는다. 같은 턴의 초안을 고쳐 다시 부르면(draft_id) 그 자리를 바꾼다."""
    draft_id = replaces if replaces in ctx.drafts else ctx.next_draft_id()
    card = {"draft_id": draft_id, "kind": kind, **card, "warnings": problems.warnings, "inferred_fields": inferred}
    ctx.drafts[draft_id] = Draft(draft_id, kind, card, payload)
    return {
        "ok": True,
        "draft": card,
        "errors": [],
        "warnings": problems.warnings,
        "preview_dates": card.get("preview_dates", []),
        "time_display": card.get("time_display"),
        "inferred_fields": inferred,
        "note": "Draft stored, not saved. The user must confirm it; do not say it was created.",
    }


def _add_inferred(inferred: list[str], name: str) -> None:
    if name not in inferred:
        inferred.append(name)


def _clock(value: str | None, field_name: str, problems: _Problems) -> time | None:
    if value is None:
        return None
    try:
        return rules.parse_hhmm(value)
    except rules.DraftRuleError:
        problems.error("invalid_time", f"{field_name} must be HH:MM (24h), got {value!r}")
        return None


def _importance(value: object, problems: _Problems) -> int | None:
    if value is None:
        return None
    try:
        return Importance(int(value)).value  # type: ignore[arg-type]
    except (TypeError, ValueError):
        problems.error("invalid_importance", f"importance must be 1-6 or null, got {value!r}")
        return None


# --- 기간·장소 매칭 ----------------------------------------------------------------------


@dataclass
class RangeRef:
    id: int | None
    name: str
    start: date
    end: date

    @property
    def is_new(self) -> bool:
        return self.id is None

    def card(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "start_date": self.start.isoformat(), "end_date": self.end.isoformat(), "is_new": self.is_new}


def _range_names(ctx: TurnContext) -> str:
    names = [r.name for r in ranges.user_ranges(ctx.db, ctx.user.id)]
    return ", ".join(names) if names else "(none)"


def resolve_range(ctx: TurnContext, spec: dict[str, Any] | None, problems: _Problems) -> RangeRef | None:
    """기존 기간 이름 → 그 기간. 이번 턴에 만들기로 제안한 기간 → 그 값. 날짜가 있으면 새 기간."""
    if not spec or not spec.get("name"):
        problems.error("date_range_required", f"A recurring event needs a date_range. Existing ranges: {_range_names(ctx)}")
        return None
    name = spec["name"].strip()
    existing = ranges.find_by_name(ctx.db, ctx.user.id, name)
    if existing is not None:
        return RangeRef(existing.id, existing.name, existing.start_date, existing.end_date)
    for draft in ctx.drafts.values():
        if draft.kind == "create_range" and ranges.same_name(draft.payload["name"], name):
            return RangeRef(None, draft.payload["name"], date.fromisoformat(draft.payload["start_date"]), date.fromisoformat(draft.payload["end_date"]))
    if spec.get("start_date") and spec.get("end_date"):
        try:
            start, end = ranges.resolve_range_dates(spec["start_date"], spec["end_date"], ctx.today, ctx.today)
        except InvalidInputError as exc:
            problems.error("invalid_date_range", f"date_range dates are invalid: {exc}")
            return None
        return RangeRef(None, name, start, end)
    problems.error(
        "unknown_date_range",
        f"No date range named {name!r}. Use an existing one ({_range_names(ctx)}) or give start_date and end_date for a new one.",
    )
    return None


def resolve_location(ctx: TurnContext, spec: dict[str, Any] | None, inferred: list[str], prefix: str) -> dict[str, Any] | None:
    """기존 장소 이름이면 그 장소, 아니면 새 장소(이동 시간을 말하지 않았으면 추정값)."""
    if not spec or not spec.get("name"):
        return None
    location = find_location(ctx.db, ctx.user.id, spec["name"])
    if location is not None:
        return {"id": location.id, "name": location.name, "travel_minutes": location.default_travel_minutes, "is_new": False}
    minutes = spec.get("travel_minutes")
    if minutes is None:
        minutes = DEFAULT_TRAVEL_MINUTES
        _add_inferred(inferred, f"{prefix}.travel_minutes")
    return {"id": None, "name": spec["name"].strip(), "travel_minutes": max(int(minutes), 0), "is_new": True}


def _check_times(problems: _Problems, spec: rules.DraftSpec, now: datetime, *, allow_past: bool = True) -> str | None:
    check = rules.validate_draft(spec, now)
    for code in check.errors:
        messages = {
            "end_not_after_start": "end must be after start",
            "deadline_has_start_time": "a deadline has no start_time; put the due time in end_time",
            "invalid_recurrence": "the recurrence rule cannot be built (check frequency/by_day/interval)",
        }
        problems.error(code, messages[code])
    for code in check.warnings:
        if code == "past_date" and not allow_past:
            continue
        problems.warn(code)
    return check.recurrence_rule


# --- 생성 ----------------------------------------------------------------------------


def propose_create_event(ctx: TurnContext, args: dict[str, Any]) -> ToolResult:
    problems = _Problems(ctx)
    inferred = [str(f) for f in args.get("inferred_fields") or []]
    deadline = args.get("event_type") == "deadline"
    title = (args.get("title") or "").strip()
    if not title:
        problems.error("missing_title", "title is required")

    start_text, end_text = args.get("start_time"), args.get("end_time")
    if deadline:
        start_text, end_text = rules.normalize_deadline_times(start_text, end_text)
        if end_text is None:
            end_text = DEFAULT_DEADLINE
            _add_inferred(inferred, "end_time")
    elif start_text is None:
        problems.error("missing_start_time", "a scheduled event needs start_time (use event_type=deadline for due dates)")
    start_clock = _clock(start_text, "start_time", problems)
    end_clock = _clock(end_text, "end_time", problems)
    if not deadline and start_clock is not None and end_clock is None and end_text is None:
        end_clock = (datetime.combine(ctx.today, start_clock) + timedelta(minutes=DEFAULT_DURATION_MINUTES)).time()
        _add_inferred(inferred, "end_time")
    importance = _importance(args.get("importance"), problems)
    location = resolve_location(ctx, args.get("location"), inferred, "location")

    recurrence = args.get("recurrence")
    date_range: RangeRef | None = None
    rule: str | None = None
    by_day: list[str] | None = None
    frequency = interval = None
    day: date | None = None
    preview: list[date] = []

    if recurrence:
        frequency = recurrence.get("frequency")
        interval = recurrence.get("interval") or 1
        by_day = list(recurrence.get("by_day") or []) or None
        date_range = resolve_range(ctx, args.get("date_range"), problems)
        start_value = recurrence.get("start_date") or args.get("date")
        year = date_range.start.year if date_range else ctx.today.year
        if start_value:
            try:
                day = ranges.parse_day(start_value, year)
            except InvalidInputError:
                problems.error("invalid_date", f"recurrence.start_date must be YYYY-MM-DD, got {start_value!r}")
        if frequency == "WEEKLY" and not by_day and day is not None:
            by_day = [rules.WEEKDAY_CODES[day.weekday()]]
        if frequency == "WEEKLY" and not by_day:
            problems.error("missing_by_day", "a weekly recurrence needs by_day (e.g. [\"TU\"])")
        try:
            rule = rules.build_rrule(frequency or "", by_day, interval)
        except rules.DraftRuleError as exc:
            problems.error("invalid_recurrence", str(exc))
        if day is not None and not recurrence.get("start_date"):
            day = rules.first_matching_day(day, by_day, frequency)  # 단발 날짜를 반복으로: 그 날 이후 첫 반복 요일부터
        anchor_clock = end_clock if deadline else start_clock
        if rule and date_range and anchor_clock is not None:
            if day is None:
                day = rules.first_occurrence(rule, anchor_clock, date_range.start, date_range.end)
            preview = rules.preview_dates(rule, datetime.combine(day, anchor_clock), date_range.start, date_range.end)
            if not preview:
                problems.error("no_occurrences", f"no {rule} dates between {day} and the range end {date_range.end}")
    else:
        if not args.get("date"):
            problems.error("missing_date", "a one-off event needs date (YYYY-MM-DD); for a repeating event pass recurrence")
        else:
            try:
                day = rules.resolve_event_date(args["date"], ctx.today)
            except rules.DraftRuleError as exc:
                problems.error("invalid_date", str(exc))

    if problems.errors or day is None or end_clock is None or (not deadline and start_clock is None):
        if not problems.errors:
            problems.error("incomplete", "date or time is missing")
        return _failed(problems, inferred)

    start_at, end_at = rules.combine_times(day, None if deadline else start_clock, end_clock)
    spec = rules.DraftSpec(
        event_type="deadline" if deadline else "scheduled",
        start=start_at,
        end=end_at,
        frequency=frequency,
        by_day=by_day,
        interval=interval,
        recurrence_start=day if recurrence else None,
    )
    rule = _check_times(problems, spec, ctx.now) or rule
    if problems.errors:
        return _failed(problems, inferred)

    payload = {
        "event": {
            "user_id": ctx.user.id,
            "title": title,
            "event_type": EventType.DEADLINE.value if deadline else EventType.SCHEDULED.value,
            "start_time": start_at.isoformat() if start_at else None,
            "end_time": end_at.isoformat(),
            "importance": importance,
            "is_recurring": bool(recurrence),
            "recurrence_rule": rule if recurrence else None,
            "date_range_id": date_range.id if date_range else None,
            "location_id": location["id"] if location else None,
        },
        "new_date_range": (
            {"name": date_range.name, "start_date": date_range.start.isoformat(), "end_date": date_range.end.isoformat()}
            if date_range and date_range.is_new
            else None
        ),
        "new_location": (
            {"name": location["name"], "default_travel_minutes": location["travel_minutes"]} if location and location["is_new"] else None
        ),
    }
    try:
        # 새 기간은 아직 id가 없으므로 검증할 때만 자리표시 값을 넣는다.
        EventCreate(**{**payload["event"], "date_range_id": payload["event"]["date_range_id"] or (0 if recurrence else None)})
    except (ValidationError, InvalidInputError) as exc:
        problems.error("invalid_event", str(exc))
        return _failed(problems, inferred)

    card = {
        "title": title,
        "event_type": "deadline" if deadline else "scheduled",
        "date": day.isoformat(),
        "start_time": start_at.isoformat() if start_at else None,
        "end_time": end_at.isoformat(),
        "time_display": rules.format_time_range(start_at, end_at, ctx.language),
        "importance": importance,
        "recurring": bool(recurrence),
        "recurrence_rule": rule if recurrence else None,
        "date_range": date_range.card() if date_range else None,
        "location": location,
        "preview_dates": [d.isoformat() for d in preview],
    }
    return _accept(ctx, "create_event", card, payload, problems, inferred, args.get("draft_id"))


# --- 대상(수정·삭제) ----------------------------------------------------------------------


def _is_recurring(event: Event) -> bool:
    return event.is_recurring or len([i for i in event.instances if i.status != EventInstanceStatus.CANCELLED]) > 1


def load_targets(ctx: TurnContext, raw_targets: list[dict[str, Any]] | None, scope: str | None, problems: _Problems) -> list[Target]:
    """search_events가 이 세션에서 돌려준 ID만 받는다. 반복 전체(series)면 이벤트 단위로 묶는다."""
    if not raw_targets:
        problems.error("missing_targets", "target_ids is empty; call search_events first and use the ids it returns")
        return []
    targets: list[Target] = []
    seen_keys: set[tuple[int, int | None]] = set()
    for raw in raw_targets:
        event_id, instance_id = raw.get("event_id"), raw.get("instance_id")
        if not isinstance(event_id, int) or not ctx.seen_event(event_id):
            problems.error("unknown_id", f"event_id {event_id!r} was not returned by search_events in this conversation")
            continue
        if instance_id is not None and (not isinstance(instance_id, int) or not ctx.seen_instance(instance_id)):
            problems.error("unknown_id", f"instance_id {instance_id!r} was not returned by search_events in this conversation")
            continue
        event = ctx.db.get(Event, event_id)
        if event is None or event.user_id != ctx.user.id or event.parent_event_id is not None:
            problems.error("unknown_id", f"event_id {event_id} no longer exists")
            continue
        instance: EventInstance | None = None
        if scope == "instance" and _is_recurring(event):
            if instance_id is None:
                problems.error("instance_id_required", f"scope=instance needs instance_id for the repeating event {event_id}")
                continue
            instance = ctx.db.get(EventInstance, instance_id)
            if instance is None or instance.event_id != event.id or instance.status == EventInstanceStatus.CANCELLED:
                problems.error("unknown_id", f"instance_id {instance_id} is not an active occurrence of event {event_id}")
                continue
        key = (event.id, instance.id if instance else None)
        if key not in seen_keys:
            seen_keys.add(key)
            targets.append(Target(event, instance))
    return targets


def _target_card(target: Target, language: str, after: tuple[datetime | None, datetime] | None = None) -> dict[str, Any]:
    event, instance = target.event, target.instance
    if instance is not None:
        start, end, day = instance.effective_start, instance.effective_end, instance.date
    else:
        start, end, day = event.start_time, event.end_time, event.anchor_time.date()
    card: dict[str, Any] = {
        "event_id": event.id,
        "instance_id": instance.id if instance else None,
        "title": event.title,
        "date": day.isoformat(),
        "recurring": _is_recurring(event),
        "time_display": rules.format_time_range(start, end, language),
        "location": event.location.name if event.location else None,
    }
    if after is not None and after != (start, end):
        card["new_date"] = (after[0] or after[1]).date().isoformat()
        card["new_time_display"] = rules.format_time_range(after[0], after[1], language)
    return card


def _scope_of(targets: list[Target]) -> str:
    return "instance" if targets and all(t.instance is not None for t in targets) else "series"


def propose_update_event(ctx: TurnContext, args: dict[str, Any]) -> ToolResult:
    problems = _Problems(ctx)
    inferred = [str(f) for f in args.get("inferred_fields") or []]
    changes = args.get("changes") or {}
    scope = args.get("scope") or "series"

    for unsupported in ("recurrence", "event_type"):
        if changes.get(unsupported) is not None:
            problems.error("unsupported_change", f"changing {unsupported} is not supported; propose delete + create instead")
    for key in ("start_time", "end_time"):
        _clock(changes.get(key), key, problems)
    importance = _importance(changes.get("importance"), problems)
    new_date: date | None = None
    if changes.get("date"):
        try:
            new_date = rules.resolve_event_date(changes["date"], ctx.today)
        except rules.DraftRuleError as exc:
            problems.error("invalid_date", str(exc))

    location_change = changes.get("location") or {}
    location_action = location_change.get("action")
    location = None
    if location_action == "set":
        location = resolve_location(ctx, location_change, inferred, "changes.location")
        if location is None:
            problems.error("missing_location_name", "changes.location.action=set needs a name")
    elif location_action not in (None, "remove"):
        problems.error("invalid_location_action", "changes.location.action must be set or remove")

    location_only = location_action is not None and not any(
        (changes.get("start_time"), changes.get("end_time"), changes.get("title"), importance is not None, new_date)
    )
    targets = load_targets(ctx, args.get("target_ids"), "series" if location_only else scope, problems)
    if new_date is not None and any(_is_recurring(t.event) for t in targets):
        problems.error("unsupported_change", "changes.date only moves one-off events; for a repeating event change the time or delete one occurrence")

    desc = CommandDescription(
        intent="update",
        title=targets[0].event.title if targets else None,
        scope="instance" if _scope_of(targets) == "instance" else "series",
        new_start_time=changes.get("start_time"),
        new_end_time=changes.get("end_time"),
        new_title=(changes.get("title") or "").strip() or None,
        new_importance=importance,
        new_date=new_date,
        location_action=location_action,
        new_location_name=location["name"] if location else None,
        new_location_minutes=location["travel_minutes"] if location else None,
    )
    if not problems.errors and not desc.has_changes():
        problems.error("nothing_to_change", "changes has no values")
    if problems.errors:
        return _failed(problems, inferred)

    cards = []
    for target in targets:
        event, instance = target.event, target.instance
        if instance is not None:
            before = (instance.effective_start, instance.effective_end)
            day = instance.date
        else:
            before = (event.start_time, event.end_time)
            day = event.anchor_time.date()
        after = new_times(desc, day, *before)
        if after != before:
            spec = rules.DraftSpec(
                event_type="deadline" if after[0] is None else "scheduled", start=after[0], end=after[1]
            )
            _check_times(problems, spec, ctx.now, allow_past=new_date is not None)
        cards.append(_target_card(target, ctx.language, after))
    if problems.errors:
        return _failed(problems, inferred)
    if len(targets) > 1:
        problems.warn("multiple_targets", count=len(targets))

    card = {
        "scope": desc.scope,
        "targets": cards,
        "changes": {
            "title": desc.new_title,
            "date": new_date.isoformat() if new_date else None,
            "start_time": desc.new_start_time,
            "end_time": desc.new_end_time,
            "importance": importance,
            "location": {"action": location_action, **(location or {})} if location_action else None,
        },
    }
    payload = {"description": desc.to_dict(), "targets": [[t.event.id, t.instance.id if t.instance else None] for t in targets]}
    return _accept(ctx, "update_event", card, payload, problems, inferred, args.get("draft_id"))


def propose_delete_event(ctx: TurnContext, args: dict[str, Any]) -> ToolResult:
    problems = _Problems(ctx)
    inferred = [str(f) for f in args.get("inferred_fields") or []]
    targets = load_targets(ctx, args.get("target_ids"), args.get("scope") or "series", problems)
    if problems.errors:
        return _failed(problems, inferred)
    if len(targets) > 1:
        problems.warn("multiple_targets", count=len(targets))
    desc = CommandDescription(intent="delete", title=targets[0].event.title, scope=_scope_of(targets))  # type: ignore[arg-type]
    card = {"scope": desc.scope, "targets": [_target_card(t, ctx.language) for t in targets]}
    payload = {"description": desc.to_dict(), "targets": [[t.event.id, t.instance.id if t.instance else None] for t in targets]}
    return _accept(ctx, "delete_event", card, payload, problems, inferred, args.get("draft_id"))


# --- 반복 기간 ------------------------------------------------------------------------------


def propose_date_range(ctx: TurnContext, args: dict[str, Any]) -> ToolResult:
    problems = _Problems(ctx)
    inferred = [str(f) for f in args.get("inferred_fields") or []]
    action = args.get("action")
    name = (args.get("name") or "").strip()
    if not name:
        problems.error("missing_name", "name is required")
        return _failed(problems, inferred)
    existing = ranges.find_by_name(ctx.db, ctx.user.id, name)

    if action == "create":
        if existing is not None and ranges.same_name(existing.name, name):
            problems.error("range_exists", f"a range named {existing.name!r} already exists; use action=update or reuse it")
        if not (args.get("start_date") and args.get("end_date")):
            problems.error("missing_dates", "create needs start_date and end_date")
        if problems.errors:
            return _failed(problems, inferred)
        try:
            start, end = ranges.resolve_range_dates(args["start_date"], args["end_date"], ctx.today, ctx.today)
        except InvalidInputError as exc:
            problems.error("invalid_date_range", str(exc))
            return _failed(problems, inferred)
        card = {"action": "create", "name": name, "start_date": start.isoformat(), "end_date": end.isoformat()}
        payload = {"name": name, "start_date": start.isoformat(), "end_date": end.isoformat()}
        return _accept(ctx, "create_range", card, payload, problems, inferred, args.get("draft_id"))

    if existing is None:
        problems.error("unknown_date_range", f"No date range named {name!r}. Existing ranges: {_range_names(ctx)}")
        return _failed(problems, inferred)
    in_use = len(ranges.events_using(ctx.db, existing.id))
    base = {"range_id": existing.id, "name": existing.name, "events_using": in_use}

    if action == "update":
        try:
            start = ranges.parse_day(args["start_date"], existing.start_date.year) if args.get("start_date") else existing.start_date
            end = ranges.parse_day(args["end_date"], start.year) if args.get("end_date") else existing.end_date
        except InvalidInputError as exc:
            problems.error("invalid_date", str(exc))
            return _failed(problems, inferred)
        if end < start:
            problems.error("invalid_date_range", "end_date must not be before start_date")
        new_name = (args.get("new_name") or "").strip() or None
        if new_name is None and (start, end) == (existing.start_date, existing.end_date):
            problems.error("nothing_to_change", "give new_name, start_date or end_date")
        if problems.errors:
            return _failed(problems, inferred)
        card = {
            "action": "update",
            **base,
            "new_name": new_name,
            "before": {"start_date": existing.start_date.isoformat(), "end_date": existing.end_date.isoformat()},
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        payload = {"range_id": existing.id, "new_name": new_name, "start_date": start.isoformat(), "end_date": end.isoformat()}
        return _accept(ctx, "update_range", card, payload, problems, inferred, args.get("draft_id"))

    if action == "delete":
        mode = args.get("mode")
        if in_use:
            problems.warn("range_in_use", count=in_use)
            if mode is None:
                mode = "range_only"
                _add_inferred(inferred, "mode")
        card = {"action": "delete", **base, "mode": mode}
        payload = {"range_id": existing.id, "mode": mode}
        return _accept(ctx, "delete_range", card, payload, problems, inferred, args.get("draft_id"))

    problems.error("invalid_action", "action must be create, update or delete")
    return _failed(problems, inferred)
